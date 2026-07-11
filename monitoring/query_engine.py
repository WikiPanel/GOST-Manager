"""Cadence-aware raw, rollup, and hybrid monitoring queries."""

from __future__ import annotations

import dataclasses
import math
import time
from collections import defaultdict
from collections.abc import Callable, Sequence

from monitoring.models import QUALITY_RANK
from monitoring.query_db import ReadOnlyDatabase
from monitoring.query_models import (
    EventRecord,
    MetricPoint,
    QueryLimitError,
    QueryNotFoundError,
    QueryResult,
    QueryWindow,
    RollupPoint,
    SeriesSummary,
)
from monitoring.query_window import RetentionPolicy, plan_window
from monitoring.schema import DEFAULT_SAMPLE_INTERVAL_SECONDS

TEXT_UNITS = {"state", "endpoint", "text"}


@dataclasses.dataclass(frozen=True)
class QueryLimits:
    max_query_rows: int = 100_000
    max_series: int = 5_000
    max_events: int = 2_000
    max_export_rows: int = 100_000
    max_seed_seconds: int = 300
    max_gap_multiplier: float = 2.5


def worst_quality(values: Sequence[str]) -> str:
    if not values:
        return "unavailable"
    return max(values, key=lambda value: QUALITY_RANK.get(value, 3))


def cadence_for(
    entity_type: str,
    metric_name: str,
    registry: dict[str, float],
    default: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
) -> float:
    cadence = default
    for key, seconds in registry.items():
        registered_type, separator, pattern = key.partition(":")
        if not separator or registered_type != entity_type:
            continue
        if pattern.endswith("*") and metric_name.startswith(pattern[:-1]):
            cadence = seconds
        elif pattern == metric_name:
            cadence = seconds
    return max(1.0, float(cadence))


def weighted_percentile(values: list[tuple[float, float]], percentile: float) -> float | None:
    positive = sorted((value, weight) for value, weight in values if weight > 0)
    total = sum(weight for _value, weight in positive)
    if not positive or total <= 0:
        return None
    target = total * percentile
    cumulative = 0.0
    for value, weight in positive:
        cumulative += weight
        if cumulative >= target:
            return value
    return positive[-1][0]


def _raw_summary(
    key: tuple[str, str, str],
    points: list[MetricPoint],
    start: int,
    end: int,
    cadence: float,
    now: int,
    max_gap_multiplier: float = 2.5,
) -> SeriesSummary:
    entity_type, entity_id, metric_name = key
    ordered = sorted(points, key=lambda item: item.ts)
    inside = [item for item in ordered if start <= item.ts < end]
    unit = (inside or ordered)[-1].unit
    numeric = unit not in TEXT_UNITS and not any(item.text_value is not None for item in inside)
    expected = max(1, math.ceil(max(0, end - start) / cadence))
    qualities = [item.quality for item in inside]
    summary = SeriesSummary(
        entity_type=entity_type,
        entity_id=entity_id,
        metric_name=metric_name,
        unit=unit,
        source_mode="raw",
        quality=worst_quality(qualities),
        sample_count=len(inside),
        expected_sample_count=expected,
        coverage=min(1.0, len(inside) / expected),
        unavailable_count=sum(
            1
            for item in inside
            if item.quality == "unavailable"
            or (item.numeric_value is None and item.text_value is None)
        ),
        reset_count=sum(int(item.reset) for item in inside),
        gap_count=sum(int(item.gap) for item in inside),
        first_timestamp=inside[0].ts if inside else None,
        last_timestamp=inside[-1].ts if inside else None,
        numeric=numeric,
    )
    if inside:
        latest = inside[-1]
        summary.latest = latest.numeric_value if numeric else latest.text_value
        summary.latest_timestamp = latest.ts
        summary.data_age_seconds = max(0, now - latest.ts)
    if not numeric:
        values = [item.text_value for item in inside if item.text_value is not None]
        summary.transition_count = sum(
            1 for previous, current in zip(values, values[1:]) if previous != current
        )
        return summary

    numeric_values = [
        float(item.numeric_value)
        for item in inside
        if item.numeric_value is not None and item.quality != "unavailable"
    ]
    summary.minimum = min(numeric_values) if numeric_values else None
    summary.maximum = max(numeric_values) if numeric_values else None
    max_gap = cadence * max_gap_multiplier
    weighted: list[tuple[float, float]] = []
    for index, point in enumerate(ordered):
        if point.numeric_value is None or point.quality == "unavailable":
            continue
        if point.ts < start and start - point.ts > max_gap:
            continue
        next_ts = ordered[index + 1].ts if index + 1 < len(ordered) else end
        segment_start = max(start, point.ts)
        segment_end = min(end, next_ts, int(point.ts + max_gap))
        if segment_end > segment_start:
            weighted.append((float(point.numeric_value), segment_end - segment_start))
    total_weight = sum(weight for _value, weight in weighted)
    summary.weighted_seconds = total_weight
    if total_weight > 0:
        summary.average = sum(value * weight for value, weight in weighted) / total_weight
        summary.p95 = weighted_percentile(weighted, 0.95)
    return summary


