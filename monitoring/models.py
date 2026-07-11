"""Shared monitoring models and quality contracts."""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable

QUALITY = ("exact", "derived", "estimated", "unavailable")
QUALITY_RANK = {"exact": 0, "derived": 1, "estimated": 2, "unavailable": 3}


@dataclasses.dataclass(frozen=True)
class Tunnel:
    side: str
    number: int
    service_name: str
    env_path: str
    listen_ports: tuple[int, ...]
    target_ports: tuple[int, ...]
    remote_endpoint: str | None = None

    @property
    def tunnel_id(self) -> str:
        return f"{self.side}-{self.number}"


@dataclasses.dataclass(frozen=True)
class Metric:
    scope: str
    name: str
    value: float | int | str | None
    unit: str
    quality: str
    labels: dict[str, str] = dataclasses.field(default_factory=dict)
    entity_type: str | None = None
    entity_id: str | None = None
    reset: bool = False
    gap: bool = False

    def __post_init__(self) -> None:
        if self.quality not in QUALITY:
            raise ValueError(f"invalid metric quality: {self.quality}")


@dataclasses.dataclass(frozen=True)
class Event:
    ts: int
    severity: str
    code: str
    message: str
    details: dict[str, object] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class MetricSample:
    tunnel_id: str | None
    collected_at: int
    service_state: int
    service_substate: int
    restart_count: int
    listen_ports_total: int
    listen_ports_up: int
    configured_mappings_total: int
    rx_bytes: int | None = None
    tx_bytes: int | None = None


@dataclasses.dataclass(frozen=True)
class CounterDelta:
    delta: int | None
    rate: float | None
    quality: str
    reset: bool
    gap: bool


@dataclasses.dataclass(frozen=True)
class Clock:
    wall: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic


@dataclasses.dataclass(frozen=True)
class CpuCounters:
    user: int
    nice: int
    system: int
    idle: int
    iowait: int
    irq: int
    softirq: int
    steal: int
    logical_cpus: int

    @property
    def total(self) -> int:
        return (
            self.user
            + self.nice
            + self.system
            + self.idle
            + self.iowait
            + self.irq
            + self.softirq
            + self.steal
        )


@dataclasses.dataclass(frozen=True)
class InterfaceCounters:
    name: str
    rx_bytes: int
    rx_packets: int
    rx_errors: int
    rx_drops: int
    tx_bytes: int
    tx_packets: int
    tx_errors: int
    tx_drops: int


@dataclasses.dataclass(frozen=True)
class DiskCounters:
    major: int
    minor: int
    name: str
    reads_completed: int
    sectors_read: int
    writes_completed: int
    sectors_written: int
    io_ms: int

    @property
    def identity(self) -> str:
        return f"{self.major}:{self.minor}"


@dataclasses.dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    start_ticks: int
    cpu_ticks: int
    rss_bytes: int
    rss_anon_bytes: int | None
    rss_file_bytes: int | None
    threads: int
    fd_count: int
    fd_soft_limit: int | None
    fd_hard_limit: int | None


@dataclasses.dataclass(frozen=True)
class SocketRecord:
    state: str
    local_address: str
    local_port: int
    peer_address: str
    peer_port: int
    pid: int | None
    process: str | None


def quality_worst(qualities: list[str] | tuple[str, ...]) -> str:
    if not qualities:
        return "unavailable"
    return max(qualities, key=lambda quality: QUALITY_RANK[quality])
