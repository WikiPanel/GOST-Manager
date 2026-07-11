"""Fault-isolated monitoring collection orchestration."""

from __future__ import annotations

import dataclasses
import os
import re
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from monitoring.entities import ENV_RE, discover_tunnels
from monitoring.event_state import EventState
from monitoring.models import (
    Clock,
    CpuCounters,
    DiskCounters,
    Event,
    InterfaceCounters,
    Metric,
    MetricSample,
    ProcessSnapshot,
    SocketRecord,
    Tunnel,
)
from monitoring.network_readers import (
    TCPEXT_COUNTERS,
    TCP_COUNTERS,
    aggregate_external,
    interface_metrics,
    parse_net_dev,
    read_interface_link,
    selected_tcp_counters,
    tcp_counter_metrics,
)
from monitoring.proc_readers import (
    conntrack_metrics,
    cpu_metrics,
    database_size_metrics,
    disk_metrics,
    file_handle_metrics,
    filesystem_metrics,
    load_metrics,
    memory_metrics,
    parse_diskstats,
    parse_proc_stat,
    process_metrics,
    read_process_snapshot,
)
from monitoring.schema import (
    DEFAULT_DB_PATH,
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    _cycle,
    checkpoint_wal,
    get_json_state,
    get_state,
    insert_event,
    insert_metric,
    insert_sample,
    migrate_database,
    open_runtime_database,
    run_maintenance,
    set_json_state,
    set_state,
    upsert_tunnel,
)
from monitoring.socket_readers import (
    established_socket_count,
    listener_ownership_exact,
    owned_listener_ports,
    parse_ss_sockets,
    process_tcp_states,
    tcp_state_counts,
)
from monitoring.systemd_readers import (
    SYSTEMD_PROPERTIES,
    cgroup_memory_metrics,
    discover_managed_services,
    parse_systemd_properties,
    read_cgroup_memory,
    service_metrics,
)

DEFAULT_TCP_SNAPSHOT_INTERVAL_SECONDS = 30.0
DEFAULT_MAX_GAP_MULTIPLIER = 2.5


def run_command(command: Sequence[str]) -> str:
    return subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _glob(root: Path, pattern: str) -> list[Path]:
    return list(root.glob(pattern))


def _file_size(path: Path) -> int:
    return path.stat().st_size


@dataclasses.dataclass(frozen=True)
class CollectorSources:
    """All external observations used by a collection cycle."""

    clock: Clock = Clock()
    command: Callable[[Sequence[str]], str] = run_command
    read_text: Callable[[Path], str] = _read_text
    list_dir: Callable[[Path], list[str]] = os.listdir
    glob: Callable[[Path, str], list[Path]] = _glob
    exists: Callable[[Path], bool] = Path.exists
    statvfs: Callable[[str], os.statvfs_result] = os.statvfs
    file_size: Callable[[Path], int] = _file_size
    proc_root: Path = Path("/proc")
    sys_root: Path = Path("/sys")
    cgroup_root: Path = Path("/sys/fs/cgroup")
    systemd_unit_root: Path = Path("/etc/systemd/system")
    ticks_per_second: int = int(os.sysconf("SC_CLK_TCK"))
    page_size: int = int(os.sysconf("SC_PAGE_SIZE"))


@dataclasses.dataclass(frozen=True)
class CollectorConfig:
    sample_interval: float = DEFAULT_SAMPLE_INTERVAL_SECONDS
    tcp_snapshot_interval: float = DEFAULT_TCP_SNAPSHOT_INTERVAL_SECONDS
    max_gap_multiplier: float = DEFAULT_MAX_GAP_MULTIPLIER
    filesystem_paths: tuple[Path, ...] = (
        Path("/"),
        Path("/etc/gost-manager"),
        Path("/var/lib/gost-manager"),
    )

    @property
    def max_gap(self) -> float:
        return self.sample_interval * self.max_gap_multiplier


@dataclasses.dataclass
class Capture:
    values: dict[str, object] = dataclasses.field(default_factory=dict)
    errors: set[str] = dataclasses.field(default_factory=set)

    def read(self, code: str, reader: Callable[[], object]) -> object | None:
        try:
            value = reader()
        except Exception:
            self.errors.add(code)
            return None
        self.values[code] = value
        return value


class CollectionCycleError(RuntimeError):
    def __init__(self, ts: int, message: str):
        super().__init__(message)
        self.ts = ts


def _source_metric_name(source: str) -> str:
    return "source_" + re.sub(r"[^a-zA-Z0-9]+", "_", source).strip("_").lower() + "_available"


def _unavailable(
    scope: str,
    names: Sequence[tuple[str, str]],
    entity_type: str,
    entity_id: str,
    labels: dict[str, str] | None = None,
) -> list[Metric]:
    return [
        Metric(
            scope,
            name,
            None,
            unit,
            "unavailable",
            labels or {},
            entity_type,
            entity_id,
        )
        for name, unit in names
    ]


def _timed_state(
    conn: sqlite3.Connection,
    key: str,
) -> tuple[float | None, object | None]:
    state = get_json_state(conn, key)
    if not isinstance(state, dict):
        return None, None
    monotonic = state.get("monotonic")
    return (
        float(monotonic) if isinstance(monotonic, (int, float)) else None,
        state.get("value"),
    )


def _save_timed_state(
    conn: sqlite3.Connection,
    key: str,
    monotonic: float,
    value: object,
) -> None:
    set_json_state(conn, key, {"monotonic": monotonic, "value": value})


def _cpu_from_state(value: object) -> CpuCounters | None:
    if not isinstance(value, dict):
        return None
    try:
        return CpuCounters(**{key: int(item) for key, item in value.items()})
    except (TypeError, ValueError):
        return None


