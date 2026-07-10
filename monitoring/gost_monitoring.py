#!/usr/bin/env python3
"""Deterministic monitoring collector core for GOST Manager.

This module intentionally uses only the Python standard library.  It can be
imported by tests or executed as a small CLI for one-shot collection,
retention, and rollup maintenance.  It does not start services, change tunnel
configuration, integrate with menus, or implement a dashboard.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Callable, Sequence

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = "/var/lib/gost-manager/monitoring.sqlite3"
DEFAULT_ENV_DIR = "/etc/gost"
RAW_RETENTION_SECONDS = 7 * 24 * 3600
HOURLY_RETENTION_SECONDS = 90 * 24 * 3600
DAILY_RETENTION_SECONDS = 370 * 24 * 3600

METRIC_FIELDS = (
    "service_state",
    "service_substate",
    "restart_count",
    "listen_ports_total",
    "listen_ports_up",
    "configured_mappings_total",
    "rx_bytes",
    "tx_bytes",
)

CREATE_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tunnels (
  tunnel_id TEXT PRIMARY KEY,
  side TEXT NOT NULL CHECK (side IN ('iran','kharej')),
  tunnel_number INTEGER NOT NULL CHECK (tunnel_number > 0),
  service_name TEXT NOT NULL UNIQUE,
  env_path TEXT NOT NULL,
  listen_ports_json TEXT NOT NULL DEFAULT '[]',
  target_ports_json TEXT NOT NULL DEFAULT '[]',
  updated_at INTEGER NOT NULL,
  UNIQUE(side, tunnel_number)
);
CREATE TABLE IF NOT EXISTS metric_samples (
  sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
  tunnel_id TEXT NOT NULL REFERENCES tunnels(tunnel_id) ON DELETE CASCADE,
  collected_at INTEGER NOT NULL,
  service_state INTEGER NOT NULL,
  service_substate INTEGER NOT NULL,
  restart_count INTEGER NOT NULL DEFAULT 0,
  listen_ports_total INTEGER NOT NULL DEFAULT 0,
  listen_ports_up INTEGER NOT NULL DEFAULT 0,
  configured_mappings_total INTEGER NOT NULL DEFAULT 0,
  rx_bytes INTEGER NOT NULL DEFAULT 0,
  tx_bytes INTEGER NOT NULL DEFAULT 0,
  UNIQUE(tunnel_id, collected_at)
);
CREATE INDEX IF NOT EXISTS idx_metric_samples_time ON metric_samples(collected_at);
CREATE TABLE IF NOT EXISTS metric_rollups (
  tunnel_id TEXT NOT NULL REFERENCES tunnels(tunnel_id) ON DELETE CASCADE,
  bucket_start INTEGER NOT NULL,
  bucket_size INTEGER NOT NULL CHECK (bucket_size IN (3600,86400)),
  samples INTEGER NOT NULL,
  service_state_avg REAL NOT NULL,
  service_substate_avg REAL NOT NULL,
  restart_count_max INTEGER NOT NULL,
  listen_ports_total_max INTEGER NOT NULL,
  listen_ports_up_avg REAL NOT NULL,
  configured_mappings_total_max INTEGER NOT NULL,
  rx_bytes_max INTEGER NOT NULL,
  tx_bytes_max INTEGER NOT NULL,
  PRIMARY KEY(tunnel_id, bucket_start, bucket_size)
);
CREATE INDEX IF NOT EXISTS idx_metric_rollups_time ON metric_rollups(bucket_size, bucket_start);
"""

SERVICE_RE = re.compile(r"^gost-(iran|kharej)-([1-9][0-9]*)\.service$")
PORT_RE = re.compile(r"(?<!\d)([1-9][0-9]{0,4})(?!\d)")

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
class MetricSample:
    tunnel_id: str
    collected_at: int
    service_state: int
    service_substate: int
    restart_count: int
    listen_ports_total: int
    listen_ports_up: int
    configured_mappings_total: int
    rx_bytes: int = 0
    tx_bytes: int = 0


