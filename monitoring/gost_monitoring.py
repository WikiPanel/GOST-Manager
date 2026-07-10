#!/usr/bin/env python3
"""GOST Manager monitoring collector.

Local-only, standard-library monitoring for Direct Mode/Gateway services.  The
collector observes runtime state and writes bounded history; it never mutates
traffic configuration or service lifecycle.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Sequence

SCHEMA_VERSION = 4
DEFAULT_DB_PATH = "/var/lib/gost-manager/metrics.sqlite3"
DEFAULT_ENV_DIR = "/etc/gost"
DEFAULT_SAMPLE_INTERVAL_SECONDS = 5.0
RAW_RETENTION_SECONDS = 48 * 3600
ROLLUP_RETENTION_SECONDS = 30 * 24 * 3600
MAINTENANCE_INTERVAL_SECONDS = 15 * 60
ROLLUP_BATCH_MINUTES = 240
QUALITY = ("exact", "derived", "estimated", "unavailable")
QUALITY_RANK = {"exact": 0, "derived": 1, "estimated": 2, "unavailable": 3}
SERVICE_RE = re.compile(r"^gost-(iran|kharej)-([1-9][0-9]*)\.service$")
ENV_RE = re.compile(r"^(iran|kharej)-([1-9][0-9]*)\.env$")
SS_PROCESS_RE = re.compile(r'"(?P<process>[^"]+)",pid=(?P<pid>\d+),fd=(?P<fd>\d+)')


@dataclasses.dataclass(frozen=True)
class Tunnel:
    side: str
    number: int
    service_name: str
    env_path: str
    listen_ports: tuple[int, ...]
    target_ports: tuple[int, ...]

    @property
    def tunnel_id(self) -> str:
        return f"{self.side}-{self.number}"


@dataclasses.dataclass(frozen=True)
class Metric:
    scope: str
    name: str
    value: float | int | str | None
    unit: str
    quality: str
    labels: dict[str, str] = dataclasses.field(default_factory=dict)
    entity_type: str | None = None
    entity_id: str | None = None
    reset: bool = False
    gap: bool = False


@dataclasses.dataclass(frozen=True)
class Event:
    ts: int
    severity: str
    code: str
    message: str
    details: dict[str, object] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class MetricSample:
    tunnel_id: str | None
    collected_at: int
    service_state: int
    service_substate: int
    restart_count: int
    listen_ports_total: int
    listen_ports_up: int
    configured_mappings_total: int
    rx_bytes: int | None = None
    tx_bytes: int | None = None


@dataclasses.dataclass(frozen=True)
class CounterDelta:
    delta: int | None
    rate: float | None
    quality: str
    reset: bool
    gap: bool


@dataclasses.dataclass(frozen=True)
class Clock:
    wall: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic


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


def _required_indexes() -> dict[str, str]:
    return {
        "idx_entities_lookup": "CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_lookup ON entities(entity_type,entity_id)",
        "idx_metric_points_lookup": "CREATE INDEX IF NOT EXISTS idx_metric_points_lookup ON metric_points(entity_pk,metric_name,ts)",
        "idx_metric_points_unique": "CREATE UNIQUE INDEX IF NOT EXISTS idx_metric_points_unique ON metric_points(cycle_id,entity_pk,metric_name)",
        "idx_metric_points_time": "CREATE INDEX IF NOT EXISTS idx_metric_points_time ON metric_points(ts)",
        "idx_metric_samples_time": "CREATE INDEX IF NOT EXISTS idx_metric_samples_time ON metric_samples(collected_at)",
        "idx_metric_samples_identity": "CREATE UNIQUE INDEX IF NOT EXISTS idx_metric_samples_identity ON metric_samples(cycle_id,sample_identity)",
        "idx_events_time": "CREATE INDEX IF NOT EXISTS idx_events_time ON events(ts)",
        "idx_minute_rollups_time": "CREATE INDEX IF NOT EXISTS idx_minute_rollups_time ON minute_rollups(minute_start)",
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
            if "locked" not in str(exc).lower() and "i/o" not in str(exc).lower():
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
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _views(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _index_columns(conn: sqlite3.Connection, index: str) -> tuple[str, ...]:
    return tuple(str(r[2]) for r in conn.execute(f"PRAGMA index_info({index})"))


def _has_metric_points_unique(conn: sqlite3.Connection) -> bool:
    for row in conn.execute("PRAGMA index_list(metric_points)"):
        if not int(row[2]):
            continue
        if _index_columns(conn, str(row[1])) == ("cycle_id", "entity_pk", "metric_name"):
            return True
    return False


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    for sql in _required_indexes().values():
        conn.execute(sql)
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    missing = set(_required_indexes()) - existing
    if missing:
        raise RuntimeError(f"missing indexes: {sorted(missing)}")


def _ensure_metrics_view(conn: sqlite3.Connection) -> None:
    if "metrics" in _views(conn):
        conn.execute("DROP VIEW metrics")
    conn.execute(
        "CREATE VIEW metrics AS "
        "SELECT NULL AS sample_id, e.entity_type AS scope, p.metric_name AS name, "
        "p.numeric_value AS value, p.unit, p.quality, e.metadata_json AS labels_json "
        "FROM metric_points p "
        "JOIN entities e ON e.entity_pk = p.entity_pk"
    )


def _ensure_entity(conn: sqlite3.Connection, entity_type: str, entity_id: str, display: str | None, metadata: dict[str, object], now: int) -> int:
    conn.execute(
        "INSERT INTO entities(entity_type,entity_id,display_name,metadata_json,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(entity_type,entity_id) DO UPDATE SET display_name=excluded.display_name,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
        (entity_type, entity_id, display, json.dumps(metadata, sort_keys=True), now),
    )
    return int(conn.execute("SELECT entity_pk FROM entities WHERE entity_type=? AND entity_id=?", (entity_type, entity_id)).fetchone()[0])


def _legacy_metric_entity(scope: str, labels: dict[str, object]) -> tuple[str, str]:
    if scope.startswith("tunnel."):
        return "tunnel", scope.removeprefix("tunnel.")
    if scope.startswith("service."):
        return "service", scope.removeprefix("service.")
    if scope.startswith("route."):
        return "route", scope.removeprefix("route.")
    if "interface" in labels:
        iface = str(labels["interface"])
        return "interface", f"interface:{iface}"
    if "path" in labels:
        path = str(labels["path"])
        return "filesystem", f"fs:{path}"
    for key in ("tunnel_id", "tunnel", "service"):
        if key in labels:
            value = str(labels[key])
            if value.startswith("gost-"):
                match = SERVICE_RE.match(value)
                if match:
                    return "tunnel", f"{match.group(1)}-{match.group(2)}"
                return "service", value
            return "tunnel", value
    if scope == "collector":
        return "collector", "local"
    if scope == "host":
        return "host", "local"
    canonical = json.dumps(labels, sort_keys=True, separators=(",", ":"))
    return scope, f"{scope}:{canonical}"


def _dedupe_sample_identities(conn: sqlite3.Connection) -> None:
    if "metrics_legacy" in _tables(conn) and {"sample_id"}.issubset(_columns(conn, "metrics_legacy")):
        for cycle_id, identity, keep in conn.execute("SELECT cycle_id,sample_identity,MIN(sample_id) FROM metric_samples GROUP BY cycle_id,sample_identity HAVING COUNT(*)>1"):
            dupes = [r[0] for r in conn.execute("SELECT sample_id FROM metric_samples WHERE cycle_id=? AND sample_identity=? AND sample_id<>?", (cycle_id, identity, keep))]
            for sample_id in dupes:
                conn.execute("UPDATE metrics_legacy SET sample_id=? WHERE sample_id=?", (keep, sample_id))
    conn.execute(
        "DELETE FROM metric_samples WHERE sample_id NOT IN "
        "(SELECT MIN(sample_id) FROM metric_samples GROUP BY cycle_id,sample_identity)"
    )


def migrate_database(db_path: str = DEFAULT_DB_PATH, inject_failure: str | None = None) -> sqlite3.Connection:
    conn = connect_db(db_path)
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = _version(conn)
        tables = _tables(conn)
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"unsupported schema version {version}")
        pre_counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in tables if name in {"tunnels", "metric_samples", "metrics", "metric_points", "minute_rollups", "sample_cycles"}}
        if "metrics" in tables:
            conn.execute("ALTER TABLE metrics RENAME TO metrics_legacy")
            tables = _tables(conn)
        if "metric_samples" in tables and "cycle_id" not in _columns(conn, "metric_samples"):
            conn.execute("ALTER TABLE metric_samples RENAME TO metric_samples_legacy")
            tables = _tables(conn)
        if "tunnels" in tables and "entity_pk" not in _columns(conn, "tunnels"):
            conn.execute("ALTER TABLE tunnels RENAME TO tunnels_legacy")
            tables = _tables(conn)
        if "minute_rollups" in tables and not {"entity_pk","metric_name","minute_start","samples","expected_samples","unavailable_count","coverage","unit","quality"}.issubset(_columns(conn, "minute_rollups")):
            conn.execute("ALTER TABLE minute_rollups RENAME TO minute_rollups_legacy")
            tables = _tables(conn)
        if "metric_points" in tables and not _has_metric_points_unique(conn):
            conn.execute("ALTER TABLE metric_points RENAME TO metric_points_legacy")
            tables = _tables(conn)
        for sql in _v4_statements():
            conn.execute(sql)
        for column, default in (("missed_deadlines", "0"), ("overrun_seconds", "0.0")):
            if column not in _columns(conn, "sample_cycles"):
                conn.execute(f"ALTER TABLE sample_cycles ADD COLUMN {column} {'INTEGER' if column == 'missed_deadlines' else 'REAL'} NOT NULL DEFAULT {default}")
        if "sample_identity" not in _columns(conn, "metric_samples"):
            conn.execute("ALTER TABLE metric_samples ADD COLUMN sample_identity TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE metric_samples SET sample_identity=COALESCE(tunnel_id, 'host') WHERE sample_identity=''")
        if inject_failure == "after_create":
            raise RuntimeError("injected migration failure")
        now = int(time.time())
        if "tunnels_legacy" in _tables(conn):
            for row in conn.execute("SELECT tunnel_id,side,tunnel_number,service_name,env_path,listen_ports_json,target_ports_json,updated_at FROM tunnels_legacy"):
                entity_pk = _ensure_entity(conn, "tunnel", row[0], row[3], {"service": row[3]}, now)
                conn.execute("INSERT OR IGNORE INTO tunnels VALUES(?,?,?,?,?,?,?,?,?)", (row[0], entity_pk, row[1], row[2], row[3], row[4], row[5], row[6], row[7]))
        host_pk = _ensure_entity(conn, "host", "local", "local host", {}, now)
        if "metric_samples_legacy" in _tables(conn):
            for row in conn.execute("SELECT sample_id,tunnel_id,collected_at,service_state,service_substate,restart_count,listen_ports_total,listen_ports_up,configured_mappings_total,rx_bytes,tx_bytes FROM metric_samples_legacy"):
                cycle = _cycle(conn, int(row[2]), float(row[2]), float(row[2]), 0.0, True, False)
                conn.execute("INSERT OR IGNORE INTO metric_samples(sample_id,cycle_id,tunnel_id,sample_identity,collected_at,service_state,service_substate,restart_count,listen_ports_total,listen_ports_up,configured_mappings_total,rx_bytes,tx_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (row[0], cycle, row[1], row[1] or 'host', row[2], row[3], row[4], row[5], row[6], row[7], row[8], None if row[9] == 0 else row[9], None if row[10] == 0 else row[10]))
            pass
        if "metric_points_legacy" in _tables(conn):
            cols = _columns(conn, "metric_points_legacy")
            select_cols = "point_id,cycle_id,entity_pk,metric_name,ts,numeric_value,text_value,unit,quality,reset,gap"
            if {"point_id","cycle_id","entity_pk","metric_name","ts","numeric_value","text_value","unit","quality","reset","gap"}.issubset(cols):
                conn.execute(
                    "INSERT OR IGNORE INTO metric_points(point_id,cycle_id,entity_pk,metric_name,ts,numeric_value,text_value,unit,quality,reset,gap) "
                    f"SELECT {select_cols} FROM metric_points_legacy"
                )
            conn.execute("DROP TABLE metric_points_legacy")
        if "metrics_legacy" in _tables(conn):
            cols = _columns(conn, "metrics_legacy")
            if {"sample_id","scope","name","value","unit","quality","labels_json"}.issubset(cols):
                for row in conn.execute("SELECT sample_id,scope,name,value,unit,quality,labels_json FROM metrics_legacy"):
                    sample = conn.execute("SELECT cycle_id,collected_at FROM metric_samples WHERE sample_id=?", (row[0],)).fetchone()
                    if not sample:
                        continue
                    labels = json.loads(row[6] or "{}")
                    entity_type, entity_id = _legacy_metric_entity(str(row[1]), labels)
                    entity_pk = _ensure_entity(conn, entity_type, entity_id, entity_id, labels, int(sample[1]))
                    exists = conn.execute(
                        "SELECT 1 FROM metric_points WHERE cycle_id=? AND entity_pk=? AND metric_name=?",
                        (sample[0], entity_pk, row[2]),
                    ).fetchone()
                    if exists:
                        continue
                    conn.execute(
                        "INSERT INTO metric_points(cycle_id,entity_pk,metric_name,ts,numeric_value,text_value,unit,quality,reset,gap) VALUES(?,?,?,?,?,?,?,?,0,0) "
                        "ON CONFLICT(cycle_id,entity_pk,metric_name) DO UPDATE SET ts=excluded.ts,numeric_value=excluded.numeric_value,unit=excluded.unit,quality=excluded.quality",
                        (sample[0], entity_pk, row[2], sample[1], row[3], None, row[4], row[5]),
                    )
            conn.execute("DROP TABLE metrics_legacy")
        if "metric_samples_legacy" in _tables(conn):
            conn.execute("DROP TABLE metric_samples_legacy")
        if "tunnels_legacy" in _tables(conn):
            conn.execute("DROP TABLE tunnels_legacy")
        if "minute_rollups_legacy" in _tables(conn):
            conn.execute("ALTER TABLE minute_rollups_legacy RENAME TO minute_rollups_archive")
        _ensure_metrics_view(conn)
        conn.execute("INSERT OR REPLACE INTO schema_migrations(version,applied_at) VALUES(?,?)", (SCHEMA_VERSION, now))
        _dedupe_sample_identities(conn)
        _ensure_indexes(conn)
        if inject_failure == "after_indexes":
            raise RuntimeError("injected migration failure")
        post_counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in {"tunnels", "metric_samples", "metric_points", "sample_cycles"}}
        if pre_counts.get("tunnels", 0) and post_counts["tunnels"] < pre_counts["tunnels"]:
            raise RuntimeError("tunnel migration row count decreased")
        legacy_left = {name for name in _tables(conn) if name.endswith("_legacy")}
        if legacy_left:
            raise RuntimeError(f"stale legacy tables remain: {sorted(legacy_left)}")
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            raise RuntimeError(f"foreign key check failed: {fk}")
        required = {"schema_migrations","sample_cycles","entities","tunnels","metric_samples","metric_points","minute_rollups","events","collector_state"}
        missing_tables = required - _tables(conn)
        if missing_tables:
            raise RuntimeError(f"missing required tables: {sorted(missing_tables)}")
        if _version(conn) != SCHEMA_VERSION:
            raise RuntimeError("schema v4 postcondition failed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn


def init_db(db_path: str = DEFAULT_DB_PATH, inject_failure: str | None = None) -> sqlite3.Connection:
    return migrate_database(db_path, inject_failure)


def open_runtime_database(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = connect_db(db_path)
    if _version(conn) != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError("monitoring database requires migration")
    return conn


def parse_env_file(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"line {lineno}: missing '='")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            raise ValueError(f"line {lineno}: invalid key")
        values[key] = shlex.split(value, posix=True)[0] if value.strip() else ""
    return values


def _port(s: str) -> int:
    if not re.match(r"^[1-9][0-9]{0,4}$", s):
        raise ValueError("invalid port")
    port = int(s)
    if port > 65535:
        raise ValueError("invalid port")
    return port


def parse_mappings(value: str) -> tuple[tuple[int, int], ...]:
    if not value or value.startswith(",") or value.endswith(",") or ",," in value:
        raise ValueError("MAPPINGS must use listen:target")
    out: list[tuple[int, int]] = []
    seen: set[int] = set()
    for item in value.split(","):
        if not re.match(r"^[0-9]+:[0-9]+$", item.strip()):
            raise ValueError(f"invalid mapping: {item}")
        listen, target = item.strip().split(":", 1)
        listen_port = _port(listen)
        if listen_port in seen:
            raise ValueError(f"duplicate listen port: {listen_port}")
        seen.add(listen_port)
        out.append((listen_port, _port(target)))
    return tuple(out)


def tunnel_from_env(path: str | Path) -> Tunnel:
    p = Path(path)
    match = ENV_RE.match(p.name)
    if not match:
        raise ValueError(f"unsupported env name: {p.name}")
    side = match.group(1)
    number = int(match.group(2))
    values = parse_env_file(p)
    if side == "iran":
        mappings = parse_mappings(values.get("MAPPINGS", ""))
        return Tunnel(side, number, f"gost-{side}-{number}.service", str(p), tuple(a for a, _ in mappings), tuple(b for _, b in mappings))
    return Tunnel(side, number, f"gost-{side}-{number}.service", str(p), (_port(values.get("TUNNEL_PORT", "")),), ())


def discover_tunnels(env_dir: str | Path = DEFAULT_ENV_DIR, clock: Clock = Clock()) -> tuple[list[Tunnel], list[Event]]:
    root = Path(env_dir)
    if not root.exists():
        return [], []
    tunnels: list[Tunnel] = []
    events: list[Event] = []
    now = int(clock.wall())
    for path in sorted(root.glob("*.env")):
        if not ENV_RE.match(path.name):
            continue
        try:
            tunnels.append(tunnel_from_env(path))
        except Exception as exc:
            events.append(Event(now, "warning", "env_parse_error", f"Skipping malformed env file {path.name}", {"path": str(path), "error": str(exc)}))
    return tunnels, events


def parse_systemd_properties(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def _run(cmd: Sequence[str]) -> str:
    return subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout


def parse_listener_address(local: str) -> tuple[str, int] | None:
    if local.startswith("["):
        host, sep, port = local.rpartition("]:")
        return (host[1:], _port(port)) if sep else None
    host, sep, port = local.rpartition(":")
    return (host, _port(port)) if sep else None


def parse_ss_listeners(text: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for raw in text.splitlines():
        parts = raw.strip().split()
        if len(parts) < 5 or parts[0] != "LISTEN":
            continue
        try:
            addr = parse_listener_address(parts[3])
        except ValueError:
            continue
        if not addr:
            continue
        proc = SS_PROCESS_RE.search(raw)
        rows.append({"address": addr[0], "port": addr[1], "pid": int(proc.group("pid")) if proc else None, "process": proc.group("process") if proc else None})
    return rows


def collect_sample(tunnel: Tunnel, now: int | None = None, runner: Callable[[Sequence[str]], str] = _run) -> MetricSample:
    ts = int(time.time() if now is None else now)
    props = parse_systemd_properties(runner(["systemctl", "--no-pager", "show", tunnel.service_name, "--property=ActiveState,SubState,NRestarts,MainPID,ExecMainStartTimestampMonotonic"]))
    main_pid = int(props.get("MainPID") or 0)
    listeners = parse_ss_listeners(runner(["ss", "-H", "-lntp"]))
    owned_ports = {int(r["port"]) for r in listeners if r["port"] in tunnel.listen_ports and r["pid"] == main_pid and r["process"] == "gost"}
    return MetricSample(tunnel.tunnel_id, ts, int(props.get("ActiveState") == "active"), int(props.get("SubState") == "running"), int(props.get("NRestarts") or 0), len(tunnel.listen_ports), len(owned_ports), len(tunnel.target_ports))


def collect_tunnel_observation(tunnel: Tunnel, ts: int, props: dict[str, str], listeners: list[dict[str, object]]) -> tuple[MetricSample, str]:
    main_pid = int(props.get("MainPID") or 0)
    owned_ports = {int(r["port"]) for r in listeners if r["port"] in tunnel.listen_ports and r["pid"] == main_pid and r["process"] == "gost"}
    quality = "exact"
    for row in listeners:
        if row["port"] in tunnel.listen_ports and (row["pid"] is None or not main_pid):
            quality = "unavailable"
            break
    sample = MetricSample(tunnel.tunnel_id, ts, int(props.get("ActiveState") == "active"), int(props.get("SubState") == "running"), int(props.get("NRestarts") or 0), len(tunnel.listen_ports), len(owned_ports), len(tunnel.target_ports))
    return sample, quality


def listener_quality(tunnel: Tunnel, runner: Callable[[Sequence[str]], str] = _run) -> str:
    props = parse_systemd_properties(runner(["systemctl", "--no-pager", "show", tunnel.service_name, "--property=MainPID"]))
    main_pid = int(props.get("MainPID") or 0)
    listeners = parse_ss_listeners(runner(["ss", "-H", "-lntp"]))
    for row in listeners:
        if row["port"] in tunnel.listen_ports and (row["pid"] is None or not main_pid):
            return "unavailable"
    return "exact"


def counter_delta(prev: int | None, cur: int | None, elapsed: float, max_gap: float | None = None) -> CounterDelta:
    if prev is None or cur is None or elapsed <= 0:
        return CounterDelta(None, None, "unavailable", False, False)
    gap = bool(max_gap is not None and elapsed > max_gap)
    if cur < prev:
        return CounterDelta(None, None, "unavailable", True, gap)
    delta = cur - prev
    return CounterDelta(delta, delta / elapsed, "derived", False, gap)


def read_key_values(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            values[parts[0].rstrip(":")] = int(parts[1])
    return values


def collect_host_metrics(proc: Path = Path("/proc"), fs_paths: Iterable[Path] = (Path("/"), Path("/etc/gost-manager"), Path("/var/lib/gost-manager"))) -> tuple[list[Metric], list[Event]]:
    metrics: list[Metric] = []
    events: list[Event] = []
    ts = int(time.time())
    def unavailable(name: str, unit: str = "count") -> None:
        metrics.append(Metric("host", name, None, unit, "unavailable", entity_type="host", entity_id="local"))
    try:
        vals = list(map(int, proc.joinpath("stat").read_text().splitlines()[0].split()[1:]))
        metrics.append(Metric("host", "cpu_jiffies_total", sum(vals), "jiffies", "exact", entity_type="host", entity_id="local"))
    except Exception as exc:
        unavailable("cpu_jiffies_total", "jiffies")
        events.append(Event(ts, "warning", "proc_stat_unavailable", str(exc)))
    try:
        la = proc.joinpath("loadavg").read_text().split()
        for name, val in zip(("load1", "load5", "load15"), la[:3]):
            metrics.append(Metric("host", name, float(val), "load", "exact", entity_type="host", entity_id="local"))
    except Exception:
        unavailable("load1", "load")
    try:
        mem = read_key_values(proc / "meminfo")
        for key in ("MemTotal", "MemAvailable", "Buffers", "Cached", "SwapTotal", "SwapFree", "Dirty", "Writeback"):
            metrics.append(Metric("host", key.lower(), mem.get(key), "KiB", "exact" if key in mem else "unavailable", entity_type="host", entity_id="local"))
        total = mem.get("MemTotal")
        avail = mem.get("MemAvailable")
        metrics.append(Metric("host", "mem_used", None if total is None or avail is None else total - avail, "KiB", "derived" if total is not None and avail is not None else "unavailable", entity_type="host", entity_id="local"))
    except Exception:
        unavailable("memtotal", "KiB")
    try:
        for line in (proc / "net/dev").read_text().splitlines()[2:]:
            iface, rest = line.split(":", 1)
            iface = iface.strip()
            vals = rest.split()
            scope = "net.loopback" if iface == "lo" else "net.external"
            entity_id = f"interface:{iface}"
            for name, idx, unit in (("rx_bytes", 0, "bytes"), ("tx_bytes", 8, "bytes"), ("rx_packets", 1, "packets"), ("tx_packets", 9, "packets")):
                metrics.append(Metric(scope, name, int(vals[idx]), unit, "exact", {"interface": iface}, "interface", entity_id))
    except Exception:
        unavailable("net_dev")
    for fs_path in fs_paths:
        entity_id = f"fs:{fs_path}"
        try:
            st = os.statvfs(fs_path)
            metrics.append(Metric("fs", "free_bytes", st.f_bavail * st.f_frsize, "bytes", "exact", {"path": str(fs_path)}, "filesystem", entity_id))
            metrics.append(Metric("fs", "free_inodes", st.f_favail, "count", "exact", {"path": str(fs_path)}, "filesystem", entity_id))
        except Exception:
            metrics.append(Metric("fs", "free_bytes", None, "bytes", "unavailable", {"path": str(fs_path)}, "filesystem", entity_id))
    return metrics, events


def _cycle(conn: sqlite3.Connection, ts: int, started: float, finished: float, duration: float, success: bool, overrun: bool, missed: int = 0, overrun_seconds: float = 0.0) -> int:
    conn.execute("INSERT INTO sample_cycles(collected_at,monotonic_started,monotonic_finished,duration_seconds,success,overrun,missed_deadlines,overrun_seconds) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(collected_at) DO UPDATE SET monotonic_started=excluded.monotonic_started,monotonic_finished=excluded.monotonic_finished,duration_seconds=excluded.duration_seconds,success=excluded.success,overrun=excluded.overrun,missed_deadlines=excluded.missed_deadlines,overrun_seconds=excluded.overrun_seconds", (ts, started, finished, duration, int(success), int(overrun), int(missed), float(overrun_seconds)))
    return int(conn.execute("SELECT cycle_id FROM sample_cycles WHERE collected_at=?", (ts,)).fetchone()[0])


def insert_event(conn: sqlite3.Connection, event: Event) -> None:
    conn.execute("INSERT INTO events(ts,severity,code,message,details_json) VALUES(?,?,?,?,?)", (event.ts, event.severity, event.code, event.message, json.dumps(event.details, sort_keys=True)))


def upsert_tunnel(conn: sqlite3.Connection, tunnel: Tunnel, now: int) -> None:
    entity_pk = _ensure_entity(conn, "tunnel", tunnel.tunnel_id, tunnel.service_name, {"service": tunnel.service_name}, now)
    conn.execute(
        "INSERT INTO tunnels VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tunnel_id) DO UPDATE SET entity_pk=excluded.entity_pk,env_path=excluded.env_path,listen_ports_json=excluded.listen_ports_json,target_ports_json=excluded.target_ports_json,updated_at=excluded.updated_at",
        (tunnel.tunnel_id, entity_pk, tunnel.side, tunnel.number, tunnel.service_name, tunnel.env_path, json.dumps(tunnel.listen_ports), json.dumps(tunnel.target_ports), now),
    )


def insert_sample(conn: sqlite3.Connection, sample: MetricSample, cycle_id: int | None = None) -> int:
    cid = cycle_id if cycle_id is not None else _cycle(conn, sample.collected_at, float(sample.collected_at), float(sample.collected_at), 0.0, True, False)
    identity = sample.tunnel_id or "host"
    values = (cid, sample.tunnel_id, identity, sample.collected_at, sample.service_state, sample.service_substate, sample.restart_count, sample.listen_ports_total, sample.listen_ports_up, sample.configured_mappings_total, sample.rx_bytes, sample.tx_bytes)
    conn.execute("INSERT INTO metric_samples(cycle_id,tunnel_id,sample_identity,collected_at,service_state,service_substate,restart_count,listen_ports_total,listen_ports_up,configured_mappings_total,rx_bytes,tx_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cycle_id,sample_identity) DO UPDATE SET tunnel_id=excluded.tunnel_id,collected_at=excluded.collected_at,service_state=excluded.service_state,service_substate=excluded.service_substate,restart_count=excluded.restart_count,listen_ports_total=excluded.listen_ports_total,listen_ports_up=excluded.listen_ports_up,configured_mappings_total=excluded.configured_mappings_total,rx_bytes=excluded.rx_bytes,tx_bytes=excluded.tx_bytes", values)
    return int(conn.execute("SELECT sample_id FROM metric_samples WHERE cycle_id=? AND sample_identity=?", (cid, identity)).fetchone()[0])


def insert_metric(conn: sqlite3.Connection, sample_id: int, metric: Metric, cycle_id: int | None = None, ts: int | None = None) -> None:
    if cycle_id is None or ts is None:
        row = conn.execute("SELECT cycle_id,collected_at FROM metric_samples WHERE sample_id=?", (sample_id,)).fetchone()
        cycle_id = int(row[0])
        ts = int(row[1])
    entity_type = metric.entity_type or metric.scope
    entity_id = metric.entity_id or metric.scope
    entity_pk = _ensure_entity(conn, entity_type, entity_id, entity_id, metric.labels, ts)
    numeric = metric.value if isinstance(metric.value, (int, float)) else None
    text = metric.value if isinstance(metric.value, str) else None
    conn.execute("INSERT INTO metric_points(cycle_id,entity_pk,metric_name,ts,numeric_value,text_value,unit,quality,reset,gap) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cycle_id,entity_pk,metric_name) DO UPDATE SET ts=excluded.ts,numeric_value=excluded.numeric_value,text_value=excluded.text_value,unit=excluded.unit,quality=excluded.quality,reset=excluded.reset,gap=excluded.gap", (cycle_id, entity_pk, metric.name, ts, numeric, text, metric.unit, metric.quality, int(metric.reset), int(metric.gap)))


def quality_worst(qualities: Iterable[str]) -> str:
    return max(qualities, key=lambda q: QUALITY_RANK[q])


def _state_int(conn: sqlite3.Connection, key: str, default: int) -> int:
    row = conn.execute("SELECT value FROM collector_state WHERE key=?", (key,)).fetchone()
    return int(row[0]) if row else default


def _set_state(conn: sqlite3.Connection, key: str, value: int) -> None:
    conn.execute("INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)", (key, str(value)))


def rollup_completed_minutes(conn: sqlite3.Connection, now: int, interval: float = DEFAULT_SAMPLE_INTERVAL_SECONDS, batch_minutes: int = ROLLUP_BATCH_MINUTES) -> None:
    complete_before = (now // 60) * 60
    start = _state_int(conn, "minute_rollup_watermark", 0)
    first = conn.execute("SELECT MIN(ts) FROM metric_points").fetchone()[0]
    if first is None:
        _set_state(conn, "minute_rollup_watermark", complete_before)
        return
    if start <= 0:
        start = (int(first) // 60) * 60
    end = min(complete_before, start + batch_minutes * 60)
    if end <= start:
        return
    expected = max(1, int(60 / interval))
    groups: dict[tuple[int, str, int, str], list[sqlite3.Row]] = {}
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT entity_pk,metric_name,ts,numeric_value,unit,quality,reset,gap FROM metric_points WHERE ts>=? AND ts<?", (start, end)).fetchall()
    conn.row_factory = None
    for row in rows:
        key = (int(row["entity_pk"]), str(row["metric_name"]), (int(row["ts"]) // 60) * 60, str(row["unit"]))
        groups.setdefault(key, []).append(row)
    for (entity_pk, metric_name, minute, unit), points in groups.items():
        values = [float(p["numeric_value"]) for p in points if p["numeric_value"] is not None]
        q = quality_worst(str(p["quality"]) for p in points)
        unavailable = sum(1 for p in points if p["quality"] == "unavailable")
        resets = sum(int(p["reset"]) for p in points)
        gaps = sum(int(p["gap"]) for p in points)
        coverage = min(1.0, len(points) / expected)
        min_v = min(values) if values else None
        avg_v = (sum(values) / len(values)) if values else None
        max_v = max(values) if values else None
        conn.execute("INSERT OR REPLACE INTO minute_rollups(entity_pk,metric_name,minute_start,samples,expected_samples,min_value,avg_value,max_value,unavailable_count,reset_count,gap_count,coverage,unit,quality) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (entity_pk, metric_name, minute, len(points), expected, min_v, avg_v, max_v, unavailable, resets, gaps, coverage, unit, q))
    _set_state(conn, "minute_rollup_watermark", end)


def apply_retention(conn: sqlite3.Connection, now: int) -> None:
    conn.execute("DELETE FROM metric_samples WHERE collected_at < ?", (now - RAW_RETENTION_SECONDS,))
    conn.execute("DELETE FROM sample_cycles WHERE collected_at < ?", (now - RAW_RETENTION_SECONDS,))
    conn.execute("DELETE FROM metric_points WHERE ts < ?", (now - RAW_RETENTION_SECONDS,))
    conn.execute("DELETE FROM minute_rollups WHERE minute_start < ?", (now - ROLLUP_RETENTION_SECONDS,))


def run_maintenance(conn: sqlite3.Connection, now: int) -> None:
    rollup_completed_minutes(conn, now)
    apply_retention(conn, now)


def checkpoint_wal(db_path: str) -> tuple[int, int, int]:
    conn = connect_db(db_path)
    try:
        return tuple(int(x) for x in conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())  # type: ignore[return-value]
    finally:
        conn.close()


def record_cycle_overrun(db_path: str, ts: int, finished: float, deadline: float, interval: float) -> None:
    overrun_seconds = max(0.0, finished - (deadline + interval))
    if overrun_seconds <= 0:
        return
    missed = int(overrun_seconds // interval) + 1 if interval > 0 else 0
    conn = open_runtime_database(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE sample_cycles SET overrun=1, missed_deadlines=?, overrun_seconds=? WHERE collected_at=?",
            (missed, overrun_seconds, ts),
        )
        insert_event(conn, Event(ts, "warning", "collection_overrun", "Collection cycle exceeded its deadline", {"missed_deadlines": missed, "overrun_seconds": overrun_seconds}))
        row = conn.execute("SELECT cycle_id FROM sample_cycles WHERE collected_at=?", (ts,)).fetchone()
        if row:
            sid = insert_sample(conn, MetricSample(None, ts, 1, 1, 0, 0, 0, 0), int(row[0]))
            insert_metric(conn, sid, Metric("collector", "missed_deadlines", missed, "count", "exact", entity_type="collector", entity_id="local"), int(row[0]), ts)
            insert_metric(conn, sid, Metric("collector", "overrun_seconds", overrun_seconds, "seconds", "exact", entity_type="collector", entity_id="local"), int(row[0]), ts)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class CollectionCycleError(RuntimeError):
    def __init__(self, ts: int, message: str):
        super().__init__(message)
        self.ts = ts


def _record_checkpoint_result(db_path: str, ts: int, cycle_id: int, sample_id: int, result: tuple[int, int, int] | None, error: Exception | None, conn_factory: Callable[[str], sqlite3.Connection], event_writer: Callable[[sqlite3.Connection, Event], None], metric_writer: Callable[[sqlite3.Connection, int, Metric, int | None, int | None], None]) -> None:
    conn = conn_factory(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if error is None and result is not None:
            event_writer(conn, Event(ts, "info", "wal_checkpoint", "WAL checkpoint completed", {"busy": result[0], "log": result[1], "checkpointed": result[2]}))
            metric_writer(conn, sample_id, Metric("collector", "checkpoint_success", 1, "count", "exact", entity_type="collector", entity_id="local"), cycle_id, ts)
        else:
            event_writer(conn, Event(ts, "warning", "wal_checkpoint_failed", "WAL checkpoint failed after collection commit", {"error": str(error)}))
            metric_writer(conn, sample_id, Metric("collector", "checkpoint_success", 0, "count", "exact", entity_type="collector", entity_id="local"), cycle_id, ts)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def collect_once(db_path: str, env_dir: str, now: int | None = None, runner: Callable[[Sequence[str]], str] = _run, proc: Path = Path("/proc"), clock: Clock = Clock(), maintenance: bool = False, overrun: bool = False, missed_deadlines: int = 0, overrun_seconds: float = 0.0, checkpoint: Callable[[str], tuple[int, int, int]] = checkpoint_wal, maintenance_conn_factory: Callable[[str], sqlite3.Connection] = open_runtime_database, checkpoint_event_writer: Callable[[sqlite3.Connection, Event], None] = insert_event, checkpoint_metric_writer: Callable[[sqlite3.Connection, int, Metric, int | None, int | None], None] = insert_metric) -> int:
    ts = int(clock.wall() if now is None else now)
    started = clock.monotonic()
    if not Path(db_path).exists():
        migrate_database(db_path)
    conn = open_runtime_database(db_path)
    success = False
    try:
        tunnels, events = discover_tunnels(env_dir, clock)
        conn.execute("BEGIN IMMEDIATE")
        for event in events:
            insert_event(conn, event)
        finished = clock.monotonic()
        cycle_id = _cycle(conn, ts, started, finished, max(0.0, finished - started), True, overrun)
        ss_text = runner(["ss", "-H", "-lntp"]) if tunnels else ""
        listeners = parse_ss_listeners(ss_text)
        service_props = {t.service_name: parse_systemd_properties(runner(["systemctl", "--no-pager", "show", t.service_name, "--property=ActiveState,SubState,NRestarts,MainPID,ExecMainStartTimestampMonotonic"])) for t in tunnels}
        for tunnel in tunnels:
            upsert_tunnel(conn, tunnel, ts)
            sample, lq = collect_tunnel_observation(tunnel, ts, service_props[tunnel.service_name], listeners)
            sid = insert_sample(conn, sample, cycle_id)
            insert_metric(conn, sid, Metric(f"tunnel.{tunnel.tunnel_id}", "listen_ports_up", sample.listen_ports_up, "count", lq, entity_type="tunnel", entity_id=tunnel.tunnel_id), cycle_id, ts)
        host_metrics, host_events = collect_host_metrics(proc)
        sid = insert_sample(conn, MetricSample(None, ts, 1, 1, 0, 0, 0, 0), cycle_id)
        all_metrics = host_metrics + [Metric("collector", "duration_seconds", clock.monotonic() - started, "seconds", "derived", entity_type="collector", entity_id="local"), Metric("collector", "tunnels_discovered", len(tunnels), "count", "exact", entity_type="collector", entity_id="local")]
        for metric in all_metrics:
            insert_metric(conn, sid, metric, cycle_id, ts)
        for event in host_events:
            insert_event(conn, event)
        if maintenance:
            run_maintenance(conn, ts)
        finished = clock.monotonic()
        _cycle(conn, ts, started, finished, max(0.0, finished - started), True, overrun, missed_deadlines, overrun_seconds)
        conn.commit()
        success = True
        if maintenance:
            ckpt: tuple[int, int, int] | None = None
            ckpt_error: Exception | None = None
            try:
                ckpt = checkpoint(db_path)
            except Exception as exc:
                ckpt_error = exc
            try:
                _record_checkpoint_result(db_path, ts, cycle_id, sid, ckpt, ckpt_error, maintenance_conn_factory, checkpoint_event_writer, checkpoint_metric_writer)
            except Exception:
                pass
        return ts
    except Exception as exc:
        conn.rollback()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cycle_id = _cycle(conn, ts, started, clock.monotonic(), max(0.0, clock.monotonic() - started), False, overrun, missed_deadlines, overrun_seconds)
            insert_event(conn, Event(ts, "error", "collection_error", str(exc), {"cycle_id": cycle_id}))
            conn.commit()
        except Exception:
            conn.rollback()
        raise CollectionCycleError(ts, str(exc)) from exc
    finally:
        if not success:
            pass
        conn.close()


def scheduler_ticks(start: float, interval: float, durations: Sequence[float]) -> list[float]:
    ticks: list[float] = []
    deadline = start
    for duration in durations:
        ticks.append(deadline)
        end = deadline + duration
        deadline += interval
        while deadline < end:
            deadline += interval
    return ticks


def run_daemon(db_path: str, env_dir: str, interval: float = DEFAULT_SAMPLE_INTERVAL_SECONDS, maintenance_interval: float = MAINTENANCE_INTERVAL_SECONDS, runner: Callable[[Sequence[str]], str] = _run, clock: Clock = Clock(), sleeper: Callable[[float], None] = time.sleep, stop_requested: Callable[[], bool] | None = None) -> int:
    stop = False
    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True
    migrate_database(db_path)
    old_term = signal.signal(signal.SIGTERM, _stop)
    old_int = signal.signal(signal.SIGINT, _stop)
    try:
        deadline = clock.monotonic()
        next_maintenance = deadline
        while not stop and not (stop_requested and stop_requested()):
            now_mono = clock.monotonic()
            if now_mono < deadline:
                sleeper(deadline - now_mono)
                continue
            maintenance = now_mono >= next_maintenance
            if maintenance:
                next_maintenance = now_mono + maintenance_interval
            try:
                cycle_ts = collect_once(db_path, env_dir, runner=runner, clock=clock, maintenance=maintenance, overrun=False, missed_deadlines=0, overrun_seconds=0.0)
                finished = clock.monotonic()
                record_cycle_overrun(db_path, cycle_ts, finished, deadline, interval)
            except CollectionCycleError as exc:
                finished = clock.monotonic()
                try:
                    record_cycle_overrun(db_path, exc.ts, finished, deadline, interval)
                except Exception:
                    pass
                print(f"collection failed: {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"collection failed: {exc}", file=sys.stderr)
            deadline += interval
            current = clock.monotonic()
            while deadline < current:
                deadline += interval
        return 0
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.environ.get("GOST_MONITOR_DB", DEFAULT_DB_PATH))
    parser.add_argument("--env-dir", default=os.environ.get("GOST_ENV_DIR", DEFAULT_ENV_DIR))
    parser.add_argument("--now", type=int)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--daemon", action="store_true")
    args = parser.parse_args(argv)
    if args.once:
        try:
            migrate_database(args.db)
            collect_once(args.db, args.env_dir, args.now, maintenance=True)
            return 0
        except Exception as exc:
            print(f"collection failed: {exc}", file=sys.stderr)
            return 1
    return run_daemon(args.db, args.env_dir)


if __name__ == "__main__":
    raise SystemExit(main())
