#!/usr/bin/env python3
"""Production-profile regressions for the PR #16 technical review."""

from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from monitoring.collector import CollectorConfig, collect_once
from monitoring.exporters import CSV_FIELDS, export_data
from monitoring.health import evaluate_snapshot
from monitoring.query_db import ReadOnlyDatabase
from monitoring.query_engine import QueryEngine
from monitoring.query_models import QueryLimitError, QueryWindow
from monitoring.query_window import RetentionPolicy, plan_window, resolve_window
from monitoring.renderers import render_ansi_snapshot, render_snapshot_plain
from monitoring.schema import _cycle, connect_db, ensure_entity, init_db

try:
    from test_monitoring_coverage import (
        FIXTURES,
        fixture,
        integration_sources,
        write_tunnel_env,
    )
except ModuleNotFoundError:
    from tests.test_monitoring_coverage import (
        FIXTURES,
        fixture,
        integration_sources,
        write_tunnel_env,
    )


NOW = 2_000_000_000


def insert_point(
    conn,
    ts,
    entity_type,
    entity_id,
    metric_name,
    value,
    unit="percent",
    quality="exact",
):
    cycle_id = _cycle(conn, ts, float(ts), float(ts) + 0.1, 0.1, True, False)
    entity_pk = ensure_entity(conn, entity_type, entity_id, entity_id, {}, ts)
    conn.execute(
        "INSERT OR REPLACE INTO metric_points(cycle_id,entity_pk,metric_name,ts,"
        "numeric_value,text_value,unit,quality,reset,gap) VALUES(?,?,?,?,?,?,?,?,0,0)",
        (
            cycle_id,
            entity_pk,
            metric_name,
            ts,
            value if isinstance(value, (int, float)) else None,
            value if isinstance(value, str) else None,
            unit,
            quality,
        ),
    )