def parse_env_file(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def parse_service_name(service_name: str) -> tuple[str, int]:
    match = SERVICE_RE.match(service_name)
    if not match:
        raise ValueError(f"unsupported service name: {service_name}")
    return match.group(1), int(match.group(2))


def parse_ports_from_text(text: str) -> tuple[int, ...]:
    ports = []
    for match in PORT_RE.finditer(text):
        port = int(match.group(1))
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return tuple(ports)


def parse_mappings(value: str) -> tuple[tuple[int, int], ...]:
    mappings = []
    for item in [part.strip() for part in value.split(",") if part.strip()]:
        if ":" not in item:
            continue
        listen, target = item.split(":", 1)
        if listen.isdigit() and target.isdigit():
            lp, tp = int(listen), int(target)
            if 1 <= lp <= 65535 and 1 <= tp <= 65535:
                mappings.append((lp, tp))
    return tuple(mappings)


def tunnel_from_env(path: str | Path) -> Tunnel:
    env_path = Path(path)
    side, number = parse_service_name(f"gost-{env_path.stem}.service")
    values = parse_env_file(env_path)
    mappings = parse_mappings(values.get("PORT_MAPPINGS", ""))
    if mappings:
        listen_ports = tuple(port for port, _ in mappings)
        target_ports = tuple(port for _, port in mappings)
    else:
        listen_ports = parse_ports_from_text(" ".join(values.values()))
        target_ports = ()
    return Tunnel(side, number, f"gost-{side}-{number}.service", str(env_path), listen_ports, target_ports)


def discover_tunnels(env_dir: str | Path = DEFAULT_ENV_DIR) -> list[Tunnel]:
    root = Path(env_dir)
    if not root.exists():
        return []
    tunnels = []
    for path in sorted(root.glob("*.env")):
        if SERVICE_RE.match(f"gost-{path.stem}.service"):
            tunnels.append(tunnel_from_env(path))
    return tunnels


def init_db(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(CREATE_SCHEMA)
    conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (SCHEMA_VERSION, int(time.time())))
    conn.commit()
    return conn


def upsert_tunnel(conn: sqlite3.Connection, tunnel: Tunnel, now: int) -> None:
    conn.execute(
        """INSERT INTO tunnels(tunnel_id, side, tunnel_number, service_name, env_path, listen_ports_json, target_ports_json, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(tunnel_id) DO UPDATE SET env_path=excluded.env_path, listen_ports_json=excluded.listen_ports_json,
           target_ports_json=excluded.target_ports_json, updated_at=excluded.updated_at""",
        (tunnel.tunnel_id, tunnel.side, tunnel.number, tunnel.service_name, tunnel.env_path,
         json.dumps(tunnel.listen_ports), json.dumps(tunnel.target_ports), now),
    )


def insert_sample(conn: sqlite3.Connection, sample: MetricSample) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO metric_samples(tunnel_id,collected_at,service_state,service_substate,restart_count,
           listen_ports_total,listen_ports_up,configured_mappings_total,rx_bytes,tx_bytes) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        dataclasses.astuple(sample),
    )


def _run(command: Sequence[str]) -> str:
    return subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout


def collect_sample(tunnel: Tunnel, now: int | None = None, runner: Callable[[Sequence[str]], str] = _run) -> MetricSample:
    collected_at = int(time.time() if now is None else now)
    props = runner(["systemctl", "show", tunnel.service_name, "--property=ActiveState,SubState,NRestarts", "--no-page"])
    active = "ActiveState=active" in props
    running = "SubState=running" in props
    restarts = 0
    match = re.search(r"(?:NRestarts|RestartCount)=(\d+)", props)
    if match:
        restarts = int(match.group(1))
    sockets = runner(["ss", "-lnt"])
    up = sum(1 for port in tunnel.listen_ports if re.search(rf"[:.]({port})\b", sockets))
    return MetricSample(tunnel.tunnel_id, collected_at, int(active), int(running), restarts,
                        len(tunnel.listen_ports), up, len(tunnel.target_ports))


def rollup(conn: sqlite3.Connection, bucket_size: int, before: int) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO metric_rollups
           SELECT tunnel_id, (collected_at / ?) * ?, ?, COUNT(*), AVG(service_state), AVG(service_substate),
                  MAX(restart_count), MAX(listen_ports_total), AVG(listen_ports_up), MAX(configured_mappings_total),
                  MAX(rx_bytes), MAX(tx_bytes)
           FROM metric_samples WHERE collected_at < ? GROUP BY tunnel_id, (collected_at / ?)""",
        (bucket_size, bucket_size, bucket_size, before, bucket_size),
    )


def apply_retention(conn: sqlite3.Connection, now: int) -> None:
    rollup(conn, 3600, now)
    rollup(conn, 86400, now)
    conn.execute("DELETE FROM metric_samples WHERE collected_at < ?", (now - RAW_RETENTION_SECONDS,))
    conn.execute("DELETE FROM metric_rollups WHERE bucket_size = 3600 AND bucket_start < ?", (now - HOURLY_RETENTION_SECONDS,))
    conn.execute("DELETE FROM metric_rollups WHERE bucket_size = 86400 AND bucket_start < ?", (now - DAILY_RETENTION_SECONDS,))


def collect_once(db_path: str, env_dir: str, now: int | None = None) -> int:
    ts = int(time.time() if now is None else now)
    conn = init_db(db_path)
    try:
        for tunnel in discover_tunnels(env_dir):
            upsert_tunnel(conn, tunnel, ts)
            insert_sample(conn, collect_sample(tunnel, ts))
        apply_retention(conn, ts)
        conn.commit()
    finally:
        conn.close()
    return ts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GOST Manager monitoring collector core")
    parser.add_argument("--db", default=os.environ.get("GOST_MONITOR_DB", DEFAULT_DB_PATH))
    parser.add_argument("--env-dir", default=os.environ.get("GOST_ENV_DIR", DEFAULT_ENV_DIR))
    parser.add_argument("--now", type=int)
    args = parser.parse_args(argv)
    collect_once(args.db, args.env_dir, args.now)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
