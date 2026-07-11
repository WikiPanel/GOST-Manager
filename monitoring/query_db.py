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
    def list_entities(
        conn: sqlite3.Connection,
        entity_type: str | None = None,
    ) -> list[dict[str, object]]:
        sql = "SELECT entity_type,entity_id,display_name,metadata_json,updated_at FROM entities"
        params: list[object] = []
        if entity_type is not None:
            sql += " WHERE entity_type=?"
            params.append(validate_filter(entity_type, "entity type"))
        sql += " ORDER BY entity_type,entity_id"
        rows = []
        for row in conn.execute(sql, params):
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
        entity_type: str | None = None,
        entity_id: str | None = None,
        metric_names: Sequence[str] | None = None,
    ) -> list[MetricPoint]:
        filters, params = self._filter_clause(entity_type, entity_id, metric_names)
        sql = (
            "SELECT e.entity_type,e.entity_id,p.metric_name,p.ts,p.numeric_value,"
            "p.text_value,p.unit,p.quality,p.reset,p.gap FROM metric_points p "
            "JOIN entities e ON e.entity_pk=p.entity_pk "
            "WHERE p.ts>=? AND p.ts<?" + filters + " ORDER BY e.entity_type,e.entity_id,p.metric_name,p.ts LIMIT ?"
        )
        query_params: list[object] = [start - seed_seconds, end]
        query_params.extend(params)
        query_params.append(max_rows + 1)
        rows = conn.execute(sql, query_params).fetchall()
        if len(rows) > max_rows:
            raise QueryLimitError("raw query exceeds the safe row limit")
        return [MetricPoint(*tuple(row)) for row in rows]

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
    ) -> list[MetricPoint]:
        filters, params = self._filter_clause(entity_type, entity_id, metric_names)
        sql = (
            "SELECT e.entity_type,e.entity_id,p.metric_name,p.ts,p.numeric_value,"
            "p.text_value,p.unit,p.quality,p.reset,p.gap FROM metric_points p "
            "JOIN entities e ON e.entity_pk=p.entity_pk WHERE p.cycle_id=("
            "SELECT cycle_id FROM sample_cycles ORDER BY collected_at DESC LIMIT 1)"
            + filters
            + " ORDER BY e.entity_type,e.entity_id,p.metric_name LIMIT ?"
        )
        params.append(max_rows + 1)
        rows = conn.execute(sql, params).fetchall()
        if len(rows) > max_rows:
            raise QueryLimitError("snapshot exceeds the safe series limit")
        return [MetricPoint(*tuple(row)) for row in rows]

    @staticmethod
    def events(
        conn: sqlite3.Connection,
        start: int,
        end: int,
        max_rows: int,
        severities: Sequence[str] | None = None,
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
            raise QueryLimitError("event query exceeds the safe row limit")
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

    @staticmethod
    def count_rows(
        conn: sqlite3.Connection,
        table: str,
        time_column: str,
        start: int,
        end: int,
    ) -> int:
        allowed = {
            ("metric_points", "ts"),
            ("minute_rollups", "minute_start"),
            ("events", "ts"),
        }
        if (table, time_column) not in allowed:
            raise QueryInputError("invalid export source")
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {time_column}>=? AND {time_column}<?",
                (start, end),
            ).fetchone()[0]
        )

    def count_export_rows(
        self,
        conn: sqlite3.Connection,
        source: str,
        start: int,
        end: int,
        entity_type: str | None = None,
        entity_id: str | None = None,
        metric_names: Sequence[str] | None = None,
    ) -> int:
        filters, params = self._filter_clause(entity_type, entity_id, metric_names)
        if source == "raw":
            table, time_column = "metric_points", "ts"
        elif source == "rollup":
            table, time_column = "minute_rollups", "minute_start"
        else:
            raise QueryInputError("invalid export source")
        query_params: list[object] = [start, end]
        query_params.extend(params)
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} p "
                "JOIN entities e ON e.entity_pk=p.entity_pk "
                f"WHERE p.{time_column}>=? AND p.{time_column}<?" + filters,
                query_params,
            ).fetchone()[0]
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
                "NULL AS samples,NULL AS expected_samples,NULL AS coverage"
            )
        elif source == "rollup":
            table = "minute_rollups"
            time_column = "minute_start"
            columns = (
                "e.entity_type,e.entity_id,p.metric_name,NULL AS timestamp,p.minute_start,"
                "p.avg_value AS numeric_value,NULL AS text_value,p.unit,p.quality,"
                "p.reset_count AS reset,p.gap_count AS gap,p.samples,p.expected_samples,p.coverage"
            )
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
        )
        if estimated > max_rows:
            raise QueryLimitError(
                f"export estimate {estimated} rows exceeds the safe limit {max_rows}"
            )
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
                    "source_mode": source,
                }
