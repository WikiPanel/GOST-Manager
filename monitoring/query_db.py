"""Read-only SQLite access for monitoring queries."""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from urllib.parse import quote

from monitoring.query_models import (
    EventRecord,
    MetricPoint,
    QueryDatabaseError,
    QueryInputError,
    QueryLimitError,
    RollupPoint,
)
from monitoring.schema import SCHEMA_VERSION, sanitize_mapping, sanitize_text

SAFE_FILTER_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
REQUIRED_TABLES = {
    "schema_migrations",
    "sample_cycles",
    "entities",
    "metric_points",
    "minute_rollups",
    "events",
    "collector_state",
}

SNAPSHOT_METRICS_BY_ENTITY = {
    "host": (
        "cpu_utilization_percent", "memory_used_percent", "memory_used_bytes",
        "memory_available_bytes", "swap_used_bytes", "load1", "load5", "load15",
        "conntrack_utilization_percent", "file_handles_utilization_percent",
        "tcp_state_estab", "tcp_state_syn_sent", "tcp_state_syn_recv",
        "tcp_state_close_wait", "tcp_state_time_wait",
        "tcp_retransmitted_segments_per_second", "tcp_listen_drops",
        "tcp_listen_overflows",
    ),
    "filesystem": ("filesystem_used_percent",),
    "interface": (
        "rx_bytes_per_second", "tx_bytes_per_second", "rx_packets_per_second",
        "tx_packets_per_second", "rx_errors", "tx_errors", "rx_drops", "tx_drops",
    ),
    "service": (
        "service_active", "service_active_state", "process_cpu_percent",
        "process_rss_bytes", "process_count", "process_open_fds",
        "listener_owned_count", "established_sockets_total", "service_restart_count",
    ),
    "tunnel": (
        "service_active", "configured_listener_count", "observed_listener_count",
        "listener_ownership_exact", "remote_endpoint", "established_remote_sockets",
        "process_cpu_percent", "process_rss_bytes", "process_open_fds",
        "service_restart_count",
    ),
    "collector": (
        "cycle_status", "duration_seconds", "missed_deadlines", "source_errors_total",
        "database_size_bytes", "database_wal_size_bytes", "checkpoint_success",
        "checkpoint_duration_seconds", "last_successful_cycle_timestamp",
    ),
}

HEALTH_EVENT_CODES = (
    "collection_failed", "collection_recovered",
    "database_retention_failed", "database_retention_recovered",
    "wal_checkpoint_failed", "wal_checkpoint_recovered",
    "metric_source_unavailable", "metric_source_available",
    "service_state_changed", "pid_replaced",
    "listener_disappeared", "listener_returned", "sampling_gap",
    "env_parse_error", "env_parse_recovered",
)


def validate_filter(value: str, label: str) -> str:
    if not SAFE_FILTER_RE.fullmatch(value):
        raise QueryInputError(f"invalid {label} filter")
    return value


