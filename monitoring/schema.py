"""SQLite schema, migrations, persistence, and bounded maintenance."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from monitoring.models import Event, Metric, MetricSample, QUALITY_RANK, Tunnel

SCHEMA_VERSION = 4
DEFAULT_DB_PATH = "/var/lib/gost-manager/metrics.sqlite3"
DEFAULT_SAMPLE_INTERVAL_SECONDS = 5.0
RAW_RETENTION_SECONDS = 48 * 3600
ROLLUP_RETENTION_SECONDS = 30 * 24 * 3600
ROLLUP_BATCH_MINUTES = 240
SENSITIVE_KEY_RE = re.compile(
    r"(?:^|_)(?:pass|password|token|secret|credential|auth|username|gost_user)(?:$|_)",
    re.IGNORECASE,
)
SENSITIVE_TEXT_RE = re.compile(
    r"(?i)\b(?:pass(?:word)?|token|secret|credential|auth|user)\s*[:=]\s*[^\s,;]+"
)
URI_USERINFO_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@")
ALLOWED_TEXT_METRICS = {
    "link_state",
    "remote_endpoint",
    "service_active_state",
    "service_sub_state",
}

# Kept for v1 migration regression fixtures.
CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tunnels(tunnel_id TEXT PRIMARY KEY, side TEXT NOT NULL, tunnel_number INTEGER NOT NULL, service_name TEXT NOT NULL UNIQUE, env_path TEXT NOT NULL, listen_ports_json TEXT NOT NULL DEFAULT '[]', target_ports_json TEXT NOT NULL DEFAULT '[]', updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS metric_samples(sample_id INTEGER PRIMARY KEY AUTOINCREMENT, tunnel_id TEXT NOT NULL REFERENCES tunnels(tunnel_id) ON DELETE CASCADE, collected_at INTEGER NOT NULL, service_state INTEGER NOT NULL, service_substate INTEGER NOT NULL, restart_count INTEGER NOT NULL DEFAULT 0, listen_ports_total INTEGER NOT NULL DEFAULT 0, listen_ports_up INTEGER NOT NULL DEFAULT 0, configured_mappings_total INTEGER NOT NULL DEFAULT 0, rx_bytes INTEGER NOT NULL DEFAULT 0, tx_bytes INTEGER NOT NULL DEFAULT 0, UNIQUE(tunnel_id,collected_at));
CREATE TABLE IF NOT EXISTS metric_rollups(tunnel_id TEXT NOT NULL, bucket_start INTEGER NOT NULL, bucket_size INTEGER NOT NULL, samples INTEGER NOT NULL, service_state_avg REAL NOT NULL, service_substate_avg REAL NOT NULL, restart_count_max INTEGER NOT NULL, listen_ports_total_max INTEGER NOT NULL, listen_ports_up_avg REAL NOT NULL, configured_mappings_total_max INTEGER NOT NULL, rx_bytes_max INTEGER NOT NULL, tx_bytes_max INTEGER NOT NULL, PRIMARY KEY(tunnel_id,bucket_start,bucket_size));
INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,1);
"""


