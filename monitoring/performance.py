"""Deterministic monitoring capacity estimates."""

from __future__ import annotations

import dataclasses
import math

SECONDS_PER_DAY = 24 * 60 * 60
MINUTES_PER_DAY = 24 * 60
GIB = 1024 ** 3

REPRESENTATIVE_FAST_POINTS_PER_CYCLE = 522
REPRESENTATIVE_FULL_SOCKET_EXTRA_POINTS = 9
REPRESENTATIVE_SLOW_EXTRA_POINTS = 52
REPRESENTATIVE_ROLLUP_SERIES_PER_MINUTE = 583
REPRESENTATIVE_SAMPLES_PER_CYCLE = 7

ESTIMATED_RAW_TABLE_BYTES_PER_POINT = 128
ESTIMATED_ROLLUP_TABLE_BYTES_PER_ROW = 160
ESTIMATED_SAMPLE_CYCLE_BYTES_PER_ROW = 128
ESTIMATED_METRIC_SAMPLE_BYTES_PER_ROW = 192
ESTIMATED_EVENT_BYTES_PER_ROW = 512
ESTIMATED_ENTITY_BYTES_PER_ROW = 512
REPRESENTATIVE_EVENTS_PER_DAY = 5_000
REPRESENTATIVE_ENTITY_CAPACITY = 2_048
SQLITE_INDEX_AND_FREE_PAGE_RATIO = 0.50
WAL_AND_OPERATIONAL_HEADROOM_RATIO = 0.20
RESERVATION_INCREMENT_BYTES = 2 * GIB


@dataclasses.dataclass(frozen=True)
class StorageBudget:
    metric_points_per_day: int
    raw_metric_points: int
    minute_rollup_rows: int
    sample_cycle_rows: int
    metric_sample_rows: int
    event_rows: int
    entity_rows: int
    estimated_raw_table_bytes: int
    estimated_rollup_table_bytes: int
    estimated_sample_cycles_bytes: int
    estimated_metric_samples_bytes: int
    estimated_events_bytes: int
    estimated_entities_bytes: int
    estimated_indexes_and_free_pages_bytes: int
    wal_and_operational_headroom_bytes: int
    estimated_total_database_bytes: int
    recommended_disk_reservation_bytes: int

    @property
    def estimated_auxiliary_table_bytes(self) -> int:
        return (
            self.estimated_sample_cycles_bytes
            + self.estimated_metric_samples_bytes
            + self.estimated_events_bytes
            + self.estimated_entities_bytes
        )

    @property
    def estimated_total_database_gib(self) -> float:
        return self.estimated_total_database_bytes / GIB

    @property
    def recommended_disk_reservation_gib(self) -> float:
        return self.recommended_disk_reservation_bytes / GIB


def estimate_storage_budget(
    fast_points_per_cycle: int = REPRESENTATIVE_FAST_POINTS_PER_CYCLE,
    full_socket_extra_points: int = REPRESENTATIVE_FULL_SOCKET_EXTRA_POINTS,
    slow_extra_points: int = REPRESENTATIVE_SLOW_EXTRA_POINTS,
    rollup_series_per_minute: int = REPRESENTATIVE_ROLLUP_SERIES_PER_MINUTE,
    samples_per_cycle: int = REPRESENTATIVE_SAMPLES_PER_CYCLE,
    sample_interval: float = 5.0,
    full_socket_interval: float = 30.0,
    slow_interval: float = 60.0,
    raw_retention_hours: int = 48,
    rollup_retention_days: int = 30,
    events_per_day: int = REPRESENTATIVE_EVENTS_PER_DAY,
    entity_capacity: int = REPRESENTATIVE_ENTITY_CAPACITY,
    raw_bytes_per_point: int = ESTIMATED_RAW_TABLE_BYTES_PER_POINT,
    rollup_bytes_per_row: int = ESTIMATED_ROLLUP_TABLE_BYTES_PER_ROW,
    index_and_free_page_ratio: float = SQLITE_INDEX_AND_FREE_PAGE_RATIO,
    wal_and_headroom_ratio: float = WAL_AND_OPERATIONAL_HEADROOM_RATIO,
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
    rollup_rows = rollup_series_per_minute * MINUTES_PER_DAY * rollup_retention_days
    sample_cycle_rows = math.ceil(raw_retention_hours * 3600 / sample_interval)
    metric_sample_rows = sample_cycle_rows * samples_per_cycle
    event_rows = events_per_day * rollup_retention_days

    raw_table_bytes = raw_points * raw_bytes_per_point
    rollup_table_bytes = rollup_rows * rollup_bytes_per_row
    sample_cycles_bytes = sample_cycle_rows * ESTIMATED_SAMPLE_CYCLE_BYTES_PER_ROW
    metric_samples_bytes = metric_sample_rows * ESTIMATED_METRIC_SAMPLE_BYTES_PER_ROW
    events_bytes = event_rows * ESTIMATED_EVENT_BYTES_PER_ROW
    entities_bytes = entity_capacity * ESTIMATED_ENTITY_BYTES_PER_ROW
    table_bytes = (
        raw_table_bytes
        + rollup_table_bytes
        + sample_cycles_bytes
        + metric_samples_bytes
        + events_bytes
        + entities_bytes
    )
    index_and_free_bytes = math.ceil(table_bytes * index_and_free_page_ratio)
    database_before_headroom = table_bytes + index_and_free_bytes
    wal_and_headroom_bytes = math.ceil(
        database_before_headroom * wal_and_headroom_ratio
    )
    total_bytes = database_before_headroom + wal_and_headroom_bytes
    recommended_bytes = (
        math.ceil(total_bytes / RESERVATION_INCREMENT_BYTES)
        * RESERVATION_INCREMENT_BYTES
    )
    return StorageBudget(
        metric_points_per_day=points_per_day,
        raw_metric_points=raw_points,
        minute_rollup_rows=rollup_rows,
        sample_cycle_rows=sample_cycle_rows,
        metric_sample_rows=metric_sample_rows,
        event_rows=event_rows,
        entity_rows=entity_capacity,
        estimated_raw_table_bytes=raw_table_bytes,
        estimated_rollup_table_bytes=rollup_table_bytes,
        estimated_sample_cycles_bytes=sample_cycles_bytes,
        estimated_metric_samples_bytes=metric_samples_bytes,
        estimated_events_bytes=events_bytes,
        estimated_entities_bytes=entities_bytes,
        estimated_indexes_and_free_pages_bytes=index_and_free_bytes,
        wal_and_operational_headroom_bytes=wal_and_headroom_bytes,
        estimated_total_database_bytes=total_bytes,
        recommended_disk_reservation_bytes=recommended_bytes,
    )


def representative_storage_budget() -> StorageBudget:
    return estimate_storage_budget()