class ReadOnlyDatabase:
    def __init__(
        self,
        path: str,
        connect: Callable[..., sqlite3.Connection] = sqlite3.connect,
        exists: Callable[[Path], bool] = Path.exists,
        busy_timeout_ms: int = 5_000,
        trace_callback: Callable[[str], None] | None = None,
    ):
        self.path = path
        self._connect = connect
        self._exists = exists
        self.busy_timeout_ms = busy_timeout_ms
        self.trace_callback = trace_callback

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        path = Path(self.path)
        if not self._exists(path):
            raise QueryDatabaseError(f"monitoring database does not exist: {self.path}")
        uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect(
                uri,
                uri=True,
                timeout=self.busy_timeout_ms / 1000.0,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            if self.trace_callback is not None:
                conn.set_trace_callback(self.trace_callback)
            conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
            conn.execute("PRAGMA query_only=ON")
            self._validate(conn)
            conn.execute("BEGIN")
        except QueryDatabaseError:
            if conn is not None:
                conn.close()
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            if conn is not None:
                conn.close()
            raise QueryDatabaseError(f"monitoring database is unavailable: {exc}") from exc
        try:
            assert conn is not None
            yield conn
        except sqlite3.DatabaseError as exc:
            raise QueryDatabaseError(f"monitoring query failed: {exc}") from exc
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    @staticmethod
    def _validate(conn: sqlite3.Connection) -> None:
        try:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            version = int(row[0] or 0)
        except sqlite3.DatabaseError as exc:
            raise QueryDatabaseError("monitoring database has no supported schema") from exc
        if version != SCHEMA_VERSION:
            raise QueryDatabaseError(
                f"unsupported monitoring schema version {version}; expected {SCHEMA_VERSION}"
            )
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = REQUIRED_TABLES - tables
        if missing:
            raise QueryDatabaseError(
                "monitoring database is missing required tables: " + ", ".join(sorted(missing))
            )

    @staticmethod
    def schema_version(conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])

    @staticmethod
    def cadence_registry(conn: sqlite3.Connection) -> dict[str, float]:
        row = conn.execute(
            "SELECT value FROM collector_state WHERE key='metric_cadence_seconds'"
        ).fetchone()
        if not row:
            return {}
        try:
            value = json.loads(str(row[0]))
        except json.JSONDecodeError:
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            str(key): float(seconds)
            for key, seconds in value.items()
            if isinstance(seconds, (int, float)) and float(seconds) > 0
        }

    @staticmethod
    def rollup_watermark_status(
        conn: sqlite3.Connection,
        now: int,
    ) -> tuple[int | None, str]:
        row = conn.execute(
            "SELECT value FROM collector_state WHERE key='minute_rollup_watermark'"
        ).fetchone()
        if not row:
            return None, "missing"
        try:
            value = int(str(row[0]))
        except ValueError:
            return None, "invalid_nonnumeric"
        if value < 0:
            return None, "invalid_negative"
        if value % 60:
            return None, "invalid_misaligned"
        completed = (int(now) // 60) * 60
        if value > completed:
            return None, "invalid_future"
        return value, "valid"

    @staticmethod
    def rollup_watermark(
        conn: sqlite3.Connection,
        now: int | None = None,
    ) -> int | None:
        if now is None:
            row = conn.execute(
                "SELECT value FROM collector_state WHERE key='minute_rollup_watermark'"
            ).fetchone()
            if not row:
                return None
            try:
                value = int(str(row[0]))
            except ValueError:
                return None
            return value if value >= 0 and value % 60 == 0 else None
        return ReadOnlyDatabase.rollup_watermark_status(conn, now)[0]

    @staticmethod
    def current_entities(
        conn: sqlite3.Connection,
        max_rows: int = 256,
    ) -> list[dict[str, object]]:
        rows = conn.execute(
            "WITH last_success AS ("
            "SELECT cycle_id FROM sample_cycles WHERE success=1 "
            "ORDER BY collected_at DESC LIMIT 1), current_tunnels AS ("
            "SELECT DISTINCT p.entity_pk FROM metric_points p "
            "JOIN entities e ON e.entity_pk=p.entity_pk JOIN last_success c "
            "ON c.cycle_id=p.cycle_id WHERE e.entity_type='tunnel' "
            "AND p.metric_name='env_source_valid' AND p.numeric_value=1 "
            "AND p.quality='exact'), current_entities AS ("
            "SELECT entity_pk FROM current_tunnels UNION "
            "SELECT DISTINCT p.entity_pk FROM metric_points p JOIN entities e "
            "ON e.entity_pk=p.entity_pk JOIN last_success c ON c.cycle_id=p.cycle_id "
            "WHERE e.entity_type='service' AND p.metric_name='service_active' UNION "
            "SELECT service.entity_pk FROM current_tunnels current "
            "JOIN tunnels t ON t.entity_pk=current.entity_pk JOIN entities service "
            "ON service.entity_type='service' AND service.entity_id=t.service_name) "
            "SELECT e.entity_type,e.entity_id,e.display_name,e.metadata_json,e.updated_at "
            "FROM entities e JOIN current_entities c ON c.entity_pk=e.entity_pk "
            "ORDER BY e.entity_type,e.entity_id LIMIT ?",
            (max_rows + 1,),
        ).fetchall()
        if len(rows) > max_rows:
            raise QueryLimitError("current entity query exceeds the safe row limit")
        result = []
        for row in rows:
            try:
                metadata = json.loads(str(row[3] or "{}"))
            except json.JSONDecodeError:
                metadata = {}
            result.append(
                {
                    "entity_type": str(row[0]),
                    "entity_id": str(row[1]),
                    "display_name": row[2],
                    "metadata": sanitize_mapping(metadata) if isinstance(metadata, dict) else {},
                    "updated_at": int(row[4]),
                }
            )
        return result

    @staticmethod
    def optional_source_states(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
        rows = conn.execute(
            "SELECT entity_id,metadata_json,updated_at FROM entities "
            "WHERE entity_type='collector_source' AND entity_id='conntrack'"
        ).fetchall()
        result: dict[str, dict[str, object]] = {}
        for row in rows:
            try:
                metadata = json.loads(str(row[1] or "{}"))
            except json.JSONDecodeError:
                continue
            if not isinstance(metadata, dict):
                continue
            state = metadata.get("state")
            if state not in {"available", "unsupported", "failed"}:
                continue
            result[str(row[0])] = {
                "state": str(state),
                "updated_at": int(row[2]),
            }
        return result

    @staticmethod
    def invalid_managed_env_sources(
        conn: sqlite3.Connection,
        max_rows: int = 64,
    ) -> list[dict[str, object]]:
        rows = conn.execute(
            "WITH last_success AS (SELECT cycle_id FROM sample_cycles WHERE success=1 "
            "ORDER BY collected_at DESC LIMIT 1) "
            "SELECT e.entity_id,e.metadata_json,p.ts FROM metric_points p "
            "JOIN entities e ON e.entity_pk=p.entity_pk JOIN last_success c "
            "ON c.cycle_id=p.cycle_id WHERE e.entity_type='tunnel_source' "
            "AND p.metric_name='env_source_valid' AND p.numeric_value=0 "
            "AND p.quality='exact' ORDER BY e.entity_id LIMIT ?",
            (max_rows + 1,),
        ).fetchall()
        if len(rows) > max_rows:
            raise QueryLimitError("invalid managed env source query exceeds the safe limit")
        result = []
        for row in rows:
            try:
                metadata = json.loads(str(row[1] or "{}"))
            except json.JSONDecodeError:
                metadata = {}
            safe = sanitize_mapping(metadata) if isinstance(metadata, dict) else {}
            result.append(
                {
                    "source_id": str(row[0]),
                    "path": safe.get("path"),
                    "observed_at": int(row[2]),
                }
            )
        return result

    @staticmethod
    def list_entities(
        conn: sqlite3.Connection,
        entity_type: str | Sequence[str] | None = None,
        entity_id: str | None = None,
        max_rows: int = 256,
    ) -> list[dict[str, object]]:
        sql = "SELECT entity_type,entity_id,display_name,metadata_json,updated_at FROM entities"
        params: list[object] = []
        clauses: list[str] = []
        if isinstance(entity_type, str):
            clauses.append("entity_type=?")
            params.append(validate_filter(entity_type, "entity type"))
        elif entity_type:
            values = [validate_filter(value, "entity type") for value in entity_type]
            clauses.append("entity_type IN (" + ",".join("?" for _ in values) + ")")
            params.extend(values)
        if entity_id is not None:
            clauses.append("entity_id=?")
            params.append(validate_filter(entity_id, "entity id"))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY entity_type,entity_id LIMIT ?"
        params.append(max_rows + 1)
        rows = []
        selected = conn.execute(sql, params).fetchall()
        if len(selected) > max_rows:
            raise QueryLimitError("entity query exceeds the safe row limit")
        for row in selected:
            try:
                metadata = json.loads(str(row[3] or "{}"))
            except json.JSONDecodeError:
                metadata = {}
            rows.append(
                {
                    "entity_type": str(row[0]),
                    "entity_id": str(row[1]),
                    "display_name": row[2],
                    "metadata": sanitize_mapping(metadata) if isinstance(metadata, dict) else {},
                    "updated_at": int(row[4]),
                }
            )
        return rows

    @staticmethod
    def _filter_clause(
        entity_type: str | None,
        entity_id: str | None,
        metric_names: Sequence[str] | None,
        alias: str = "e",
    ) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        if entity_type is not None:
            clauses.append(f"{alias}.entity_type=?")
            params.append(validate_filter(entity_type, "entity type"))
        if entity_id is not None:
            clauses.append(f"{alias}.entity_id=?")
            params.append(validate_filter(entity_id, "entity id"))
        if metric_names:
            if len(metric_names) > 100:
                raise QueryLimitError("metric filter exceeds the safe limit of 100 names")
            values = [validate_filter(name, "metric") for name in metric_names]
            clauses.append("p.metric_name IN (" + ",".join("?" for _ in values) + ")")
            params.extend(values)
        return (" AND " + " AND ".join(clauses) if clauses else ""), params

    def raw_points(
        self,
        conn: sqlite3.Connection,
        start: int,
        end: int,
        seed_seconds: int,
        max_rows: int,
        max_seed_rows: int,
        entity_type: str | None = None,
        entity_id: str | None = None,
        metric_names: Sequence[str] | None = None,
    ) -> list[MetricPoint]:
        filters, params = self._filter_clause(entity_type, entity_id, metric_names)
        columns = (
            "e.entity_type,e.entity_id,p.metric_name,p.ts,p.numeric_value,"
            "p.text_value,p.unit,p.quality,p.reset,p.gap"
        )
        sql = (
            "WITH in_window AS (SELECT " + columns + " FROM metric_points p "
            "JOIN entities e ON e.entity_pk=p.entity_pk WHERE p.ts>=? AND p.ts<?"
            + filters
            + "), seed_ranked AS (SELECT " + columns
            + ",ROW_NUMBER() OVER(PARTITION BY p.entity_pk,p.metric_name ORDER BY p.ts DESC) rn "
            "FROM metric_points p JOIN entities e ON e.entity_pk=p.entity_pk "
            "WHERE p.ts>=? AND p.ts<?" + filters
            + ") SELECT entity_type,entity_id,metric_name,ts,numeric_value,text_value,unit,quality,reset,gap "
            "FROM (SELECT *,0 rn FROM in_window UNION ALL SELECT * FROM seed_ranked WHERE rn=1) "
            "ORDER BY entity_type,entity_id,metric_name,ts LIMIT ?"
        )
        query_params: list[object] = [start, end]
        query_params.extend(params)
        query_params.extend([start - seed_seconds, start])
        query_params.extend(params)
        query_params.append(max_rows + max_seed_rows + 1)
        rows = conn.execute(sql, query_params).fetchall()
        if len(rows) > max_rows + max_seed_rows:
            raise QueryLimitError("raw query exceeds the safe row limit")
        return [MetricPoint(*tuple(row)) for row in rows]

    def iter_raw_points(
        self,
        conn: sqlite3.Connection,
        start: int,
        end: int,
        seed_seconds: int,
        entity_type: str | None = None,
        entity_id: str | None = None,
        metric_names: Sequence[str] | None = None,
        batch_size: int = 500,
    ) -> Iterator[MetricPoint]:
        filters, params = self._filter_clause(entity_type, entity_id, metric_names)
        columns = (
            "e.entity_type,e.entity_id,p.metric_name,p.ts,p.numeric_value,"
            "p.text_value,p.unit,p.quality,p.reset,p.gap"
        )
        sql = (
            "WITH in_window AS (SELECT " + columns + " FROM metric_points p "
            "JOIN entities e ON e.entity_pk=p.entity_pk WHERE p.ts>=? AND p.ts<?"
            + filters
            + "), seed_ranked AS (SELECT " + columns
            + ",ROW_NUMBER() OVER(PARTITION BY p.entity_pk,p.metric_name ORDER BY p.ts DESC) rn "
            "FROM metric_points p JOIN entities e ON e.entity_pk=p.entity_pk "
            "WHERE p.ts>=? AND p.ts<?" + filters
            + ") SELECT entity_type,entity_id,metric_name,ts,numeric_value,text_value,"
            "unit,quality,reset,gap FROM (SELECT *,0 rn FROM in_window UNION ALL "
            "SELECT * FROM seed_ranked WHERE rn=1) "
            "ORDER BY entity_type,entity_id,metric_name,ts"
        )
        query_params: list[object] = [start, end]
        query_params.extend(params)
        query_params.extend([start - seed_seconds, start])
        query_params.extend(params)
        cursor = conn.execute(sql, query_params)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            for row in rows:
                yield MetricPoint(*tuple(row))

    def bounded_point_count(
        self,
        conn: sqlite3.Connection,
        source: str,
        start: int,
        end: int,
        limit: int,
        entity_type: str | None = None,
        entity_id: str | None = None,
        metric_names: Sequence[str] | None = None,
    ) -> int:
        filters, params = self._filter_clause(entity_type, entity_id, metric_names)
        if source == "raw":
            table, column = "metric_points", "ts"
        elif source == "rollup":
            table, column = "minute_rollups", "minute_start"
        elif source == "raw_minute":
            query_params: list[object] = [start, end]
            query_params.extend(params)
            query_params.append(limit + 1)
            row = conn.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM metric_points p "
                "JOIN entities e ON e.entity_pk=p.entity_pk WHERE p.ts>=? AND p.ts<?"
                + filters
                + " GROUP BY p.entity_pk,p.metric_name,(p.ts/60) LIMIT ?)",
                query_params,
            ).fetchone()
            return int(row[0])
        else:
            raise QueryInputError("invalid query source")
        query_params: list[object] = [start, end]
        query_params.extend(params)
        query_params.append(limit + 1)
        row = conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM " + table + " p "
            "JOIN entities e ON e.entity_pk=p.entity_pk WHERE p." + column
            + ">=? AND p." + column + "<?" + filters + " LIMIT ?)",
            query_params,
        ).fetchone()
        return int(row[0])

    def rollup_points(
        self,
        conn: sqlite3.Connection,
        start: int,
        end: int,
        max_rows: int,
        entity_type: str | None = None,
        entity_id: str | None = None,
        metric_names: Sequence[str] | None = None,
    ) -> list[RollupPoint]:
        filters, params = self._filter_clause(entity_type, entity_id, metric_names)
        sql = (
            "SELECT e.entity_type,e.entity_id,p.metric_name,p.minute_start,p.samples,"
            "p.expected_samples,p.min_value,p.avg_value,p.max_value,p.unavailable_count,"
            "p.reset_count,p.gap_count,p.coverage,p.unit,p.quality FROM minute_rollups p "
            "JOIN entities e ON e.entity_pk=p.entity_pk "
            "WHERE p.minute_start>=? AND p.minute_start<?" + filters + " ORDER BY e.entity_type,e.entity_id,p.metric_name,p.minute_start LIMIT ?"
        )
        query_params: list[object] = [start, end]
        query_params.extend(params)
        query_params.append(max_rows + 1)
        rows = conn.execute(sql, query_params).fetchall()
        if len(rows) > max_rows:
            raise QueryLimitError("rollup query exceeds the safe row limit")
        return [RollupPoint(*tuple(row)) for row in rows]

    def latest_points(
        self,
        conn: sqlite3.Connection,
        max_rows: int,
        entity_type: str | None = None,
        entity_id: str | None = None,
        metric_names: Sequence[str] | None = None,
        max_entities: int = 256,
        current_only: bool = False,
    ) -> list[MetricPoint]:
        entity_clauses: list[str] = []
        entity_params: list[object] = []
        if entity_type is not None:
            entity_clauses.append("entity_type=?")
            entity_params.append(validate_filter(entity_type, "entity type"))
        else:
            kinds = tuple(SNAPSHOT_METRICS_BY_ENTITY)
            entity_clauses.append("entity_type IN (" + ",".join("?" for _ in kinds) + ")")
            entity_params.extend(kinds)
        if entity_id is not None:
            entity_clauses.append("entity_id=?")
            entity_params.append(validate_filter(entity_id, "entity id"))
        entity_where = " AND ".join(entity_clauses)
        membership = ""
        membership_ctes = ""
        if current_only:
            membership_ctes = (
                "last_success AS (SELECT cycle_id FROM sample_cycles WHERE success=1 "
                "ORDER BY collected_at DESC LIMIT 1),current_tunnels AS ("
                "SELECT DISTINCT p.entity_pk FROM metric_points p JOIN entities ce "
                "ON ce.entity_pk=p.entity_pk JOIN last_success c ON c.cycle_id=p.cycle_id "
                "WHERE ce.entity_type='tunnel' AND p.metric_name='env_source_valid' "
                "AND p.numeric_value=1 AND p.quality='exact'),current_membership AS ("
                "SELECT entity_pk FROM current_tunnels UNION SELECT DISTINCT p.entity_pk "
                "FROM metric_points p JOIN entities ce ON ce.entity_pk=p.entity_pk "
                "JOIN last_success c ON c.cycle_id=p.cycle_id WHERE ce.entity_type='service' "
                "AND p.metric_name='service_active' UNION SELECT service.entity_pk "
                "FROM current_tunnels current JOIN tunnels t ON t.entity_pk=current.entity_pk "
                "JOIN entities service ON service.entity_type='service' "
                "AND service.entity_id=t.service_name),"
            )
            membership = (
                " AND (entity_type NOT IN ('service','tunnel','interface') "
                "OR (entity_type='interface' AND entity_id IN "
                "('interface:external-total','interface:lo')) OR entity_pk IN "
                "(SELECT entity_pk FROM current_membership))"
            )
        count_params = [*entity_params, max_entities + 1]
        entity_count = len(
            conn.execute(
                ("WITH " + membership_ctes[:-1] + " " if membership_ctes else "")
                + "SELECT 1 FROM entities WHERE " + entity_where + membership + " LIMIT ?",
                count_params,
            ).fetchall()
        )
        if entity_count > max_entities:
            raise QueryLimitError("snapshot entity count exceeds the safe limit")

        specs: list[tuple[str, str]] = []
        if metric_names:
            if len(metric_names) > 100:
                raise QueryLimitError("metric filter exceeds the safe limit of 100 names")
            kinds = (entity_type,) if entity_type else tuple(SNAPSHOT_METRICS_BY_ENTITY)
            specs = [
                (str(kind), validate_filter(name, "metric"))
                for kind in kinds
                for name in metric_names
            ]
        elif entity_type:
            specs = [(entity_type, name) for name in SNAPSHOT_METRICS_BY_ENTITY.get(entity_type, ())]
        else:
            specs = [
                (kind, name)
                for kind, names in SNAPSHOT_METRICS_BY_ENTITY.items()
                for name in names
            ]
        if not specs:
            return []
        values_sql = ",".join("(?,?)" for _ in specs)
        spec_params = [value for spec in specs for value in spec]
        sql = (
            "WITH " + membership_ctes + "desired(entity_type,metric_name) AS (VALUES " + values_sql + "),"
            "active AS (SELECT entity_pk,entity_type,entity_id FROM entities WHERE "
            + entity_where
            + membership
            + "), latest AS (SELECT a.entity_type,a.entity_id,d.metric_name,("
            "SELECT p.point_id FROM metric_points p WHERE p.entity_pk=a.entity_pk "
            "AND p.metric_name=d.metric_name ORDER BY p.ts DESC LIMIT 1) point_id "
            "FROM active a JOIN desired d ON d.entity_type=a.entity_type) "
            "SELECT l.entity_type,l.entity_id,l.metric_name,p.ts,p.numeric_value,p.text_value,"
            "p.unit,p.quality,p.reset,p.gap FROM latest l JOIN metric_points p "
            "ON p.point_id=l.point_id WHERE l.point_id IS NOT NULL "
            "ORDER BY l.entity_type,l.entity_id,l.metric_name LIMIT ?"
        )
        rows = conn.execute(sql, [*spec_params, *entity_params, max_rows + 1]).fetchall()
        if len(rows) > max_rows:
            raise QueryLimitError("snapshot exceeds the safe series limit")
        return [MetricPoint(*tuple(row)) for row in rows]

    @staticmethod
    def health_events(
        conn: sqlite3.Connection,
        start: int,
        end: int,
        max_rows: int,
    ) -> tuple[list[EventRecord], bool]:
        params: list[object] = [start, end, *HEALTH_EVENT_CODES, max_rows + 1]
        rows = conn.execute(
            "SELECT ts,severity,code,message,details_json FROM events WHERE ts>=? AND ts<? "
            "AND code IN (" + ",".join("?" for _ in HEALTH_EVENT_CODES) + ") "
            "ORDER BY ts DESC,event_id DESC LIMIT ?",
            params,
        ).fetchall()
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        result: list[EventRecord] = []
        for row in rows:
            try:
                details = json.loads(str(row[4] or "{}"))
            except json.JSONDecodeError:
                details = {}
            result.append(
                EventRecord(
                    int(row[0]), str(row[1]), str(row[2]),
                    sanitize_text(str(row[3])),
                    sanitize_mapping(details) if isinstance(details, dict) else {},
                )
            )
        return result, truncated

    @staticmethod
    def events(
        conn: sqlite3.Connection,
        start: int,
        end: int,
        max_rows: int,
        severities: Sequence[str] | None = None,
        truncate: bool = False,
    ) -> list[EventRecord]:
        clauses = ["ts>=?", "ts<?"]
        params: list[object] = [start, end]
        if severities:
            values = [validate_filter(value, "severity") for value in severities]
            clauses.append("severity IN (" + ",".join("?" for _ in values) + ")")
            params.extend(values)
        params.append(max_rows + 1)
        rows = conn.execute(
            "SELECT ts,severity,code,message,details_json FROM events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY ts DESC,event_id DESC LIMIT ?",
            params,
        ).fetchall()
        if len(rows) > max_rows:
            if not truncate:
                raise QueryLimitError("event query exceeds the safe row limit")
            rows = rows[:max_rows]
        result: list[EventRecord] = []
        for row in rows:
            try:
                details = json.loads(str(row[4] or "{}"))
            except json.JSONDecodeError:
                details = {}
            result.append(
                EventRecord(
                    int(row[0]),
                    str(row[1]),
                    str(row[2]),
                    sanitize_text(str(row[3])),
                    sanitize_mapping(details) if isinstance(details, dict) else {},
                )
            )
        return result

    @staticmethod
    def latest_cycle(conn: sqlite3.Connection) -> dict[str, object] | None:
        row = conn.execute(
            "SELECT collected_at,duration_seconds,success,overrun,missed_deadlines,"
            "overrun_seconds FROM sample_cycles ORDER BY collected_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "collected_at": int(row[0]),
            "duration_seconds": float(row[1]),
            "success": bool(row[2]),
            "overrun": bool(row[3]),
            "missed_deadlines": int(row[4]),
            "overrun_seconds": float(row[5]),
        }

    def count_export_rows(
        self,
        conn: sqlite3.Connection,
        source: str,
        start: int,
        end: int,
        entity_type: str | None = None,
        entity_id: str | None = None,
        metric_names: Sequence[str] | None = None,
        limit: int = 100_000,
    ) -> int:
        return self.bounded_point_count(
            conn, source, start, end, limit,
            entity_type, entity_id, metric_names,
        )

    def iter_export_rows(
        self,
        conn: sqlite3.Connection,
        source: str,
        start: int,
        end: int,
        max_rows: int,
        entity_type: str | None = None,
        entity_id: str | None = None,
        metric_names: Sequence[str] | None = None,
        batch_size: int = 500,
    ) -> Iterator[dict[str, object]]:
        filters, params = self._filter_clause(entity_type, entity_id, metric_names)
        if source == "raw":
            table = "metric_points"
            time_column = "ts"
            columns = (
                "e.entity_type,e.entity_id,p.metric_name,p.ts AS timestamp,NULL AS minute_start,"
                "p.numeric_value,p.text_value,p.unit,p.quality,p.reset,p.gap,"
                "NULL AS samples,NULL AS expected_samples,NULL AS coverage,NULL AS unavailable_count,"
                "NULL AS minimum,NULL AS average,NULL AS maximum,p.numeric_value AS latest_numeric,"
                "p.text_value AS latest_text,p.ts AS latest_timestamp"
            )
        elif source == "rollup":
            table = "minute_rollups"
            time_column = "minute_start"
            columns = (
                "e.entity_type,e.entity_id,p.metric_name,NULL AS timestamp,p.minute_start,"
                "p.avg_value AS numeric_value,NULL AS text_value,p.unit,p.quality,"
                "p.reset_count AS reset,p.gap_count AS gap,p.samples,p.expected_samples,p.coverage,"
                "p.unavailable_count,p.min_value,p.avg_value,p.max_value,NULL,NULL,NULL"
            )
        elif source == "raw_minute":
            table = "metric_points"
            time_column = "ts"
            columns = ""
        else:
            raise QueryInputError("invalid export source")
        query_params: list[object] = [start, end]
        query_params.extend(params)
        estimated = self.count_export_rows(
            conn,
            source,
            start,
            end,
            entity_type,
            entity_id,
            metric_names,
            max_rows,
        )
        if estimated > max_rows:
            raise QueryLimitError(
                f"export estimate {estimated} rows exceeds the safe limit {max_rows}"
            )
        if source == "raw_minute":
            cursor = conn.execute(
                "WITH ranked AS (SELECT p.*,e.entity_type,e.entity_id,(p.ts/60)*60 minute,"
                "ROW_NUMBER() OVER(PARTITION BY p.entity_pk,p.metric_name,(p.ts/60) "
                "ORDER BY p.ts DESC,p.point_id DESC) latest_rank FROM metric_points p "
                "JOIN entities e ON e.entity_pk=p.entity_pk WHERE p.ts>=? AND p.ts<?"
                + filters
                + ") SELECT r.entity_type,r.entity_id,r.metric_name,NULL,r.minute,"
                "AVG(CASE WHEN r.quality!='unavailable' THEN r.numeric_value END),NULL,"
                "MAX(r.unit),CASE MAX(CASE r.quality WHEN 'exact' THEN 0 WHEN 'derived' "
                "THEN 1 WHEN 'estimated' THEN 2 ELSE 3 END) WHEN 0 THEN 'exact' "
                "WHEN 1 THEN 'derived' WHEN 2 THEN 'estimated' ELSE 'unavailable' END,"
                "SUM(r.reset),SUM(r.gap),COUNT(*),NULL,NULL,"
                "SUM(CASE WHEN r.quality='unavailable' OR "
                "(r.numeric_value IS NULL AND r.text_value IS NULL) THEN 1 ELSE 0 END),"
                "MIN(CASE WHEN r.quality!='unavailable' THEN r.numeric_value END),"
                "AVG(CASE WHEN r.quality!='unavailable' THEN r.numeric_value END),"
                "MAX(CASE WHEN r.quality!='unavailable' THEN r.numeric_value END),"
                "MAX(CASE WHEN r.latest_rank=1 THEN r.numeric_value END),"
                "MAX(CASE WHEN r.latest_rank=1 THEN r.text_value END),"
                "MAX(CASE WHEN r.latest_rank=1 THEN r.ts END) FROM ranked r "
                "GROUP BY r.entity_pk,r.metric_name,r.minute "
                "ORDER BY r.minute,r.entity_type,r.entity_id,r.metric_name",
                query_params,
            )
        else:
            cursor = conn.execute(
                f"SELECT {columns} FROM {table} p JOIN entities e ON e.entity_pk=p.entity_pk "
                f"WHERE p.{time_column}>=? AND p.{time_column}<?"
                + filters
                + f" ORDER BY p.{time_column},e.entity_type,e.entity_id,p.metric_name",
                query_params,
            )
        emitted = 0
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                emitted += 1
                if emitted > max_rows:
                    raise QueryLimitError("export exceeded the safe actual-row limit")
                yield {
                    "entity_type": str(row[0]),
                    "entity_id": str(row[1]),
                    "metric_name": str(row[2]),
                    "timestamp": None if row[3] is None else int(row[3]),
                    "minute_start": None if row[4] is None else int(row[4]),
                    "numeric_value": None if row[5] is None else float(row[5]),
                    "text_value": None if row[6] is None else str(row[6]),
                    "unit": str(row[7]),
                    "quality": str(row[8]),
                    "reset": int(row[9]),
                    "gap": int(row[10]),
                    "samples": None if row[11] is None else int(row[11]),
                    "expected_samples": None if row[12] is None else int(row[12]),
                    "coverage": None if row[13] is None else float(row[13]),
                    "unavailable_count": (
                        int(row[14]) if len(row) > 14 and row[14] is not None else None
                    ),
                    "minimum": None if row[15] is None else float(row[15]),
                    "average": None if row[16] is None else float(row[16]),
                    "maximum": None if row[17] is None else float(row[17]),
                    "latest_numeric": None if row[18] is None else float(row[18]),
                    "latest_text": None if row[19] is None else str(row[19]),
                    "latest_timestamp": None if row[20] is None else int(row[20]),
                    "source_mode": source,
                }