def _v4_statements() -> list[str]:
    return [
        "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)",
        "CREATE TABLE IF NOT EXISTS sample_cycles(cycle_id INTEGER PRIMARY KEY AUTOINCREMENT, collected_at INTEGER NOT NULL UNIQUE, monotonic_started REAL NOT NULL, monotonic_finished REAL NOT NULL, duration_seconds REAL NOT NULL, success INTEGER NOT NULL, overrun INTEGER NOT NULL DEFAULT 0, missed_deadlines INTEGER NOT NULL DEFAULT 0, overrun_seconds REAL NOT NULL DEFAULT 0.0)",
        "CREATE TABLE IF NOT EXISTS entities(entity_pk INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, display_name TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', updated_at INTEGER NOT NULL, UNIQUE(entity_type,entity_id))",
        "CREATE TABLE IF NOT EXISTS tunnels(tunnel_id TEXT PRIMARY KEY, entity_pk INTEGER REFERENCES entities(entity_pk) ON DELETE SET NULL, side TEXT NOT NULL CHECK(side IN('iran','kharej')), tunnel_number INTEGER NOT NULL, service_name TEXT NOT NULL UNIQUE, env_path TEXT NOT NULL, listen_ports_json TEXT NOT NULL DEFAULT '[]', target_ports_json TEXT NOT NULL DEFAULT '[]', updated_at INTEGER NOT NULL, UNIQUE(side,tunnel_number))",
        "CREATE TABLE IF NOT EXISTS metric_samples(sample_id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL REFERENCES sample_cycles(cycle_id) ON DELETE CASCADE, tunnel_id TEXT REFERENCES tunnels(tunnel_id) ON DELETE CASCADE, sample_identity TEXT NOT NULL DEFAULT '', collected_at INTEGER NOT NULL, service_state INTEGER NOT NULL DEFAULT 0, service_substate INTEGER NOT NULL DEFAULT 0, restart_count INTEGER NOT NULL DEFAULT 0, listen_ports_total INTEGER NOT NULL DEFAULT 0, listen_ports_up INTEGER NOT NULL DEFAULT 0, configured_mappings_total INTEGER NOT NULL DEFAULT 0, rx_bytes INTEGER, tx_bytes INTEGER, UNIQUE(cycle_id,tunnel_id))",
        "CREATE TABLE IF NOT EXISTS metric_points(point_id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL REFERENCES sample_cycles(cycle_id) ON DELETE CASCADE, entity_pk INTEGER NOT NULL REFERENCES entities(entity_pk) ON DELETE CASCADE, metric_name TEXT NOT NULL, ts INTEGER NOT NULL, numeric_value REAL, text_value TEXT, unit TEXT NOT NULL, quality TEXT NOT NULL CHECK(quality IN('exact','derived','estimated','unavailable')), reset INTEGER NOT NULL DEFAULT 0, gap INTEGER NOT NULL DEFAULT 0, UNIQUE(cycle_id,entity_pk,metric_name))",
        "CREATE TABLE IF NOT EXISTS minute_rollups(entity_pk INTEGER NOT NULL REFERENCES entities(entity_pk) ON DELETE CASCADE, metric_name TEXT NOT NULL, minute_start INTEGER NOT NULL, samples INTEGER NOT NULL, expected_samples INTEGER NOT NULL, min_value REAL, avg_value REAL, max_value REAL, unavailable_count INTEGER NOT NULL, reset_count INTEGER NOT NULL DEFAULT 0, gap_count INTEGER NOT NULL DEFAULT 0, coverage REAL NOT NULL, unit TEXT NOT NULL, quality TEXT NOT NULL CHECK(quality IN('exact','derived','estimated','unavailable')), PRIMARY KEY(entity_pk,metric_name,minute_start))",
        "CREATE TABLE IF NOT EXISTS events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, severity TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}')",
        "CREATE TABLE IF NOT EXISTS collector_state(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    ]


def _required_indexes() -> dict[str, tuple[str, str, tuple[str, ...], bool]]:
    return {
        "idx_metric_points_lookup": (
            "CREATE INDEX IF NOT EXISTS idx_metric_points_lookup ON metric_points(entity_pk,metric_name,ts)",
            "metric_points",
            ("entity_pk", "metric_name", "ts"),
            False,
        ),
        "idx_metric_points_time": (
            "CREATE INDEX IF NOT EXISTS idx_metric_points_time ON metric_points(ts)",
            "metric_points",
            ("ts",),
            False,
        ),
        "idx_metric_samples_time": (
            "CREATE INDEX IF NOT EXISTS idx_metric_samples_time ON metric_samples(collected_at)",
            "metric_samples",
            ("collected_at",),
            False,
        ),
        "idx_metric_samples_identity": (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_metric_samples_identity ON metric_samples(cycle_id,sample_identity)",
            "metric_samples",
            ("cycle_id", "sample_identity"),
            True,
        ),
        "idx_events_time": (
            "CREATE INDEX IF NOT EXISTS idx_events_time ON events(ts)",
            "events",
            ("ts",),
            False,
        ),
        "idx_minute_rollups_time": (
            "CREATE INDEX IF NOT EXISTS idx_minute_rollups_time ON minute_rollups(minute_start)",
            "minute_rollups",
            ("minute_start",),
            False,
        ),
    }


def connect_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    for _ in range(50):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "i/o" not in message:
                raise
            time.sleep(0.02)
    else:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)
    except sqlite3.OperationalError:
        return 0


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _views(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _index_columns(conn: sqlite3.Connection, index: str) -> tuple[str, ...]:
    return tuple(str(row[2]) for row in conn.execute(f"PRAGMA index_info({index})"))


def _has_metric_points_unique(conn: sqlite3.Connection) -> bool:
    for row in conn.execute("PRAGMA index_list(metric_points)"):
        if int(row[2]) and _index_columns(conn, str(row[1])) == (
            "cycle_id",
            "entity_pk",
            "metric_name",
        ):
            return True
    return False


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    required = _required_indexes()
    for sql, _table, _columns_value, _unique in required.values():
        conn.execute(sql)
    by_name = {str(row[1]): row for row in conn.execute("PRAGMA index_list(metric_points)")}
    for table in ("entities", "metric_samples", "events", "minute_rollups"):
        by_name.update({str(row[1]): row for row in conn.execute(f"PRAGMA index_list({table})")})
    for name, (_sql, table, columns, unique) in required.items():
        row = by_name.get(name)
        if row is None:
            raise RuntimeError(f"missing index: {name}")
        owner = conn.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
        if owner is None or owner[0] != table:
            actual = owner[0] if owner else None
            raise RuntimeError(f"index {name} is owned by {actual}, expected {table}")
        if _index_columns(conn, name) != columns:
            raise RuntimeError(f"index {name} has wrong columns")
        if bool(row[2]) != unique:
            raise RuntimeError(f"index {name} uniqueness mismatch")


def _ensure_metrics_view(conn: sqlite3.Connection) -> None:
    if "metrics" in _views(conn):
        conn.execute("DROP VIEW metrics")
    conn.execute(
        "CREATE VIEW metrics AS "
        "SELECT NULL AS sample_id, e.entity_type AS scope, p.metric_name AS name, "
        "p.numeric_value AS value, p.unit, p.quality, e.metadata_json AS labels_json "
        "FROM metric_points p JOIN entities e ON e.entity_pk = p.entity_pk"
    )


def ensure_entity(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    display: str | None,
    metadata: dict[str, object],
    now: int,
) -> int:
    metadata = sanitize_mapping(metadata)
    conn.execute(
        "INSERT INTO entities(entity_type,entity_id,display_name,metadata_json,updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(entity_type,entity_id) DO UPDATE SET "
        "display_name=excluded.display_name,"
        "metadata_json=CASE WHEN excluded.metadata_json='{}' THEN entities.metadata_json "
        "ELSE excluded.metadata_json END,updated_at=excluded.updated_at",
        (entity_type, entity_id, display, json.dumps(metadata, sort_keys=True), now),
    )
    row = conn.execute(
        "SELECT entity_pk FROM entities WHERE entity_type=? AND entity_id=?",
        (entity_type, entity_id),
    ).fetchone()
    return int(row[0])


_ensure_entity = ensure_entity


def _legacy_metric_entity(scope: str, labels: dict[str, object]) -> tuple[str, str]:
    for prefix, entity_type in (
        ("tunnel.", "tunnel"),
        ("service.", "service"),
        ("route.", "route"),
    ):
        if scope.startswith(prefix):
            return entity_type, scope.removeprefix(prefix)
    if "interface" in labels:
        return "interface", f"interface:{labels['interface']}"
    if "path" in labels:
        return "filesystem", f"fs:{labels['path']}"
    for key in ("tunnel_id", "tunnel", "service"):
        if key in labels:
            value = str(labels[key])
            if value.startswith("gost-"):
                parts = value.removesuffix(".service").split("-")
                if len(parts) == 3 and parts[1] in {"iran", "kharej"} and parts[2].isdigit():
                    return "tunnel", f"{parts[1]}-{parts[2]}"
                return "service", value
            return "tunnel", value
    if scope in {"collector", "host"}:
        return scope, "local"
    canonical = json.dumps(labels, sort_keys=True, separators=(",", ":"))
    return scope, f"{scope}:{canonical}"


def _cycle(
    conn: sqlite3.Connection,
    ts: int,
    started: float,
    finished: float,
    duration: float,
    success: bool,
    overrun: bool,
    missed: int = 0,
    overrun_seconds: float = 0.0,
) -> int:
    conn.execute(
        "INSERT INTO sample_cycles(collected_at,monotonic_started,monotonic_finished,duration_seconds,"
        "success,overrun,missed_deadlines,overrun_seconds) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(collected_at) DO UPDATE SET monotonic_started=excluded.monotonic_started,"
        "monotonic_finished=excluded.monotonic_finished,duration_seconds=excluded.duration_seconds,"
        "success=excluded.success,overrun=excluded.overrun,missed_deadlines=excluded.missed_deadlines,"
        "overrun_seconds=excluded.overrun_seconds",
        (
            ts,
            started,
            finished,
            duration,
            int(success),
            int(overrun),
            int(missed),
            float(overrun_seconds),
        ),
    )
    row = conn.execute("SELECT cycle_id FROM sample_cycles WHERE collected_at=?", (ts,)).fetchone()
    return int(row[0])


def _dedupe_sample_identities(conn: sqlite3.Connection) -> None:
    if "metrics_legacy" in _tables(conn) and "sample_id" in _columns(conn, "metrics_legacy"):
        duplicates = conn.execute(
            "SELECT cycle_id,sample_identity,MIN(sample_id) FROM metric_samples "
            "GROUP BY cycle_id,sample_identity HAVING COUNT(*)>1"
        )
        for cycle_id, identity, keep in duplicates:
            sample_ids = conn.execute(
                "SELECT sample_id FROM metric_samples WHERE cycle_id=? AND sample_identity=? AND sample_id<>?",
                (cycle_id, identity, keep),
            )
            for sample_id, in sample_ids:
                conn.execute(
                    "UPDATE metrics_legacy SET sample_id=? WHERE sample_id=?",
                    (keep, sample_id),
                )
    conn.execute(
        "DELETE FROM metric_samples WHERE sample_id NOT IN "
        "(SELECT MIN(sample_id) FROM metric_samples GROUP BY cycle_id,sample_identity)"
    )


def migrate_database(
    db_path: str = DEFAULT_DB_PATH,
    inject_failure: str | None = None,
) -> sqlite3.Connection:
    conn = connect_db(db_path)
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = _version(conn)
        tables = _tables(conn)
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"unsupported schema version {version}")
        relevant = {
            "tunnels",
            "metric_samples",
            "metrics",
            "metric_points",
            "minute_rollups",
            "sample_cycles",
        }
        pre_counts = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in tables & relevant
        }
        if "metrics" in tables:
            conn.execute("ALTER TABLE metrics RENAME TO metrics_legacy")
            tables = _tables(conn)
        if "metric_samples" in tables and "cycle_id" not in _columns(conn, "metric_samples"):
            conn.execute("ALTER TABLE metric_samples RENAME TO metric_samples_legacy")
            tables = _tables(conn)
        if "tunnels" in tables and "entity_pk" not in _columns(conn, "tunnels"):
            conn.execute("ALTER TABLE tunnels RENAME TO tunnels_legacy")
            tables = _tables(conn)
        rollup_columns = {
            "entity_pk",
            "metric_name",
            "minute_start",
            "samples",
            "expected_samples",
            "unavailable_count",
            "coverage",
            "unit",
            "quality",
        }
        if "minute_rollups" in tables and not rollup_columns.issubset(
            _columns(conn, "minute_rollups")
        ):
            conn.execute("ALTER TABLE minute_rollups RENAME TO minute_rollups_legacy")
            tables = _tables(conn)
        if "metric_points" in tables and not _has_metric_points_unique(conn):
            conn.execute("ALTER TABLE metric_points RENAME TO metric_points_legacy")
        for sql in _v4_statements():
            conn.execute(sql)
        for column, sql_type, default in (
            ("missed_deadlines", "INTEGER", "0"),
            ("overrun_seconds", "REAL", "0.0"),
        ):
            if column not in _columns(conn, "sample_cycles"):
                conn.execute(
                    f"ALTER TABLE sample_cycles ADD COLUMN {column} {sql_type} NOT NULL DEFAULT {default}"
                )
        if "sample_identity" not in _columns(conn, "metric_samples"):
            conn.execute(
                "ALTER TABLE metric_samples ADD COLUMN sample_identity TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                "UPDATE metric_samples SET sample_identity=COALESCE(tunnel_id, 'host') "
                "WHERE sample_identity=''"
            )
        if inject_failure == "after_create":
            raise RuntimeError("injected migration failure")
        now = int(time.time())
        if "tunnels_legacy" in _tables(conn):
            rows = conn.execute(
                "SELECT tunnel_id,side,tunnel_number,service_name,env_path,listen_ports_json,"
                "target_ports_json,updated_at FROM tunnels_legacy"
            )
            for row in rows:
                entity_pk = ensure_entity(
                    conn,
                    "tunnel",
                    str(row[0]),
                    str(row[3]),
                    {"service": str(row[3])},
                    now,
                )
                conn.execute(
                    "INSERT OR IGNORE INTO tunnels VALUES(?,?,?,?,?,?,?,?,?)",
                    (row[0], entity_pk, *row[1:]),
                )
        ensure_entity(conn, "host", "local", "local host", {}, now)
        if "metric_samples_legacy" in _tables(conn):
            rows = conn.execute(
                "SELECT sample_id,tunnel_id,collected_at,service_state,service_substate,restart_count,"
                "listen_ports_total,listen_ports_up,configured_mappings_total,rx_bytes,tx_bytes "
                "FROM metric_samples_legacy"
            )
            for row in rows:
                cycle = _cycle(
                    conn,
                    int(row[2]),
                    float(row[2]),
                    float(row[2]),
                    0.0,
                    True,
                    False,
                )
                conn.execute(
                    "INSERT OR IGNORE INTO metric_samples(sample_id,cycle_id,tunnel_id,sample_identity,"
                    "collected_at,service_state,service_substate,restart_count,listen_ports_total,"
                    "listen_ports_up,configured_mappings_total,rx_bytes,tx_bytes) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row[0],
                        cycle,
                        row[1],
                        row[1] or "host",
                        *row[2:9],
                        None if row[9] == 0 else row[9],
                        None if row[10] == 0 else row[10],
                    ),
                )
        if "metric_points_legacy" in _tables(conn):
            required = {
                "point_id",
                "cycle_id",
                "entity_pk",
                "metric_name",
                "ts",
                "numeric_value",
                "text_value",
                "unit",
                "quality",
                "reset",
                "gap",
            }
            if required.issubset(_columns(conn, "metric_points_legacy")):
                columns = ",".join(required)
                # Use deterministic column order instead of set iteration.
                columns = "point_id,cycle_id,entity_pk,metric_name,ts,numeric_value,text_value,unit,quality,reset,gap"
                conn.execute(
                    "INSERT OR IGNORE INTO metric_points(" + columns + ") "
                    "SELECT " + columns + " FROM metric_points_legacy"
                )
            conn.execute("DROP TABLE metric_points_legacy")
        if "metrics_legacy" in _tables(conn):
            required = {"sample_id", "scope", "name", "value", "unit", "quality", "labels_json"}
            if required.issubset(_columns(conn, "metrics_legacy")):
                rows = conn.execute(
                    "SELECT sample_id,scope,name,value,unit,quality,labels_json FROM metrics_legacy"
                )
                for row in rows:
                    sample = conn.execute(
                        "SELECT cycle_id,collected_at FROM metric_samples WHERE sample_id=?",
                        (row[0],),
                    ).fetchone()
                    if not sample:
                        continue
                    labels = json.loads(row[6] or "{}")
                    entity_type, entity_id = _legacy_metric_entity(str(row[1]), labels)
                    entity_pk = ensure_entity(
                        conn,
                        entity_type,
                        entity_id,
                        entity_id,
                        labels,
                        int(sample[1]),
                    )
                    exists = conn.execute(
                        "SELECT 1 FROM metric_points WHERE cycle_id=? AND entity_pk=? AND metric_name=?",
                        (sample[0], entity_pk, row[2]),
                    ).fetchone()
                    if exists:
                        continue
                    conn.execute(
                        "INSERT INTO metric_points(cycle_id,entity_pk,metric_name,ts,numeric_value,"
                        "text_value,unit,quality,reset,gap) VALUES(?,?,?,?,?,?,?,?,0,0) "
                        "ON CONFLICT(cycle_id,entity_pk,metric_name) DO UPDATE SET "
                        "ts=excluded.ts,numeric_value=excluded.numeric_value,unit=excluded.unit,"
                        "quality=excluded.quality",
                        (sample[0], entity_pk, row[2], sample[1], row[3], None, row[4], row[5]),
                    )
            conn.execute("DROP TABLE metrics_legacy")
        if "metric_samples_legacy" in _tables(conn):
            conn.execute("DROP TABLE metric_samples_legacy")
        if "tunnels_legacy" in _tables(conn):
            conn.execute("DROP TABLE tunnels_legacy")
        if "minute_rollups_legacy" in _tables(conn):
            conn.execute("DROP INDEX IF EXISTS idx_minute_rollups_time")
            conn.execute("ALTER TABLE minute_rollups_legacy RENAME TO minute_rollups_archive")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_archive_minute_rollups_time "
                "ON minute_rollups_archive(minute_start)"
            )
        _ensure_metrics_view(conn)
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations(version,applied_at) VALUES(?,?)",
            (SCHEMA_VERSION, now),
        )
        _dedupe_sample_identities(conn)
        _ensure_indexes(conn)
        if inject_failure == "after_indexes":
            raise RuntimeError("injected migration failure")
        post_counts = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in {"tunnels", "metric_samples", "metric_points", "sample_cycles"}
        }
        if pre_counts.get("tunnels", 0) and post_counts["tunnels"] < pre_counts["tunnels"]:
            raise RuntimeError("tunnel migration row count decreased")
        legacy_left = {name for name in _tables(conn) if name.endswith("_legacy")}
        if legacy_left:
            raise RuntimeError(f"stale legacy tables remain: {sorted(legacy_left)}")
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(f"foreign key check failed: {foreign_keys}")
        required_tables = {
            "schema_migrations",
            "sample_cycles",
            "entities",
            "tunnels",
            "metric_samples",
            "metric_points",
            "minute_rollups",
            "events",
            "collector_state",
        }
        missing = required_tables - _tables(conn)
        if missing:
            raise RuntimeError(f"missing required tables: {sorted(missing)}")
        if _version(conn) != SCHEMA_VERSION:
            raise RuntimeError("schema v4 postcondition failed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn


def init_db(
    db_path: str = DEFAULT_DB_PATH,
    inject_failure: str | None = None,
) -> sqlite3.Connection:
    return migrate_database(db_path, inject_failure)


def open_runtime_database(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = connect_db(db_path)
    if _version(conn) != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError("monitoring database requires migration")
    return conn


def insert_event(conn: sqlite3.Connection, event: Event) -> None:
    conn.execute(
        "INSERT INTO events(ts,severity,code,message,details_json) VALUES(?,?,?,?,?)",
        (
            event.ts,
            event.severity,
            event.code,
            event.message,
            json.dumps(sanitize_mapping(event.details), sort_keys=True),
        ),
    )


def upsert_tunnel(conn: sqlite3.Connection, tunnel: Tunnel, now: int) -> None:
    metadata: dict[str, object] = {"service": tunnel.service_name}
    if tunnel.remote_endpoint:
        metadata["remote_endpoint"] = tunnel.remote_endpoint
    entity_pk = ensure_entity(
        conn,
        "tunnel",
        tunnel.tunnel_id,
        tunnel.service_name,
        metadata,
        now,
    )
    conn.execute(
        "INSERT INTO tunnels VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tunnel_id) DO UPDATE SET "
        "entity_pk=excluded.entity_pk,env_path=excluded.env_path,"
        "listen_ports_json=excluded.listen_ports_json,target_ports_json=excluded.target_ports_json,"
        "updated_at=excluded.updated_at",
        (
            tunnel.tunnel_id,
            entity_pk,
            tunnel.side,
            tunnel.number,
            tunnel.service_name,
            tunnel.env_path,
            json.dumps(tunnel.listen_ports),
            json.dumps(tunnel.target_ports),
            now,
        ),
    )


def insert_sample(
    conn: sqlite3.Connection,
    sample: MetricSample,
    cycle_id: int | None = None,
) -> int:
    cycle = cycle_id
    if cycle is None:
        cycle = _cycle(
            conn,
            sample.collected_at,
            float(sample.collected_at),
            float(sample.collected_at),
            0.0,
            True,
            False,
        )
    identity = sample.tunnel_id or "host"
    values = (
        cycle,
        sample.tunnel_id,
        identity,
        sample.collected_at,
        sample.service_state,
        sample.service_substate,
        sample.restart_count,
        sample.listen_ports_total,
        sample.listen_ports_up,
        sample.configured_mappings_total,
        sample.rx_bytes,
        sample.tx_bytes,
    )
    conn.execute(
        "INSERT INTO metric_samples(cycle_id,tunnel_id,sample_identity,collected_at,service_state,"
        "service_substate,restart_count,listen_ports_total,listen_ports_up,configured_mappings_total,"
        "rx_bytes,tx_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(cycle_id,sample_identity) DO UPDATE SET tunnel_id=excluded.tunnel_id,"
        "collected_at=excluded.collected_at,service_state=excluded.service_state,"
        "service_substate=excluded.service_substate,restart_count=excluded.restart_count,"
        "listen_ports_total=excluded.listen_ports_total,listen_ports_up=excluded.listen_ports_up,"
        "configured_mappings_total=excluded.configured_mappings_total,rx_bytes=excluded.rx_bytes,"
        "tx_bytes=excluded.tx_bytes",
        values,
    )
    row = conn.execute(
        "SELECT sample_id FROM metric_samples WHERE cycle_id=? AND sample_identity=?",
        (cycle, identity),
    ).fetchone()
    return int(row[0])


def insert_metric(
    conn: sqlite3.Connection,
    sample_id: int,
    metric: Metric,
    cycle_id: int | None = None,
    ts: int | None = None,
) -> None:
    if SENSITIVE_KEY_RE.search(metric.name):
        return
    if cycle_id is None or ts is None:
        row = conn.execute(
            "SELECT cycle_id,collected_at FROM metric_samples WHERE sample_id=?",
            (sample_id,),
        ).fetchone()
        cycle_id = int(row[0])
        ts = int(row[1])
    entity_type = metric.entity_type or metric.scope
    entity_id = metric.entity_id or metric.scope
    entity_pk = ensure_entity(
        conn,
        entity_type,
        entity_id,
        entity_id,
        metric.labels,
        ts,
    )
    numeric = metric.value if isinstance(metric.value, (int, float)) else None
    text = (
        sanitize_text(metric.value)
        if isinstance(metric.value, str) and metric.name in ALLOWED_TEXT_METRICS
        else None
    )
    conn.execute(
        "INSERT INTO metric_points(cycle_id,entity_pk,metric_name,ts,numeric_value,text_value,unit,"
        "quality,reset,gap) VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(cycle_id,entity_pk,metric_name) DO UPDATE SET ts=excluded.ts,"
        "numeric_value=excluded.numeric_value,text_value=excluded.text_value,unit=excluded.unit,"
        "quality=excluded.quality,reset=excluded.reset,gap=excluded.gap",
        (
            cycle_id,
            entity_pk,
            metric.name,
            ts,
            numeric,
            text,
            metric.unit,
            metric.quality,
            int(metric.reset),
            int(metric.gap),
        ),
    )


def sanitize_text(value: str) -> str:
    value = URI_USERINFO_RE.sub(r"\1[redacted]@", value)
    return SENSITIVE_TEXT_RE.sub("[redacted]", value)


def sanitize_mapping(values: dict[str, object]) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for key, value in values.items():
        if SENSITIVE_KEY_RE.search(str(key)):
            continue
        if isinstance(value, dict):
            sanitized[str(key)] = sanitize_mapping(value)
        elif isinstance(value, list):
            sanitized[str(key)] = [
                sanitize_mapping(item) if isinstance(item, dict)
                else sanitize_text(item) if isinstance(item, str)
                else item
                for item in value
            ]
        elif isinstance(value, str):
            sanitized[str(key)] = sanitize_text(value)
        else:
            sanitized[str(key)] = value
    return sanitized


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM collector_state WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)",
        (key, value),
    )


