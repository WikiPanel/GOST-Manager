"""Pure Linux procfs readers for host, disk, and process metrics."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from monitoring.models import CpuCounters, DiskCounters, Metric, ProcessSnapshot
from monitoring.network_readers import counter_delta


def parse_proc_stat(text: str) -> CpuCounters:
    aggregate: list[int] | None = None
    logical_cpus = 0
    for raw in text.splitlines():
        parts = raw.split()
        if not parts:
            continue
        if parts[0] == "cpu":
            aggregate = [int(value) for value in parts[1:9]]
        elif parts[0].startswith("cpu") and parts[0][3:].isdigit():
            logical_cpus += 1
    if aggregate is None or len(aggregate) < 8:
        raise ValueError("missing aggregate cpu counters")
    return CpuCounters(*aggregate[:8], logical_cpus=max(1, logical_cpus))


def cpu_metrics(
    current: CpuCounters,
    previous: CpuCounters | None,
    elapsed: float | None,
    max_gap: float | None = None,
) -> list[Metric]:
    fields = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
    metrics = [
        Metric("host", f"cpu_{field}_jiffies", getattr(current, field), "jiffies", "exact", entity_type="host", entity_id="local")
        for field in fields
    ]
    metrics.extend(
        [
            Metric("host", "cpu_jiffies_total", current.total, "jiffies", "exact", entity_type="host", entity_id="local"),
            Metric("host", "cpu_logical_count", current.logical_cpus, "count", "exact", entity_type="host", entity_id="local"),
        ]
    )
    gap = bool(max_gap is not None and elapsed is not None and elapsed > max_gap)
    reset = previous is not None and any(
        getattr(current, field) < getattr(previous, field) for field in fields
    )
    total_delta = current.total - previous.total if previous is not None else 0
    available = previous is not None and total_delta > 0 and not reset and (elapsed or 0) > 0
    for field in fields:
        value = None
        if available:
            value = (getattr(current, field) - getattr(previous, field)) * 100.0 / total_delta
        metrics.append(
            Metric(
                "host",
                f"cpu_{field}_percent",
                value,
                "percent",
                "derived" if available else "unavailable",
                entity_type="host",
                entity_id="local",
                reset=reset,
                gap=gap,
            )
        )
    utilization = None
    if available:
        idle_delta = current.idle - previous.idle
        iowait_delta = current.iowait - previous.iowait
        utilization = (total_delta - idle_delta - iowait_delta) * 100.0 / total_delta
    metrics.append(
        Metric(
            "host",
            "cpu_utilization_percent",
            utilization,
            "percent",
            "derived" if available else "unavailable",
            entity_type="host",
            entity_id="local",
            reset=reset,
            gap=gap,
        )
    )
    return metrics


def parse_key_values(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) >= 2:
            try:
                values[parts[0].rstrip(":")] = int(parts[1])
            except ValueError:
                continue
    return values


def read_key_values(path: Path) -> dict[str, int]:
    return parse_key_values(path.read_text(encoding="utf-8"))


def memory_metrics(text: str) -> list[Metric]:
    values = parse_key_values(text)
    names = {
        "MemTotal": "memory_total_bytes",
        "MemAvailable": "memory_available_bytes",
        "Buffers": "memory_buffers_bytes",
        "Cached": "memory_cache_bytes",
        "SwapTotal": "swap_total_bytes",
        "SwapFree": "swap_free_bytes",
        "Dirty": "memory_dirty_bytes",
        "Writeback": "memory_writeback_bytes",
    }
    metrics: list[Metric] = []
    for source, name in names.items():
        value = values.get(source)
        metrics.append(
            Metric(
                "host",
                name,
                None if value is None else value * 1024,
                "bytes",
                "exact" if value is not None else "unavailable",
                entity_type="host",
                entity_id="local",
            )
        )
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    used = None if total is None or available is None else (total - available) * 1024
    used_percent = None if total in (None, 0) or used is None else used * 100.0 / (total * 1024)
    swap_total = values.get("SwapTotal")
    swap_free = values.get("SwapFree")
    swap_used = None if swap_total is None or swap_free is None else (swap_total - swap_free) * 1024
    swap_percent = None
    if swap_total not in (None, 0) and swap_used is not None:
        swap_percent = swap_used * 100.0 / (swap_total * 1024)
    metrics.extend(
        [
            Metric("host", "memory_used_bytes", used, "bytes", "derived" if used is not None else "unavailable", entity_type="host", entity_id="local"),
            Metric("host", "memory_used_percent", used_percent, "percent", "derived" if used_percent is not None else "unavailable", entity_type="host", entity_id="local"),
            Metric("host", "swap_used_bytes", swap_used, "bytes", "derived" if swap_used is not None else "unavailable", entity_type="host", entity_id="local"),
            Metric("host", "swap_used_percent", swap_percent, "percent", "derived" if swap_percent is not None else "unavailable", entity_type="host", entity_id="local"),
        ]
    )
    return metrics


def load_metrics(text: str) -> list[Metric]:
    parts = text.split()
    if len(parts) < 3:
        raise ValueError("invalid loadavg")
    return [
        Metric("host", name, float(value), "load", "exact", entity_type="host", entity_id="local")
        for name, value in zip(("load1", "load5", "load15"), parts[:3])
    ]

def parse_diskstats(text: str) -> dict[str, DiskCounters]:
    disks: dict[str, DiskCounters] = {}
    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) < 14:
            continue
        try:
            disk = DiskCounters(
                major=int(parts[0]),
                minor=int(parts[1]),
                name=parts[2],
                reads_completed=int(parts[3]),
                sectors_read=int(parts[5]),
                writes_completed=int(parts[7]),
                sectors_written=int(parts[9]),
                io_ms=int(parts[12]),
            )
        except ValueError:
            continue
        disks[disk.name] = disk
    return disks


def disk_metrics(
    current: DiskCounters,
    previous: DiskCounters | None,
    elapsed: float | None,
    max_gap: float | None = None,
) -> list[Metric]:
    labels = {"device": current.name, "device_id": current.identity}
    entity_id = f"disk:{current.identity}"
    cumulative = {
        "disk_reads_completed": (current.reads_completed, "operations"),
        "disk_read_bytes": (current.sectors_read * 512, "bytes"),
        "disk_writes_completed": (current.writes_completed, "operations"),
        "disk_written_bytes": (current.sectors_written * 512, "bytes"),
        "disk_io_time_ms": (current.io_ms, "milliseconds"),
    }
    metrics = [
        Metric("disk", name, value, unit, "exact", labels, "disk", entity_id)
        for name, (value, unit) in cumulative.items()
    ]
    replacement = previous is not None and previous.identity != current.identity
    previous_values = None
    if previous is not None and not replacement:
        previous_values = {
            "disk_reads_completed": previous.reads_completed,
            "disk_read_bytes": previous.sectors_read * 512,
            "disk_writes_completed": previous.writes_completed,
            "disk_written_bytes": previous.sectors_written * 512,
            "disk_io_time_ms": previous.io_ms,
        }
    for name, (value, _unit) in cumulative.items():
        delta = counter_delta(
            previous_values.get(name) if previous_values else None,
            value,
            elapsed or 0.0,
            max_gap,
        )
        rate_name = f"{name}_per_second"
        rate_unit = "B/s" if "bytes" in name else "ops/s"
        if name == "disk_io_time_ms":
            rate_name = "disk_utilization_percent"
            rate_unit = "percent"
            rate = None if delta.rate is None else min(100.0, delta.rate / 10.0)
        else:
            rate = delta.rate
        metrics.append(
            Metric(
                "disk",
                rate_name,
                rate,
                rate_unit,
                delta.quality,
                labels,
                "disk",
                entity_id,
                replacement or delta.reset,
                delta.gap,
            )
        )
    return metrics


def filesystem_metrics(path: Path, statvfs: Callable[[str], os.statvfs_result]) -> list[Metric]:
    stats = statvfs(str(path))
    block_size = stats.f_frsize or stats.f_bsize
    total = stats.f_blocks * block_size
    free = stats.f_bavail * block_size
    used = max(0, total - free)
    inode_total = stats.f_files
    inode_free = stats.f_favail
    inode_used = max(0, inode_total - inode_free)
    labels = {"path": str(path)}
    entity_id = f"fs:{path}"
    values = (
        ("filesystem_total_bytes", total, "bytes", "exact"),
        ("filesystem_used_bytes", used, "bytes", "derived"),
        ("filesystem_free_bytes", free, "bytes", "exact"),
        ("filesystem_used_percent", used * 100.0 / total if total else None, "percent", "derived" if total else "unavailable"),
        ("filesystem_inode_total", inode_total, "count", "exact"),
        ("filesystem_inode_used", inode_used, "count", "derived"),
        ("filesystem_inode_free", inode_free, "count", "exact"),
        ("filesystem_inode_used_percent", inode_used * 100.0 / inode_total if inode_total else None, "percent", "derived" if inode_total else "unavailable"),
    )
    return [
        Metric("fs", name, value, unit, quality, labels, "filesystem", entity_id)
        for name, value, unit, quality in values
    ]


def database_size_metrics(
    db_path: str,
    file_size: Callable[[Path], int],
) -> list[Metric]:
    metrics: list[Metric] = []
    for name, path, missing_zero in (
        ("database_size_bytes", Path(db_path), False),
        ("database_wal_size_bytes", Path(f"{db_path}-wal"), True),
    ):
        try:
            value = file_size(path)
            quality = "exact"
        except FileNotFoundError:
            value = 0 if missing_zero else None
            quality = "exact" if missing_zero else "unavailable"
        except OSError:
            value = None
            quality = "unavailable"
        metrics.append(Metric("collector", name, value, "bytes", quality, entity_type="collector", entity_id="local"))
    return metrics


def conntrack_metrics(count_text: str, max_text: str) -> list[Metric]:
    count = int(count_text.strip())
    maximum = int(max_text.strip())
    utilization = count * 100.0 / maximum if maximum > 0 else None
    return [
        Metric("host", "conntrack_count", count, "count", "exact", entity_type="host", entity_id="local"),
        Metric("host", "conntrack_max", maximum, "count", "exact", entity_type="host", entity_id="local"),
        Metric("host", "conntrack_utilization_percent", utilization, "percent", "derived" if utilization is not None else "unavailable", entity_type="host", entity_id="local"),
    ]


def file_handle_metrics(file_nr_text: str, file_max_text: str) -> list[Metric]:
    parts = file_nr_text.split()
    if len(parts) < 3:
        raise ValueError("invalid file-nr")
    allocated = int(parts[0])
    maximum = int(file_max_text.strip())
    utilization = allocated * 100.0 / maximum if maximum > 0 else None
    return [
        Metric("host", "file_handles_allocated", allocated, "count", "exact", entity_type="host", entity_id="local"),
        Metric("host", "file_handles_max", maximum, "count", "exact", entity_type="host", entity_id="local"),
        Metric("host", "file_handles_utilization_percent", utilization, "percent", "derived" if utilization is not None else "unavailable", entity_type="host", entity_id="local"),
    ]


def parse_process_stat(text: str, page_size: int = 4096) -> ProcessSnapshot:
    close = text.rfind(")")
    if close < 0:
        raise ValueError("invalid process stat")
    pid_raw = text[: text.find(" ")]
    fields = text[close + 2 :].split()
    if len(fields) < 22:
        raise ValueError("short process stat")
    return ProcessSnapshot(
        pid=int(pid_raw),
        start_ticks=int(fields[19]),
        cpu_ticks=int(fields[11]) + int(fields[12]),
        rss_bytes=int(fields[21]) * page_size,
        rss_anon_bytes=None,
        rss_file_bytes=None,
        threads=int(fields[17]),
        fd_count=0,
        fd_soft_limit=None,
        fd_hard_limit=None,
    )


def parse_process_status(text: str) -> dict[str, int]:
    return parse_key_values(text)


def parse_open_file_limits(text: str) -> tuple[int | None, int | None]:
    for raw in text.splitlines():
        if not raw.startswith("Max open files"):
            continue
        parts = raw.split()
        if len(parts) < 5:
            break
        def number(value: str) -> int | None:
            return None if value == "unlimited" else int(value)
        return number(parts[-3]), number(parts[-2])
    return None, None


def read_process_snapshot(
    pid: int,
    proc_root: Path = Path("/proc"),
    read_text: Callable[[Path], str] | None = None,
    list_dir: Callable[[Path], list[str]] | None = None,
    page_size: int = 4096,
) -> ProcessSnapshot:
    reader = read_text or (lambda path: path.read_text(encoding="utf-8"))
    lister = list_dir or (lambda path: os.listdir(path))
    root = proc_root / str(pid)
    base = parse_process_stat(reader(root / "stat"), page_size)
    status = parse_process_status(reader(root / "status"))
    soft, hard = parse_open_file_limits(reader(root / "limits"))
    return ProcessSnapshot(
        pid=pid,
        start_ticks=base.start_ticks,
        cpu_ticks=base.cpu_ticks,
        rss_bytes=status.get("VmRSS", base.rss_bytes // 1024) * 1024,
        rss_anon_bytes=status.get("RssAnon", 0) * 1024 if "RssAnon" in status else None,
        rss_file_bytes=status.get("RssFile", 0) * 1024 if "RssFile" in status else None,
        threads=status.get("Threads", base.threads),
        fd_count=len(lister(root / "fd")),
        fd_soft_limit=soft,
        fd_hard_limit=hard,
    )


def process_metrics(
    service: str,
    current: ProcessSnapshot,
    previous: ProcessSnapshot | None,
    elapsed: float | None,
    ticks_per_second: int,
    max_gap: float | None = None,
) -> list[Metric]:
    labels = {"service": service}
    entity_id = service
    identity_changed = previous is not None and previous.start_ticks != current.start_ticks
    delta = counter_delta(
        previous.cpu_ticks if previous and not identity_changed else None,
        current.cpu_ticks,
        elapsed or 0.0,
        max_gap,
    )
    cpu_percent = None
    if delta.rate is not None and ticks_per_second > 0:
        cpu_percent = delta.rate * 100.0 / ticks_per_second
    values: list[tuple[str, int | float | None, str, str]] = [
        ("process_pid", current.pid, "pid", "exact"),
        ("process_start_ticks", current.start_ticks, "ticks", "exact"),
        ("process_cpu_ticks", current.cpu_ticks, "ticks", "exact"),
        ("process_cpu_percent", cpu_percent, "percent", delta.quality),
        ("process_rss_bytes", current.rss_bytes, "bytes", "exact"),
        ("process_rss_anon_bytes", current.rss_anon_bytes, "bytes", "exact" if current.rss_anon_bytes is not None else "unavailable"),
        ("process_rss_file_bytes", current.rss_file_bytes, "bytes", "exact" if current.rss_file_bytes is not None else "unavailable"),
        ("process_threads", current.threads, "count", "exact"),
        ("process_open_fds", current.fd_count, "count", "exact"),
        ("process_fd_soft_limit", current.fd_soft_limit, "count", "exact" if current.fd_soft_limit is not None else "unavailable"),
        ("process_fd_hard_limit", current.fd_hard_limit, "count", "exact" if current.fd_hard_limit is not None else "unavailable"),
    ]
    return [
        Metric(
            "service",
            name,
            value,
            unit,
            quality,
            labels,
            "service",
            entity_id,
            identity_changed or (name == "process_cpu_percent" and delta.reset),
            name == "process_cpu_percent" and delta.gap,
        )
        for name, value, unit, quality in values
    ]
