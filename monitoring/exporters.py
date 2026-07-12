"""Bounded streaming JSON and CSV monitoring exports."""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import json
import math
import os
import re
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TextIO

from monitoring.metric_semantics import classify_metric
from monitoring.query_engine import QueryEngine, cadence_for
from monitoring.query_models import QueryInputError, QueryLimitError, QueryWindow
from monitoring.schema import SCHEMA_VERSION

EXPORT_VERSION = 2
CSV_FIELDS = (
    "record_type",
    "export_version",
    "database_schema_version",
    "generated_at_utc",
    "generated_at_epoch",
    "requested_start_utc",
    "requested_end_utc",
    "requested_start_epoch",
    "requested_end_epoch",
    "effective_start_utc",
    "effective_end_utc",
    "effective_start_epoch",
    "effective_end_epoch",
    "source_mode",
    "granularity",
    "truncated",
    "raw_retention_seconds",
    "rollup_retention_seconds",
    "event_retention_seconds",
    "filters_json",
    "row_count",
    "entity_type",
    "entity_id",
    "metric_name",
    "metric_semantics",
    "aggregate_kind",
    "value_available",
    "timestamp",
    "minute_start",
    "numeric_value",
    "text_value",
    "unit",
    "quality",
    "reset",
    "gap",
    "samples",
    "expected_samples",
    "coverage",
    "latest",
    "latest_timestamp",
    "minimum",
    "average",
    "maximum",
    "p95",
    "unavailable_count",
    "reset_count",
    "gap_count",
    "first_timestamp",
    "last_timestamp",
    "transition_count",
    "data_age_seconds",
)
SENSITIVE_KEY_RE = re.compile(
    r"pass|password|token|secret|credential|authorization|username",
    re.IGNORECASE,
)
SENSITIVE_TEXT_RE = re.compile(
    r"(?i)\b(?:pass(?:word)?|token|secret|credential|authorization|username)\s*[:=]\s*[^\s,;]+"
)


@dataclasses.dataclass(frozen=True)
class ExportFilesystem:
    exists: Callable[[Path], bool] = Path.exists
    mkstemp: Callable[..., tuple[int, str]] = tempfile.mkstemp
    chmod: Callable[[str, int], None] = os.chmod
    fdopen: Callable[..., TextIO] = os.fdopen
    close_fd: Callable[[int], None] = os.close
    replace: Callable[[str, str], None] = os.replace
    unlink: Callable[[str], None] = os.unlink


def _safe(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _safe(item)
            for key, item in value.items()
            if not SENSITIVE_KEY_RE.search(str(key))
        }
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return SENSITIVE_TEXT_RE.sub("[redacted]", value)
    return value


def _utc_iso(timestamp: int) -> str:
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _summary_rows(engine: QueryEngine, window: QueryWindow, filters: dict[str, object]):
    result = engine.summary(
        window,
        filters.get("entity_type") if isinstance(filters.get("entity_type"), str) else None,
        filters.get("entity_id") if isinstance(filters.get("entity_id"), str) else None,
        filters.get("metric_names") if isinstance(filters.get("metric_names"), list) else None,
    )
    rows = []
    for item in result.series:
        rows.append({
            "record_type": "summary",
            "entity_type": item.entity_type,
            "entity_id": item.entity_id,
            "metric_name": item.metric_name,
            "metric_semantics": item.metric_semantics,
            "aggregate_kind": "summary",
            "value_available": item.latest is not None or item.average is not None,
            "timestamp": item.latest_timestamp,
            "minute_start": None,
            "numeric_value": item.average if item.numeric else None,
            "text_value": None if item.numeric else item.latest,
            "unit": item.unit,
            "quality": item.quality,
            "reset": item.reset_count,
            "gap": item.gap_count,
            "samples": item.sample_count,
            "expected_samples": item.expected_sample_count,
            "coverage": item.coverage,
            "source_mode": item.source_mode,
            "latest": item.latest,
            "latest_timestamp": item.latest_timestamp,
            "minimum": item.minimum,
            "average": item.average,
            "maximum": item.maximum,
            "p95": item.p95,
            "unavailable_count": item.unavailable_count,
            "reset_count": item.reset_count,
            "gap_count": item.gap_count,
            "first_timestamp": item.first_timestamp,
            "last_timestamp": item.last_timestamp,
            "transition_count": item.transition_count,
            "data_age_seconds": item.data_age_seconds,
        })
    return result, rows


