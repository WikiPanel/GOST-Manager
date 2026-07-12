"""Validated query-window parsing and raw/rollup planning."""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import re

from monitoring.query_models import QueryInputError, QueryPlan, QueryWindow
from monitoring.schema import (
    EVENT_RETENTION_SECONDS,
    RAW_RETENTION_SECONDS,
    ROLLUP_RETENTION_SECONDS,
)

MAX_WINDOW_SECONDS = ROLLUP_RETENTION_SECONDS
MAX_DURATION_NUMBER = 10_000_000
DURATION_RE = re.compile(r"^(?P<number>[1-9][0-9]{0,7})(?P<unit>[smhd])$")
DURATION_FACTORS = {"s": 1, "m": 60, "h": 3600, "d": 24 * 3600}


@dataclasses.dataclass(frozen=True)
class RetentionPolicy:
    raw_seconds: int = RAW_RETENTION_SECONDS
    rollup_seconds: int = ROLLUP_RETENTION_SECONDS
    event_seconds: int = EVENT_RETENTION_SECONDS


def parse_duration(value: str, maximum: int = MAX_WINDOW_SECONDS) -> int:
    match = DURATION_RE.fullmatch(value.strip())
    if not match:
        raise QueryInputError("duration must use an explicit form such as 90s, 10m, 1h, or 24h")
    number = int(match.group("number"))
    if number <= 0 or number > MAX_DURATION_NUMBER:
        raise QueryInputError("duration is outside the safe range")
    seconds = number * DURATION_FACTORS[match.group("unit")]
    if seconds <= 0 or seconds > maximum:
        raise QueryInputError(f"window exceeds the maximum of {maximum} seconds")
    return seconds


def parse_timestamp(value: str) -> int:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise QueryInputError("timestamp must be ISO-8601 with Z or an explicit UTC offset") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QueryInputError("timestamp must include Z or an explicit UTC offset")
    try:
        return int(parsed.timestamp())
    except (OverflowError, OSError, ValueError) as exc:
        raise QueryInputError("timestamp is outside the supported range") from exc


def resolve_window(
    now: int,
    duration: str | None = None,
    start: str | None = None,
    end: str | None = None,
    retention: RetentionPolicy = RetentionPolicy(),
    maximum: int = MAX_WINDOW_SECONDS,
) -> QueryWindow:
    if start is not None or end is not None:
        if start is None or end is None:
            raise QueryInputError("--start and --end must be provided together")
        if duration is not None:
            raise QueryInputError("use either --window or --start/--end, not both")
        requested_start = parse_timestamp(start)
        requested_end = parse_timestamp(end)
    else:
        seconds = parse_duration(duration or "10m", maximum)
        requested_end = now
        requested_start = now - seconds
    if requested_start >= requested_end:
        raise QueryInputError("window start must be earlier than window end")
    if requested_start >= now:
        raise QueryInputError("future-only windows are not allowed")
    if requested_end - requested_start > maximum:
        raise QueryInputError("window exceeds the 24-hour safety limit")
    oldest = now - max(retention.rollup_seconds, retention.event_seconds)
    if requested_end <= oldest:
        raise QueryInputError("requested window is outside retained monitoring history")
    effective_start = max(requested_start, oldest)
    effective_end = min(requested_end, now)
    if effective_start >= effective_end:
        raise QueryInputError("requested window has no retained interval")
    return QueryWindow(
        requested_start,
        requested_end,
        effective_start,
        effective_end,
        effective_start != requested_start or effective_end != requested_end,
    )


def plan_window(
    window: QueryWindow,
    now: int,
    retention: RetentionPolicy = RetentionPolicy(),
) -> QueryPlan:
    raw_cutoff = now - retention.raw_seconds
    start = window.effective_start
    end = window.effective_end
    if start >= raw_cutoff:
        return QueryPlan("raw", raw_start=start, raw_end=end)
    if end <= raw_cutoff:
        return QueryPlan("rollup", rollup_start=start, rollup_end=end)
    rollup_end = int(math.floor(raw_cutoff / 60.0) * 60)
    rollup_end = min(max(rollup_end, start), end)
    raw_start = max(start, raw_cutoff)
    return QueryPlan(
        "hybrid",
        raw_start=raw_start,
        raw_end=end,
        rollup_start=start,
        rollup_end=rollup_end,
    )