class SnapshotContractTests(unittest.TestCase):
    def test_real_collector_mixed_cadence_snapshot_survives_fast_cycles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_dir = root / "env"
            write_tunnel_env(env_dir)
            db = str(root / "metrics.sqlite3")
            monotonic = [10.0]
            sample_index = [0]

            def command(parts):
                if parts[0] == "ss":
                    return fixture("ss.txt")
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
            config = CollectorConfig(
                sample_interval=5.0,
                tcp_snapshot_interval=30.0,
                slow_sample_interval=60.0,
                filesystem_paths=(Path("/"), Path("/var/lib/gost-manager")),
            )
            collect_once(
                db,
                str(env_dir),
                1000,
                sources,
                config,
                maintenance=True,
                checkpoint=lambda _path: (0, 0, 0),
            )
            for cycle in range(1, 11):
                monotonic[0] += 5
                sample_index[0] = cycle
                collect_once(
                    db,
                    str(env_dir),
                    1000 + cycle * 5,
                    sources,
                    config,
                )

            engine = QueryEngine(ReadOnlyDatabase(db), clock=lambda: 1050)
            snapshot = engine.snapshot()
            points = {
                (item["entity_type"], item["entity_id"], item["metric_name"]): item
                for item in snapshot["metrics"]
            }
            self.assertEqual(1050, snapshot["cycle"]["collected_at"])
            expected = {
                ("host", "local", "memory_used_bytes"): 0,
                ("host", "local", "memory_available_bytes"): 0,
                ("host", "local", "swap_used_bytes"): 0,
                ("filesystem", "fs:/", "filesystem_used_percent"): 50,
                ("filesystem", "fs:/var/lib/gost-manager", "filesystem_used_percent"): 50,
                ("collector", "local", "database_size_bytes"): 50,
                ("collector", "local", "database_wal_size_bytes"): 50,
                ("service", "gost-iran-1.service", "process_open_fds"): 0,
                ("host", "local", "tcp_state_estab"): 20,
                ("collector", "local", "checkpoint_success"): 50,
            }
            for key, age in expected.items():
                with self.subTest(key=key):
                    self.assertIn(key, points)
                    self.assertEqual(age, points[key]["data_age_seconds"])
                    self.assertFalse(points[key]["stale"])
            health = evaluate_snapshot(snapshot)
            required_points = {
                key: value for key, value in points.items()
                if key in {
                    ("host", "local", "cpu_utilization_percent"),
                    ("host", "local", "memory_used_percent"),
                    ("filesystem", "fs:/", "filesystem_used_percent"),
                }
            }
            self.assertEqual("healthy", health["overall"]["status"], (health, required_points))
            self.assertIn("root filesystem", render_snapshot_plain(snapshot))
            self.assertIn("\x1b[", render_ansi_snapshot(snapshot))

            json_output = io.StringIO()
            export_data(
                engine,
                resolve_window(1050, "10m"),
                "-",
                "json",
                "summary",
                stdout=json_output,
            )
            self.assertEqual(4, json.loads(json_output.getvalue())["metadata"]["database_schema_version"])

    def test_snapshot_read_transaction_is_coherent_across_concurrent_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            insert_point(conn, NOW - 5, "host", "local", "cpu_utilization_percent", 10)
            insert_point(conn, NOW - 5, "host", "local", "memory_used_percent", 20)
            insert_point(conn, NOW - 5, "filesystem", "fs:/", "filesystem_used_percent", 30)
            conn.execute(
                "INSERT INTO events(ts,severity,code,message,details_json) VALUES(?,?,?,?,?)",
                (NOW - 5, "warning", "metric_source_unavailable", "old", '{}'),
            )
            conn.close()
            called = []

            def commit_new(_stage):
                if called:
                    return
                called.append(True)
                writer = connect_db(path)
                insert_point(writer, NOW, "host", "local", "cpu_utilization_percent", 99)
                writer.execute(
                    "INSERT INTO events(ts,severity,code,message,details_json) VALUES(?,?,?,?,?)",
                    (NOW, "error", "collection_failed", "new", '{}'),
                )
                writer.close()

            engine = QueryEngine(
                ReadOnlyDatabase(path), clock=lambda: NOW, read_hook=commit_new
            )
            first = engine.snapshot()
            cpu = next(
                item for item in first["metrics"]
                if item["metric_name"] == "cpu_utilization_percent"
            )
            self.assertEqual(NOW - 5, first["cycle"]["collected_at"])
            self.assertEqual(10, cpu["numeric_value"])
            self.assertNotIn("new", [event["message"] for event in first["health_events"]])

            second = QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW).snapshot()
            cpu = next(
                item for item in second["metrics"]
                if item["metric_name"] == "cpu_utilization_percent"
            )
            self.assertEqual(NOW, second["cycle"]["collected_at"])
            self.assertEqual(99, cpu["numeric_value"])
            self.assertIn("new", [event["message"] for event in second["health_events"]])

    def test_health_events_are_independent_from_display_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            insert_point(conn, NOW - 5, "host", "local", "cpu_utilization_percent", 10)
            insert_point(conn, NOW - 5, "host", "local", "memory_used_percent", 20)
            insert_point(conn, NOW - 60, "filesystem", "fs:/", "filesystem_used_percent", 30)
            conn.execute(
                "INSERT INTO events(ts,severity,code,message,details_json) VALUES(?,?,?,?,?)",
                (NOW - 100, "warning", "metric_source_unavailable", "required failure", '{"source":"proc_stat"}'),
            )
            conn.executemany(
                "INSERT INTO events(ts,severity,code,message,details_json) VALUES(?,?,?,?,?)",
                [
                    (NOW - 99 + index, "info", "informational", f"info {index}", "{}")
                    for index in range(60)
                ],
            )
            conn.close()
            snapshot = QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW).snapshot()
            self.assertEqual(50, len(snapshot["events"]))
            self.assertIn(
                "metric_source_unavailable",
                {event["code"] for event in snapshot["health_events"]},
            )
            self.assertEqual("degraded", evaluate_snapshot(snapshot)["overall"]["status"])