def _export_selection(
    engine: QueryEngine,
    conn,
    window: QueryWindow,
    granularity: str,
    filters: dict[str, object],
) -> tuple[str, str, list[tuple[str, int, int]]]:
    entity_type = filters.get("entity_type") if isinstance(filters.get("entity_type"), str) else None
    entity_id = filters.get("entity_id") if isinstance(filters.get("entity_id"), str) else None
    metric_names = filters.get("metric_names") if isinstance(filters.get("metric_names"), list) else None
    plan = engine.query_plan(
        conn, window, entity_type, entity_id, metric_names
    )
    selected = granularity
    if selected == "auto":
        selected = (
            "raw"
            if plan.source_mode == "raw"
            and not plan.stream_raw
            and window.duration_seconds <= 3600
            else "minute"
        )
    if selected == "raw" and (plan.source_mode != "raw" or plan.stream_raw):
        if plan.stream_raw or "watermark" in plan.reason:
            raise QueryLimitError("raw export exceeds the safe row budget; use auto or minute")
        raise QueryInputError("raw export is unavailable for data outside raw retention")

    if selected == "raw":
        sources = [("raw", window.effective_start, window.effective_end)]
    elif selected in {"rollup", "minute"}:
        sources = []
        if plan.rollup_start is not None and plan.rollup_end is not None:
            sources.append((
                "rollup",
                int(math.ceil(plan.rollup_start / 60.0) * 60),
                int(math.floor(plan.rollup_end / 60.0) * 60),
            ))
        if plan.raw_start is not None and plan.raw_end is not None:
            sources.append(("raw_minute", plan.raw_start, plan.raw_end))
    else:
        raise QueryInputError("granularity must be summary, raw, minute, or auto")
    actual_source = plan.source_mode
    return selected, actual_source, sources


def _point_rows(
    engine: QueryEngine,
    conn,
    sources: list[tuple[str, int, int]],
    filters: dict[str, object],
    expected_rows: int,
    cadence_registry: dict[str, float],
) -> Iterator[dict[str, object]]:
    entity_type = filters.get("entity_type") if isinstance(filters.get("entity_type"), str) else None
    entity_id = filters.get("entity_id") if isinstance(filters.get("entity_id"), str) else None
    metric_names = filters.get("metric_names") if isinstance(filters.get("metric_names"), list) else None
    emitted = 0
    for source, start, end in sources:
        for row in engine.database.iter_export_rows(
            conn,
            source,
            start,
            end,
            expected_rows - emitted,
            entity_type,
            entity_id,
            metric_names,
        ):
            emitted += 1
            semantics = classify_metric(str(row["metric_name"]), str(row["unit"]))
            row["metric_semantics"] = semantics.category
            row["record_type"] = source
            latest = (
                row.get("latest_numeric")
                if row.get("latest_numeric") is not None
                else row.get("latest_text")
            )
            if source == "raw_minute":
                minute = int(row["minute_start"])
                seconds = max(0, min(end, minute + 60) - max(start, minute))
                cadence = cadence_for(
                    str(row["entity_type"]),
                    str(row["metric_name"]),
                    cadence_registry,
                )
                expected = max(1, math.ceil(seconds / cadence))
                samples = int(row["samples"] or 0)
                row["expected_samples"] = expected
                row["coverage"] = min(1.0, samples / expected)
                row["source_mode"] = "raw"
            if source == "raw":
                row["aggregate_kind"] = "point"
                row["value_available"] = (
                    row.get("numeric_value") is not None
                    or row.get("text_value") is not None
                )
                row["latest"] = latest
                row["latest_timestamp"] = row.get("timestamp")
            elif semantics.supports_average:
                row["aggregate_kind"] = "minute_statistics"
                row["numeric_value"] = row.get("average")
                row["text_value"] = None
                row["latest"] = latest if source == "raw_minute" else None
                row["latest_timestamp"] = (
                    row.get("latest_timestamp") if source == "raw_minute" else None
                )
                row["value_available"] = row.get("average") is not None
                if not row["value_available"]:
                    row["aggregate_kind"] = "historical_value_unavailable"
            elif source == "raw_minute" and latest is not None:
                row["aggregate_kind"] = "minute_latest"
                row["value_available"] = True
                row["numeric_value"] = row.get("latest_numeric")
                row["text_value"] = row.get("latest_text")
                row["latest"] = latest
                row["minimum"] = None
                row["average"] = None
                row["maximum"] = None
            else:
                row["aggregate_kind"] = "historical_value_unavailable"
                row["value_available"] = False
                row["numeric_value"] = None
                row["text_value"] = None
                row["latest"] = None
                row["latest_timestamp"] = None
                row["minimum"] = None
                row["average"] = None
                row["maximum"] = None
            row.pop("latest_numeric", None)
            row.pop("latest_text", None)
            yield row


