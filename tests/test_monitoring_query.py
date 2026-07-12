import io
import json
import math
import os
import sqlite3
import stat
import tempfile
import time
import unittest
from pathlib import Path

from monitoring.exporters import CSV_FIELDS, ExportFilesystem, export_data
from monitoring.query_db import ReadOnlyDatabase
from monitoring.query_engine import QueryEngine, QueryLimits, cadence_for
from monitoring.query_models import QueryDatabaseError, QueryInputError, QueryLimitError
from monitoring.query_window import (
    RetentionPolicy,
    parse_duration,
    parse_timestamp,
    plan_window,
    resolve_window,
)
from monitoring.schema import _cycle, ensure_entity, init_db


NOW = 2_000_000_000


class QueryDatabaseFixture:
    def __init__(self, testcase):
        self.testcase = testcase
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "metrics.sqlite3")
        self.conn = init_db(self.path)
        self.conn.execute(
            "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)",
            (
                "metric_cadence_seconds",
                json.dumps(
                    {
                        "host:cpu_*": 5,
                        "service:established_sockets_total": 30,
                        "service:process_open_fds": 60,
                    }
                ),
            ),
        )

    def close(self):
        self.conn.close()
        self.temp.cleanup()

    def entity(self, kind="host", entity_id="local"):
        return ensure_entity(self.conn, kind, entity_id, entity_id, {}, NOW)

    def point(
        self,
        ts,
        name,
        value,
        unit="percent",
        quality="exact",
        kind="host",
        entity_id="local",
        reset=0,
        gap=0,
    ):
        cycle = _cycle(self.conn, ts, float(ts), float(ts) + 0.1, 0.1, True, False)
        entity_pk = self.entity(kind, entity_id)
        numeric = value if isinstance(value, (int, float)) else None
        text = value if isinstance(value, str) else None
        self.conn.execute(
            "INSERT OR REPLACE INTO metric_points(cycle_id,entity_pk,metric_name,ts,"
            "numeric_value,text_value,unit,quality,reset,gap) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (cycle, entity_pk, name, ts, numeric, text, unit, quality, reset, gap),
        )

    def rollup(
        self,
        minute,
        name,
        average,
        samples=12,
        expected=12,
        quality="exact",
        kind="host",
        entity_id="local",
        unit="percent",
        unavailable=0,
        reset=0,
        gap=0,
    ):
        entity_pk = self.entity(kind, entity_id)
        self.conn.execute(
            "INSERT OR REPLACE INTO minute_rollups(entity_pk,metric_name,minute_start,samples,"
            "expected_samples,min_value,avg_value,max_value,unavailable_count,reset_count,"
            "gap_count,coverage,unit,quality) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entity_pk,
                name,
                minute,
                samples,
                expected,
                average,
                average,
                average,
                unavailable,
                reset,
                gap,
                samples / expected if expected else 0,
                unit,
                quality,
            ),
        )
        self.conn.execute(
            "INSERT INTO collector_state(key,value) VALUES('minute_rollup_watermark',?) "
            "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS INTEGER),excluded.value)",
            (str(minute + 60),),
        )

    def cycle(self, ts=NOW - 2, success=True):
        _cycle(self.conn, ts, float(ts), float(ts) + 0.1, 0.1, success, False)

    def event(self, ts, severity, code, message, details=None):
        self.conn.execute(
            "INSERT INTO events(ts,severity,code,message,details_json) VALUES(?,?,?,?,?)",
            (ts, severity, code, message, json.dumps(details or {})),
        )

    def engine(self, limits=QueryLimits()):
        return QueryEngine(ReadOnlyDatabase(self.path), clock=lambda: NOW, limits=limits)