def _interfaces_from_state(value: object) -> dict[str, InterfaceCounters]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, InterfaceCounters] = {}
    for name, raw in value.items():
        if not isinstance(raw, dict):
            continue
        try:
            result[str(name)] = InterfaceCounters(
                **{key: (str(item) if key == "name" else int(item)) for key, item in raw.items()}
            )
        except (TypeError, ValueError):
            continue
    return result


def _disks_from_state(value: object) -> dict[str, DiskCounters]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, DiskCounters] = {}
    for name, raw in value.items():
        if not isinstance(raw, dict):
            continue
        try:
            result[str(name)] = DiskCounters(
                **{
                    key: (str(item) if key == "name" else int(item))
                    for key, item in raw.items()
                }
            )
        except (TypeError, ValueError):
            continue
    return result


def _process_from_state(value: object) -> ProcessSnapshot | None:
    if not isinstance(value, dict):
        return None
    try:
        return ProcessSnapshot(
            **{
                key: (None if item is None else int(item))
                for key, item in value.items()
            }
        )
    except (TypeError, ValueError):
        return None


def _deduplicate_metrics(metrics: list[Metric]) -> list[Metric]:
    unique: dict[tuple[str, str, str], Metric] = {}
    for metric in metrics:
        key = (
            metric.entity_type or metric.scope,
            metric.entity_id or metric.scope,
            metric.name,
        )
        unique[key] = metric
    return list(unique.values())


def _record_flag_event(
    state: EventState,
    key: str,
    active: bool,
    ts: int,
    code: str,
    details: dict[str, object],
) -> list[Event]:
    return state.edge(key, active, ts, code, code.replace("_", " ").capitalize(), details)


def _capture_raw(
    db_path: str,
    env_dir: str,
    sources: CollectorSources,
) -> tuple[Capture, list[Tunnel], list[Event], tuple[str, ...]]:
    capture = Capture()
    env_paths = capture.read("tunnel_env_directory", lambda: sources.glob(Path(env_dir), "*.env"))
    paths = env_paths if isinstance(env_paths, list) else []
    tunnels, env_events = discover_tunnels(
        env_dir,
        sources.clock,
        paths=paths,
        read_text=sources.read_text,
    )
    proc = sources.proc_root
    capture.read("proc_stat", lambda: parse_proc_stat(sources.read_text(proc / "stat")))
    capture.read("proc_loadavg", lambda: sources.read_text(proc / "loadavg"))
    capture.read("proc_meminfo", lambda: sources.read_text(proc / "meminfo"))
    interfaces = capture.read("proc_net_dev", lambda: parse_net_dev(sources.read_text(proc / "net/dev")))
    if isinstance(interfaces, dict):
        for interface in interfaces:
            capture.read(
                f"sysfs_net:{interface}",
                lambda interface=interface: read_interface_link(
                    str(interface),
                    sources.sys_root,
                    sources.read_text,
                ),
            )
    capture.read("proc_net_snmp", lambda: sources.read_text(proc / "net/snmp"))
    capture.read("proc_net_netstat", lambda: sources.read_text(proc / "net/netstat"))
    capture.read("proc_diskstats", lambda: parse_diskstats(sources.read_text(proc / "diskstats")))
    capture.read(
        "conntrack",
        lambda: (
            sources.read_text(proc / "sys/net/netfilter/nf_conntrack_count"),
            sources.read_text(proc / "sys/net/netfilter/nf_conntrack_max"),
        ),
    )
    capture.read(
        "file_handles",
        lambda: (
            sources.read_text(proc / "sys/fs/file-nr"),
            sources.read_text(proc / "sys/fs/file-max"),
        ),
    )
    capture.read("ss", lambda: parse_ss_sockets(sources.command(["ss", "-H", "-tanp"])))
    unit_paths = capture.read(
        "systemd_unit_directory",
        lambda: sources.glob(sources.systemd_unit_root, "gost-*.service"),
    )
    service_names = [tunnel.service_name for tunnel in tunnels]
    if isinstance(unit_paths, list):
        service_names.extend(path.name for path in unit_paths)
    services = discover_managed_services(service_names)
    for service in services:
        def read_properties(service_name: str = service) -> dict[str, str]:
            properties = parse_systemd_properties(
                sources.command(
                    [
                        "systemctl",
                        "--no-pager",
                        "show",
                        service_name,
                        f"--property={SYSTEMD_PROPERTIES}",
                    ]
                )
            )
            if not properties:
                raise OSError("systemd properties unavailable")
            return properties

        properties = capture.read(
            f"systemd:{service}",
            read_properties,
        )
        if isinstance(properties, dict):
            pid_raw = properties.get("MainPID", "")
            pid = int(pid_raw) if isinstance(pid_raw, str) and pid_raw.isdigit() else 0
            if pid > 0:
                capture.read(
                    f"process:{service}",
                    lambda pid=pid: read_process_snapshot(
                        pid,
                        sources.proc_root,
                        sources.read_text,
                        sources.list_dir,
                        sources.page_size,
                    ),
                )
            control_group = properties.get("ControlGroup", "")
            if control_group:
                def read_cgroup(group: str = control_group) -> dict[str, int | None]:
                    values = read_cgroup_memory(
                        group,
                        sources.cgroup_root,
                        sources.read_text,
                    )
                    if all(value is None for value in values.values()):
                        raise OSError("cgroup memory unavailable")
                    return values

                capture.read(f"cgroup:{service}", read_cgroup)
    capture.values["db_size_metrics"] = database_size_metrics(db_path, sources.file_size)
    return capture, tunnels, env_events, services


