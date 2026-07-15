"""Dedicated bounded SQLite state and transition history."""

from __future__ import annotations

import sqlite3
import stat
import time
from collections.abc import Iterable
from pathlib import Path

from gost_watchdog.models import (
    EVENT_RETENTION_SECONDS,
    HEALTH_STATES,
    ProfileState,
    WatchdogEvent,
)


SCHEMA_VERSION = 1
DEFAULT_DB_PATH = "/var/lib/gost-manager/watchdog/watchdog.sqlite3"
EVENT_CODES = {
    "watchdog_degraded",
    "watchdog_upstream_down",
    "watchdog_profile_stopped",
    "watchdog_recovering",
    "watchdog_upstream_healthy",
    "watchdog_profile_started",
    "watchdog_stop_failed",
    "watchdog_start_failed",
    "watchdog_manual_override",
    "watchdog_maintenance_enabled",
    "watchdog_maintenance_disabled",
    "watchdog_config_error",
    "watchdog_daemon_started",
    "watchdog_daemon_stopped",
}
SAFE_ACTION_RESULTS = {
    None,
    "stopped",
    "started",
    "already_inactive",
    "already_active",
    "manual_start",
    "manual_stop",
    "maintenance_stop",
    "maintenance_exit_no_start",
    "maintenance_exit_start",
    "rearmed",
    "failed",
}
SAFE_ERROR_CATEGORIES = {
    None,
    "invalid_global_config",
    "invalid_profile",
    "unsafe_directory",
    "service_state_unavailable",
    "stop_failed",
    "start_failed",
    "runtime_error",
}


def _reject_unsafe_database_path(destination: Path) -> None:
    if not destination.is_absolute():
        raise RuntimeError("Watchdog database path must be absolute")
    if destination.is_symlink() or destination.parent.is_symlink():
        raise RuntimeError("Watchdog database path may not use a symlink")
    if destination.exists() and not stat.S_ISREG(destination.lstat().st_mode):
        raise RuntimeError("Watchdog database must be a regular file")


