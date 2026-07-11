"""Deterministic monitoring capacity estimates."""

from __future__ import annotations

import dataclasses
import math

SECONDS_PER_DAY = 24 * 60 * 60
REPRESENTATIVE_FAST_POINTS_PER_CYCLE = 522
REPRESENTATIVE_FULL_SOCKET_EXTRA_POINTS = 9
REPRESENTATIVE_SLOW_EXTRA_POINTS = 52
ESTIMATED_SQLITE_BYTES_PER_POINT = 192
SQLITE_AUXILIARY_OVERHEAD = 1.15


@dataclasses.dataclass(frozen=True)
class StorageBudget:
    metric_points_per_day: int
    raw_metric_points: int
    estimated_sqlite_bytes: int

    @property
    def estimated_sqlite_gib(self) -> float:
        return self.estimated_sqlite_bytes / (1024 ** 3)


def estimate_storage_budget(
    fast_points_per_cycle: int = REPRESENTATIVE_FAST_POINTS_PER_CYCLE,
    full_socket_extra_points: int = REPRESENTATIVE_FULL_SOCKET_EXTRA_POINTS,
    slow_extra_points: int = REPRESENTATIVE_SLOW_EXTRA_POINTS,
    sample_interval: float = 5.0,
    full_socket_interval: float = 30.0,
    slow_interval: float = 60.0,
    raw_retention_hours: int = 48,
    bytes_per_point: int = ESTIMATED_SQLITE_BYTES_PER_POINT,
    auxiliary_overhead: float = SQLITE_AUXILIARY_OVERHEAD,
) -> StorageBudget:
    fast_cycles = math.ceil(SECONDS_PER_DAY / sample_interval)
    full_cycles = math.ceil(SECONDS_PER_DAY / full_socket_interval)
    slow_cycles = math.ceil(SECONDS_PER_DAY / slow_interval)
    points_per_day = (
        fast_cycles * fast_points_per_cycle
        + full_cycles * full_socket_extra_points
        + slow_cycles * slow_extra_points
    )
    raw_points = math.ceil(points_per_day * raw_retention_hours / 24)
    estimated_bytes = math.ceil(raw_points * bytes_per_point * auxiliary_overhead)
    return StorageBudget(points_per_day, raw_points, estimated_bytes)


def representative_storage_budget() -> StorageBudget:
    return estimate_storage_budget()