class WindowTests(unittest.TestCase):
    def test_duration_parser_accepts_explicit_units(self):
        self.assertEqual(
            [90, 900, 7200, 86400],
            [parse_duration(v) for v in ("90s", "15m", "2h", "24h")],
        )

    def test_duration_parser_rejects_ambiguous_zero_and_oversized(self):
        for value in ("10", "0s", "-1h", "1.5h", "25h", "2d", "999999999s"):
            with self.subTest(value=value), self.assertRaises(QueryInputError):
                parse_duration(value)

    def test_timestamp_accepts_z_and_offset(self):
        self.assertEqual(
            parse_timestamp("2026-07-11T10:00:00Z"),
            parse_timestamp("2026-07-11T12:00:00+02:00"),
        )

    def test_timestamp_rejects_local_time(self):
        with self.assertRaises(QueryInputError):
            parse_timestamp("2026-07-11T10:00:00")

    def test_window_validation_and_truncation(self):
        policy = RetentionPolicy(raw_seconds=100, rollup_seconds=1000, event_seconds=1000)
        result = resolve_window(NOW, "900s", retention=policy)
        self.assertFalse(result.truncated)
        result = resolve_window(
            NOW,
            start="2033-05-18T03:16:30Z",
            end="2033-05-18T03:32:30Z",
            retention=policy,
        )
        self.assertTrue(result.truncated)
        self.assertEqual(NOW - 1000, result.effective_start)

    def test_invalid_future_and_partial_absolute_windows(self):
        with self.assertRaises(QueryInputError):
            resolve_window(NOW, start="2033-05-18T04:00:00Z", end="2033-05-18T05:00:00Z")
        with self.assertRaises(QueryInputError):
            resolve_window(NOW, start="2026-07-11T10:00:00Z")

    def test_planner_selects_raw_rollup_and_hybrid(self):
        policy = RetentionPolicy(raw_seconds=100, rollup_seconds=1000, event_seconds=1000)
        raw = resolve_window(NOW, "50s", retention=policy)
        old = resolve_window(NOW, "300s", retention=policy)
        rollup_window = type(old)(old.requested_start, NOW - 200, old.effective_start, NOW - 200)
        self.assertEqual("raw", plan_window(raw, NOW, policy).source_mode)
        self.assertEqual("rollup", plan_window(rollup_window, NOW, policy).source_mode)
        self.assertEqual("hybrid", plan_window(old, NOW, policy).source_mode)


class QueryEngineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = QueryDatabaseFixture(self)

    def tearDown(self):
        self.fixture.close()

    def test_irregular_time_weighted_average_seed_max_gap_and_p95(self):
        start = NOW - 30
        self.fixture.point(start - 5, "cpu_utilization_percent", 10)
        self.fixture.point(start + 5, "cpu_utilization_percent", 20)
        self.fixture.point(start + 25, "cpu_utilization_percent", 40)
        item = self.fixture.engine().summary(resolve_window(NOW, "30s")).series[0]
        self.assertAlmostEqual(490 / 22, item.average)
        self.assertEqual(40, item.p95)
        self.assertEqual(22, item.weighted_seconds)
        self.assertEqual((2, 6), (item.sample_count, item.expected_sample_count))

    def test_cadence_matching_and_sparse_coverage(self):
        self.assertEqual(30, cadence_for("service", "established_sockets_total", {"service:established_sockets_total": 30}))
        self.assertEqual(60, cadence_for("service", "process_open_fds", {"service:process_*": 60}))
        self.fixture.point(NOW - 50, "established_sockets_total", 3, "count", kind="service", entity_id="nginx.service")
        item = self.fixture.engine().summary(resolve_window(NOW, "60s")).series[0]
        self.assertEqual(2, item.expected_sample_count)
        self.assertEqual(0.5, item.coverage)

    def test_expected_samples_for_fast_socket_and_slow_families(self):
        self.fixture.point(NOW - 5, "cpu_utilization_percent", 10)
        self.fixture.point(NOW - 30, "established_sockets_total", 2, "count", kind="service", entity_id="nginx.service")
        self.fixture.point(NOW - 30, "process_open_fds", 20, "count", kind="service", entity_id="nginx.service")
        items = {
            item.metric_name: item
            for item in self.fixture.engine().summary(resolve_window(NOW, "60s")).series
        }
        self.assertEqual(12, items["cpu_utilization_percent"].expected_sample_count)
        self.assertEqual(2, items["established_sockets_total"].expected_sample_count)
        self.assertEqual(1, items["process_open_fds"].expected_sample_count)

    def test_unavailable_only_series_remains_visible(self):
        self.fixture.point(NOW - 5, "cpu_utilization_percent", None, quality="unavailable")
        item = self.fixture.engine().summary(resolve_window(NOW, "10s")).series[0]
        self.assertIsNone(item.average)
        self.assertEqual("unavailable", item.quality)
        self.assertEqual(1, item.unavailable_count)

    def test_unavailable_reset_gap_and_worst_quality(self):
        self.fixture.point(NOW - 10, "cpu_utilization_percent", None, quality="unavailable", reset=1, gap=1)
        self.fixture.point(NOW - 5, "cpu_utilization_percent", 25, quality="estimated")
        item = self.fixture.engine().summary(resolve_window(NOW, "20s")).series[0]
        self.assertEqual("unavailable", item.quality)
        self.assertEqual((1, 1, 1), (item.unavailable_count, item.reset_count, item.gap_count))

    def test_categorical_raw_has_transitions_not_numeric_summary(self):
        for offset, value in ((15, "active"), (10, "failed"), (5, "active")):
            self.fixture.point(NOW - offset, "service_active_state", value, "state", kind="service", entity_id="nginx.service")
        item = self.fixture.engine().summary(resolve_window(NOW, "20s")).series[0]
        self.assertFalse(item.numeric)
        self.assertEqual("active", item.latest)
        self.assertEqual(2, item.transition_count)
        self.assertIsNone(item.average)
        self.assertIsNone(item.p95)

    def test_rollup_p95_unavailable_and_counts_preserved(self):
        minute = ((NOW - 12 * 3600) // 60) * 60
        self.fixture.rollup(minute, "cpu_utilization_percent", 30, samples=10, expected=12, unavailable=2, reset=1, gap=1)
        window = resolve_window(
            NOW,
            start=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(minute)),
            end=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(minute + 60)),
        )
        item = self.fixture.engine().summary(window).series[0]
        self.assertEqual("rollup", item.source_mode)
        self.assertIsNone(item.p95)
        self.assertEqual((10, 12, 2, 1, 1), (item.sample_count, item.expected_sample_count, item.unavailable_count, item.reset_count, item.gap_count))

    def test_partial_rollup_minutes_are_not_fabricated(self):
        minute = ((NOW - 12 * 3600) // 60) * 60
        self.fixture.rollup(minute, "cpu_utilization_percent", 30)
        self.fixture.rollup(minute + 60, "cpu_utilization_percent", 90)
        window = resolve_window(
            NOW,
            start=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(minute + 30)),
            end=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(minute + 120)),
        )
        item = self.fixture.engine().summary(window).series[0]
        self.assertEqual(90, item.average)
        self.assertEqual(12, item.sample_count)
        self.assertEqual(18, item.expected_sample_count)
        self.assertAlmostEqual(2 / 3, item.coverage)

    def test_rollup_categorical_is_unavailable_not_fabricated(self):
        minute = ((NOW - 12 * 3600) // 60) * 60
        self.fixture.rollup(minute, "service_active_state", None, unit="state", kind="service", entity_id="nginx.service")
        window = resolve_window(NOW, start=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(minute)), end=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(minute + 60)))
        item = self.fixture.engine().summary(window).series[0]
        self.assertFalse(item.numeric)
        self.assertEqual("unavailable", item.quality)
        self.assertIsNone(item.latest)

    def test_rollup_reports_current_categorical_family_as_historically_unavailable(self):
        minute = ((NOW - 12 * 3600) // 60) * 60
        self.fixture.rollup(minute, "cpu_utilization_percent", 30)
        self.fixture.point(NOW - 5, "service_active_state", "active", "state", kind="service", entity_id="nginx.service")
        window = resolve_window(NOW, start=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(minute)), end=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(minute + 60)))
        items = {item.metric_name: item for item in self.fixture.engine().summary(window).series}
        self.assertIn("service_active_state", items)
        self.assertEqual("unavailable", items["service_active_state"].quality)
        self.assertFalse(items["service_active_state"].numeric)

    def test_hybrid_boundary_has_no_double_count_and_no_p95(self):
        policy = RetentionPolicy(raw_seconds=120, rollup_seconds=1000, event_seconds=1000)
        cutoff = NOW - 120
        boundary = math.floor(cutoff / 60) * 60
        self.fixture.rollup(boundary - 60, "cpu_utilization_percent", 10, samples=12)
        self.fixture.point(cutoff, "cpu_utilization_percent", 20)
        self.fixture.point(cutoff + 5, "cpu_utilization_percent", 30)
        engine = QueryEngine(ReadOnlyDatabase(self.fixture.path), clock=lambda: NOW, retention=policy)
        result = engine.summary(resolve_window(NOW, "300s", retention=policy))
        item = result.series[0]
        self.assertEqual("hybrid", result.source_mode)
        self.assertEqual(14, item.sample_count)
        self.assertIsNone(item.p95)

    def test_hybrid_coverage_counts_missing_rollup_side(self):
        policy = RetentionPolicy(raw_seconds=120, rollup_seconds=1000, event_seconds=1000)
        boundary = math.ceil((NOW - 120) / 60) * 60
        self.fixture.point(boundary + 5, "cpu_utilization_percent", 20)
        engine = QueryEngine(ReadOnlyDatabase(self.fixture.path), clock=lambda: NOW, retention=policy)
        item = engine.summary(resolve_window(NOW, "180s", retention=policy)).series[0]
        self.assertEqual(36, item.expected_sample_count)
        self.assertEqual(1 / 36, item.coverage)

    def test_query_limits_and_no_match(self):
        self.fixture.point(NOW - 5, "cpu_utilization_percent", 20)
        with self.assertRaises(QueryLimitError):
            self.fixture.engine(QueryLimits(max_materialized_rows=0)).summary(resolve_window(NOW, "10s"))
        with self.assertRaises(QueryInputError):
            self.fixture.engine().summary(resolve_window(NOW, "10s"), entity_type="service", require_match=True)

    def test_read_only_trace_contains_no_mutation(self):
        self.fixture.point(NOW - 5, "cpu_utilization_percent", 20)
        statements = []
        engine = QueryEngine(ReadOnlyDatabase(self.fixture.path, trace_callback=statements.append), clock=lambda: NOW)
        engine.summary(resolve_window(NOW, "10s"))
        forbidden = ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "REPLACE", "VACUUM", "WAL_CHECKPOINT")
        self.assertFalse([sql for sql in statements if sql.lstrip().upper().startswith(forbidden)])
        self.assertTrue(any("QUERY_ONLY=ON" in sql.upper() for sql in statements))

    def test_event_filters_are_parameterized_and_secret_safe(self):
        self.fixture.event(
            NOW - 5,
            "warning",
            "source_failed",
            "token=canary-value source failed",
            {"source": "proc_stat", "password": "canary-value"},
        )
        events = self.fixture.engine().events(resolve_window(NOW, "10s"), ["warning"])
        self.assertEqual(1, len(events))
        self.assertNotIn("canary-value", events[0].message)
        self.assertNotIn("password", events[0].details)

    def test_query_plan_uses_existing_lookup_index(self):
        self.fixture.point(NOW - 5, "cpu_utilization_percent", 20)
        rows = self.fixture.conn.execute(
            "EXPLAIN QUERY PLAN SELECT e.entity_type,p.metric_name,p.ts FROM metric_points p "
            "JOIN entities e ON e.entity_pk=p.entity_pk WHERE p.ts>=? AND p.ts<? "
            "AND e.entity_type=? ORDER BY e.entity_type,e.entity_id,p.metric_name,p.ts LIMIT ?",
            (NOW - 10, NOW, "host", 100),
        ).fetchall()
        details = " ".join(str(row[3]) for row in rows)
        self.assertIn("idx_metric_points_lookup", details)

    def test_concurrent_writer_and_reader(self):
        self.fixture.point(NOW - 5, "cpu_utilization_percent", 20)
        self.fixture.conn.execute("BEGIN IMMEDIATE")
        try:
            result = self.fixture.engine().summary(resolve_window(NOW, "10s"))
        finally:
            self.fixture.conn.rollback()
        self.assertEqual(1, len(result.series))