class HealthPolicyReviewTests(unittest.TestCase):
    @staticmethod
    def snapshot(service_active=1, listeners=1, listener_quality="exact", fs_age=60):
        def point(kind, entity, name, value, quality="exact", age=5):
            return {
                "entity_type": kind, "entity_id": entity, "metric_name": name,
                "ts": NOW - age, "numeric_value": value, "text_value": None,
                "unit": "count", "quality": quality, "reset": 0, "gap": 0,
                "data_age_seconds": age, "stale": age > (150 if kind == "filesystem" else 12.5),
            }
        return {
            "generated_at": NOW,
            "cycle": {"collected_at": NOW - 5, "success": True},
            "events": [], "health_events": [],
            "entities": [
                {"entity_type": "tunnel", "entity_id": "iran-1", "metadata": {"service": "gost-iran-1.service"}},
            ],
            "metrics": [
                point("host", "local", "cpu_utilization_percent", 10, age=5),
                point("host", "local", "memory_used_percent", 20, age=5),
                point("filesystem", "fs:/", "filesystem_used_percent", 30, age=fs_age),
                point("service", "gost-iran-1.service", "service_active", service_active, age=5),
                point("service", "gost-iran-1.service", "process_rss_bytes", 100, age=5),
                point("service", "gost-iran-1.service", "listener_owned_count", listeners, listener_quality, age=5),
                point("tunnel", "iran-1", "service_active", service_active, age=5),
                point("tunnel", "iran-1", "listener_ownership_exact", listeners, listener_quality, age=5),
            ],
        }

    def test_snapshot_contains_only_current_direct_mode_service(self):
        health = evaluate_snapshot(self.snapshot())
        self.assertEqual({"gost-iran-1.service"}, set(health["services"]))
        self.assertTrue(health["services"]["gost-iran-1.service"]["required"])
        self.assertEqual("healthy", health["overall"]["status"])

    def test_required_service_inactive_zero_listener_and_unavailable(self):
        self.assertEqual("critical", evaluate_snapshot(self.snapshot(service_active=0))["overall"]["status"])
        zero = evaluate_snapshot(self.snapshot(listeners=0))
        self.assertEqual("down", zero["services"]["gost-iran-1.service"]["status"])
        self.assertIn("required_listener_missing", zero["services"]["gost-iran-1.service"]["reason_codes"])
        unavailable = evaluate_snapshot(self.snapshot(listeners=None, listener_quality="unavailable"))
        self.assertEqual("unknown", unavailable["services"]["gost-iran-1.service"]["status"])

    def test_slow_filesystem_freshness_boundary(self):
        for age in (5, 30, 60, 150):
            with self.subTest(age=age):
                self.assertEqual("healthy", evaluate_snapshot(self.snapshot(fs_age=age))["overall"]["status"])
        self.assertEqual("unknown", evaluate_snapshot(self.snapshot(fs_age=151))["overall"]["status"])

    def test_optional_source_event_does_not_degrade_overall(self):
        snapshot = self.snapshot()
        snapshot["health_events"] = [
            {
                "ts": NOW - 1,
                "severity": "warning",
                "code": "metric_source_unavailable",
                "message": "optional source unavailable",
                "details": {"source": "optional_kernel_source"},
            }
        ]
        self.assertEqual("healthy", evaluate_snapshot(snapshot)["overall"]["status"])


