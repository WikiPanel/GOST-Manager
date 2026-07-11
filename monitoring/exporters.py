"""Bounded streaming JSON and CSV monitoring exports."""

from __future__ import annotations

import csv
import dataclasses
import json
import math
import os
import re
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TextIO

from monitoring.query_engine import QueryEngine
from monitoring.query_models import QueryInputError, QueryLimitError, QueryWindow
from monitoring.query_window import plan_window
from monitoring.schema import SCHEMA_VERSION

EXPORT_VERSION = 1
CSV_FIELDS = (
    "entity_type",
    "entity_id",
    "metric_name",
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
    "source_mode",
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


def _summary_rows(engine: QueryEngine, window: QueryWindow, filters: dict[str, object]) -> Iterator[dict[str, object]]:
    result = engine.summary(
        window,
        filters.get("entity_type") if isinstance(filters.get("entity_type"), str) else None,
        filters.get("entity_id") if isinstance(filters.get("entity_id"), str) else None,
        filters.get("metric_names") if isinstance(filters.get("metric_names"), list) else None,
    )
    for item in result.series:
        yield {
            "entity_type": item.entity_type,
            "entity_id": item.entity_id,
            "metric_name": item.metric_name,
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
            "minimum": item.minimum,
            "maximum": item.maximum,
            "p95": item.p95,
        }


def _export_selection(
    engine: QueryEngine,
    window: QueryWindow,
    granularity: str,
    filters: dict[str, object],
) -> tuple[str, list[tuple[str, int, int]]]:
    now = int(engine.clock())
    plan = plan_window(window, now, engine.retention)
    entity_type = filters.get("entity_type") if isinstance(filters.get("entity_type"), str) else None
    entity_id = filters.get("entity_id") if isinstance(filters.get("entity_id"), str) else None
    metric_names = filters.get("metric_names") if isinstance(filters.get("metric_names"), list) else None
    selected = granularity
    if selected == "auto":
        selected = "raw" if plan.source_mode == "raw" and window.duration_seconds <= 3600 else plan.source_mode
    if selected == "raw" and plan.source_mode != "raw":
        raise QueryInputError("raw export is unavailable for data outside raw retention")

    if selected in {"rollup", "minute"}:
        sources = [
            (
                "rollup",
                int(math.ceil(window.effective_start / 60.0) * 60),
                int(math.floor(window.effective_end / 60.0) * 60),
            )
        ]
    elif selected == "raw":
        sources = [("raw", window.effective_start, window.effective_end)]
    elif selected == "hybrid":
        assert plan.rollup_start is not None and plan.rollup_end is not None
        assert plan.raw_start is not None and plan.raw_end is not None
        sources = [
            (
                "rollup",
                int(math.ceil(plan.rollup_start / 60.0) * 60),
                int(math.floor(plan.rollup_end / 60.0) * 60),
            ),
            ("raw", plan.raw_start, plan.raw_end),
        ]
    else:
        raise QueryInputError("granularity must be summary, raw, minute, or auto")
    return selected, sources


def _point_rows(
    engine: QueryEngine,
    sources: list[tuple[str, int, int]],
    filters: dict[str, object],
    expected_rows: int,
) -> Iterator[dict[str, object]]:
    entity_type = filters.get("entity_type") if isinstance(filters.get("entity_type"), str) else None
    entity_id = filters.get("entity_id") if isinstance(filters.get("entity_id"), str) else None
    metric_names = filters.get("metric_names") if isinstance(filters.get("metric_names"), list) else None
    with engine.database.connection() as conn:
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
                yield row


def _count_point_rows(
    engine: QueryEngine,
    sources: list[tuple[str, int, int]],
    filters: dict[str, object],
) -> int:
    entity_type = filters.get("entity_type") if isinstance(filters.get("entity_type"), str) else None
    entity_id = filters.get("entity_id") if isinstance(filters.get("entity_id"), str) else None
    metric_names = filters.get("metric_names") if isinstance(filters.get("metric_names"), list) else None
    with engine.database.connection() as conn:
        count = sum(
            engine.database.count_export_rows(
                conn, source, start, end, entity_type, entity_id, metric_names
            )
            for source, start, end in sources
        )
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
    if granularity == "summary":
        selected = "summary"
        summary_rows = list(_summary_rows(engine, window, active_filters))
        if len(summary_rows) > engine.limits.max_export_rows:
            raise QueryLimitError("summary export exceeds the safe row limit")
        row_count = len(summary_rows)
        rows: Iterator[dict[str, object]] = iter(summary_rows)
    else:
        selected, sources = _export_selection(engine, window, granularity, active_filters)
        row_count = _count_point_rows(engine, sources, active_filters)
        rows = _point_rows(engine, sources, active_filters, row_count)
    metadata = {
        "export_version": EXPORT_VERSION,
        "database_schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "requested_window": {
            "start": window.requested_start,
            "end": window.requested_end,
        },
        "effective_window": {
            "start": window.effective_start,
            "end": window.effective_end,
        },
        "source_mode": plan_window(window, generated_at, engine.retention).source_mode,
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
            for row in rows:
                count += 1
                if count > engine.limits.max_export_rows:
                    raise QueryLimitError("export exceeded the actual row limit")
                writer.writerow(_safe(row))
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
