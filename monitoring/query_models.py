"""Shared models for the read-only monitoring query layer."""

from __future__ import annotations

import dataclasses
from typing import Any


EXIT_INVALID_INPUT = 2
EXIT_DATABASE = 3
EXIT_LIMIT = 4


class QueryError(RuntimeError):
    exit_code = EXIT_INVALID_INPUT


class QueryInputError(QueryError):
    pass


class QueryDatabaseError(QueryError):
    exit_code = EXIT_DATABASE


class QueryLimitError(QueryError):
    exit_code = EXIT_LIMIT


class QueryNotFoundError(QueryInputError):
    pass


@dataclasses.dataclass(frozen=True)
class QueryWindow:
    requested_start: int
    requested_end: int
    effective_start: int
    effective_end: int
    truncated: bool = False

    @property
    def duration_seconds(self) -> int:
        return max(0, self.effective_end - self.effective_start)

    def to_dict(self) -> dict[str, int | bool]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class QueryPlan:
    source_mode: str
    raw_start: int | None = None
    raw_end: int | None = None
    rollup_start: int | None = None
    rollup_end: int | None = None
    estimated_rows: int | None = None
    reason: str = "retention"
    stream_raw: bool = False
    rollup_watermark: int | None = None


@dataclasses.dataclass(frozen=True)
class MetricPoint:
    entity_type: str
    entity_id: str
    metric_name: str
    ts: int
    numeric_value: float | None
    text_value: str | None
    unit: str
    quality: str
    reset: int = 0
    gap: int = 0


@dataclasses.dataclass(frozen=True)
class RollupPoint:
    entity_type: str
    entity_id: str
    metric_name: str
    minute_start: int
    samples: int
    expected_samples: int
    min_value: float | None
    avg_value: float | None
    max_value: float | None
    unavailable_count: int
    reset_count: int
    gap_count: int
    coverage: float
    unit: str
    quality: str


@dataclasses.dataclass
class SeriesSummary:
    entity_type: str
    entity_id: str
    metric_name: str
    unit: str
    source_mode: str
    quality: str
    metric_semantics: str = "unknown"
    latest: float | str | None = None
    latest_timestamp: int | None = None
    minimum: float | None = None
    average: float | None = None
    maximum: float | None = None
    p95: float | None = None
    sample_count: int = 0
    expected_sample_count: int = 0
    coverage: float = 0.0
    unavailable_count: int = 0
    reset_count: int = 0
    gap_count: int = 0
    first_timestamp: int | None = None
    last_timestamp: int | None = None
    transition_count: int | None = None
    data_age_seconds: int | None = None
    numeric: bool = True
    weighted_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value.pop("weighted_seconds", None)
        return value


@dataclasses.dataclass(frozen=True)
class EventRecord:
    ts: int
    severity: str
    code: str
    message: str
    details: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class HealthResult:
    status: str
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    evaluated_at: int
    data_age_seconds: int | None
    source_quality: str
    entity_type: str
    entity_id: str

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class QueryResult:
    window: QueryWindow
    source_mode: str
    series: list[SeriesSummary]
    generated_at: int
    schema_version: int
    granularity: str = "summary"
    filters: dict[str, object] = dataclasses.field(default_factory=dict)
    materialized_rows: int = 0
    rows_scanned: int = 0
    maximum_rows_buffered: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "window": self.window.to_dict(),
            "source_mode": self.source_mode,
            "generated_at": self.generated_at,
            "schema_version": self.schema_version,
            "granularity": self.granularity,
            "filters": self.filters,
            "materialized_rows": self.materialized_rows,
            "rows_scanned": self.rows_scanned,
            "maximum_rows_buffered": self.maximum_rows_buffered,
            "series": [item.to_dict() for item in self.series],
        }
