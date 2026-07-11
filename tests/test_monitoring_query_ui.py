import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from monitoring.health import evaluate_snapshot
from monitoring.query_cli import main
from monitoring.query_models import QueryDatabaseError, QueryInputError, QueryLimitError
from monitoring.renderers import (
    CLEAR_SCREEN,
    HIDE_CURSOR,
    SHOW_CURSOR,
    ansi_enabled,
    render_ansi_snapshot,
    render_snapshot_plain,
    run_live,
)
from monitoring.schema import _cycle, ensure_entity, init_db


NOW = 2_000_000_000
FIXTURES = Path(__file__).parent / "fixtures" / "monitoring" / "query-ui"


def metric(kind, entity_id, name, value, unit="percent", quality="exact", ts=NOW - 2):
    return {
        "entity_type": kind,
        "entity_id": entity_id,
        "metric_name": name,
        "ts": ts,
        "numeric_value": value if isinstance(value, (int, float)) else None,
        "text_value": value if isinstance(value, str) else None,
        "unit": unit,
        "quality": quality,
        "reset": 0,
        "gap": 0,
    }


def representative_snapshot():
    metrics = [
        metric("host", "local", "cpu_utilization_percent", 20, quality="derived"),
        metric("host", "local", "memory_used_percent", 50, quality="derived"),
        metric("host", "local", "load1", 0.2, "load"),
        metric("host", "local", "load5", 0.3, "load"),
        metric("host", "local", "load15", 0.4, "load"),
        metric("host", "local", "conntrack_utilization_percent", 10, quality="derived"),
        metric("host", "local", "file_handles_utilization_percent", 5, quality="derived"),
        metric("filesystem", "fs:/", "filesystem_used_percent", 45, quality="derived"),
        metric("filesystem", "fs:/var/lib/gost-manager", "filesystem_used_percent", 35, quality="derived"),
        metric("interface", "interface:external-total", "rx_bytes_per_second", 1000, "bytes_per_second", "derived"),
        metric("interface", "interface:external-total", "tx_bytes_per_second", 500, "bytes_per_second", "derived"),
        metric("host", "local", "tcp_state_estab", 4, "count"),
        metric("service", "nginx.service", "service_active", 1, "boolean"),
        metric("service", "nginx.service", "service_active_state", "active", "state"),
        metric("service", "nginx.service", "process_cpu_percent", 2),
        metric("service", "nginx.service", "process_rss_bytes", 4096, "bytes"),
        metric("service", "nginx.service", "process_count", 3, "count"),
        metric("service", "nginx.service", "process_open_fds", 20, "count"),
        metric("service", "nginx.service", "listener_owned_count", 2, "count"),
        metric("service", "nginx.service", "established_sockets_total", 4, "count"),
        metric("service", "nginx.service", "service_restart_count", 0, "count"),
        metric("tunnel", "iran-1", "service_active", 1, "boolean"),
        metric("tunnel", "iran-1", "listener_ownership_exact", 1, "boolean"),
        metric("tunnel", "iran-1", "configured_listener_count", 1, "count"),
        metric("tunnel", "iran-1", "observed_listener_count", 1, "count"),
        metric("tunnel", "iran-1", "remote_endpoint", "example.invalid:443", "endpoint"),
        metric("tunnel", "iran-1", "established_remote_sockets", 1, "count"),
        metric("tunnel", "iran-1", "process_cpu_percent", 1),
        metric("tunnel", "iran-1", "process_rss_bytes", 2048, "bytes"),
        metric("tunnel", "iran-1", "process_open_fds", 8, "count"),
        metric("collector", "local", "cycle_status", 1, "boolean"),
        metric("collector", "local", "duration_seconds", 0.15, "seconds", "derived"),
        metric("collector", "local", "missed_deadlines", 0, "count"),
        metric("collector", "local", "source_errors_total", 0, "count"),
        metric("collector", "local", "database_size_bytes", 123456, "bytes"),
        metric("collector", "local", "database_wal_size_bytes", 0, "bytes"),
        metric("collector", "local", "checkpoint_success", 1, "boolean"),
        metric("collector", "local", "last_successful_cycle_timestamp", NOW - 2, "unix_seconds"),
    ]
    return {
        "generated_at": NOW,
        "schema_version": 4,
        "cycle": {
            "collected_at": NOW - 2,
            "duration_seconds": 0.15,
            "success": True,
            "overrun": False,
            "missed_deadlines": 0,
            "overrun_seconds": 0,
        },
        "metrics": metrics,
        "entities": [
            {
                "entity_type": "tunnel",
                "entity_id": "iran-1",
                "display_name": "iran-1",
                "metadata": {"service": "gost-iran-1.service"},
                "updated_at": NOW,
            }
        ],
        "events": [
            {
                "ts": NOW - 10,
                "severity": "info",
                "code": "metric_source_available",
                "message": "Metric source recovered",
                "details": {"source": "proc_stat"},
            }
        ],
    }