class ProductionProfileQueryTests(unittest.TestCase):
    def test_accepted_542_series_profile_windows_and_query_plans(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            entities = []
            entity_specs = [("host", "local")]
            entity_specs += [("service", f"gost-iran-{index}.service") for index in range(1, 7)]
            entity_specs += [("tunnel", f"iran-{index}") for index in range(1, 7)]
            entity_specs += [("interface", f"interface:eth{index}") for index in range(3)]
            for kind, entity_id in entity_specs:
                entities.append((kind, ensure_entity(conn, kind, entity_id, entity_id, {}, NOW)))
            specs = []
            for prefix, count, cadence in (("fast", 485, 10), ("socket", 9, 30), ("slow", 48, 60)):
                for index in range(count):
                    kind, entity_pk = entities[index % len(entities)]
                    specs.append((kind, entity_pk, f"{prefix}_{index:03d}", cadence))
            cadences = {
                f"{kind}:{prefix}_*": cadence
                for kind, _entity_pk in entities
                for prefix, cadence in (("fast", 10), ("socket", 30), ("slow", 60))
            }
            conn.execute(
                "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)",
                ("metric_cadence_seconds", json.dumps(cadences)),
            )
            start = NOW - 130 * 60
            timestamps = list(range(start, NOW, 10))
            conn.execute("BEGIN")
            conn.executemany(
                "INSERT INTO sample_cycles(cycle_id,collected_at,monotonic_started,monotonic_finished,"
                "duration_seconds,success,overrun,missed_deadlines,overrun_seconds) VALUES(?,?,?,?,?,1,0,0,0)",
                [
                    (index + 1, ts, float(ts), float(ts) + 0.1, 0.1)
                    for index, ts in enumerate(timestamps)
                ],
            )

            def raw_rows():
                for cycle_index, ts in enumerate(timestamps, 1):
                    for _kind, entity_pk, name, cadence in specs:
                        if (NOW - ts) % cadence == 0:
                            yield (
                                cycle_index, entity_pk, name, ts, float(ts % 100),
                                None, "count", "exact", 0, 0,
                            )

            conn.executemany(
                "INSERT INTO metric_points(cycle_id,entity_pk,metric_name,ts,numeric_value,text_value,"
                "unit,quality,reset,gap) VALUES(?,?,?,?,?,?,?,?,?,?)",
                raw_rows(),
            )
            minute_start = math.floor(start / 60) * 60
            minute_end = math.floor(NOW / 60) * 60

            def rollup_rows():
                minute = minute_start
                while minute < minute_end:
                    for _kind, entity_pk, name, cadence in specs:
                        expected = math.ceil(60 / cadence)
                        yield (
                            entity_pk, name, minute, expected, expected, 1.0, 1.0,
                            1.0, 0, 0, 0, 1.0, "count", "exact",
                        )
                    minute += 60

            conn.executemany(
                "INSERT INTO minute_rollups(entity_pk,metric_name,minute_start,samples,expected_samples,"
                "min_value,avg_value,max_value,unavailable_count,reset_count,gap_count,coverage,unit,quality) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rollup_rows(),
            )
            conn.execute(
                "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)",
                ("minute_rollup_watermark", str(math.floor((NOW - 15 * 60) / 60) * 60)),
            )
            conn.commit()
            conn.close()

            statements = []
            engine = QueryEngine(
                ReadOnlyDatabase(path, trace_callback=statements.append), clock=lambda: NOW
            )
            results = {}
            all_selects = []
            statement_counts = []
            started = time.monotonic()
            for duration in ("10m", "30m", "1h"):
                statements.clear()
                results[duration] = engine.summary(resolve_window(NOW, duration))
                current = [
                    sql for sql in statements
                    if sql.lstrip().upper().startswith(("SELECT", "WITH"))
                ]
                statement_counts.append(len(current))
                all_selects.extend(current)
            elapsed = time.monotonic() - started
            self.assertEqual("raw", results["10m"].source_mode)
            self.assertEqual("raw", results["30m"].source_mode)
            self.assertEqual("hybrid", results["1h"].source_mode)
            self.assertTrue(all(item.p95 is None for item in results["1h"].series))
            self.assertTrue(all(len(result.series) == 542 for result in results.values()))
            maximum = max(result.materialized_rows for result in results.values())
            self.assertLessEqual(maximum, 105_843)
            self.assertLessEqual(maximum, engine.limits.max_materialized_rows)
            self.assertLessEqual(
                max(result.rows_scanned for result in results.values()), 158_313
            )
            self.assertLessEqual(
                max(result.maximum_rows_buffered for result in results.values()), 105_843
            )
            self.assertLess(elapsed, 12.0)
            self.assertEqual(10, max(statement_counts))

            writer = connect_db(path)
            writer.execute(
                "DELETE FROM collector_state WHERE key='minute_rollup_watermark'"
            )
            writer.close()
            missing_watermark = QueryEngine(
                ReadOnlyDatabase(path), clock=lambda: NOW
            ).summary(resolve_window(NOW, "2h"))
            self.assertEqual("raw", missing_watermark.source_mode)
            self.assertLessEqual(missing_watermark.rows_scanned, 760_663)
            self.assertLessEqual(
                missing_watermark.maximum_rows_buffered,
                missing_watermark.filters["max_stream_scan_rows"],
            )
            self.assertTrue(all(item.p95 is None for item in missing_watermark.series))

            explain_conn = sqlite3.connect(path)
            plans = []
            for sql in all_selects:
                if "metric_points" in sql or "minute_rollups" in sql:
                    plans.extend(
                        str(row[3])
                        for row in explain_conn.execute("EXPLAIN QUERY PLAN " + sql)
                    )
            snapshot_engine = QueryEngine(
                ReadOnlyDatabase(path, trace_callback=statements.append), clock=lambda: NOW
            )
            snapshot_engine.snapshot()
            snapshot_sql = next(
                sql for sql in reversed(statements) if "desired(entity_type" in sql
            )
            snapshot_plan = " ".join(
                str(row[3])
                for row in explain_conn.execute("EXPLAIN QUERY PLAN " + snapshot_sql)
            )
            explain_conn.close()
            joined = " ".join(plans)
            self.assertIn("idx_metric_points_time", joined)
            self.assertIn("idx_minute_rollups_time", joined)
            self.assertIn("idx_metric_points_lookup", snapshot_plan)

    def test_event_and_export_queries_use_time_indexes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            for offset in range(1000, 0, -10):
                insert_point(conn, NOW - offset, "host", "local", "cpu_utilization_percent", offset)
                conn.execute(
                    "INSERT INTO events(ts,severity,code,message,details_json) VALUES(?,?,?,?,?)",
                    (NOW - offset, "info", "informational", "safe", "{}"),
                )
            conn.close()
            statements = []
            engine = QueryEngine(
                ReadOnlyDatabase(path, trace_callback=statements.append), clock=lambda: NOW
            )
            engine.events(resolve_window(NOW, "10m"))
            output = io.StringIO()
            export_data(
                engine, resolve_window(NOW, "10m"), "-", "json", "raw", stdout=output
            )
            relevant = [
                sql for sql in statements
                if (
                    "FROM events WHERE" in sql
                    or "SELECT COUNT(*) FROM (SELECT 1 FROM metric_points" in sql
                    or "SELECT e.entity_type,e.entity_id,p.metric_name" in sql
                )
            ]
            reader = sqlite3.connect(path)
            plans = {
                sql: " ".join(
                    str(row[3])
                    for row in reader.execute("EXPLAIN QUERY PLAN " + sql)
                )
                for sql in relevant
            }
            reader.close()
            self.assertTrue(any("idx_events_time" in plan for sql, plan in plans.items() if "events" in sql))
            metric_plans = [plan for sql, plan in plans.items() if "metric_points" in sql]
            self.assertTrue(metric_plans)
            self.assertTrue(all("idx_metric_points_time" in plan for plan in metric_plans))

    def test_rollup_uses_historical_expected_counts_after_cadence_change(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            entity_pk = ensure_entity(conn, "host", "local", "local", {}, NOW)
            start = math.floor((NOW - 3 * 24 * 3600) / 60) * 60
            conn.executemany(
                "INSERT INTO minute_rollups(entity_pk,metric_name,minute_start,samples,expected_samples,"
                "min_value,avg_value,max_value,unavailable_count,reset_count,gap_count,coverage,unit,quality) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (entity_pk, "cadence_changed", start, 12, 12, 1, 1, 1, 0, 0, 0, 1, "count", "exact"),
                    (entity_pk, "cadence_changed", start + 60, 2, 2, 2, 2, 2, 0, 0, 0, 1, "count", "exact"),
                ],
            )
            conn.execute(
                "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)",
                ("metric_cadence_seconds", json.dumps({"host:cadence_changed": 60})),
            )
            conn.execute(
                "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)",
                ("minute_rollup_watermark", str(start + 120)),
            )
            conn.close()
            window = QueryWindow(start, start + 120, start, start + 120)
            item = QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW).summary(window).series[0]
            self.assertEqual(14, item.expected_sample_count)
            self.assertEqual(14, item.sample_count)
            self.assertEqual(1.0, item.coverage)

    def test_hybrid_short_raw_tail_survives_all_cutoff_offsets(self):
        for offset in (0, 10, 30, 59):
            with self.subTest(offset=offset), tempfile.TemporaryDirectory() as temp:
                now = NOW + (offset - (NOW - 120) % 60)
                policy = RetentionPolicy(raw_seconds=120, rollup_seconds=1000, event_seconds=1000)
                cutoff = now - policy.raw_seconds
                next_minute = math.floor(cutoff / 60) * 60 + 60
                end = min(next_minute, cutoff + max(1, (next_minute - cutoff) // 2))
                if end <= cutoff:
                    end = cutoff + 1
                start = cutoff - 120
                path = str(Path(temp) / "metrics.sqlite3")
                conn = init_db(path)
                insert_point(conn, cutoff, "host", "local", "cpu_utilization_percent", 42)
                conn.close()
                window = QueryWindow(start, end, start, end)
                plan = plan_window(window, now, policy)
                self.assertEqual("hybrid", plan.source_mode)
                self.assertEqual(cutoff, plan.raw_start)
                result = QueryEngine(
                    ReadOnlyDatabase(path), clock=lambda: now, retention=policy
                ).summary(window)
                self.assertEqual(1, result.series[0].sample_count)
                self.assertEqual(42, result.series[0].latest)

    def test_entity_queries_are_exact_and_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            for index in range(12):
                ensure_entity(conn, "service", f"service-{index}", None, {}, NOW)
            with ReadOnlyDatabase(path).connection() as reader:
                exact = ReadOnlyDatabase.list_entities(
                    reader, "service", "service-3", max_rows=10
                )
                self.assertEqual(["service-3"], [item["entity_id"] for item in exact])
                with self.assertRaises(QueryLimitError):
                    ReadOnlyDatabase.list_entities(reader, "service", max_rows=10)
            conn.close()


class ExportParityTests(unittest.TestCase):
    def test_csv_metadata_summary_fields_empty_and_concurrent_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            insert_point(conn, NOW - 5, "host", "local", "cpu_utilization_percent", 20)
            conn.close()
            engine = QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW)
            window = resolve_window(NOW, "10m")
            json_stream = io.StringIO()
            csv_stream = io.StringIO()
            json_meta = export_data(engine, window, "-", "json", "summary", stdout=json_stream)
            csv_meta = export_data(engine, window, "-", "csv", "summary", stdout=csv_stream)
            rows = list(csv.DictReader(io.StringIO(csv_stream.getvalue())))
            self.assertEqual(CSV_FIELDS, tuple(rows[0].keys()))
            self.assertEqual("metadata", rows[0]["record_type"])
            self.assertEqual("summary", rows[1]["record_type"])
            for field in (
                "latest", "latest_timestamp", "minimum", "average", "maximum",
                "p95", "unavailable_count", "reset_count", "gap_count",
                "first_timestamp", "last_timestamp", "transition_count",
                "data_age_seconds",
            ):
                self.assertIn(field, rows[1])
            self.assertEqual(json_meta["generated_at_utc"], rows[0]["generated_at_utc"])
            self.assertEqual(json_meta["row_count"], csv_meta["row_count"])

            empty_json = io.StringIO()
            empty_csv = io.StringIO()
            filters = {"entity_type": "service", "entity_id": "missing.service"}
            export_data(engine, window, "-", "json", "summary", filters, empty_json)
            export_data(engine, window, "-", "csv", "summary", filters, empty_csv)
            self.assertEqual(0, json.loads(empty_json.getvalue())["metadata"]["row_count"])
            empty_rows = list(csv.DictReader(io.StringIO(empty_csv.getvalue())))
            self.assertEqual(1, len(empty_rows))
            self.assertEqual("metadata", empty_rows[0]["record_type"])

    def test_json_csv_parity_for_raw_minute_hybrid_categorical_unavailable_and_truncated(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            insert_point(conn, NOW - 5, "host", "local", "cpu_utilization_percent", 20)
            insert_point(conn, NOW - 4, "service", "gost-iran-1.service", "service_active_state", "active", "state")
            insert_point(conn, NOW - 3, "host", "local", "unavailable_metric", None, "count", "unavailable")
            old = math.floor((NOW - 3 * 24 * 3600) / 60) * 60
            entity_pk = ensure_entity(conn, "host", "local", "local", {}, NOW)
            conn.execute(
                "INSERT INTO minute_rollups(entity_pk,metric_name,minute_start,samples,expected_samples,"
                "min_value,avg_value,max_value,unavailable_count,reset_count,gap_count,coverage,unit,quality) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (entity_pk, "cpu_utilization_percent", old, 12, 12, 1, 2, 3, 0, 0, 0, 1, "percent", "derived"),
            )
            conn.close()

            cases = [
                (QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW), resolve_window(NOW, "10m"), "raw"),
                (QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW), QueryWindow(old, old + 60, old, old + 60), "minute"),
                (
                    QueryEngine(
                        ReadOnlyDatabase(path),
                        clock=lambda: NOW,
                        retention=RetentionPolicy(raw_seconds=120, rollup_seconds=1000, event_seconds=1000),
                    ),
                    QueryWindow(NOW - 180, NOW, NOW - 180, NOW),
                    "auto",
                ),
            ]
            for engine, window, granularity in cases:
                with self.subTest(granularity=granularity):
                    json_stream, csv_stream = io.StringIO(), io.StringIO()
                    json_meta = export_data(
                        engine, window, "-", "json", granularity, stdout=json_stream
                    )
                    csv_meta = export_data(
                        engine, window, "-", "csv", granularity, stdout=csv_stream
                    )
                    csv_rows = list(csv.DictReader(io.StringIO(csv_stream.getvalue())))
                    self.assertEqual(json_meta["row_count"], csv_meta["row_count"])
                    self.assertEqual(json_meta["source_mode"], csv_rows[0]["source_mode"])
                    self.assertEqual(json_meta["generated_at_utc"], csv_rows[0]["generated_at_utc"])
                    self.assertEqual(json_meta["truncated"], csv_rows[0]["truncated"] == "true")

            truncated = resolve_window(
                NOW,
                "900s",
                retention=RetentionPolicy(raw_seconds=100, rollup_seconds=600, event_seconds=600),
            )
            self.assertTrue(truncated.truncated)
            stream = io.StringIO()
            metadata = export_data(
                QueryEngine(
                    ReadOnlyDatabase(path),
                    clock=lambda: NOW,
                    retention=RetentionPolicy(raw_seconds=100, rollup_seconds=600, event_seconds=600),
                ),
                truncated,
                "-",
                "csv",
                "summary",
                stdout=stream,
            )
            self.assertTrue(metadata["truncated"])
            self.assertIn("service_active_state", stream.getvalue())
            self.assertIn("unavailable_metric", stream.getvalue())

    def test_concurrent_commit_does_not_mix_export_count_and_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            insert_point(conn, NOW - 5, "host", "local", "cpu_utilization_percent", 20)
            conn.close()
            database = ReadOnlyDatabase(path)
            original = database.count_export_rows
            committed = []

            def count_then_commit(*args, **kwargs):
                count = original(*args, **kwargs)
                if not committed:
                    committed.append(True)
                    writer = connect_db(path)
                    insert_point(writer, NOW - 4, "host", "local", "memory_used_percent", 30)
                    writer.close()
                return count

            database.count_export_rows = count_then_commit
            engine = QueryEngine(database, clock=lambda: NOW)
            first = io.StringIO()
            export_data(engine, resolve_window(NOW, "10m"), "-", "json", "raw", stdout=first)
            self.assertEqual(1, json.loads(first.getvalue())["metadata"]["row_count"])
            second = io.StringIO()
            export_data(
                QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW),
                resolve_window(NOW, "10m"),
                "-", "json", "raw", stdout=second,
            )
            self.assertEqual(2, json.loads(second.getvalue())["metadata"]["row_count"])


if __name__ == "__main__":
    unittest.main()