def _rollup_summary(
    key: tuple[str, str, str],
    points: list[RollupPoint],
    start: int,
    end: int,
    cadence: float,
) -> SeriesSummary:
    entity_type, entity_id, metric_name = key
    ordered = sorted(points, key=lambda item: item.minute_start)
    unit = ordered[-1].unit
    expected = max(1, math.ceil(max(0, end - start) / cadence))
    if unit in TEXT_UNITS:
        return SeriesSummary(
            entity_type=entity_type,
            entity_id=entity_id,
            metric_name=metric_name,
            unit=unit,
            source_mode="rollup",
            quality="unavailable",
            sample_count=sum(item.samples for item in ordered),
            expected_sample_count=expected,
            coverage=0.0,
            unavailable_count=sum(item.unavailable_count for item in ordered),
            reset_count=sum(item.reset_count for item in ordered),
            gap_count=sum(item.gap_count for item in ordered),
            first_timestamp=ordered[0].minute_start,
            last_timestamp=ordered[-1].minute_start + 59,
            numeric=False,
        )
    samples = sum(item.samples for item in ordered)
    weighted_total = 0.0
    weighted_seconds = 0.0
    for item in ordered:
        seconds = max(
            0,
            min(end, item.minute_start + 60) - max(start, item.minute_start),
        )
        covered = seconds * max(0.0, min(1.0, item.coverage))
        if item.avg_value is not None and covered > 0:
            weighted_total += float(item.avg_value) * covered
            weighted_seconds += covered
    minimums = [float(item.min_value) for item in ordered if item.min_value is not None]
    maximums = [float(item.max_value) for item in ordered if item.max_value is not None]
    return SeriesSummary(
        entity_type=entity_type,
        entity_id=entity_id,
        metric_name=metric_name,
        unit=unit,
        source_mode="rollup",
        quality=worst_quality([item.quality for item in ordered]),
        minimum=min(minimums) if minimums else None,
        average=weighted_total / weighted_seconds if weighted_seconds else None,
        maximum=max(maximums) if maximums else None,
        p95=None,
        sample_count=samples,
        expected_sample_count=expected,
        coverage=min(1.0, samples / expected) if expected else 0.0,
        unavailable_count=sum(item.unavailable_count for item in ordered),
        reset_count=sum(item.reset_count for item in ordered),
        gap_count=sum(item.gap_count for item in ordered),
        first_timestamp=ordered[0].minute_start if ordered else None,
        last_timestamp=ordered[-1].minute_start + 59 if ordered else None,
        numeric=True,
        weighted_seconds=weighted_seconds,
    )