def _count_point_rows(
    engine: QueryEngine,
    conn,
    sources: list[tuple[str, int, int]],
    filters: dict[str, object],
) -> int:
    entity_type = filters.get("entity_type") if isinstance(filters.get("entity_type"), str) else None
    entity_id = filters.get("entity_id") if isinstance(filters.get("entity_id"), str) else None
    metric_names = filters.get("metric_names") if isinstance(filters.get("metric_names"), list) else None
    count = 0
    for source, start, end in sources:
        if source == "raw_minute":
            scanned = engine.database.bounded_point_count(
                conn,
                "raw",
                start,
                end,
                engine.limits.max_stream_scan_rows,
                entity_type,
                entity_id,
                metric_names,
            )
            if scanned > engine.limits.max_stream_scan_rows:
                raise QueryLimitError(
                    "stream_scan_limit: minute export exceeds the maximum scan budget "
                    f"of {engine.limits.max_stream_scan_rows} rows"
                )
        remaining = engine.limits.max_export_rows - count
        value = engine.database.count_export_rows(
            conn, source, start, end, entity_type, entity_id, metric_names, remaining
        )
        count += value
        if count > engine.limits.max_export_rows:
            break
    if count > engine.limits.max_export_rows:
        raise QueryLimitError(
            f"export estimate {count} rows exceeds the safe limit {engine.limits.max_export_rows}"
        )
    return count