def _source_status(
    conn: sqlite3.Connection,
    capture: Capture,
    ts: int,
) -> tuple[list[Metric], list[Event], dict[str, int]]:
    state = EventState(conn)
    metrics: list[Metric] = []
    events: list[Event] = []
    all_sources = set(capture.values) | capture.errors
    for source in sorted(all_sources):
        available = source not in capture.errors
        metrics.append(
            Metric(
                "collector",
                _source_metric_name(source),
                int(available),
                "boolean",
                "exact",
                {"source": source},
                "collector_source",
                source,
            )
        )
        events.extend(state.availability(source, available, ts))
    previous = get_json_state(conn, "counter.source_errors")
    counts = {
        str(key): int(value)
        for key, value in previous.items()
    } if isinstance(previous, dict) else {}
    for source in capture.errors:
        counts[source] = counts.get(source, 0) + 1
    set_json_state(conn, "counter.source_errors", counts)
    metrics.append(
        Metric(
            "collector",
            "source_errors_total",
            sum(counts.values()),
            "count",
            "exact",
            entity_type="collector",
            entity_id="local",
        )
    )
    for source, count in sorted(counts.items()):
        metrics.append(
            Metric(
                "collector",
                "source_errors",
                count,
                "count",
                "exact",
                {"source": source},
                "collector_source",
                source,
            )
        )
    return metrics, events, counts