def _combine_hybrid(
    raw: SeriesSummary | None,
    rollup: SeriesSummary | None,
    key: tuple[str, str, str],
    start: int,
    end: int,
    cadence: float,
) -> SeriesSummary:
    parts = [item for item in (rollup, raw) if item is not None]
    base = raw or rollup
    assert base is not None
    numeric = all(item.numeric for item in parts)
    weighted_seconds = sum(item.weighted_seconds for item in parts)
    averages = sum(
        float(item.average) * item.weighted_seconds
        for item in parts
        if item.average is not None
    )
    minimums = [float(item.minimum) for item in parts if item.minimum is not None]
    maximums = [float(item.maximum) for item in parts if item.maximum is not None]
    expected = max(1, math.ceil(max(0, end - start) / cadence))
    samples = sum(item.sample_count for item in parts)
    return SeriesSummary(
        entity_type=key[0],
        entity_id=key[1],
        metric_name=key[2],
        unit=base.unit,
        source_mode="hybrid",
        quality=worst_quality([item.quality for item in parts]),
        latest=raw.latest if raw is not None else None,
        latest_timestamp=raw.latest_timestamp if raw is not None else None,
        minimum=min(minimums) if minimums else None,
        average=averages / weighted_seconds if weighted_seconds else None,
        maximum=max(maximums) if maximums else None,
        p95=None,
        sample_count=samples,
        expected_sample_count=expected,
        coverage=min(1.0, samples / expected) if expected else 0.0,
        unavailable_count=sum(item.unavailable_count for item in parts),
        reset_count=sum(item.reset_count for item in parts),
        gap_count=sum(item.gap_count for item in parts),
        first_timestamp=min(
            item.first_timestamp for item in parts if item.first_timestamp is not None
        ) if any(item.first_timestamp is not None for item in parts) else None,
        last_timestamp=max(
            item.last_timestamp for item in parts if item.last_timestamp is not None
        ) if any(item.last_timestamp is not None for item in parts) else None,
        transition_count=raw.transition_count if raw is not None else None,
        data_age_seconds=raw.data_age_seconds if raw is not None else None,
        numeric=numeric,
        weighted_seconds=weighted_seconds,
    )