class DatabaseFailureTests(unittest.TestCase):
    def test_valid_empty_database_returns_empty_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "empty-valid.db")
            conn = init_db(path)
            conn.close()
            engine = QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW)
            self.assertEqual([], engine.summary(resolve_window(NOW, "10s")).series)

    def test_missing_empty_corrupt_and_wrong_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("missing.db", "empty.db", "corrupt.db", "wrong.db")]
            paths[1].touch()
            paths[2].write_bytes(b"not sqlite")
            conn = sqlite3.connect(paths[3])
            conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER)")
            conn.execute("INSERT INTO schema_migrations VALUES(3,1)")
            conn.commit()
            conn.close()
            for path in paths:
                with self.subTest(path=path.name), self.assertRaises(QueryDatabaseError):
                    with ReadOnlyDatabase(str(path)).connection():
                        pass
            self.assertFalse(paths[0].exists())


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = QueryDatabaseFixture(self)
        self.fixture.point(NOW - 5, "cpu_utilization_percent", 20)

    def tearDown(self):
        self.fixture.close()

    def test_json_metadata_rows_and_private_atomic_file(self):
        output = str(Path(self.fixture.temp.name) / "export.json")
        metadata = export_data(self.fixture.engine(), resolve_window(NOW, "10s"), output, "json", "raw")
        payload = json.loads(Path(output).read_text())
        self.assertEqual(1, metadata["row_count"])
        self.assertEqual(1, payload["metadata"]["row_count"])
        self.assertEqual(
            {
                "database_schema_version",
                "effective_window",
                "export_version",
                "filters",
                "generated_at",
                "generated_at_utc",
                "granularity",
                "requested_window",
                "retention",
                "row_count",
                "source_mode",
                "truncated",
            },
            set(payload["metadata"]),
        )
        self.assertEqual("cpu_utilization_percent", payload["rows"][0]["metric_name"])
        self.assertEqual(0o600, stat.S_IMODE(os.stat(output).st_mode))
        self.assertFalse(list(Path(self.fixture.temp.name).glob(".export.json.*")))

    def test_csv_has_fixed_schema(self):
        output = io.StringIO()
        export_data(self.fixture.engine(), resolve_window(NOW, "10s"), "-", "csv", "raw", stdout=output)
        self.assertEqual(",".join(CSV_FIELDS), output.getvalue().splitlines()[0])

    def test_secret_canary_is_redacted(self):
        self.fixture.point(NOW - 4, "remote_endpoint", "token=canary-value", "endpoint", kind="tunnel", entity_id="iran-1")
        output = io.StringIO()
        export_data(self.fixture.engine(), resolve_window(NOW, "10s"), "-", "json", "raw", stdout=output)
        self.assertNotIn("canary-value", output.getvalue())
        self.assertIn("[redacted]", output.getvalue())

    def test_row_limit_is_checked_before_file_creation(self):
        output = str(Path(self.fixture.temp.name) / "too-many.json")
        engine = self.fixture.engine(QueryLimits(max_export_rows=0))
        with self.assertRaises(QueryLimitError):
            export_data(engine, resolve_window(NOW, "10s"), output, "json", "raw")
        self.assertFalse(Path(output).exists())

    def test_replace_failure_cleans_temporary_file(self):
        output = str(Path(self.fixture.temp.name) / "failed.json")
        def fail_replace(_source, _target):
            raise OSError("injected replace failure")
        with self.assertRaises(OSError):
            export_data(self.fixture.engine(), resolve_window(NOW, "10s"), output, "json", "raw", replace=fail_replace)
        self.assertFalse(Path(output).exists())
        self.assertFalse(list(Path(self.fixture.temp.name).glob(".failed.json.*")))

    def test_actual_row_growth_is_bounded_and_cleans_file(self):
        output = str(Path(self.fixture.temp.name) / "grew.json")
        engine = self.fixture.engine()
        engine.database.count_export_rows = lambda *args, **kwargs: 0
        with self.assertRaises(QueryLimitError):
            export_data(engine, resolve_window(NOW, "10s"), output, "json", "raw")
        self.assertFalse(Path(output).exists())

    def test_filesystem_setup_failure_cleans_temporary_file(self):
        output = str(Path(self.fixture.temp.name) / "chmod-failed.json")
        def fail_chmod(_path, _mode):
            raise OSError("injected chmod failure")
        filesystem = ExportFilesystem(chmod=fail_chmod)
        with self.assertRaises(OSError):
            export_data(
                self.fixture.engine(),
                resolve_window(NOW, "10s"),
                output,
                "json",
                "raw",
                filesystem=filesystem,
            )
        self.assertFalse(Path(output).exists())
        self.assertFalse(list(Path(self.fixture.temp.name).glob(".chmod-failed.json.*")))