def _host_metrics(
    conn: sqlite3.Connection,
    capture: Capture,
    sources: CollectorSources,
    config: CollectorConfig,
    monotonic: float,
    ts: int,
) -> tuple[list[Metric], list[Event]]:
    metrics: list[Metric] = []
    events: list[Event] = []
    event_state = EventState(conn)

    cpu = capture.values.get("proc_stat")
    previous_mono, previous_raw = _timed_state(conn, "counter.cpu")
    if isinstance(cpu, CpuCounters):
        previous = _cpu_from_state(previous_raw)
        elapsed = monotonic - previous_mono if previous_mono is not None else None
        cpu_values = cpu_metrics(cpu, previous, elapsed, config.max_gap)
        metrics.extend(cpu_values)
        reset = any(metric.reset for metric in cpu_values)
        gap = any(metric.gap for metric in cpu_values)
        events.extend(_record_flag_event(event_state, "cpu.reset", reset, ts, "counter_reset", {"source": "proc_stat", "entity": "host"}))
        events.extend(_record_flag_event(event_state, "cpu.gap", gap, ts, "sampling_gap", {"source": "proc_stat", "entity": "host"}))
        _save_timed_state(conn, "counter.cpu", monotonic, dataclasses.asdict(cpu))
    else:
        names = [(f"cpu_{field}_jiffies", "jiffies") for field in ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")]
        names += [(f"cpu_{field}_percent", "percent") for field in ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")]
        names += [("cpu_jiffies_total", "jiffies"), ("cpu_utilization_percent", "percent"), ("cpu_logical_count", "count")]
        metrics.extend(_unavailable("host", names, "host", "local"))

    loadavg = capture.values.get("proc_loadavg")
    if isinstance(loadavg, str):
        try:
            metrics.extend(load_metrics(loadavg))
        except ValueError:
            metrics.extend(_unavailable("host", (("load1", "load"), ("load5", "load"), ("load15", "load")), "host", "local"))
    else:
        metrics.extend(_unavailable("host", (("load1", "load"), ("load5", "load"), ("load15", "load")), "host", "local"))

    meminfo = capture.values.get("proc_meminfo")
    if isinstance(meminfo, str):
        metrics.extend(memory_metrics(meminfo))
    else:
        memory_names = (
            "memory_total_bytes", "memory_available_bytes", "memory_used_bytes",
            "memory_used_percent", "memory_buffers_bytes", "memory_cache_bytes",
            "swap_total_bytes", "swap_free_bytes", "swap_used_bytes", "swap_used_percent",
            "memory_dirty_bytes", "memory_writeback_bytes",
        )
        metrics.extend(_unavailable("host", tuple((name, "percent" if name.endswith("percent") else "bytes") for name in memory_names), "host", "local"))

    conntrack = capture.values.get("conntrack")
    if isinstance(conntrack, tuple):
        try:
            metrics.extend(conntrack_metrics(str(conntrack[0]), str(conntrack[1])))
        except (ValueError, IndexError):
            conntrack = None
    if conntrack is None:
        metrics.extend(_unavailable("host", (("conntrack_count", "count"), ("conntrack_max", "count"), ("conntrack_utilization_percent", "percent")), "host", "local"))

    handles = capture.values.get("file_handles")
    if isinstance(handles, tuple):
        try:
            metrics.extend(file_handle_metrics(str(handles[0]), str(handles[1])))
        except (ValueError, IndexError):
            handles = None
    if handles is None:
        metrics.extend(_unavailable("host", (("file_handles_allocated", "count"), ("file_handles_max", "count"), ("file_handles_utilization_percent", "percent")), "host", "local"))
    return metrics, events


def _network_metrics(
    conn: sqlite3.Connection,
    capture: Capture,
    sources: CollectorSources,
    config: CollectorConfig,
    monotonic: float,
    ts: int,
) -> tuple[list[Metric], list[Event]]:
    metrics: list[Metric] = []
    events: list[Event] = []
    event_state = EventState(conn)
    interfaces = capture.values.get("proc_net_dev")
    previous_mono, previous_raw = _timed_state(conn, "counter.interfaces")
    previous = _interfaces_from_state(previous_raw)
    elapsed = monotonic - previous_mono if previous_mono is not None else None
    if isinstance(interfaces, dict):
        current = {str(name): value for name, value in interfaces.items() if isinstance(value, InterfaceCounters)}
        events.extend(event_state.set_transitions("interfaces", set(current), ts, "interface_added", "interface_removed", "interface"))
        for name, counters in sorted(current.items()):
            link_raw = capture.values.get(f"sysfs_net:{name}")
            link = link_raw if isinstance(link_raw, dict) else {
                "state": None,
                "link_up": None,
                "mtu": None,
                "speed_mbps": None,
            }
            values = interface_metrics(counters, previous.get(name), elapsed, link, config.max_gap)
            metrics.extend(values)
            events.extend(_record_flag_event(event_state, f"interface.{name}.reset", any(metric.reset for metric in values), ts, "counter_reset", {"source": "proc_net_dev", "interface": name}))
            events.extend(_record_flag_event(event_state, f"interface.{name}.gap", any(metric.gap for metric in values), ts, "sampling_gap", {"source": "proc_net_dev", "interface": name}))
        aggregate = aggregate_external(current)
        previous_aggregate = aggregate_external(previous) if previous else None
        metrics.extend(interface_metrics(aggregate, previous_aggregate, elapsed, None, config.max_gap))
        _save_timed_state(conn, "counter.interfaces", monotonic, {name: dataclasses.asdict(value) for name, value in current.items()})
    else:
        names = []
        for field in ("rx_bytes", "rx_packets", "rx_errors", "rx_drops", "tx_bytes", "tx_packets", "tx_errors", "tx_drops"):
            names.append((field, "bytes" if "bytes" in field else "packets"))
            names.append((f"{field}_per_second", "B/s" if "bytes" in field else "pps"))
        metrics.extend(_unavailable("net.external", names, "interface", "interface:external-total", {"interface": "external-total"}))

    snmp = capture.values.get("proc_net_snmp")
    netstat = capture.values.get("proc_net_netstat")
    current_tcp = selected_tcp_counters(snmp if isinstance(snmp, str) else "", netstat if isinstance(netstat, str) else "")
    tcp_mono, tcp_raw = _timed_state(conn, "counter.tcp")
    previous_tcp = {str(key): int(value) for key, value in tcp_raw.items()} if isinstance(tcp_raw, dict) else None
    tcp_elapsed = monotonic - tcp_mono if tcp_mono is not None else None
    metrics.extend(tcp_counter_metrics(current_tcp, previous_tcp, tcp_elapsed, config.max_gap))
    expected = set(TCP_COUNTERS.values()) | set(TCPEXT_COUNTERS.values())
    for missing in sorted(expected - set(current_tcp)):
        metrics.append(Metric("tcp", missing, None, "count", "unavailable", entity_type="host", entity_id="local"))
        if missing != "tcp_current_established":
            metrics.append(
                Metric(
                    "tcp",
                    f"{missing}_per_second",
                    None,
                    "events/s",
                    "unavailable",
                    entity_type="host",
                    entity_id="local",
                )
            )
    if current_tcp:
        _save_timed_state(conn, "counter.tcp", monotonic, current_tcp)
    tcp_values = [metric for metric in metrics if metric.scope == "tcp"]
    events.extend(_record_flag_event(event_state, "tcp.reset", any(metric.reset for metric in tcp_values), ts, "counter_reset", {"source": "tcp", "entity": "host"}))
    events.extend(_record_flag_event(event_state, "tcp.gap", any(metric.gap for metric in tcp_values), ts, "sampling_gap", {"source": "tcp", "entity": "host"}))

    last_snapshot_raw = get_state(conn, "tcp_snapshot_last_ts")
    last_snapshot = int(last_snapshot_raw) if last_snapshot_raw else 0
    if ts - last_snapshot >= config.tcp_snapshot_interval or last_snapshot == 0:
        records = capture.values.get("ss")
        if isinstance(records, list):
            counts = tcp_state_counts([row for row in records if isinstance(row, SocketRecord)])
            state_names = ("ESTAB", "SYN-SENT", "SYN-RECV", "FIN-WAIT-1", "FIN-WAIT-2", "CLOSE-WAIT", "TIME-WAIT")
            for state_name in state_names:
                metric_name = "tcp_state_" + state_name.lower().replace("-", "_")
                metrics.append(Metric("tcp", metric_name, counts.get(state_name, 0), "count", "exact", entity_type="host", entity_id="local"))
            metrics.append(Metric("tcp", "tcp_state_orphan", None, "count", "unavailable", entity_type="host", entity_id="local"))
            set_state(conn, "tcp_snapshot_last_ts", str(ts))
    return metrics, events


def _storage_metrics(
    conn: sqlite3.Connection,
    capture: Capture,
    sources: CollectorSources,
    config: CollectorConfig,
    monotonic: float,
    ts: int,
) -> tuple[list[Metric], list[Event]]:
    metrics: list[Metric] = []
    events: list[Event] = []
    event_state = EventState(conn)
    for path in config.filesystem_paths:
        try:
            metrics.extend(filesystem_metrics(path, sources.statvfs))
            events.extend(event_state.availability(f"filesystem:{path}", True, ts))
        except Exception:
            metrics.extend(_unavailable("fs", (("filesystem_total_bytes", "bytes"), ("filesystem_used_bytes", "bytes"), ("filesystem_free_bytes", "bytes"), ("filesystem_used_percent", "percent"), ("filesystem_inode_total", "count"), ("filesystem_inode_used", "count"), ("filesystem_inode_free", "count"), ("filesystem_inode_used_percent", "percent")), "filesystem", f"fs:{path}", {"path": str(path)}))
            events.extend(event_state.availability(f"filesystem:{path}", False, ts))
    db_metrics = capture.values.get("db_size_metrics")
    if isinstance(db_metrics, list):
        metrics.extend(metric for metric in db_metrics if isinstance(metric, Metric))

    disks = capture.values.get("proc_diskstats")
    previous_mono, previous_raw = _timed_state(conn, "counter.disks")
    previous = _disks_from_state(previous_raw)
    elapsed = monotonic - previous_mono if previous_mono is not None else None
    if isinstance(disks, dict):
        current = {str(name): value for name, value in disks.items() if isinstance(value, DiskCounters)}
        for name, disk in sorted(current.items()):
            values = disk_metrics(disk, previous.get(name), elapsed, config.max_gap)
            metrics.extend(values)
            events.extend(_record_flag_event(event_state, f"disk.{name}.reset", any(metric.reset for metric in values), ts, "counter_reset", {"source": "diskstats", "device": name}))
            events.extend(_record_flag_event(event_state, f"disk.{name}.gap", any(metric.gap for metric in values), ts, "sampling_gap", {"source": "diskstats", "device": name}))
        _save_timed_state(conn, "counter.disks", monotonic, {name: dataclasses.asdict(value) for name, value in current.items()})
    return metrics, events


def _service_unavailable(service: str) -> list[Metric]:
    names = (
        ("service_active", "boolean"),
        ("service_active_state", "state"),
        ("service_sub_state", "state"),
        ("service_main_pid", "pid"),
        ("service_start_monotonic_us", "microseconds"),
        ("service_restart_count", "count"),
        ("process_cpu_percent", "percent"),
        ("process_rss_bytes", "bytes"),
        ("process_threads", "count"),
        ("process_open_fds", "count"),
        ("cgroup_memory_current_bytes", "bytes"),
        ("cgroup_memory_peak_bytes", "bytes"),
        ("systemd_ip_ingress_bytes", "bytes"),
        ("systemd_ip_egress_bytes", "bytes"),
        ("listener_owned_count", "count"),
        ("established_remote_sockets", "count"),
    )
    return _unavailable("service", names, "service", service, {"service": service})


def _service_and_tunnel_metrics(
    conn: sqlite3.Connection,
    capture: Capture,
    tunnels: list[Tunnel],
    services: tuple[str, ...],
    sources: CollectorSources,
    config: CollectorConfig,
    monotonic: float,
    ts: int,
    cycle_id: int,
) -> tuple[list[Metric], list[Event], list[int]]:
    metrics: list[Metric] = []
    events: list[Event] = []
    samples: list[int] = []
    event_state = EventState(conn)
    records_raw = capture.values.get("ss")
    records = [record for record in records_raw if isinstance(record, SocketRecord)] if isinstance(records_raw, list) else []
    process_by_service: dict[str, ProcessSnapshot] = {}
    process_metrics_by_service: dict[str, dict[str, Metric]] = {}
    props_by_service: dict[str, dict[str, str]] = {}

    for service in services:
        props_raw = capture.values.get(f"systemd:{service}")
        props = props_raw if isinstance(props_raw, dict) else {}
        props_by_service[service] = {str(key): str(value) for key, value in props.items()}
        if props_raw is None:
            metrics.extend(_service_unavailable(service))
            continue
        service_values = service_metrics(service, props_by_service[service])
        metrics.extend(service_values)
        active_state = props_by_service[service].get("ActiveState", "unavailable")
        events.extend(event_state.value_transition(f"service.state.{service}", active_state, ts, "service_state_changed", "Managed service state changed", {"service": service}, severity="warning" if active_state in {"failed", "inactive"} else "info"))
        pid_raw = props_by_service[service].get("MainPID", "")
        pid = int(pid_raw) if pid_raw.isdigit() else 0
        start_identity = props_by_service[service].get("ExecMainStartTimestampMonotonic", "")
        events.extend(event_state.value_transition(f"service.pid_identity.{service}", f"{pid}:{start_identity}", ts, "pid_replaced", "Managed service process identity changed", {"service": service, "pid": pid}))
        if pid > 0:
            snapshot = capture.values.get(f"process:{service}")
            if isinstance(snapshot, ProcessSnapshot):
                process_by_service[service] = snapshot
                previous_mono, previous_raw = _timed_state(conn, f"counter.process.{service}")
                previous = _process_from_state(previous_raw)
                elapsed = monotonic - previous_mono if previous_mono is not None else None
                values = process_metrics(service, snapshot, previous, elapsed, sources.ticks_per_second, config.max_gap)
                metrics.extend(values)
                process_metrics_by_service[service] = {
                    metric.name: metric for metric in values
                }
                events.extend(_record_flag_event(event_state, f"process.{service}.reset", any(metric.reset for metric in values), ts, "counter_reset", {"source": "process", "service": service}))
                _save_timed_state(conn, f"counter.process.{service}", monotonic, dataclasses.asdict(snapshot))
            else:
                metrics.extend(_unavailable("service", (("process_cpu_percent", "percent"), ("process_rss_bytes", "bytes"), ("process_threads", "count"), ("process_open_fds", "count")), "service", service, {"service": service}))
        control_group = props_by_service[service].get("ControlGroup", "")
        if control_group:
            cgroup_raw = capture.values.get(f"cgroup:{service}")
            metrics.extend(
                cgroup_memory_metrics(
                    service,
                    cgroup_raw if isinstance(cgroup_raw, dict) else {},
                )
            )
        if pid > 0 and isinstance(records_raw, list):
            listener_count = len(owned_listener_ports(records, tuple(record.local_port for record in records if record.state == "LISTEN"), pid))
            metrics.append(Metric("service", "listener_owned_count", listener_count, "count", "exact", {"service": service}, "service", service))
            metrics.append(Metric("service", "established_remote_sockets", established_socket_count(records, pid), "count", "exact", {"service": service}, "service", service))
            for state_name, count in sorted(process_tcp_states(records, pid).items()):
                metrics.append(Metric("service", "tcp_state_" + state_name.lower().replace("-", "_"), count, "count", "exact", {"service": service}, "service", service))

    for tunnel in tunnels:
        upsert_tunnel(conn, tunnel, ts)
        props = props_by_service.get(tunnel.service_name, {})
        pid_raw = props.get("MainPID", "")
        pid = int(pid_raw) if pid_raw.isdigit() else 0
        owned = owned_listener_ports(records, tunnel.listen_ports, pid) if isinstance(records_raw, list) else set()
        ownership = listener_ownership_exact(records, tunnel.listen_ports, pid) if isinstance(records_raw, list) else None
        active = int(props.get("ActiveState") == "active")
        running = int(props.get("SubState") == "running")
        restarts_raw = props.get("NRestarts", "")
        restarts = int(restarts_raw) if restarts_raw.isdigit() else 0
        sample = MetricSample(tunnel.tunnel_id, ts, active, running, restarts, len(tunnel.listen_ports), len(owned), len(tunnel.target_ports))
        sample_id = insert_sample(conn, sample, cycle_id)
        samples.append(sample_id)
        quality = "exact" if ownership is not None else "unavailable"
        tunnel_metrics = [
            Metric("tunnel", "configured_listener_count", len(tunnel.listen_ports), "count", "exact", entity_type="tunnel", entity_id=tunnel.tunnel_id),
            Metric("tunnel", "observed_listener_count", len(owned) if ownership is not None else None, "count", quality, entity_type="tunnel", entity_id=tunnel.tunnel_id),
            Metric("tunnel", "listener_ownership_exact", None if ownership is None else int(ownership), "boolean", quality, entity_type="tunnel", entity_id=tunnel.tunnel_id),
            Metric("tunnel", "target_count", len(tunnel.target_ports), "count", "exact", entity_type="tunnel", entity_id=tunnel.tunnel_id),
            Metric("tunnel", "remote_endpoint", tunnel.remote_endpoint, "endpoint", "exact" if tunnel.remote_endpoint else "unavailable", entity_type="tunnel", entity_id=tunnel.tunnel_id),
            Metric("tunnel", "established_remote_sockets", established_socket_count(records, pid) if pid > 0 and isinstance(records_raw, list) else None, "count", "exact" if pid > 0 and isinstance(records_raw, list) else "unavailable", entity_type="tunnel", entity_id=tunnel.tunnel_id),
            Metric("tunnel", "service_active", active if props else None, "boolean", "exact" if props else "unavailable", entity_type="tunnel", entity_id=tunnel.tunnel_id),
            Metric("tunnel", "service_restart_count", restarts if props else None, "count", "exact" if props else "unavailable", entity_type="tunnel", entity_id=tunnel.tunnel_id),
        ]
        process = process_by_service.get(tunnel.service_name)
        process_values = process_metrics_by_service.get(tunnel.service_name, {})
        process_cpu = process_values.get("process_cpu_percent")
        tunnel_metrics.extend(
            [
                Metric("tunnel", "process_cpu_percent", process_cpu.value if process_cpu else None, "percent", process_cpu.quality if process_cpu else "unavailable", entity_type="tunnel", entity_id=tunnel.tunnel_id, reset=process_cpu.reset if process_cpu else False, gap=process_cpu.gap if process_cpu else False),
                Metric("tunnel", "process_rss_bytes", process.rss_bytes if process else None, "bytes", "exact" if process else "unavailable", entity_type="tunnel", entity_id=tunnel.tunnel_id),
                Metric("tunnel", "process_threads", process.threads if process else None, "count", "exact" if process else "unavailable", entity_type="tunnel", entity_id=tunnel.tunnel_id),
                Metric("tunnel", "process_open_fds", process.fd_count if process else None, "count", "exact" if process else "unavailable", entity_type="tunnel", entity_id=tunnel.tunnel_id),
            ]
        )
        metrics.extend(tunnel_metrics)
        listener_missing = ownership is False or (ownership is not None and len(owned) < len(tunnel.listen_ports))
        previous_missing = get_json_state(conn, f"event.value.tunnel.listener_missing.{tunnel.tunnel_id}")
        set_json_state(conn, f"event.value.tunnel.listener_missing.{tunnel.tunnel_id}", listener_missing)
        if previous_missing is not None and bool(previous_missing) != listener_missing:
            events.append(Event(ts, "warning" if listener_missing else "info", "listener_disappeared" if listener_missing else "listener_returned", "Tunnel listener state changed", {"tunnel_id": tunnel.tunnel_id}))
    return metrics, events, samples


def _cycle_recovery(conn: sqlite3.Connection, ts: int) -> list[Event]:
    previous = get_state(conn, "event.cycle_health")
    set_state(conn, "event.cycle_health", "ok")
    if previous == "failed":
        return [Event(ts, "info", "collection_recovered", "Monitoring collection recovered")]
    return []


def _record_cycle_failure(
    conn: sqlite3.Connection,
    ts: int,
    started: float,
    finished: float,
    message: str,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        cycle_id = _cycle(conn, ts, started, finished, max(0.0, finished - started), False, False)
        sample_id = insert_sample(
            conn,
            MetricSample(None, ts, 0, 0, 0, 0, 0, 0),
            cycle_id,
        )
        insert_metric(
            conn,
            sample_id,
            Metric(
                "collector",
                "cycle_status",
                0,
                "boolean",
                "exact",
                entity_type="collector",
                entity_id="local",
            ),
            cycle_id,
            ts,
        )
        previous = get_state(conn, "event.cycle_health")
        set_state(conn, "event.cycle_health", "failed")
        if previous != "failed":
            insert_event(conn, Event(ts, "error", "collection_failed", "Monitoring collection cycle failed", {"cycle_id": cycle_id, "reason": message}))
        conn.commit()
    except Exception:
        conn.rollback()


def _record_checkpoint_result(
    db_path: str,
    ts: int,
    cycle_id: int,
    sample_id: int,
    result: tuple[int, int, int] | None,
    error: Exception | None,
    conn_factory: Callable[[str], sqlite3.Connection] = open_runtime_database,
    event_writer: Callable[[sqlite3.Connection, Event], None] = insert_event,
    metric_writer: Callable[[sqlite3.Connection, int, Metric, int | None, int | None], None] = insert_metric,
) -> None:
    conn = conn_factory(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        previous = get_state(conn, "event.checkpoint_health")
        success = error is None and result is not None
        set_state(conn, "event.checkpoint_health", "ok" if success else "failed")
        if success and result is not None:
            for name, value in zip(("checkpoint_busy", "checkpoint_log_frames", "checkpointed_frames"), result):
                metric_writer(conn, sample_id, Metric("collector", name, value, "count", "exact", entity_type="collector", entity_id="local"), cycle_id, ts)
            metric_writer(conn, sample_id, Metric("collector", "checkpoint_success", 1, "boolean", "exact", entity_type="collector", entity_id="local"), cycle_id, ts)
            if previous is None:
                event_writer(conn, Event(ts, "info", "wal_checkpoint", "WAL checkpoint completed", {"busy": result[0], "log": result[1], "checkpointed": result[2]}))
            elif previous == "failed":
                event_writer(conn, Event(ts, "info", "wal_checkpoint_recovered", "WAL checkpoint recovered"))
        else:
            metric_writer(conn, sample_id, Metric("collector", "checkpoint_success", 0, "boolean", "exact", entity_type="collector", entity_id="local"), cycle_id, ts)
            if previous != "failed":
                event_writer(conn, Event(ts, "warning", "wal_checkpoint_failed", "WAL checkpoint failed"))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def collect_once(
    db_path: str = DEFAULT_DB_PATH,
    env_dir: str = "/etc/gost",
    now: int | None = None,
    sources: CollectorSources | None = None,
    config: CollectorConfig = CollectorConfig(),
    maintenance: bool = False,
    overrun: bool = False,
    missed_deadlines: int = 0,
    overrun_seconds: float = 0.0,
    checkpoint: Callable[[str], tuple[int, int, int]] = checkpoint_wal,
    maintenance_conn_factory: Callable[[str], sqlite3.Connection] = open_runtime_database,
    checkpoint_event_writer: Callable[[sqlite3.Connection, Event], None] = insert_event,
    checkpoint_metric_writer: Callable[[sqlite3.Connection, int, Metric, int | None, int | None], None] = insert_metric,
) -> int:
    active_sources = sources or CollectorSources()
    ts = int(active_sources.clock.wall() if now is None else now)
    started = active_sources.clock.monotonic()
    if not active_sources.exists(Path(db_path)):
        migrate_database(db_path)
    conn = open_runtime_database(db_path)
    try:
        capture, tunnels, env_events, services = _capture_raw(db_path, env_dir, active_sources)
        sampled_monotonic = active_sources.clock.monotonic()
        transaction_started = active_sources.clock.monotonic()
        conn.execute("BEGIN IMMEDIATE")
        cycle_id = _cycle(conn, ts, started, transaction_started, max(0.0, transaction_started - started), True, overrun, missed_deadlines, overrun_seconds)
        host_sample_id = insert_sample(conn, MetricSample(None, ts, 1, 1, 0, 0, 0, 0), cycle_id)
        metrics: list[Metric] = []
        events: list[Event] = []
        source_metrics, source_events, source_errors = _source_status(conn, capture, ts)
        metrics.extend(source_metrics)
        events.extend(source_events)
        event_state = EventState(conn)
        malformed_paths = {str(event.details.get("path", "")) for event in env_events}
        previous_bad = get_json_state(conn, "event.malformed_env_paths")
        previous_paths = {str(value) for value in previous_bad} if isinstance(previous_bad, list) else set()
        for path in sorted(malformed_paths - previous_paths):
            events.append(Event(ts, "warning", "env_parse_error", "Tunnel env source is malformed", {"path": path}))
        for path in sorted(previous_paths - malformed_paths):
            events.append(Event(ts, "info", "env_parse_recovered", "Tunnel env source recovered", {"path": path}))
        set_json_state(conn, "event.malformed_env_paths", sorted(malformed_paths))
        for path in sorted(malformed_paths):
            source_id = Path(path).stem or Path(path).name
            metrics.append(
                Metric(
                    "tunnel_source",
                    "env_source_valid",
                    0,
                    "boolean",
                    "exact",
                    {"path": path},
                    "tunnel_source",
                    source_id,
                )
            )
        for tunnel in tunnels:
            metrics.append(
                Metric(
                    "tunnel",
                    "env_source_valid",
                    1,
                    "boolean",
                    "exact",
                    entity_type="tunnel",
                    entity_id=tunnel.tunnel_id,
                )
            )

        for collector in (_host_metrics, _network_metrics, _storage_metrics):
            try:
                collected_metrics, collected_events = collector(conn, capture, active_sources, config, sampled_monotonic, ts)
                metrics.extend(collected_metrics)
                events.extend(collected_events)
            except Exception:
                source = collector.__name__.removeprefix("_")
                metrics.append(Metric("collector", _source_metric_name(source), 0, "boolean", "exact", {"source": source}, "collector_source", source))
                events.extend(event_state.availability(source, False, ts))

        service_values, service_events, tunnel_sample_ids = _service_and_tunnel_metrics(conn, capture, tunnels, services, active_sources, config, sampled_monotonic, ts, cycle_id)
        metrics.extend(service_values)
        events.extend(service_events)
        events.extend(_cycle_recovery(conn, ts))

        maintenance_started = active_sources.clock.monotonic()
        maintenance_duration = 0.0
        if maintenance:
            conn.execute("SAVEPOINT monitoring_maintenance")
            try:
                run_maintenance(conn, ts)
                conn.execute("RELEASE monitoring_maintenance")
                previous_maintenance = get_state(conn, "event.maintenance_health")
                set_state(conn, "event.maintenance_health", "ok")
                if previous_maintenance == "failed":
                    events.append(
                        Event(
                            ts,
                            "info",
                            "database_retention_recovered",
                            "Database retention maintenance recovered",
                        )
                    )
            except Exception:
                conn.execute("ROLLBACK TO monitoring_maintenance")
                conn.execute("RELEASE monitoring_maintenance")
                previous_maintenance = get_state(conn, "event.maintenance_health")
                set_state(conn, "event.maintenance_health", "failed")
                if previous_maintenance != "failed":
                    events.append(
                        Event(
                            ts,
                            "warning",
                            "database_retention_failed",
                            "Database retention maintenance failed",
                        )
                    )
            maintenance_duration = max(0.0, active_sources.clock.monotonic() - maintenance_started)

        overrun_count_raw = get_state(conn, "counter.overrun_count")
        overrun_count = int(overrun_count_raw) if overrun_count_raw else 0
        if overrun:
            overrun_count += 1
        set_state(conn, "counter.overrun_count", str(overrun_count))
        set_state(conn, "last_successful_cycle_timestamp", str(ts))
        transaction_duration = max(0.0, active_sources.clock.monotonic() - transaction_started)
        self_metrics = [
            Metric("collector", "cycle_status", 1, "boolean", "exact", entity_type="collector", entity_id="local"),
            Metric("collector", "duration_seconds", max(0.0, active_sources.clock.monotonic() - started), "seconds", "derived", entity_type="collector", entity_id="local"),
            Metric("collector", "database_transaction_duration_seconds", transaction_duration, "seconds", "derived", entity_type="collector", entity_id="local"),
            Metric("collector", "maintenance_duration_seconds", maintenance_duration, "seconds", "derived", entity_type="collector", entity_id="local"),
            Metric("collector", "tunnels_discovered", len(tunnels), "count", "exact", entity_type="collector", entity_id="local"),
            Metric("collector", "missed_deadlines", missed_deadlines, "count", "exact", entity_type="collector", entity_id="local"),
            Metric("collector", "overrun_count", overrun_count, "count", "exact", entity_type="collector", entity_id="local"),
            Metric("collector", "last_successful_cycle_timestamp", ts, "unix_seconds", "exact", entity_type="collector", entity_id="local"),
            Metric("collector", "source_error_codes", len(source_errors), "count", "exact", entity_type="collector", entity_id="local"),
        ]
        metrics.extend(self_metrics)
        metrics = _deduplicate_metrics(metrics)
        projected_metric_count = len(metrics) + 3
        metrics.extend(
            [
                Metric("collector", "metrics_written", projected_metric_count, "count", "exact", entity_type="collector", entity_id="local"),
                Metric("collector", "events_written", len(events), "count", "exact", entity_type="collector", entity_id="local"),
                Metric("collector", "rows_written", projected_metric_count + len(events) + len(tunnel_sample_ids) + len(tunnels) + 2, "count", "exact", entity_type="collector", entity_id="local"),
            ]
        )
        for metric in metrics:
            insert_metric(conn, host_sample_id, metric, cycle_id, ts)
        for event in events:
            insert_event(conn, event)
        finished = active_sources.clock.monotonic()
        insert_metric(
            conn,
            host_sample_id,
            Metric(
                "collector",
                "database_transaction_duration_seconds",
                max(0.0, finished - transaction_started),
                "seconds",
                "derived",
                entity_type="collector",
                entity_id="local",
            ),
            cycle_id,
            ts,
        )
        insert_metric(
            conn,
            host_sample_id,
            Metric(
                "collector",
                "duration_seconds",
                max(0.0, finished - started),
                "seconds",
                "derived",
                entity_type="collector",
                entity_id="local",
            ),
            cycle_id,
            ts,
        )
        _cycle(conn, ts, started, finished, max(0.0, finished - started), True, overrun, missed_deadlines, overrun_seconds)
        conn.commit()
        if maintenance:
            result: tuple[int, int, int] | None = None
            checkpoint_error: Exception | None = None
            try:
                result = checkpoint(db_path)
            except Exception as exc:
                checkpoint_error = exc
            try:
                _record_checkpoint_result(
                    db_path,
                    ts,
                    cycle_id,
                    host_sample_id,
                    result,
                    checkpoint_error,
                    maintenance_conn_factory,
                    checkpoint_event_writer,
                    checkpoint_metric_writer,
                )
            except Exception:
                pass
        return ts
    except Exception as exc:
        conn.rollback()
        _record_cycle_failure(conn, ts, started, active_sources.clock.monotonic(), exc.__class__.__name__)
        raise CollectionCycleError(ts, str(exc)) from exc
    finally:
        conn.close()


def collect_tunnel_observation(
    tunnel: Tunnel,
    ts: int,
    properties: dict[str, str],
    listeners: list[dict[str, object]],
) -> tuple[MetricSample, str]:
    pid_raw = properties.get("MainPID", "")
    pid = int(pid_raw) if pid_raw.isdigit() else 0
    owned = {
        int(listener["port"])
        for listener in listeners
        if listener.get("port") in tunnel.listen_ports
        and listener.get("pid") == pid
        and listener.get("process") == "gost"
    }
    incomplete = any(
        listener.get("port") in tunnel.listen_ports and listener.get("pid") is None
        for listener in listeners
    )
    sample = MetricSample(
        tunnel.tunnel_id,
        ts,
        int(properties.get("ActiveState") == "active"),
        int(properties.get("SubState") == "running"),
        int(properties.get("NRestarts") or 0),
        len(tunnel.listen_ports),
        len(owned),
        len(tunnel.target_ports),
    )
    return sample, "unavailable" if incomplete else "exact"
