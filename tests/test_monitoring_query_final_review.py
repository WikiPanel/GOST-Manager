#!/usr/bin/env python3
"""Final PR #16 correctness regressions."""

from __future__ import annotations

import csv
import io
import json
import math
import tempfile
import unittest
from pathlib import Path

from monitoring.collector import CollectorConfig, collect_once
from monitoring.exporters import export_data
from monitoring.health import evaluate_snapshot
from monitoring.metric_semantics import (
    CATEGORICAL,
    CUMULATIVE_COUNTER,
    GAUGE,
    IDENTITY,
    RATE,
    TIMESTAMP,
    UNKNOWN,
    classify_metric,
)
from monitoring.query_db import ReadOnlyDatabase
from monitoring.query_engine import QueryEngine, QueryLimits
from monitoring.query_models import QueryWindow
from monitoring.query_window import resolve_window
from monitoring.renderers import render_summary
from monitoring.schema import _cycle, connect_db, ensure_entity, init_db

try:
    from test_monitoring_coverage import (
        FIXTURES,
        fixture,
        integration_sources,
        write_tunnel_env,
    )
    from test_monitoring_query_ui import representative_snapshot
except ModuleNotFoundError:
    from tests.test_monitoring_coverage import (
        FIXTURES,
        fixture,
        integration_sources,
        write_tunnel_env,
    )
    from tests.test_monitoring_query_ui import representative_snapshot


NOW = 2_000_000_040  # exact minute boundary


def set_fast_cadence(conn, seconds: int = 5) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)",
        ("metric_cadence_seconds", json.dumps({"host:cpu_*": seconds})),
    )


def point(
    conn,
    ts: int,
    kind: str,
    entity_id: str,
    name: str,
    value: float | str | None,
    unit: str = "percent",
    quality: str = "exact",
) -> None:
    cycle = _cycle(conn, ts, float(ts), float(ts) + 0.1, 0.1, True, False)
    entity_pk = ensure_entity(conn, kind, entity_id, entity_id, {}, ts)
    conn.execute(
        "INSERT OR REPLACE INTO metric_points(cycle_id,entity_pk,metric_name,ts,"
        "numeric_value,text_value,unit,quality,reset,gap) VALUES(?,?,?,?,?,?,?,?,0,0)",
        (
            cycle,
            entity_pk,
            name,
            ts,
            value if isinstance(value, (int, float)) else None,
            value if isinstance(value, str) else None,
            unit,
            quality,
        ),
    )


def rollup(
    conn,
    entity_pk: int,
    name: str,
    minute: int,
    average: float | None,
    samples: int = 12,
    expected: int = 12,
    unavailable: int = 0,
    quality: str = "exact",
    unit: str = "percent",
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO minute_rollups(entity_pk,metric_name,minute_start,"
        "samples,expected_samples,min_value,avg_value,max_value,unavailable_count,"
        "reset_count,gap_count,coverage,unit,quality) VALUES(?,?,?,?,?,?,?,?,?,0,0,?,?,?)",
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
            samples / expected,
            unit,
            quality,
        ),
    )


