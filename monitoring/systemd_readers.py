"""systemd, cgroup, and managed-service metric helpers."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path

from monitoring.models import Metric

GOST_SERVICE_RE = re.compile(r"^gost-(?:iran|kharej)-[1-9][0-9]*\.service$")
SYSTEMD_PROPERTIES = ",".join(
    (
        "ActiveState",
        "SubState",
        "MainPID",
        "ExecMainStartTimestampMonotonic",
        "NRestarts",
        "TasksCurrent",
        "MemoryCurrent",
        "MemoryPeak",
        "ControlGroup",
        "IPAccounting",
        "IPIngressBytes",
        "IPEgressBytes",
    )
)


def parse_systemd_properties(text: str) -> dict[str, str]:
    return {
        key: value
        for line in text.splitlines()
        if "=" in line
        for key, value in (line.split("=", 1),)
    }


def discover_managed_services(
    tunnels: Iterable[str],
    list_units_text: str = "",
) -> tuple[str, ...]:
    services = {service for service in tunnels if GOST_SERVICE_RE.match(service)}
    for raw in list_units_text.splitlines():
        parts = raw.split()
        if parts and GOST_SERVICE_RE.match(parts[0]):
            services.add(parts[0])
    return tuple(sorted(services))


def _int_property(properties: dict[str, str], key: str) -> int | None:
    value = properties.get(key, "")
    return int(value) if value.isdigit() else None


def service_metrics(service: str, properties: dict[str, str]) -> list[Metric]:
    labels = {"service": service}
    entity_id = service
    active = properties.get("ActiveState")
    substate = properties.get("SubState")
    main_pid = _int_property(properties, "MainPID")
    start = _int_property(properties, "ExecMainStartTimestampMonotonic")
    restarts = _int_property(properties, "NRestarts")
    tasks = _int_property(properties, "TasksCurrent")
    memory_current = _int_property(properties, "MemoryCurrent")
    memory_peak = _int_property(properties, "MemoryPeak")
    values: list[tuple[str, int | str | None, str, str]] = [
        ("service_active", None if active is None else int(active == "active"), "boolean", "exact" if active is not None else "unavailable"),
        ("service_active_state", active, "state", "exact" if active is not None else "unavailable"),
        ("service_sub_state", substate, "state", "exact" if substate is not None else "unavailable"),
        ("service_main_pid", main_pid, "pid", "exact" if main_pid is not None else "unavailable"),
        ("service_start_monotonic_us", start, "microseconds", "exact" if start is not None else "unavailable"),
        ("service_restart_count", restarts, "count", "exact" if restarts is not None else "unavailable"),
        ("service_tasks", tasks, "count", "exact" if tasks is not None else "unavailable"),
        ("cgroup_memory_current_bytes", memory_current, "bytes", "exact" if memory_current is not None else "unavailable"),
        ("cgroup_memory_peak_bytes", memory_peak, "bytes", "exact" if memory_peak is not None else "unavailable"),
    ]
    accounting = properties.get("IPAccounting", "").lower() == "yes"
    for key, name in (
        ("IPIngressBytes", "systemd_ip_ingress_bytes"),
        ("IPEgressBytes", "systemd_ip_egress_bytes"),
    ):
        value = _int_property(properties, key) if accounting else None
        values.append(
            (name, value, "bytes", "exact" if value is not None else "unavailable")
        )
    return [
        Metric("service", name, value, unit, quality, labels, "service", entity_id)
        for name, value, unit, quality in values
    ]

def read_cgroup_memory(
    control_group: str,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    read_text: Callable[[Path], str] | None = None,
) -> dict[str, int | None]:
    reader = read_text or (lambda path: path.read_text(encoding="utf-8"))
    relative = Path(control_group.lstrip("/"))
    if ".." in relative.parts:
        raise ValueError("unsafe cgroup path")
    root = cgroup_root / relative

    def read_number(name: str) -> int | None:
        try:
            value = reader(root / name).strip()
        except OSError:
            return None
        if value == "max":
            return None
        return int(value) if value.isdigit() else None

    return {
        "memory_current": read_number("memory.current"),
        "memory_peak": read_number("memory.peak"),
    }


def read_cgroup_pids(
    control_group: str,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    read_text: Callable[[Path], str] | None = None,
) -> tuple[int, ...]:
    reader = read_text or (lambda path: path.read_text(encoding="utf-8"))
    relative = Path(control_group.lstrip("/"))
    if ".." in relative.parts:
        raise ValueError("unsafe cgroup path")
    text = reader(cgroup_root / relative / "cgroup.procs")
    pids: set[int] = set()
    for raw in text.splitlines():
        value = raw.strip()
        if not value:
            continue
        if not value.isdigit() or int(value) <= 0:
            raise ValueError("invalid cgroup.procs entry")
        pids.add(int(value))
    return tuple(sorted(pids))


def cgroup_memory_metrics(service: str, values: dict[str, int | None]) -> list[Metric]:
    labels = {"service": service}
    return [
        Metric(
            "service",
            name,
            values.get(source),
            "bytes",
            "exact" if values.get(source) is not None else "unavailable",
            labels,
            "service",
            service,
        )
        for source, name in (
            ("memory_current", "cgroup_memory_current_bytes"),
            ("memory_peak", "cgroup_memory_peak_bytes"),
        )
    ]