class HealthTests(unittest.TestCase):
    def test_no_data_stale_and_failed_cycle(self):
        no_data = evaluate_snapshot({"generated_at": NOW, "metrics": [], "events": []})
        self.assertEqual("unknown", no_data["overall"]["status"])
        stale = representative_snapshot()
        stale["cycle"]["collected_at"] = NOW - 100
        self.assertEqual("unknown", evaluate_snapshot(stale)["overall"]["status"])
        failed = representative_snapshot()
        failed["cycle"]["success"] = False
        self.assertEqual("critical", evaluate_snapshot(failed)["overall"]["status"])

    def test_threshold_reasons_and_estimated_cannot_be_critical(self):
        snapshot = representative_snapshot()
        for item in snapshot["metrics"]:
            if item["metric_name"] == "filesystem_used_percent" and item["entity_id"] == "fs:/":
                item["numeric_value"] = 96
        result = evaluate_snapshot(snapshot)["overall"]
        self.assertEqual("critical", result["status"])
        self.assertIn("filesystem_critical", result["reason_codes"])
        snapshot["metrics"] = [
            {**item, "quality": "estimated"}
            if item["metric_name"] == "filesystem_used_percent" and item["entity_id"] == "fs:/"
            else item
            for item in snapshot["metrics"]
        ]
        result = evaluate_snapshot(snapshot)["overall"]
        self.assertEqual("degraded", result["status"])
        self.assertIn("filesystem_estimated_high", result["reason_codes"])

    def test_service_down_unknown_and_healthy(self):
        snapshot = representative_snapshot()
        self.assertEqual("healthy", evaluate_snapshot(snapshot)["services"]["nginx.service"]["status"])
        active = next(item for item in snapshot["metrics"] if item["entity_type"] == "service" and item["metric_name"] == "service_active")
        active["numeric_value"] = 0
        evaluated = evaluate_snapshot(snapshot)
        self.assertEqual("down", evaluated["services"]["nginx.service"]["status"])
        self.assertEqual("critical", evaluated["overall"]["status"])
        active["numeric_value"] = None
        active["quality"] = "unavailable"
        self.assertEqual("unknown", evaluate_snapshot(snapshot)["services"]["nginx.service"]["status"])
        active["numeric_value"] = 1
        active["quality"] = "estimated"
        self.assertEqual("unknown", evaluate_snapshot(snapshot)["services"]["nginx.service"]["status"])

    def test_tunnel_down_unknown_and_healthy(self):
        snapshot = representative_snapshot()
        self.assertEqual("healthy", evaluate_snapshot(snapshot)["tunnels"]["iran-1"]["status"])
        ownership = next(item for item in snapshot["metrics"] if item["entity_type"] == "tunnel" and item["metric_name"] == "listener_ownership_exact")
        ownership["numeric_value"] = 0
        self.assertEqual("down", evaluate_snapshot(snapshot)["tunnels"]["iran-1"]["status"])
        ownership["numeric_value"] = None
        ownership["quality"] = "unavailable"
        self.assertEqual("unknown", evaluate_snapshot(snapshot)["tunnels"]["iran-1"]["status"])

    def test_recent_source_failure_degrades_node(self):
        snapshot = representative_snapshot()
        snapshot["events"] = [{"ts": NOW - 1, "code": "metric_source_unavailable"}]
        result = evaluate_snapshot(snapshot)["overall"]
        self.assertEqual("degraded", result["status"])
        self.assertIn("recent_monitoring_event", result["reason_codes"])