class RepresentativeQueryTests(unittest.TestCase):
    def test_one_hour_fixture_has_bounded_statements_and_runtime(self):
        fixture = QueryDatabaseFixture(self)
        try:
            entity_specs = [("host", "local"), ("service", "nginx.service")]
            entity_specs.extend(("service", f"gost-iran-{index}.service") for index in range(1, 7))
            for offset in range(3600, 0, -5):
                ts = NOW - offset
                for kind, entity_id in entity_specs:
                    fixture.point(ts, "cpu_utilization_percent", float(offset % 100), kind=kind, entity_id=entity_id)
            for offset in range(3600, 0, -30):
                ts = NOW - offset
                for index in range(1, 4):
                    fixture.point(ts, "rx_bytes_per_second", offset, "bytes_per_second", kind="interface", entity_id=f"interface:eth{index}")
                for index in range(1, 7):
                    fixture.point(ts, "established_remote_sockets", index, "count", kind="tunnel", entity_id=f"iran-{index}")
            for offset in range(3600, 0, -60):
                ts = NOW - offset
                for _kind, entity_id in entity_specs[1:]:
                    fixture.point(ts, "process_open_fds", 20, "count", kind="service", entity_id=entity_id)
            statements = []
            engine = QueryEngine(ReadOnlyDatabase(fixture.path, trace_callback=statements.append), clock=lambda: NOW)
            started = time.monotonic()
            result = engine.summary(resolve_window(NOW, "1h"))
            elapsed = time.monotonic() - started
            selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
            self.assertEqual(24, len(result.series))
            self.assertLessEqual(len(selects), 6)
            self.assertLess(elapsed, 5.0)
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