class QueryEngine:
    def __init__(
        self,
        database: ReadOnlyDatabase,
        clock: Callable[[], float] = time.time,
        retention: RetentionPolicy = RetentionPolicy(),
        limits: QueryLimits = QueryLimits(),
    ):
        self.database = database
        self.clock = clock
        self.retention = retention
        self.limits = limits

    def summary(
        self,
        window: QueryWindow,
        entity_type: str | None = None,
        entity_id: str | None = None,
        metric_names: Sequence[str] | None = None,
        require_match: bool = False,
    ) -> QueryResult:
        now = int(self.clock())
        plan = plan_window(window, now, self.retention)
        with self.database.connection() as conn:
            registry = self.database.cadence_registry(conn)
            raw_points: list[MetricPoint] = []
            rollup_points: list[RollupPoint] = []
            categorical_catalog: list[MetricPoint] = []
            if plan.raw_start is not None and plan.raw_end is not None:
                seed = min(
                    self.limits.max_seed_seconds,
                    int(max(registry.values(), default=60.0) * self.limits.max_gap_multiplier),
                )
                raw_points = self.database.raw_points(
                    conn,
                    plan.raw_start,
                    plan.raw_end,
                    seed,
                    self.limits.max_query_rows,
                    entity_type,
                    entity_id,
                    metric_names,
                )
            if plan.rollup_start is not None and plan.rollup_end is not None:
                complete_start = int(math.ceil(plan.rollup_start / 60.0) * 60)
                complete_end = int(math.floor(plan.rollup_end / 60.0) * 60)
                if complete_start < complete_end:
                    rollup_points = self.database.rollup_points(
                        conn,
                        complete_start,
                        complete_end,
                        self.limits.max_query_rows,
                        entity_type,
                        entity_id,
                        metric_names,
                    )
                if plan.source_mode == "rollup":
                    categorical_catalog = [
                        point
                        for point in self.database.latest_points(
                            conn,
                            self.limits.max_series,
                            entity_type,
                            entity_id,
                            metric_names,
                        )
                        if point.unit in TEXT_UNITS
                    ]
            schema_version = self.database.schema_version(conn)
        if len(raw_points) + len(rollup_points) > self.limits.max_query_rows:
            raise QueryLimitError("combined query exceeds the safe row limit")

        raw_groups: dict[tuple[str, str, str], list[MetricPoint]] = defaultdict(list)
        for point in raw_points:
            raw_groups[(point.entity_type, point.entity_id, point.metric_name)].append(point)
        rollup_groups: dict[tuple[str, str, str], list[RollupPoint]] = defaultdict(list)
        for point in rollup_points:
            rollup_groups[(point.entity_type, point.entity_id, point.metric_name)].append(point)
        catalog_keys = {
            (point.entity_type, point.entity_id, point.metric_name): point
            for point in categorical_catalog
        }
        keys = sorted(set(raw_groups) | set(rollup_groups) | set(catalog_keys))
        if len(keys) > self.limits.max_series:
            raise QueryLimitError("query exceeds the safe series limit")
        if require_match and not keys:
            raise QueryNotFoundError("no matching monitoring entity or metric")

        series: list[SeriesSummary] = []
        for key in keys:
            if key in catalog_keys and key not in raw_groups and key not in rollup_groups:
                point = catalog_keys[key]
                series.append(
                    SeriesSummary(
                        entity_type=key[0],
                        entity_id=key[1],
                        metric_name=key[2],
                        unit=point.unit,
                        source_mode=plan.source_mode,
                        quality="unavailable",
                        expected_sample_count=max(
                            1,
                            math.ceil(
                                window.duration_seconds
                                / cadence_for(key[0], key[2], registry)
                            ),
                        ),
                        numeric=False,
                    )
                )
                continue
            raw_summary = None
            rollup_summary = None
            if key in raw_groups and plan.raw_start is not None and plan.raw_end is not None:
                raw_summary = _raw_summary(
                    key,
                    raw_groups[key],
                    plan.raw_start,
                    plan.raw_end,
                    cadence_for(key[0], key[2], registry),
                    now,
                    self.limits.max_gap_multiplier,
                )
            if key in rollup_groups and plan.rollup_start is not None and plan.rollup_end is not None:
                rollup_summary = _rollup_summary(
                    key,
                    rollup_groups[key],
                    plan.rollup_start,
                    plan.rollup_end,
                    cadence_for(key[0], key[2], registry),
                )
            if plan.source_mode == "hybrid":
                item = _combine_hybrid(
                    raw_summary,
                    rollup_summary,
                    key,
                    window.effective_start,
                    window.effective_end,
                    cadence_for(key[0], key[2], registry),
                )
            else:
                item = raw_summary or rollup_summary
                assert item is not None
            series.append(item)
        return QueryResult(
            window=window,
            source_mode=plan.source_mode,
            series=series,
            generated_at=now,
            schema_version=schema_version,
            filters={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "metrics": list(metric_names or ()),
            },
        )

    def events(
        self,
        window: QueryWindow,
        severities: Sequence[str] | None = None,
    ) -> list[EventRecord]:
        with self.database.connection() as conn:
            return self.database.events(
                conn,
                window.effective_start,
                window.effective_end,
                self.limits.max_events,
                severities,
            )

    def snapshot(self, recent_event_seconds: int = 3600) -> dict[str, object]:
        now = int(self.clock())
        with self.database.connection() as conn:
            points = self.database.latest_points(conn, self.limits.max_series)
            cycle = self.database.latest_cycle(conn)
            events = self.database.events(
                conn,
                now - recent_event_seconds,
                now + 1,
                min(50, self.limits.max_events),
            )
            entities = self.database.list_entities(conn)
            schema_version = self.database.schema_version(conn)
        return {
            "generated_at": now,
            "schema_version": schema_version,
            "cycle": cycle,
            "metrics": [dataclasses.asdict(point) for point in points],
            "entities": entities,
            "events": [event.to_dict() for event in events],
        }

    def entities(self, entity_type: str | None = None) -> list[dict[str, object]]:
        with self.database.connection() as conn:
            return self.database.list_entities(conn, entity_type)