def get_json_state(conn: sqlite3.Connection, key: str) -> object | None:
    value = get_state(conn, key)
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def set_json_state(conn: sqlite3.Connection, key: str, value: object) -> None:
    set_state(conn, key, json.dumps(value, sort_keys=True, separators=(",", ":")))


def quality_worst(qualities: Iterable[str]) -> str:
    values = list(qualities)
    if not values:
        return "unavailable"
    return max(values, key=lambda quality: QUALITY_RANK[quality])


def rollup_completed_minutes(
    conn: sqlite3.Connection,
    now: int,
    interval: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    batch_minutes: int = ROLLUP_BATCH_MINUTES,
) -> None:
    complete_before = (now // 60) * 60
    state = get_state(conn, "minute_rollup_watermark")
    start = int(state) if state else 0
    first = conn.execute("SELECT MIN(ts) FROM metric_points").fetchone()[0]
    if first is None:
        set_state(conn, "minute_rollup_watermark", str(complete_before))
        return
    if start <= 0:
        start = (int(first) // 60) * 60
    end = min(complete_before, start + batch_minutes * 60)
    if end <= start:
        return
    expected = max(1, int(60 / interval))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT entity_pk,metric_name,ts,numeric_value,unit,quality,reset,gap "
        "FROM metric_points WHERE ts>=? AND ts<?",
        (start, end),
    ).fetchall()
    conn.row_factory = None
    groups: dict[tuple[int, str, int, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (
            int(row["entity_pk"]),
            str(row["metric_name"]),
            (int(row["ts"]) // 60) * 60,
            str(row["unit"]),
        )
        groups.setdefault(key, []).append(row)
    for (entity_pk, metric_name, minute, unit), points in groups.items():
        values = [float(point["numeric_value"]) for point in points if point["numeric_value"] is not None]
        quality = quality_worst(str(point["quality"]) for point in points)
        unavailable = sum(1 for point in points if point["quality"] == "unavailable")
        resets = sum(int(point["reset"]) for point in points)
        gaps = sum(int(point["gap"]) for point in points)
        coverage = min(1.0, len(points) / expected)
        conn.execute(
            "INSERT OR REPLACE INTO minute_rollups(entity_pk,metric_name,minute_start,samples,"
            "expected_samples,min_value,avg_value,max_value,unavailable_count,reset_count,gap_count,"
            "coverage,unit,quality) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entity_pk,
                metric_name,
                minute,
                len(points),
                expected,
                min(values) if values else None,
                sum(values) / len(values) if values else None,
                max(values) if values else None,
                unavailable,
                resets,
                gaps,
                coverage,
                unit,
                quality,
            ),
        )
    set_state(conn, "minute_rollup_watermark", str(end))


def apply_retention(conn: sqlite3.Connection, now: int) -> None:
    conn.execute("DELETE FROM metric_samples WHERE collected_at < ?", (now - RAW_RETENTION_SECONDS,))
    conn.execute("DELETE FROM sample_cycles WHERE collected_at < ?", (now - RAW_RETENTION_SECONDS,))
    conn.execute("DELETE FROM metric_points WHERE ts < ?", (now - RAW_RETENTION_SECONDS,))
    conn.execute(
        "DELETE FROM minute_rollups WHERE minute_start < ?",
        (now - ROLLUP_RETENTION_SECONDS,),
    )


def run_maintenance(conn: sqlite3.Connection, now: int) -> None:
    rollup_completed_minutes(conn, now)
    apply_retention(conn, now)


def checkpoint_wal(db_path: str) -> tuple[int, int, int]:
    conn = connect_db(db_path)
    try:
        row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        return tuple(int(value) for value in row)  # type: ignore[return-value]
    finally:
        conn.close()