def export_data(
    engine: QueryEngine,
    window: QueryWindow,
    output: str,
    output_format: str,
    granularity: str = "auto",
    filters: dict[str, object] | None = None,
    stdout: TextIO | None = None,
    filesystem: ExportFilesystem = ExportFilesystem(),
    replace: Callable[[str, str], None] | None = None,
) -> dict[str, object]:
    active_filters = filters or {}
    if output_format not in {"json", "csv"}:
        raise QueryInputError("format must be json or csv")
    generated_at = int(engine.clock())
    point_context = None
    point_conn = None
    if granularity == "summary":
        selected = "summary"
        result, summary_rows = _summary_rows(engine, window, active_filters)
        if len(summary_rows) > engine.limits.max_export_rows:
            raise QueryLimitError("summary export exceeds the safe row limit")
        row_count = len(summary_rows)
        rows: Iterator[dict[str, object]] = iter(summary_rows)
        source_mode = result.source_mode
    else:
        point_context = engine.database.connection()
        point_conn = point_context.__enter__()
        try:
            selected, source_mode, sources = _export_selection(
                engine, point_conn, window, granularity, active_filters
            )
            row_count = _count_point_rows(
                engine, point_conn, sources, active_filters
            )
            cadence_registry = engine.database.cadence_registry(point_conn)
            rows = _point_rows(
                engine,
                point_conn,
                sources,
                active_filters,
                row_count,
                cadence_registry,
            )
        except Exception as exc:
            point_context.__exit__(type(exc), exc, exc.__traceback__)
            point_context = None
            point_conn = None
            raise
    metadata = {
        "export_version": EXPORT_VERSION,
        "database_schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "generated_at_utc": _utc_iso(generated_at),
        "requested_window": {
            "start": window.requested_start,
            "end": window.requested_end,
        },
        "effective_window": {
            "start": window.effective_start,
            "end": window.effective_end,
        },
        "source_mode": source_mode,
        "granularity": selected,
        "filters": active_filters,
        "retention": {
            "raw_seconds": engine.retention.raw_seconds,
            "rollup_seconds": engine.retention.rollup_seconds,
            "event_seconds": engine.retention.event_seconds,
        },
        "row_count": row_count,
        "truncated": window.truncated,
    }

    target_stream: TextIO
    temporary_path: str | None = None
    handle: TextIO | None = None
    file_descriptor: int | None = None
    try:
        if output == "-":
            if stdout is None:
                raise QueryInputError("stdout stream is unavailable")
            target_stream = stdout
        else:
            destination = Path(output)
            if not filesystem.exists(destination.parent):
                raise QueryInputError(f"output directory does not exist: {destination.parent}")
            file_descriptor, temporary_path = filesystem.mkstemp(
                prefix=f".{destination.name}.",
                dir=str(destination.parent),
                text=True,
            )
            filesystem.chmod(temporary_path, 0o600)
            handle = filesystem.fdopen(
                file_descriptor, "w", encoding="utf-8", newline=""
            )
            file_descriptor = None
            target_stream = handle
        count = 0
        if output_format == "json":
            target_stream.write('{"metadata":')
            target_stream.write(json.dumps(_safe(metadata), sort_keys=True, separators=(",", ":")))
            target_stream.write(',"rows":[')
            first = True
            for row in rows:
                count += 1
                if count > engine.limits.max_export_rows:
                    raise QueryLimitError("export exceeded the actual row limit")
                if not first:
                    target_stream.write(",")
                target_stream.write(json.dumps(_safe(row), sort_keys=True, separators=(",", ":")))
                first = False
            target_stream.write("]}")
            target_stream.write("\n")
        else:
            writer = csv.DictWriter(target_stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            base = {
                "export_version": metadata["export_version"],
                "database_schema_version": metadata["database_schema_version"],
                "generated_at_utc": metadata["generated_at_utc"],
                "generated_at_epoch": generated_at,
                "requested_start_utc": _utc_iso(window.requested_start),
                "requested_end_utc": _utc_iso(window.requested_end),
                "requested_start_epoch": window.requested_start,
                "requested_end_epoch": window.requested_end,
                "effective_start_utc": _utc_iso(window.effective_start),
                "effective_end_utc": _utc_iso(window.effective_end),
                "effective_start_epoch": window.effective_start,
                "effective_end_epoch": window.effective_end,
                "source_mode": source_mode,
                "granularity": selected,
                "truncated": str(window.truncated).lower(),
                "raw_retention_seconds": engine.retention.raw_seconds,
                "rollup_retention_seconds": engine.retention.rollup_seconds,
                "event_retention_seconds": engine.retention.event_seconds,
                "filters_json": json.dumps(_safe(active_filters), sort_keys=True, separators=(",", ":")),
                "row_count": row_count,
            }
            writer.writerow({**base, "record_type": "metadata"})
            for row in rows:
                count += 1
                if count > engine.limits.max_export_rows:
                    raise QueryLimitError("export exceeded the actual row limit")
                writer.writerow(_safe({**base, **row}))
        if count != row_count:
            raise QueryLimitError(
                f"export changed while reading: expected {row_count} rows, received {count}"
            )
        target_stream.flush()
        if handle is not None:
            handle.close()
            handle = None
            assert temporary_path is not None
            (replace or filesystem.replace)(temporary_path, output)
            temporary_path = None
        return metadata
    except Exception:
        if file_descriptor is not None:
            try:
                filesystem.close_fd(file_descriptor)
            except OSError:
                pass
        if handle is not None:
            try:
                handle.close()
            except (OSError, ValueError):
                pass
        if temporary_path is not None:
            try:
                filesystem.unlink(temporary_path)
            except OSError:
                pass
        raise
    finally:
        if point_context is not None and point_conn is not None:
            point_context.__exit__(None, None, None)