def connect_database(path: str) -> sqlite3.Connection:
    destination = Path(path)
    _reject_unsafe_database_path(destination)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_unsafe_database_path(destination)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate_database(path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = connect_database(path)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations("
            "version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
        )
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        version = int(row[0] or 0)
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"unsupported Watchdog schema version {version}")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS profile_state("
            "profile_id TEXT PRIMARY KEY,service_name TEXT NOT NULL,kharej_ip TEXT NOT NULL,"
            "health_state TEXT NOT NULL CHECK(health_state IN('unknown','healthy','degraded','down','recovering')),"
            "maintenance INTEGER NOT NULL DEFAULT 0,stopped_by_watchdog INTEGER NOT NULL DEFAULT 0,"
            "stopped_by_maintenance INTEGER NOT NULL DEFAULT 0,manual_override INTEGER NOT NULL DEFAULT 0,"
            "failure_count INTEGER NOT NULL DEFAULT 0,success_count INTEGER NOT NULL DEFAULT 0,"
            "last_check_at INTEGER,last_transition_at INTEGER,outage_started_at INTEGER,"
            "recovery_started_at INTEGER,recovery_ready_at INTEGER,recovery_jitter_seconds INTEGER NOT NULL DEFAULT 0,"
            "last_service_active INTEGER,updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events("
            "event_id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER NOT NULL,code TEXT NOT NULL,"
            "profile_id TEXT,service_name TEXT,kharej_ip TEXT,previous_state TEXT,new_state TEXT,"
            "failure_count INTEGER NOT NULL DEFAULT 0,success_count INTEGER NOT NULL DEFAULT 0,"
            "action_result TEXT,outage_duration INTEGER,error_category TEXT)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_watchdog_events_ts ON events(ts)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_watchdog_events_profile_ts "
            "ON events(profile_id,ts DESC)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)",
            (SCHEMA_VERSION, int(time.time())),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    return conn


def _state_from_row(row: sqlite3.Row) -> ProfileState:
    active = row["last_service_active"]
    return ProfileState(
        profile_id=str(row["profile_id"]),
        service_name=str(row["service_name"]),
        kharej_ip=str(row["kharej_ip"]),
        health_state=str(row["health_state"]),
        maintenance=bool(row["maintenance"]),
        stopped_by_watchdog=bool(row["stopped_by_watchdog"]),
        stopped_by_maintenance=bool(row["stopped_by_maintenance"]),
        manual_override=bool(row["manual_override"]),
        failure_count=int(row["failure_count"]),
        success_count=int(row["success_count"]),
        last_check_at=row["last_check_at"],
        last_transition_at=row["last_transition_at"],
        outage_started_at=row["outage_started_at"],
        recovery_started_at=row["recovery_started_at"],
        recovery_ready_at=row["recovery_ready_at"],
        recovery_jitter_seconds=int(row["recovery_jitter_seconds"]),
        last_service_active=None if active is None else bool(active),
    )


class WatchdogStore:
    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        self.path = path
        self.conn = migrate_database(path)

    def close(self) -> None:
        self.conn.close()

    def get_state(self, profile_id: str, service_name: str, kharej_ip: str) -> ProfileState:
        row = self.conn.execute(
            "SELECT * FROM profile_state WHERE profile_id=?", (profile_id,)
        ).fetchone()
        if row is None:
            return ProfileState(profile_id, service_name, kharej_ip)
        state = _state_from_row(row)
        state.service_name = service_name
        state.kharej_ip = kharej_ip
        return state

    def all_states(self) -> dict[str, ProfileState]:
        return {
            str(row["profile_id"]): _state_from_row(row)
            for row in self.conn.execute("SELECT * FROM profile_state ORDER BY profile_id")
        }

    def _save_state(self, state: ProfileState, now: int) -> None:
        if state.health_state not in HEALTH_STATES:
            raise ValueError("invalid Watchdog health state")
        self.conn.execute(
            "INSERT INTO profile_state("
            "profile_id,service_name,kharej_ip,health_state,maintenance,stopped_by_watchdog,"
            "stopped_by_maintenance,manual_override,failure_count,success_count,last_check_at,"
            "last_transition_at,outage_started_at,recovery_started_at,recovery_ready_at,"
            "recovery_jitter_seconds,last_service_active,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(profile_id) DO UPDATE SET service_name=excluded.service_name,"
            "kharej_ip=excluded.kharej_ip,health_state=excluded.health_state,"
            "maintenance=excluded.maintenance,stopped_by_watchdog=excluded.stopped_by_watchdog,"
            "stopped_by_maintenance=excluded.stopped_by_maintenance,manual_override=excluded.manual_override,"
            "failure_count=excluded.failure_count,success_count=excluded.success_count,"
            "last_check_at=excluded.last_check_at,last_transition_at=excluded.last_transition_at,"
            "outage_started_at=excluded.outage_started_at,recovery_started_at=excluded.recovery_started_at,"
            "recovery_ready_at=excluded.recovery_ready_at,recovery_jitter_seconds=excluded.recovery_jitter_seconds,"
            "last_service_active=excluded.last_service_active,updated_at=excluded.updated_at",
            (
                state.profile_id,
                state.service_name,
                state.kharej_ip,
                state.health_state,
                int(state.maintenance),
                int(state.stopped_by_watchdog),
                int(state.stopped_by_maintenance),
                int(state.manual_override),
                state.failure_count,
                state.success_count,
                state.last_check_at,
                state.last_transition_at,
                state.outage_started_at,
                state.recovery_started_at,
                state.recovery_ready_at,
                state.recovery_jitter_seconds,
                None if state.last_service_active is None else int(state.last_service_active),
                now,
            ),
        )

    def _insert_event(self, event: WatchdogEvent) -> None:
        if event.code not in EVENT_CODES:
            raise ValueError("unsupported Watchdog event code")
        if event.action_result not in SAFE_ACTION_RESULTS:
            raise ValueError("unsafe Watchdog action result")
        if event.error_category not in SAFE_ERROR_CATEGORIES:
            raise ValueError("unsafe Watchdog error category")
        self.conn.execute(
            "INSERT INTO events(ts,code,profile_id,service_name,kharej_ip,previous_state,new_state,"
            "failure_count,success_count,action_result,outage_duration,error_category) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.ts,
                event.code,
                event.profile_id,
                event.service_name,
                event.kharej_ip,
                event.previous_state,
                event.new_state,
                event.failure_count,
                event.success_count,
                event.action_result,
                event.outage_duration,
                event.error_category,
            ),
        )

    def persist(
        self,
        state: ProfileState,
        events: Iterable[WatchdogEvent],
        now: int,
    ) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._save_state(state, now)
            for event in events:
                self._insert_event(event)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def record_event(self, event: WatchdogEvent) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._insert_event(event)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def prune_events(
        self,
        now: int,
        *,
        retention_seconds: int = EVENT_RETENTION_SECONDS,
        batch_size: int = 500,
        max_batches: int = 100,
    ) -> int:
        cutoff = int(now) - int(retention_seconds)
        deleted = 0
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for _ in range(max_batches):
                cursor = self.conn.execute(
                    "DELETE FROM events WHERE event_id IN ("
                    "SELECT event_id FROM events WHERE ts < ? ORDER BY ts,event_id LIMIT ?)",
                    (cutoff, batch_size),
                )
                count = max(0, cursor.rowcount)
                deleted += count
                if count < batch_size:
                    break
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return deleted

    def events(
        self,
        now: int,
        *,
        profile_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        limit = max(1, min(int(limit), 1000))
        cutoff = int(now) - EVENT_RETENTION_SECONDS
        parameters: list[object] = [cutoff]
        where = "ts>=?"
        if profile_id is not None:
            where += " AND profile_id=?"
            parameters.append(profile_id)
        parameters.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM events WHERE {where} ORDER BY ts DESC,event_id DESC LIMIT ?",
            parameters,
        )
        return [dict(row) for row in rows]

    def summary(self, now: int, profile_id: str) -> dict[str, int | None]:
        cutoff = int(now) - EVENT_RETENTION_SECONDS
        rows = self.conn.execute(
            "SELECT code,ts,outage_duration FROM events WHERE profile_id=? AND ts>=? "
            "ORDER BY ts",
            (profile_id, cutoff),
        ).fetchall()
        completed = [
            int(row["outage_duration"])
            for row in rows
            if row["code"] == "watchdog_upstream_healthy"
            and row["outage_duration"] is not None
        ]
        state_row = self.conn.execute(
            "SELECT outage_started_at FROM profile_state WHERE profile_id=?", (profile_id,)
        ).fetchone()
        ongoing = 0
        if state_row is not None and state_row[0] is not None:
            ongoing = max(0, int(now) - int(state_row[0]))
        durations = completed + ([ongoing] if ongoing else [])
        down_events = [row for row in rows if row["code"] == "watchdog_upstream_down"]
        return {
            "outage_count": len(down_events),
            "total_downtime_seconds": sum(durations),
            "longest_outage_seconds": max(durations, default=0),
            "last_outage_at": int(down_events[-1]["ts"]) if down_events else None,
            "automatic_stop_count": sum(row["code"] == "watchdog_profile_stopped" for row in rows),
            "automatic_start_count": sum(row["code"] == "watchdog_profile_started" for row in rows),
            "failed_action_count": sum(
                row["code"] in {"watchdog_stop_failed", "watchdog_start_failed"}
                for row in rows
            ),
        }
