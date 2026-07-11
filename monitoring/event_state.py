"""Persistent transition state and deduplicated structured events."""

from __future__ import annotations

import re
import sqlite3

from monitoring.models import Event
from monitoring.schema import get_json_state, set_json_state

SENSITIVE_KEY_RE = re.compile(r"pass|password|token|secret|credential|auth|user", re.IGNORECASE)


def safe_details(details: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in details.items()
        if not SENSITIVE_KEY_RE.search(key)
    }


class EventState:
    """Build events only when persisted state changes."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def availability(self, source: str, available: bool, ts: int) -> list[Event]:
        key = f"event.source.{source}"
        previous = get_json_state(self.conn, key)
        set_json_state(self.conn, key, available)
        if previous is None and available:
            return []
        if previous is not None and bool(previous) == available:
            return []
        code = "metric_source_available" if available else "metric_source_unavailable"
        severity = "info" if available else "warning"
        message = f"Metric source {source} is {'available' if available else 'unavailable'}"
        return [Event(ts, severity, code, message, {"source": source})]

    def value_transition(
        self,
        key: str,
        value: object,
        ts: int,
        code: str,
        message: str,
        details: dict[str, object],
        severity: str = "info",
        emit_initial: bool = False,
    ) -> list[Event]:
        state_key = f"event.value.{key}"
        previous = get_json_state(self.conn, state_key)
        set_json_state(self.conn, state_key, value)
        if previous == value or (previous is None and not emit_initial):
            return []
        payload = safe_details({**details, "previous": previous, "current": value})
        return [Event(ts, severity, code, message, payload)]

    def edge(
        self,
        key: str,
        active: bool,
        ts: int,
        code: str,
        message: str,
        details: dict[str, object],
        severity: str = "warning",
    ) -> list[Event]:
        state_key = f"event.edge.{key}"
        previous = get_json_state(self.conn, state_key)
        set_json_state(self.conn, state_key, active)
        if not active or previous is True:
            return []
        return [Event(ts, severity, code, message, safe_details(details))]

    def set_transitions(
        self,
        key: str,
        values: set[str],
        ts: int,
        added_code: str,
        removed_code: str,
        noun: str,
    ) -> list[Event]:
        state_key = f"event.set.{key}"
        previous_raw = get_json_state(self.conn, state_key)
        set_json_state(self.conn, state_key, sorted(values))
        if previous_raw is None:
            return []
        previous = {str(value) for value in previous_raw} if isinstance(previous_raw, list) else set()
        events = [
            Event(ts, "info", added_code, f"{noun} added", {noun: value})
            for value in sorted(values - previous)
        ]
        events.extend(
            Event(ts, "warning", removed_code, f"{noun} removed", {noun: value})
            for value in sorted(previous - values)
        )
        return events