class WatermarkPlanningTests(unittest.TestCase):
    def build(self, lag_minutes: int | None, missing_minute: int | None = None):
        temp = tempfile.TemporaryDirectory()
        path = str(Path(temp.name) / "metrics.sqlite3")
        conn = init_db(path)
        entity_pk = ensure_entity(conn, "host", "local", "local", {}, NOW)
        set_fast_cadence(conn)
        start = NOW - 30 * 60
        for ts in range(start, NOW, 5):
            value = float((ts - start) // 60 + 1)
            point(conn, ts, "host", "local", "cpu_utilization_percent", value)
        if lag_minutes is not None:
            watermark = NOW - lag_minutes * 60
            for minute in range(start, watermark, 60):
                if minute != missing_minute:
                    rollup(
                        conn,
                        entity_pk,
                        "cpu_utilization_percent",
                        minute,
                        float((minute - start) // 60 + 1),
                    )
            conn.execute(
                "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)",
                ("minute_rollup_watermark", str(watermark)),
            )
        conn.close()
        engine = QueryEngine(
            ReadOnlyDatabase(path),
            clock=lambda: NOW,
            limits=QueryLimits(max_query_rows=100, max_materialized_rows=220),
        )
        return temp, path, engine, QueryWindow(start, NOW, start, NOW)

    def test_watermark_lags_preserve_all_observations(self):
        for lag in (0, 1, 5, 14, 20):
            with self.subTest(lag=lag):
                temp, _path, engine, window = self.build(lag)
                try:
                    result = engine.summary(window)
                finally:
                    temp.cleanup()
                self.assertEqual("rollup" if lag == 0 else "hybrid", result.source_mode)
                item = result.series[0]
                self.assertEqual(360, item.sample_count)
                self.assertEqual(360, item.expected_sample_count)
                self.assertEqual(1.0, item.coverage)
                self.assertAlmostEqual(15.5, item.average)
                self.assertEqual(1.0, item.minimum)
                self.assertEqual(30.0, item.maximum)
                self.assertIsNone(item.p95)
                self.assertLessEqual(result.maximum_rows_buffered, 220)

    def test_missing_and_stalled_watermarks_stream_raw_without_loss(self):
        temp, _path, engine, window = self.build(None)
        try:
            result = engine.summary(window)
        finally:
            temp.cleanup()
        self.assertEqual("raw", result.source_mode)
        self.assertEqual("missing_rollup_watermark_streaming", result.filters["plan_reason"])
        self.assertEqual(360, result.series[0].sample_count)
        self.assertAlmostEqual(15.5, result.series[0].average)
        self.assertIsNone(result.series[0].p95)
        self.assertEqual(360, result.rows_scanned)
        self.assertLessEqual(result.maximum_rows_buffered, 361)

        temp, path, engine, window = self.build(20)
        try:
            first = engine.summary(window)
            second = engine.summary(window)
            with ReadOnlyDatabase(path).connection() as conn:
                self.assertEqual(NOW - 20 * 60, ReadOnlyDatabase.rollup_watermark(conn))
        finally:
            temp.cleanup()
        self.assertEqual(first.series[0].sample_count, second.series[0].sample_count)
        self.assertEqual("rollup_watermark_streaming", second.filters["plan_reason"])

    def test_missing_physical_rollup_is_explicit_coverage_gap(self):
        missing = NOW - 25 * 60
        temp, _path, engine, window = self.build(0, missing)
        try:
            result = engine.summary(window)
        finally:
            temp.cleanup()
        item = result.series[0]
        self.assertEqual(348, item.sample_count)
        self.assertEqual(360, item.expected_sample_count)
        self.assertAlmostEqual(348 / 360, item.coverage)

    def test_auto_export_uses_finalized_rollups_and_raw_minutes(self):
        temp, path, _engine, window = self.build(14)
        statements = []
        engine = QueryEngine(
            ReadOnlyDatabase(path, trace_callback=statements.append),
            clock=lambda: NOW,
            limits=QueryLimits(max_query_rows=100, max_materialized_rows=220),
        )
        try:
            json_out, csv_out = io.StringIO(), io.StringIO()
            json_meta = export_data(
                engine, window, "-", "json", "auto", stdout=json_out
            )
            csv_meta = export_data(
                engine, window, "-", "csv", "auto", stdout=csv_out
            )
            raw_minute_sql = next(
                sql for sql in statements
                if "GROUP BY r.entity_pk,r.metric_name,r.minute" in sql
                and "AVG(CASE" in sql
            )
            reader = connect_db(path)
            plan = " ".join(
                str(row[3])
                for row in reader.execute("EXPLAIN QUERY PLAN " + raw_minute_sql)
            )
            reader.close()
        finally:
            temp.cleanup()
        payload = json.loads(json_out.getvalue())
        csv_rows = list(csv.DictReader(io.StringIO(csv_out.getvalue())))
        self.assertEqual("hybrid", json_meta["source_mode"])
        self.assertEqual(30, json_meta["row_count"])
        self.assertEqual(json_meta["row_count"], csv_meta["row_count"])
        self.assertEqual(31, len(csv_rows))
        self.assertEqual({"rollup", "raw_minute"}, {row["record_type"] for row in payload["rows"]})
        self.assertTrue(all(row["metric_semantics"] == GAUGE for row in payload["rows"]))
        self.assertIn("idx_metric_points_time", plan)


class ActiveMembershipTests(unittest.TestCase):
    def test_collector_retirement_history_recovery_and_failure_membership(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_dir = root / "env"
            write_tunnel_env(env_dir)
            db = str(root / "metrics.sqlite3")
            monotonic = [10.0]
            sample_index = [0]
            malformed_socket = [False]

            def command(parts):
                if parts[0] == "ss":
                    return "malformed socket output\n" if malformed_socket[0] else fixture("ss.txt")
                return fixture("systemd-gost.txt")

            def reader(path):
                if path == FIXTURES / "proc/stat":
                    lines = fixture("proc/stat").splitlines()
                    values = lines[0].split()
                    values[1] = str(int(values[1]) + sample_index[0] * 100)
                    values[4] = str(int(values[4]) + sample_index[0] * 100)
                    lines[0] = " ".join(values)
                    return "\n".join(lines) + "\n"
                return path.read_text(encoding="utf-8")

            sources = integration_sources(root, command, monotonic, reader)
            config = CollectorConfig(filesystem_paths=(Path("/"), Path("/var/lib/gost-manager")))
            collect_once(db, str(env_dir), 1000, sources, config)
            first = QueryEngine(ReadOnlyDatabase(db), clock=lambda: 1000).snapshot()
            self.assertEqual(1, sum(e["entity_type"] == "tunnel" for e in first["entities"]))
            self.assertIn("gost-iran-1.service", evaluate_snapshot(first)["services"])
            self.assertTrue(evaluate_snapshot(first)["services"]["gost-iran-1.service"]["required"])

            conn = connect_db(db)
            _cycle(conn, 1001, 1001.0, 1001.1, 0.1, False, False)
            conn.close()
            failed = QueryEngine(ReadOnlyDatabase(db), clock=lambda: 1001).snapshot()
            failed_ids = {(e["entity_type"], e["entity_id"]) for e in failed["entities"]}
            self.assertIn(("tunnel", "iran-1"), failed_ids)
            self.assertEqual("critical", evaluate_snapshot(failed)["overall"]["status"])

            (env_dir / "iran-1.env").unlink()
            monotonic[0] += 5
            sample_index[0] = 1
            collect_once(db, str(env_dir), 1005, sources, config)
            retired = QueryEngine(ReadOnlyDatabase(db), clock=lambda: 1005).snapshot()
            ids = {(e["entity_type"], e["entity_id"]) for e in retired["entities"]}
            self.assertNotIn(("tunnel", "iran-1"), ids)
            self.assertNotIn(("service", "gost-iran-1.service"), ids)
            health = evaluate_snapshot(retired)
            self.assertNotIn("iran-1", health["tunnels"])
            self.assertNotIn("gost-iran-1.service", health["services"])

            history = QueryEngine(ReadOnlyDatabase(db), clock=lambda: 1010).summary(
                QueryWindow(995, 1010, 995, 1010),
                entity_type="tunnel",
                entity_id="iran-1",
                require_match=True,
            )
            self.assertTrue(history.series)

            conn = connect_db(db)
            ensure_entity(
                conn,
                "service",
                "gost-gateway-exit-retired.service",
                None,
                {},
                900,
            )
            for index in range(300):
                ensure_entity(conn, "tunnel", f"retired-{index}", None, {}, 900)
                ensure_entity(conn, "service", f"gost-iran-{index + 10}.service", None, {}, 900)
            _cycle(conn, 1006, 1006.0, 1006.1, 0.1, False, False)
            conn.close()
            failed_latest = QueryEngine(ReadOnlyDatabase(db), clock=lambda: 1006).snapshot()
            self.assertLessEqual(len(failed_latest["entities"]), 2)
            self.assertNotIn(
                ("service", "gost-gateway-exit-retired.service"),
                {
                    (entity["entity_type"], entity["entity_id"])
                    for entity in failed_latest["entities"]
                },
            )

            write_tunnel_env(env_dir)
            monotonic[0] += 5
            sample_index[0] = 2
            malformed_socket[0] = True
            collect_once(db, str(env_dir), 1010, sources, config)
            restored = QueryEngine(ReadOnlyDatabase(db), clock=lambda: 1010).snapshot()
            restored_ids = {(e["entity_type"], e["entity_id"]) for e in restored["entities"]}
            self.assertIn(("tunnel", "iran-1"), restored_ids)
            self.assertIn(("service", "gost-iran-1.service"), restored_ids)


class HealthEventTests(unittest.TestCase):
    def test_recovery_direction_and_required_optional_services(self):
        for previous in ("inactive", "failed", "activating"):
            snapshot = representative_snapshot()
            snapshot["health_events"] = [{
                "ts": NOW - 1,
                "severity": "info",
                "code": "service_state_changed",
                "message": "recovered",
                "details": {"service": "gost-iran-1.service", "previous": previous, "current": "active"},
            }]
            self.assertEqual("healthy", evaluate_snapshot(snapshot)["overall"]["status"])

        failed = representative_snapshot()
        failed["health_events"] = [{
            "ts": NOW - 1,
            "severity": "warning",
            "code": "service_state_changed",
            "message": "failed",
            "details": {"service": "gost-iran-1.service", "previous": "active", "current": "failed"},
        }]
        self.assertEqual("degraded", evaluate_snapshot(failed)["overall"]["status"])

        optional = representative_snapshot()
        optional["health_events"] = [{
            "ts": NOW - 1,
            "severity": "warning",
            "code": "service_state_changed",
            "message": "optional failed",
            "details": {"service": "unmanaged.service", "previous": "active", "current": "failed"},
        }]
        self.assertEqual("healthy", evaluate_snapshot(optional)["overall"]["status"])

        restarted = representative_snapshot()
        restarted["health_events"] = [{
            "ts": NOW - 1,
            "severity": "warning",
            "code": "pid_replaced",
            "message": "restarted",
            "details": {"service": "gost-iran-1.service"},
        }]
        self.assertEqual("degraded", evaluate_snapshot(restarted)["overall"]["status"])

    def test_event_storm_is_bounded_and_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            point(conn, NOW - 2, "host", "local", "cpu_utilization_percent", 10)
            point(conn, NOW - 2, "host", "local", "memory_used_percent", 20)
            point(conn, NOW - 2, "filesystem", "fs:/", "filesystem_used_percent", 30)
            conn.executemany(
                "INSERT INTO events(ts,severity,code,message,details_json) VALUES(?,?,?,?,?)",
                [
                    (NOW - index % 30, "warning", "metric_source_unavailable", "failed", '{"source":"proc_stat"}')
                    for index in range(250)
                ],
            )
            conn.close()
            snapshot = QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW).snapshot()
            self.assertEqual(50, len(snapshot["events"]))
            self.assertEqual(200, len(snapshot["health_events"]))
            self.assertTrue(snapshot["health_events_truncated"])
            result = evaluate_snapshot(snapshot)["overall"]
            self.assertIn("health_event_overflow", result["reason_codes"])


class RollupWeightingTests(unittest.TestCase):
    def test_observation_coverage_and_numeric_weight_are_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            entity = ensure_entity(conn, "host", "local", "local", {}, NOW)
            set_fast_cadence(conn)
            start = NOW - 3 * 24 * 3600
            start = (start // 60) * 60
            rows = (
                (10.0, 12, 0, "exact"),
                (100.0, 12, 11, "estimated"),
                (20.0, 12, 6, "derived"),
                (None, 12, 12, "unavailable"),
            )
            for index, (average, samples, unavailable, quality) in enumerate(rows):
                rollup(conn, entity, "cpu_utilization_percent", start + index * 60, average, samples, 12, unavailable, quality)
            conn.execute(
                "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)",
                ("minute_rollup_watermark", str(start + 240)),
            )
            conn.close()
            result = QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW).summary(
                QueryWindow(start, start + 240, start, start + 240)
            )
            item = result.series[0]
            self.assertEqual(48, item.sample_count)
            self.assertEqual(48, item.expected_sample_count)
            self.assertEqual(1.0, item.coverage)
            self.assertEqual(29, item.unavailable_count)
            self.assertAlmostEqual(95.0, item.weighted_seconds)
            self.assertAlmostEqual((10 * 60 + 100 * 5 + 20 * 30) / 95, item.average)
            self.assertEqual("unavailable", item.quality)

            partial = QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW).summary(
                QueryWindow(start + 30, start + 180, start + 30, start + 180)
            ).series[0]
            self.assertEqual(24, partial.sample_count)
            self.assertEqual(30, partial.expected_sample_count)
            self.assertAlmostEqual(24 / 30, partial.coverage)
            self.assertAlmostEqual((100 * 5 + 20 * 30) / 35, partial.average)

    def test_partial_boundary_and_hybrid_numeric_weighting(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            entity = ensure_entity(conn, "host", "local", "local", {}, NOW)
            set_fast_cadence(conn)
            start = NOW - 180
            rollup(conn, entity, "cpu_utilization_percent", start, 10, 12, 12, 6, "derived")
            for ts in range(start + 60, start + 120, 5):
                point(conn, ts, "host", "local", "cpu_utilization_percent", 30)
            conn.execute(
                "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)",
                ("minute_rollup_watermark", str(start + 60)),
            )
            conn.close()
            engine = QueryEngine(
                ReadOnlyDatabase(path),
                clock=lambda: start + 120,
                limits=QueryLimits(max_query_rows=1),
            )
            item = engine.summary(
                QueryWindow(start, start + 120, start, start + 120)
            ).series[0]
            self.assertAlmostEqual((10 * 30 + 30 * 60) / 90, item.average)
            self.assertEqual(24, item.sample_count)
            self.assertEqual(24, item.expected_sample_count)
            self.assertEqual(6, item.unavailable_count)
            self.assertIsNone(item.p95)


class MetricSemanticsTests(unittest.TestCase):
    CASES = {
        "cpu_utilization_percent": ("percent", GAUGE),
        "rx_bytes_per_second": ("B/s", RATE),
        "rx_bytes": ("bytes", CUMULATIVE_COUNTER),
        "service_active": ("boolean", CATEGORICAL),
        "service_main_pid": ("pid", IDENTITY),
        "service_start_monotonic_us": ("microseconds", IDENTITY),
        "last_successful_cycle_timestamp": ("unix_seconds", TIMESTAMP),
        "service_active_state": ("state", CATEGORICAL),
        "remote_endpoint": ("endpoint", IDENTITY),
        "unknown_numeric_metric": ("count", UNKNOWN),
    }

    def test_classifier_and_raw_statistics_eligibility(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            for name, (unit, category) in self.CASES.items():
                self.assertEqual(category, classify_metric(name, unit).category)
                first, second = ("inactive", "active") if unit == "state" else (
                    ("old.invalid:1", "new.invalid:2") if unit == "endpoint" else (1, 2)
                )
                point(conn, NOW - 10, "host", "local", name, first, unit)
                point(conn, NOW - 5, "host", "local", name, second, unit)
            conn.close()
            engine = QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW)
            result = engine.summary(resolve_window(NOW, "20s"))
            items = {item.metric_name: item for item in result.series}
            for name, (_unit, category) in self.CASES.items():
                self.assertEqual(category, items[name].metric_semantics)
            for name in ("cpu_utilization_percent", "rx_bytes_per_second"):
                self.assertIsNotNone(items[name].average)
                self.assertIsNotNone(items[name].p95)
            for name in set(self.CASES) - {"cpu_utilization_percent", "rx_bytes_per_second"}:
                self.assertIsNone(items[name].minimum)
                self.assertIsNone(items[name].average)
                self.assertIsNone(items[name].maximum)
                self.assertIsNone(items[name].p95)
            self.assertEqual(1, items["service_active"].transition_count)
            rendered = render_summary(result)
            self.assertIn("SEMANTICS", rendered)
            self.assertIn("cumulative_counter", rendered)

            json_out, csv_out = io.StringIO(), io.StringIO()
            window = resolve_window(NOW, "20s")
            export_data(engine, window, "-", "json", "summary", stdout=json_out)
            export_data(engine, window, "-", "csv", "summary", stdout=csv_out)
            json_rows = {row["metric_name"]: row for row in json.loads(json_out.getvalue())["rows"]}
            csv_rows = {
                row["metric_name"]: row
                for row in list(csv.DictReader(io.StringIO(csv_out.getvalue())))[1:]
            }
            self.assertEqual(set(json_rows), set(csv_rows))
            for name in self.CASES:
                self.assertEqual(json_rows[name]["metric_semantics"], csv_rows[name]["metric_semantics"])
                self.assertEqual(json_rows[name]["p95"] is None, csv_rows[name]["p95"] == "")

    def test_rollup_and_hybrid_keep_nonstatistical_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            entity = ensure_entity(conn, "interface", "interface:eth0", "eth0", {}, NOW)
            start = NOW - 180
            rollup(conn, entity, "rx_bytes", start, 100, unit="bytes")
            for ts in range(start + 60, start + 120, 5):
                point(conn, ts, "interface", "interface:eth0", "rx_bytes", 200 + ts, "bytes")
            conn.execute(
                "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)",
                ("minute_rollup_watermark", str(start + 60)),
            )
            conn.close()
            item = QueryEngine(
                ReadOnlyDatabase(path),
                clock=lambda: start + 120,
                limits=QueryLimits(max_query_rows=1),
            ).summary(QueryWindow(start, start + 120, start, start + 120)).series[0]
            self.assertEqual(CUMULATIVE_COUNTER, item.metric_semantics)
            self.assertIsNone(item.minimum)
            self.assertIsNone(item.average)
            self.assertIsNone(item.maximum)
            self.assertIsNone(item.p95)


if __name__ == "__main__":
    unittest.main()
