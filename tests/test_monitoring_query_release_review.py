#!/usr/bin/env python3
"""Release-blocker regressions for the final PR #16 review."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from monitoring.collector import CollectorConfig, collect_once
from monitoring.exporters import EXPORT_VERSION, export_data
from monitoring.health import evaluate_snapshot
from monitoring.metric_semantics import GAUGE, RATE
from monitoring.query_db import ReadOnlyDatabase
from monitoring.query_engine import QueryEngine, QueryLimits
from monitoring.query_models import QueryLimitError, QueryWindow
from monitoring.query_window import resolve_window
from monitoring.renderers import render_ansi_snapshot, render_snapshot_plain
from monitoring.schema import _cycle, connect_db, ensure_entity, init_db

try:
    from test_monitoring_coverage import (
        FIXTURES,
        fixture,
        integration_sources,
        write_tunnel_env,
    )
    from test_monitoring_query_final_review import point, rollup
except ModuleNotFoundError:
    from tests.test_monitoring_coverage import (
        FIXTURES,
        fixture,
        integration_sources,
        write_tunnel_env,
    )
    from tests.test_monitoring_query_final_review import point, rollup


NOW = 2_000_000_040
SEMANTIC_CASES = {
    "cpu_utilization_percent": ("percent", 10.0, 20.0),
    "rx_bytes_per_second": ("B/s", 100.0, 200.0),
    "rx_bytes": ("bytes", 1000.0, 2000.0),
    "service_restart_count": ("count", 1.0, 2.0),
    "process_cpu_ticks": ("ticks", 100.0, 200.0),
    "service_active": ("boolean", 0.0, 1.0),
    "service_main_pid": ("pid", 100.0, 200.0),
    "service_start_monotonic_us": ("microseconds", 1000.0, 2000.0),
    "last_successful_cycle_timestamp": ("unix_seconds", 1000.0, 2000.0),
    "service_active_state": ("state", "inactive", "active"),
    "remote_endpoint": ("endpoint", "old.invalid:1", "new.invalid:2"),
    "unknown_numeric_metric": ("count", 5.0, 7.0),
    "unavailable_metric": ("percent", None, None),
}


def add_event(
    conn,
    ts: int,
    severity: str,
    code: str,
    details: dict[str, object] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events(ts,severity,code,message,details_json) VALUES(?,?,?,?,?)",
        (ts, severity, code, code, json.dumps(details or {}, sort_keys=True)),
    )


def add_required_host_metrics(conn, ts: int) -> None:
    point(conn, ts, "host", "local", "cpu_utilization_percent", 10)
    point(conn, ts, "host", "local", "memory_used_percent", 20)
    point(conn, ts, "filesystem", "fs:/", "filesystem_used_percent", 30)


class MinuteExportSemanticsTests(unittest.TestCase):
    def _raw_minute_export(self):
        temp = tempfile.TemporaryDirectory()
        path = str(Path(temp.name) / "metrics.sqlite3")
        conn = init_db(path)
        start = NOW - 60
        for name, (unit, first, latest) in SEMANTIC_CASES.items():
            quality = "unavailable" if name == "unavailable_metric" else "exact"
            point(conn, start + 5, "host", "local", name, first, unit, quality)
            point(conn, start + 10, "host", "local", name, latest, unit, quality)
        conn.close()
        engine = QueryEngine(
            ReadOnlyDatabase(path),
            clock=lambda: NOW,
            limits=QueryLimits(max_query_rows=1),
        )
        window = QueryWindow(start, NOW, start, NOW)
        json_out, csv_out = io.StringIO(), io.StringIO()
        json_meta = export_data(
            engine, window, "-", "json", "minute", stdout=json_out
        )
        csv_meta = export_data(
            engine, window, "-", "csv", "minute", stdout=csv_out
        )
        return temp, json_meta, json.loads(json_out.getvalue()), csv_meta, list(
            csv.DictReader(io.StringIO(csv_out.getvalue()))
        )

    def test_raw_minute_preserves_latest_without_fabricated_statistics(self):
        temp, json_meta, payload, csv_meta, csv_rows = self._raw_minute_export()
        try:
            rows = {row["metric_name"]: row for row in payload["rows"]}
            csv_data = {row["metric_name"]: row for row in csv_rows[1:]}
            self.assertEqual(2, EXPORT_VERSION)
            self.assertEqual(EXPORT_VERSION, json_meta["export_version"])
            self.assertEqual(json_meta["row_count"], csv_meta["row_count"])
            for name in ("cpu_utilization_percent", "rx_bytes_per_second"):
                self.assertEqual("minute_statistics", rows[name]["aggregate_kind"])
                self.assertTrue(rows[name]["value_available"])
                self.assertIsNotNone(rows[name]["average"])
                self.assertIsNotNone(rows[name]["minimum"])
                self.assertIsNotNone(rows[name]["maximum"])
            self.assertEqual(GAUGE, rows["cpu_utilization_percent"]["metric_semantics"])
            self.assertEqual(RATE, rows["rx_bytes_per_second"]["metric_semantics"])
            for name in set(SEMANTIC_CASES) - {
                "cpu_utilization_percent", "rx_bytes_per_second", "unavailable_metric"
            }:
                row = rows[name]
                self.assertEqual("minute_latest", row["aggregate_kind"], name)
                self.assertTrue(row["value_available"], name)
                self.assertIsNone(row["average"], name)
                self.assertIsNone(row["minimum"], name)
                self.assertIsNone(row["maximum"], name)
                self.assertEqual(SEMANTIC_CASES[name][2], row["latest"], name)
                self.assertEqual(NOW - 50, row["latest_timestamp"], name)
            unavailable = rows["unavailable_metric"]
            self.assertEqual("historical_value_unavailable", unavailable["aggregate_kind"])
            self.assertFalse(unavailable["value_available"])
            self.assertEqual("unavailable", unavailable["quality"])
            for name, row in rows.items():
                csv_row = csv_data[name]
                self.assertEqual(row["aggregate_kind"], csv_row["aggregate_kind"])
                self.assertEqual(str(row["value_available"]), csv_row["value_available"])
                self.assertEqual(row["average"] is None, csv_row["average"] == "")
        finally:
            temp.cleanup()

    def test_stored_rollup_nonstatistical_values_are_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            entity = ensure_entity(conn, "host", "local", "local", {}, NOW)
            minute = NOW - 3 * 24 * 3600
            for name, (unit, _first, _latest) in SEMANTIC_CASES.items():
                unavailable = 12 if name == "unavailable_metric" else 0
                quality = "unavailable" if unavailable else "exact"
                average = None if unit in {"state", "endpoint"} or unavailable else 99.0
                rollup(
                    conn, entity, name, minute, average,
                    unavailable=unavailable, quality=quality, unit=unit,
                )
            conn.execute(
                "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)",
                ("minute_rollup_watermark", str(minute + 60)),
            )
            conn.close()
            engine = QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW)
            window = QueryWindow(minute, minute + 60, minute, minute + 60)
            json_out, csv_out = io.StringIO(), io.StringIO()
            export_data(engine, window, "-", "json", "minute", stdout=json_out)
            export_data(engine, window, "-", "csv", "minute", stdout=csv_out)
            rows = {
                row["metric_name"]: row
                for row in json.loads(json_out.getvalue())["rows"]
            }
            csv_rows = {
                row["metric_name"]: row
                for row in list(csv.DictReader(io.StringIO(csv_out.getvalue())))[1:]
            }
            for name in ("cpu_utilization_percent", "rx_bytes_per_second"):
                self.assertEqual("minute_statistics", rows[name]["aggregate_kind"])
                self.assertEqual(99.0, rows[name]["average"])
            for name in set(SEMANTIC_CASES) - {
                "cpu_utilization_percent", "rx_bytes_per_second"
            }:
                row = rows[name]
                self.assertEqual("historical_value_unavailable", row["aggregate_kind"], name)
                self.assertFalse(row["value_available"], name)
                self.assertIsNone(row["numeric_value"], name)
                self.assertIsNone(row["average"], name)
                self.assertEqual("", csv_rows[name]["average"], name)


class IncidentStateTests(unittest.TestCase):
    def _snapshot_with_events(self, events):
        temp = tempfile.TemporaryDirectory()
        path = str(Path(temp.name) / "metrics.sqlite3")
        conn = init_db(path)
        add_required_host_metrics(conn, NOW - 2)
        for event in events:
            add_event(conn, *event)
        conn.close()
        snapshot = QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW).snapshot()
        return temp, snapshot

    def test_failure_recovery_pairs_resolve_latest_incident_state(self):
        pairs = (
            ("metric_source_unavailable", "metric_source_available", {"source": "proc_stat"}),
            ("collection_failed", "collection_recovered", {}),
            ("wal_checkpoint_failed", "wal_checkpoint_recovered", {}),
            ("database_retention_failed", "database_retention_recovered", {}),
        )
        for failure, recovery, details in pairs:
            with self.subTest(failure=failure):
                temp, snapshot = self._snapshot_with_events([
                    (NOW - 10, "warning", failure, details),
                    (NOW - 5, "info", recovery, details),
                ])
                try:
                    self.assertEqual("healthy", evaluate_snapshot(snapshot)["overall"]["status"])
                    self.assertEqual(2, len(snapshot["health_events"]))
                finally:
                    temp.cleanup()

                temp, snapshot = self._snapshot_with_events([
                    (NOW - 10, "warning", failure, details),
                    (NOW - 5, "info", recovery, details),
                    (NOW - 1, "warning", failure, details),
                ])
                try:
                    self.assertEqual("degraded", evaluate_snapshot(snapshot)["overall"]["status"])
                finally:
                    temp.cleanup()

                temp, snapshot = self._snapshot_with_events([
                    (NOW - 5, "info", recovery, details),
                ])
                try:
                    self.assertEqual("healthy", evaluate_snapshot(snapshot)["overall"]["status"])
                finally:
                    temp.cleanup()

    def test_service_failure_recovery_sequence_uses_real_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            add_required_host_metrics(conn, NOW - 2)
            tunnel_pk = ensure_entity(
                conn, "tunnel", "iran-1", "iran-1",
                {"service": "gost-iran-1.service"}, NOW - 2,
            )
            conn.execute(
                "INSERT INTO tunnels(tunnel_id,entity_pk,side,tunnel_number,service_name,"
                "env_path,listen_ports_json,target_ports_json,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("iran-1", tunnel_pk, "iran", 1, "gost-iran-1.service", "/tmp/iran-1.env", "[2052]", "[2052]", NOW - 2),
            )
            point(conn, NOW - 2, "tunnel", "iran-1", "env_source_valid", 1, "boolean")
            point(conn, NOW - 2, "tunnel", "iran-1", "service_active", 1, "boolean")
            point(conn, NOW - 2, "tunnel", "iran-1", "listener_ownership_exact", 1, "boolean")
            point(conn, NOW - 2, "service", "gost-iran-1.service", "service_active", 1, "boolean")
            point(conn, NOW - 2, "service", "gost-iran-1.service", "process_rss_bytes", 100, "bytes")
            point(conn, NOW - 2, "service", "gost-iran-1.service", "listener_owned_count", 1, "count")
            add_event(conn, NOW - 10, "warning", "service_state_changed", {"service": "gost-iran-1.service", "previous": "active", "current": "failed"})
            add_event(conn, NOW - 5, "info", "service_state_changed", {"service": "gost-iran-1.service", "previous": "failed", "current": "active"})
            conn.close()
            engine = QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW)
            self.assertEqual("healthy", evaluate_snapshot(engine.snapshot())["overall"]["status"])
            writer = connect_db(path)
            add_event(writer, NOW - 1, "warning", "service_state_changed", {"service": "gost-iran-1.service", "previous": "active", "current": "failed"})
            writer.close()
            self.assertEqual("degraded", evaluate_snapshot(engine.snapshot())["overall"]["status"])


class CollectorMembershipEventTests(unittest.TestCase):
    def _sources(self, root: Path, mode: list[str], sample_index: list[int], monotonic: list[float]):
        def command(parts):
            if parts[0] == "ss":
                if "-lntp" in parts and mode[0] == "empty":
                    return ""
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

        return integration_sources(root, command, monotonic, reader)

    def test_listener_failure_is_cleared_by_retirement_and_new_failure_reappears(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = root / "env"
            write_tunnel_env(env)
            db = str(root / "metrics.sqlite3")
            mode, sample_index, monotonic = ["valid"], [0], [10.0]
            sources = self._sources(root, mode, sample_index, monotonic)
            config = CollectorConfig(filesystem_paths=(Path("/"), Path("/var/lib/gost-manager")))

            def collect(ts):
                collect_once(db, str(env), ts, sources, config)
                monotonic[0] += 5
                sample_index[0] += 1

            collect(1000)
            mode[0] = "empty"
            collect(1005)
            failed = QueryEngine(ReadOnlyDatabase(db), clock=lambda: 1005).snapshot()
            self.assertIn(evaluate_snapshot(failed)["overall"]["status"], {"critical", "unknown"})
            self.assertTrue(any(e["code"] == "listener_disappeared" for e in failed["health_events"]))

            (env / "iran-1.env").unlink()
            collect(1010)
            retired = QueryEngine(ReadOnlyDatabase(db), clock=lambda: 1010).snapshot()
            self.assertNotIn("iran-1", evaluate_snapshot(retired)["tunnels"])
            self.assertNotIn("recent_monitoring_event", evaluate_snapshot(retired)["overall"]["reason_codes"])
            history = QueryEngine(ReadOnlyDatabase(db), clock=lambda: 1010).events(
                QueryWindow(995, 1011, 995, 1011)
            )
            self.assertTrue(any(event.code == "listener_disappeared" for event in history))

            write_tunnel_env(env)
            mode[0] = "valid"
            collect(1015)
            mode[0] = "empty"
            collect(1020)
            recreated = QueryEngine(ReadOnlyDatabase(db), clock=lambda: 1020).snapshot()
            self.assertTrue(any(e["code"] == "listener_disappeared" and e["ts"] == 1020 for e in recreated["health_events"]))
            self.assertIn(evaluate_snapshot(recreated)["overall"]["status"], {"critical", "unknown"})

    def test_malformed_env_is_current_health_state_and_secret_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = root / "env"
            write_tunnel_env(env)
            db = str(root / "metrics.sqlite3")
            mode, sample_index, monotonic = ["valid"], [0], [10.0]
            sources = self._sources(root, mode, sample_index, monotonic)
            config = CollectorConfig(filesystem_paths=(Path("/"), Path("/var/lib/gost-manager")))
            collect_once(db, str(env), 1000, sources, config)
            (env / "iran-1.env").write_text(
                "MAPPINGS=invalid\nPASSWORD=release-secret-canary\n",
                encoding="utf-8",
            )
            (env / "kharej-2.env").write_text("TUNNEL_PORT=invalid\n", encoding="utf-8")
            monotonic[0] += 5
            sample_index[0] = 1
            collect_once(db, str(env), 1005, sources, config)
            engine = QueryEngine(ReadOnlyDatabase(db), clock=lambda: 1005)
            malformed = engine.snapshot()
            self.assertEqual(2, len(malformed["invalid_managed_env_sources"]))
            health = evaluate_snapshot(malformed)["overall"]
            self.assertIn("managed_env_invalid", health["reason_codes"])
            rendered = render_snapshot_plain(malformed) + render_ansi_snapshot(malformed)
            self.assertNotIn("release-secret-canary", rendered)
            conn = connect_db(db)
            serialized = " ".join(
                str(value)
                for row in conn.execute("SELECT message,details_json FROM events")
                for value in row
            )
            conn.close()
            self.assertNotIn("release-secret-canary", serialized)

            write_tunnel_env(env)
            (env / "kharej-2.env").unlink()
            monotonic[0] += 5
            sample_index[0] = 2
            collect_once(db, str(env), 1010, sources, config)
            recovered = QueryEngine(ReadOnlyDatabase(db), clock=lambda: 1010).snapshot()
            self.assertEqual([], recovered["invalid_managed_env_sources"])
            self.assertNotIn("managed_env_invalid", evaluate_snapshot(recovered)["overall"]["reason_codes"])

            (env / "iran-1.env").unlink()
            monotonic[0] += 5
            sample_index[0] = 3
            collect_once(db, str(env), 1015, sources, config)
            removed = QueryEngine(ReadOnlyDatabase(db), clock=lambda: 1015).snapshot()
            self.assertEqual([], removed["invalid_managed_env_sources"])
            self.assertNotIn("managed_env_invalid", evaluate_snapshot(removed)["overall"]["reason_codes"])

    def test_malformed_env_on_first_collection_degrades_without_secret_exposure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = root / "env"
            env.mkdir()
            (env / "iran-1.env").write_text(
                "MAPPINGS=invalid\nPASSWORD=first-cycle-secret-canary\n",
                encoding="utf-8",
            )
            db = str(root / "metrics.sqlite3")
            mode, sample_index, monotonic = ["valid"], [0], [10.0]
            sources = self._sources(root, mode, sample_index, monotonic)
            config = CollectorConfig(filesystem_paths=(Path("/"), Path("/var/lib/gost-manager")))
            collect_once(db, str(env), 1000, sources, config)

            snapshot = QueryEngine(ReadOnlyDatabase(db), clock=lambda: 1000).snapshot()
            self.assertEqual(1, len(snapshot["invalid_managed_env_sources"]))
            self.assertIn(
                "managed_env_invalid",
                evaluate_snapshot(snapshot)["overall"]["reason_codes"],
            )
            self.assertNotIn("first-cycle-secret-canary", json.dumps(snapshot))
            self.assertNotIn(
                "first-cycle-secret-canary",
                render_snapshot_plain(snapshot) + render_ansi_snapshot(snapshot),
            )


class InterfaceMembershipTests(unittest.TestCase):
    def test_compact_snapshot_ignores_retired_interfaces_but_history_remains(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            add_required_host_metrics(conn, NOW - 2)
            point(conn, NOW - 2, "interface", "interface:external-total", "rx_bytes_per_second", 100, "B/s")
            point(conn, NOW - 2, "interface", "interface:lo", "rx_bytes_per_second", 10, "B/s")
            for index in range(300):
                point(conn, NOW - 100, "interface", f"interface:veth{index}", "rx_bytes_per_second", index, "B/s")
            conn.close()
            engine = QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW)
            snapshot = engine.snapshot()
            current = {
                item["entity_id"]
                for item in snapshot["metrics"]
                if item["entity_type"] == "interface"
            }
            self.assertEqual({"interface:external-total", "interface:lo"}, current)
            history = engine.summary(
                QueryWindow(NOW - 110, NOW - 90, NOW - 110, NOW - 90),
                entity_type="interface",
                entity_id="interface:veth299",
                require_match=True,
            )
            self.assertTrue(history.series)


class StreamBudgetAndWatermarkTests(unittest.TestCase):
    def test_stream_scan_limit_fails_before_export_file_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            for index in range(101):
                point(conn, NOW - 1000 + index * 5, "host", "local", "cpu_utilization_percent", index)
            conn.close()
            limits = QueryLimits(max_query_rows=10, max_stream_scan_rows=100)
            engine = QueryEngine(ReadOnlyDatabase(path), clock=lambda: NOW, limits=limits)
            window = resolve_window(NOW, "24h")
            with self.assertRaisesRegex(QueryLimitError, "stream_scan_limit"):
                engine.summary(window)
            destination = Path(temp) / "must-not-exist.json"
            with self.assertRaisesRegex(QueryLimitError, "stream_scan_limit"):
                export_data(engine, window, str(destination), "json", "minute")
            self.assertFalse(destination.exists())

            conn = connect_db(path)
            conn.execute(
                "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)",
                ("minute_rollup_watermark", str(NOW + 60)),
            )
            conn.close()
            invalid_destination = Path(temp) / "invalid-watermark-must-not-exist.csv"
            with self.assertRaisesRegex(QueryLimitError, "stream_scan_limit"):
                export_data(
                    engine,
                    window,
                    str(invalid_destination),
                    "csv",
                    "auto",
                )
            self.assertFalse(invalid_destination.exists())

    def test_watermark_validation_and_clock_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            cases = (
                (str(NOW), NOW, (NOW, "valid")),
                (str(NOW - 20 * 60), NOW, (NOW - 20 * 60, "valid")),
                (str(NOW + 60), NOW, (None, "invalid_future")),
                (str(NOW - 1), NOW, (None, "invalid_misaligned")),
                ("-60", NOW, (None, "invalid_negative")),
                ("not-a-number", NOW, (None, "invalid_nonnumeric")),
                (str(NOW), NOW - 120, (None, "invalid_future")),
            )
            for raw, clock_now, expected in cases:
                with self.subTest(raw=raw, now=clock_now):
                    conn.execute(
                        "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)",
                        ("minute_rollup_watermark", raw),
                    )
                    self.assertEqual(
                        expected,
                        ReadOnlyDatabase.rollup_watermark_status(conn, clock_now),
                    )
            conn.close()

    def test_default_scan_budget_uses_bounded_limit_sentinel(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "metrics.sqlite3")
            conn = init_db(path)
            point(conn, NOW - 5, "host", "local", "cpu_utilization_percent", 1)
            conn.close()
            database = ReadOnlyDatabase(path)
            original = database.bounded_point_count
            observed = []

            def simulated(conn, source, start, end, limit, *filters):
                observed.append(limit)
                if limit == 1_000_000:
                    return 1_000_001
                return original(conn, source, start, end, limit, *filters)

            database.bounded_point_count = simulated
            engine = QueryEngine(
                database,
                clock=lambda: NOW,
                limits=QueryLimits(max_query_rows=0),
            )
            with self.assertRaisesRegex(QueryLimitError, "1,000,000|1000000"):
                engine.summary(resolve_window(NOW, "24h"))
            self.assertIn(1_000_000, observed)


if __name__ == "__main__":
    unittest.main()