class RendererTests(unittest.TestCase):
    def test_plain_snapshot_has_mandatory_sections_and_safe_values(self):
        rendered = render_snapshot_plain(representative_snapshot(), width=100)
        for section in ("OVERALL", "HOST", "NETWORK", "TCP", "NGINX / SERVICES", "TUNNELS", "COLLECTOR / DATABASE", "RECENT EVENTS"):
            self.assertIn(section, rendered)
        self.assertNotIn("password", rendered.lower())

    def test_ansi_selection_and_fallbacks(self):
        self.assertTrue(ansi_enabled(lambda: True, {"TERM": "xterm"}, False))
        self.assertFalse(ansi_enabled(lambda: False, {"TERM": "xterm"}, False))
        self.assertFalse(ansi_enabled(lambda: True, {"TERM": "dumb"}, False))
        self.assertFalse(ansi_enabled(lambda: True, {"TERM": "xterm", "NO_COLOR": "1"}, False))

    def test_live_finite_non_tty_and_narrow_terminal(self):
        output = io.StringIO()
        sleeps = []
        status = run_live(
            representative_snapshot,
            output,
            refresh=1,
            iterations=2,
            sleeper=sleeps.append,
            terminal_size=lambda: os.terminal_size((30, 20)),
            isatty=lambda: False,
            environ={"TERM": "xterm"},
        )
        self.assertEqual(0, status)
        self.assertEqual([1], sleeps)
        self.assertNotIn("\x1b", output.getvalue())
        self.assertLessEqual(max(map(len, output.getvalue().splitlines())), 30)

    def test_live_ansi_restores_cursor_on_interrupt_and_exception(self):
        for failure in (KeyboardInterrupt(), RuntimeError("injected")):
            output = io.StringIO()
            def provider():
                raise failure
            if isinstance(failure, KeyboardInterrupt):
                self.assertEqual(130, run_live(provider, output, iterations=1, isatty=lambda: True, environ={"TERM": "xterm"}))
            else:
                with self.assertRaises(RuntimeError):
                    run_live(provider, output, iterations=1, isatty=lambda: True, environ={"TERM": "xterm"})
            self.assertTrue(output.getvalue().startswith(HIDE_CURSOR))
            self.assertTrue(output.getvalue().endswith(SHOW_CURSOR))

    def test_captured_plain_and_ansi_normalized_fixtures(self):
        plain = render_snapshot_plain(representative_snapshot(), width=100)
        ansi = render_ansi_snapshot(representative_snapshot(), width=100)
        normalized = re.sub(r"\x1b\[[0-9;]*m", "", ansi)
        self.assertEqual((FIXTURES / "snapshot-plain.txt").read_text(), plain)
        self.assertEqual((FIXTURES / "snapshot-ansi-normalized.txt").read_text(), normalized)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "metrics.sqlite3")
        conn = init_db(self.path)
        ts = NOW - 5
        cycle = _cycle(conn, ts, float(ts), float(ts) + 0.1, 0.1, True, False)
        entity_pk = ensure_entity(conn, "host", "local", "local", {}, ts)
        conn.execute(
            "INSERT INTO metric_points(cycle_id,entity_pk,metric_name,ts,numeric_value,text_value,unit,quality,reset,gap) VALUES(?,?,?,?,?,?,?,?,0,0)",
            (cycle, entity_pk, "cpu_utilization_percent", ts, 10, None, "percent", "derived"),
        )
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def call(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(argv, stdout, stderr, clock=lambda: NOW, sleeper=lambda _seconds: None)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_snapshot_summary_and_export_smoke(self):
        code, snapshot, _ = self.call(["snapshot", "--db", self.path])
        self.assertEqual(0, code)
        self.assertIn("OVERALL", snapshot)
        code, summary, _ = self.call(["summary", "--db", self.path, "--window", "10m"])
        self.assertEqual(0, code)
        self.assertIn("cpu_utilization_percent", summary)
        code, exported, _ = self.call(["export", "--db", self.path, "--window", "10m", "--format", "json", "--output", "-"])
        self.assertEqual(0, code)
        self.assertEqual(1, json.loads(exported)["metadata"]["row_count"])

    def test_stable_error_codes_and_no_traceback(self):
        code, _, error = self.call(["snapshot", "--db", str(Path(self.temp.name) / "missing.db")])
        self.assertEqual(QueryDatabaseError.exit_code, code)
        self.assertNotIn("Traceback", error)
        code, _, error = self.call(["summary", "--db", self.path, "--window", "0s"])
        self.assertEqual(QueryInputError.exit_code, code)
        self.assertNotIn("Traceback", error)
        self.assertEqual(4, QueryLimitError.exit_code)

    def test_missing_entity_is_input_error(self):
        code, _, error = self.call(["service", "missing.service", "--db", self.path, "--window", "10m"])
        self.assertEqual(2, code)
        self.assertIn("no matching", error)


if __name__ == "__main__":
    unittest.main()
