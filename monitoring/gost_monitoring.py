#!/usr/bin/env python3
"""Compatibility facade and CLI for GOST Manager monitoring."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from monitoring.collector import (
    CollectionCycleError,
    CollectorConfig,
    CollectorSources,
    CommandExecutionError,
    command_stdout,
    collect_once as collect_once_with_sources,
    collect_tunnel_observation,
    run_command,
)
from monitoring.config import (
    ALLOWED_KEYS,
    ConfigError,
    KEY_DB,
    KEY_ENV_DIR,
    KEY_MAINTENANCE,
    KEY_SAMPLE,
    KEY_SLOW,
    KEY_TCP,
    config_from_environment,
    config_from_mapping,
    load_config,
)
from monitoring.entities import (
    DEFAULT_ENV_DIR,
    discover_tunnels,
    parse_env_file,
    parse_mappings,
    tunnel_from_env,
)
from monitoring.models import (
    Clock,
    CommandResult,
    CounterDelta,
    Event,
    Metric,
    MetricSample,
    Tunnel,
)
from monitoring.network_readers import (
    aggregate_external,
    counter_delta,
    interface_metrics,
    parse_net_dev,
)
from monitoring.proc_readers import (
    conntrack_metrics,
    cpu_metrics,
    file_handle_metrics,
    filesystem_metrics,
    load_metrics,
    memory_metrics,
    parse_proc_stat,
)
from monitoring.schema import (
    CREATE_SCHEMA,
    DEFAULT_DB_PATH,
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    EVENT_RETENTION_SECONDS,
    RAW_RETENTION_SECONDS,
    ROLLUP_RETENTION_SECONDS,
    SCHEMA_VERSION,
    _cycle,
    apply_retention,
    checkpoint_wal,
    connect_db,
    init_db,
    insert_event,
    insert_metric,
    insert_sample,
    migrate_database,
    open_runtime_database,
    quality_worst,
    rollup_completed_minutes,
    run_maintenance,
    upsert_tunnel,
)
from monitoring.scheduler import (
    MAINTENANCE_INTERVAL_SECONDS,
    record_cycle_overrun,
    run_daemon as run_daemon_with_sources,
    scheduler_ticks,
)
from monitoring.socket_readers import (
    parse_listener_address,
    parse_ss_listeners,
)
from monitoring.systemd_readers import parse_systemd_properties

_run = run_command


def collect_sample(
    tunnel: Tunnel,
    now: int | None = None,
    runner: Callable[[Sequence[str]], str | CommandResult] = _run,
) -> MetricSample:
    timestamp = int(Clock().wall() if now is None else now)
    properties = parse_systemd_properties(
        command_stdout(
            runner(
                [
                    "systemctl",
                    "--no-pager",
                    "show",
                    tunnel.service_name,
                    "--property=ActiveState,SubState,NRestarts,MainPID,ExecMainStartTimestampMonotonic",
                ]
            )
        )
    )
    listeners = parse_ss_listeners(
        command_stdout(runner(["ss", "-H", "-lntp"]))
    )
    sample, _quality = collect_tunnel_observation(tunnel, timestamp, properties, listeners)
    return sample


def listener_quality(
    tunnel: Tunnel,
    runner: Callable[[Sequence[str]], str | CommandResult] = _run,
) -> str:
    try:
        properties = parse_systemd_properties(
            command_stdout(
                runner(
                    [
                        "systemctl",
                        "--no-pager",
                        "show",
                        tunnel.service_name,
                        "--property=MainPID",
                    ]
                )
            )
        )
        listeners = parse_ss_listeners(
            command_stdout(runner(["ss", "-H", "-lntp"]))
        )
    except (CommandExecutionError, ValueError):
        return "unavailable"
    pid_raw = properties.get("MainPID", "")
    pid = int(pid_raw) if pid_raw.isdigit() else 0
    if pid <= 0:
        return "unavailable"
    for listener in listeners:
        if listener.get("port") in tunnel.listen_ports and (
            listener.get("pid") is None
        ):
            return "unavailable"
    return "exact"


def collect_host_metrics(
    proc: Path = Path("/proc"),
    fs_paths: Iterable[Path] = (
        Path("/"),
        Path("/etc/gost-manager"),
        Path("/var/lib/gost-manager"),
    ),
) -> tuple[list[Metric], list[Event]]:
    """Compatibility snapshot API; rate metrics need the stateful collector."""
    metrics: list[Metric] = []
    events: list[Event] = []
    timestamp = int(Clock().wall())
    try:
        metrics.extend(cpu_metrics(parse_proc_stat((proc / "stat").read_text()), None, None))
    except Exception:
        metrics.append(
            Metric(
                "host",
                "cpu_jiffies_total",
                None,
                "jiffies",
                "unavailable",
                entity_type="host",
                entity_id="local",
            )
        )
        events.append(
            Event(
                timestamp,
                "warning",
                "proc_stat_unavailable",
                "CPU source unavailable",
            )
        )
    try:
        metrics.extend(load_metrics((proc / "loadavg").read_text()))
    except Exception:
        metrics.append(
            Metric("host", "load1", None, "load", "unavailable", entity_type="host", entity_id="local")
        )
    try:
        metrics.extend(memory_metrics((proc / "meminfo").read_text()))
    except Exception:
        metrics.append(
            Metric("host", "memory_total_bytes", None, "bytes", "unavailable", entity_type="host", entity_id="local")
        )
    try:
        interfaces = parse_net_dev((proc / "net/dev").read_text())
        for name, counters in sorted(interfaces.items()):
            metrics.extend(interface_metrics(counters, None, None))
        metrics.extend(interface_metrics(aggregate_external(interfaces), None, None))
    except Exception:
        metrics.append(
            Metric(
                "net.external",
                "rx_bytes",
                None,
                "bytes",
                "unavailable",
                {"interface": "external-total"},
                "interface",
                "interface:external-total",
            )
        )
    try:
        metrics.extend(
            conntrack_metrics(
                (proc / "sys/net/netfilter/nf_conntrack_count").read_text(),
                (proc / "sys/net/netfilter/nf_conntrack_max").read_text(),
            )
        )
    except Exception:
        metrics.append(
            Metric("host", "conntrack_count", None, "count", "unavailable", entity_type="host", entity_id="local")
        )
    try:
        metrics.extend(
            file_handle_metrics(
                (proc / "sys/fs/file-nr").read_text(),
                (proc / "sys/fs/file-max").read_text(),
            )
        )
    except Exception:
        metrics.append(
            Metric("host", "file_handles_allocated", None, "count", "unavailable", entity_type="host", entity_id="local")
        )
    for path in fs_paths:
        try:
            metrics.extend(filesystem_metrics(path, os.statvfs))
        except Exception:
            metrics.append(
                Metric(
                    "fs",
                    "filesystem_free_bytes",
                    None,
                    "bytes",
                    "unavailable",
                    {"path": str(path)},
                    "filesystem",
                    f"fs:{path}",
                )
            )
    return metrics, events


def collect_once(
    db_path: str,
    env_dir: str,
    now: int | None = None,
    runner: Callable[[Sequence[str]], str | CommandResult] = _run,
    proc: Path = Path("/proc"),
    clock: Clock = Clock(),
    maintenance: bool = False,
    overrun: bool = False,
    missed_deadlines: int = 0,
    overrun_seconds: float = 0.0,
    checkpoint: Callable[[str], tuple[int, int, int]] = checkpoint_wal,
    maintenance_conn_factory: Callable[[str], sqlite3.Connection] = open_runtime_database,
    checkpoint_event_writer: Callable[[sqlite3.Connection, Event], None] = insert_event,
    checkpoint_metric_writer: Callable[[sqlite3.Connection, int, Metric, int | None, int | None], None] = insert_metric,
    sources: CollectorSources | None = None,
    config: CollectorConfig = CollectorConfig(),
) -> int:
    active_sources = sources or CollectorSources(clock=clock, command=runner, proc_root=proc)
    return collect_once_with_sources(
        db_path,
        env_dir,
        now,
        active_sources,
        config,
        maintenance,
        overrun,
        missed_deadlines,
        overrun_seconds,
        checkpoint,
        maintenance_conn_factory,
        checkpoint_event_writer,
        checkpoint_metric_writer,
    )


def run_daemon(
    db_path: str,
    env_dir: str,
    interval: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    maintenance_interval: float = MAINTENANCE_INTERVAL_SECONDS,
    runner: Callable[[Sequence[str]], str | CommandResult] = _run,
    clock: Clock = Clock(),
    sleeper: Callable[[float], None] = time.sleep,
    stop_requested: Callable[[], bool] | None = None,
    config: CollectorConfig | None = None,
) -> int:
    sources = CollectorSources(clock=clock, command=runner)
    active_config = config or CollectorConfig(
        sample_interval=interval,
        maintenance_interval=maintenance_interval,
    )
    return run_daemon_with_sources(
        db_path,
        env_dir,
        interval,
        maintenance_interval,
        sources,
        sleeper,
        stop_requested,
        collect=collect_once,
        record_overrun=record_cycle_overrun,
        config=active_config,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--db")
    parser.add_argument("--env-dir")
    parser.add_argument("--interval")
    parser.add_argument("--tcp-interval")
    parser.add_argument("--slow-interval")
    parser.add_argument("--maintenance-interval")
    parser.add_argument("--now", type=int)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--daemon", action="store_true")
    args = parser.parse_args(argv)
    try:
        environment_config = (
            load_config(args.config) if args.config else config_from_environment()
        )
        overrides: dict[str, object] = environment_config.as_mapping()
        if args.db is not None:
            overrides[KEY_DB] = args.db
        if args.env_dir is not None:
            overrides[KEY_ENV_DIR] = args.env_dir
        if args.interval is not None:
            overrides[KEY_SAMPLE] = args.interval
        if args.tcp_interval is not None:
            overrides[KEY_TCP] = args.tcp_interval
        if args.slow_interval is not None:
            overrides[KEY_SLOW] = args.slow_interval
        if args.maintenance_interval is not None:
            overrides[KEY_MAINTENANCE] = args.maintenance_interval
        active = config_from_mapping(
            {key: overrides[key] for key in ALLOWED_KEYS}, require_all=True
        )
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    collector_config = active.collector_config()
    if args.once:
        try:
            migrate_database(active.db_path)
            collect_once(
                active.db_path,
                active.env_dir,
                args.now,
                maintenance=True,
                config=collector_config,
            )
            return 0
        except Exception as exc:
            print(f"collection failed: {exc}", file=sys.stderr)
            return 1
    return run_daemon(
        active.db_path,
        active.env_dir,
        interval=collector_config.sample_interval,
        maintenance_interval=collector_config.maintenance_interval,
        config=collector_config,
    )


if __name__ == "__main__":
    raise SystemExit(main())
