#!/usr/bin/env python3
"""Regression coverage for the PR #15 technical re-review."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from monitoring import collector as collector_module
from monitoring.collector import (
    MAX_TRACKED_SOURCE_ERRORS,
    Capture,
    CollectorConfig,
    CommandResult,
    collect_once,
)
from monitoring.schema import get_json_state, init_db, set_json_state, set_state

try:
    from test_monitoring_coverage import (
        FIXTURES,
        fixture,
        integration_sources,
        metric,
        stored_metric,
        write_tunnel_env,
    )
except ModuleNotFoundError:
    from tests.test_monitoring_coverage import (
        FIXTURES,
        fixture,
        integration_sources,
        metric,
        stored_metric,
        write_tunnel_env,
    )


class MonitoringTechnicalRereviewTests(unittest.TestCase):
    def test_nginx_master_and_workers_are_aggregated_as_one_service(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def command(parts):
                if parts[0] == "ss":
                    return CommandResult(fixture("ss-nginx-multiprocess.txt"), "", 0)
                return fixture("systemd-nginx.txt")

            db = str(root / "metrics.sqlite3")
            collect_once(
                db,
                str(root / "env"),
                1000,
                integration_sources(root, command),
                CollectorConfig(filesystem_paths=(Path("/"),)),
            )
            conn = init_db(db)
            self.assertEqual(stored_metric(conn, "nginx.service", "service_main_pid")[0], 3131)
            self.assertEqual(stored_metric(conn, "nginx.service", "process_count")[0], 3)
            self.assertEqual(stored_metric(conn, "nginx.service", "process_cpu_ticks")[0], 900)
            self.assertEqual(stored_metric(conn, "nginx.service", "process_rss_bytes")[0], 600 * 4096)
            self.assertEqual(stored_metric(conn, "nginx.service", "process_threads")[0], 6)
            self.assertEqual(stored_metric(conn, "nginx.service", "process_open_fds")[0], 9)
            self.assertEqual(stored_metric(conn, "nginx.service", "listener_owned_count")[0], 1)
            self.assertEqual(stored_metric(conn, "nginx.service", "established_sockets_total")[0], 2)
            self.assertEqual(stored_metric(conn, "nginx.service", "established_sockets_total")[2], "exact")

    def test_service_total_and_tunnel_remote_socket_counts_are_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_dir = root / "env"
            write_tunnel_env(env_dir)

            def command(parts):
                if parts[0] == "ss":
                    return CommandResult(fixture("ss-gost-inbound-outbound.txt"), "", 0)
                return fixture("systemd-gost.txt")

            db = str(root / "metrics.sqlite3")
            collect_once(
                db,
                str(env_dir),
                1000,
                integration_sources(root, command),
                CollectorConfig(filesystem_paths=(Path("/"),)),
            )
            conn = init_db(db)
            self.assertEqual(
                stored_metric(conn, "gost-iran-1.service", "established_sockets_total"),
                (2.0, None, "exact"),
            )
            self.assertEqual(
                stored_metric(conn, "iran-1", "established_remote_sockets"),
                (1.0, None, "exact"),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM metric_points p JOIN entities e "
                    "ON e.entity_pk=p.entity_pk WHERE e.entity_id='gost-iran-1.service' "
                    "AND p.metric_name='established_remote_sockets'"
                ).fetchone()[0],
                0,
            )

    def test_main_pid_fallback_is_estimated_not_an_authoritative_service_total(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            properties = fixture("systemd-gost.txt").replace(
                "ControlGroup=/system.slice/gost-iran-1.service",
                "ControlGroup=/missing.service",
            )

            def command(parts):
                if parts[0] == "ss":
                    return CommandResult(fixture("ss-gost-inbound-outbound.txt"), "", 0)
                return properties

            db = str(root / "metrics.sqlite3")
            collect_once(
                db,
                str(root / "env"),
                1000,
                integration_sources(root, command),
                CollectorConfig(filesystem_paths=(Path("/"),)),
            )
            conn = init_db(db)
            self.assertEqual(stored_metric(conn, "nginx.service", "service_main_pid")[2], "exact")
            self.assertEqual(stored_metric(conn, "nginx.service", "process_count")[2], "estimated")
            self.assertEqual(stored_metric(conn, "nginx.service", "process_rss_bytes")[2], "estimated")
            self.assertEqual(stored_metric(conn, "nginx.service", "listener_owned_count")[2], "unavailable")
            self.assertEqual(stored_metric(conn, "nginx.service", "established_sockets_total")[2], "unavailable")

    def test_hostname_endpoint_correlation_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_dir = root / "env"
            env_dir.mkdir()
            (env_dir / "iran-1.env").write_text(
                "KHAREJ_IP=kharej.example\nTUNNEL_PORT=28420\nMAPPINGS=2052:2052\n",
                encoding="utf-8",
            )

            def command(parts):
                if parts[0] == "ss":
                    return CommandResult(fixture("ss-gost-inbound-outbound.txt"), "", 0)
                return fixture("systemd-gost.txt")

            db = str(root / "metrics.sqlite3")
            collect_once(
                db,
                str(env_dir),
                1000,
                integration_sources(root, command),
                CollectorConfig(filesystem_paths=(Path("/"),)),
            )
            conn = init_db(db)
            self.assertEqual(
                stored_metric(conn, "iran-1", "established_remote_sockets")[2],
                "unavailable",
            )

    def test_failed_full_snapshot_obeys_attempt_cadence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            calls: list[tuple[str, ...]] = []
            mono = [10.0]

            def command(parts):
                calls.append(tuple(parts))
                if parts[0] == "ss" and parts[-1] == "-tanp":
                    return CommandResult("", "failed", 1)
                if parts[0] == "ss":
                    return CommandResult("", "", 0)
                return "ActiveState=inactive\nSubState=dead\nMainPID=0\nNRestarts=0\n"

            db = str(root / "metrics.sqlite3")
            config = CollectorConfig(
                sample_interval=5.0,
                tcp_snapshot_interval=30.0,
                filesystem_paths=(Path("/"),),
            )
            sources = integration_sources(root, command, mono)
            for offset in range(0, 30, 5):
                collect_once(db, str(root / "env"), 1000 + offset, sources, config)
                mono[0] += 5
            light = [call for call in calls if call[:3] == ("ss", "-H", "-lntp")]
            full = [call for call in calls if call[:3] == ("ss", "-H", "-tanp")]
            self.assertEqual(len(light), 6)
            self.assertEqual(len(full), 1)
            conn = init_db(db)
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM collector_state WHERE key='tcp_snapshot_last_attempt_ts'"
                ).fetchone()[0],
                "1000",
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT value FROM collector_state WHERE key='tcp_snapshot_last_success_ts'"
                ).fetchone()
            )
            cadences = get_json_state(conn, "metric_cadence_seconds")
            self.assertEqual(
                cadences["collector_source:source_ss_connections_available"],
                30.0,
            )

    def test_malformed_ss_is_unavailable_and_never_emits_false_listener_event(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_dir = root / "env"
            write_tunnel_env(env_dir)
            malformed = [False]
            mono = [10.0]

            def command(parts):
                if parts[0] == "ss":
                    if parts[-1] == "-lntp" and malformed[0]:
                        return CommandResult(fixture("ss-malformed.txt"), "", 0)
                    return CommandResult(fixture("ss-gost-inbound-outbound.txt"), "", 0)
                return fixture("systemd-gost.txt")

            sources = integration_sources(root, command, mono)
            config = CollectorConfig(filesystem_paths=(Path("/"),))
            db = str(root / "metrics.sqlite3")
            collect_once(db, str(env_dir), 1000, sources, config)
            malformed[0] = True
            mono[0] += 5
            collect_once(db, str(env_dir), 1005, sources, config)
            conn = init_db(db)
            self.assertEqual(stored_metric(conn, "iran-1", "listener_ownership_exact")[2], "unavailable")
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE code='listener_disappeared'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE code='metric_source_unavailable' "
                    "AND details_json LIKE '%ss_listeners%'"
                ).fetchone()[0],
                1,
            )
            conn.close()
            malformed[0] = False
            mono[0] += 5
            collect_once(db, str(env_dir), 1010, sources, config)
            conn = init_db(db)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE code='metric_source_available' "
                    "AND details_json LIKE '%ss_listeners%'"
                ).fetchone()[0],
                1,
            )

    def test_malformed_proc_network_sources_recover_without_exact_zero(self):
        cases = (
            ("net/dev", "net/dev-malformed", "interface:external-total", "rx_bytes"),
            ("net/snmp", "net/snmp-malformed", "local", "tcp_active_opens"),
            ("net/netstat", "net/netstat-malformed", "local", "tcp_listen_drops"),
        )
        for relative, malformed_fixture, entity_id, metric_name in cases:
            with self.subTest(source=relative), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                invalid = [True]
                mono = [10.0]
                target = FIXTURES / "proc" / relative

                def reader(path):
                    if invalid[0] and path == target:
                        return fixture(malformed_fixture)
                    return path.read_text(encoding="utf-8")

                def command(parts):
                    if parts[0] == "ss":
                        return CommandResult("", "", 0)
                    return "ActiveState=inactive\nSubState=dead\nMainPID=0\nNRestarts=0\n"

                source_name = "proc_net_" + relative.split("/")[-1]
                db = str(root / "metrics.sqlite3")
                config = CollectorConfig(filesystem_paths=(Path("/"),))
                sources = integration_sources(root, command, mono, reader)
                collect_once(db, str(root / "env"), 1000, sources, config)
                conn = init_db(db)
                self.assertEqual(stored_metric(conn, entity_id, metric_name)[2], "unavailable")
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM events WHERE code='metric_source_unavailable' "
                        "AND details_json LIKE ?",
                        (f"%{source_name}%",),
                    ).fetchone()[0],
                    1,
                )
                conn.close()
                invalid[0] = False
                mono[0] += 5
                collect_once(db, str(root / "env"), 1005, sources, config)
                conn = init_db(db)
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM events WHERE code='metric_source_available' "
                        "AND details_json LIKE ?",
                        (f"%{source_name}%",),
                    ).fetchone()[0],
                    1,
                )

    def test_systemd_and_cgroup_memory_use_quality_precedence(self):
        cases = (
            (
                "systemd-wins",
                "ActiveState=inactive\nSubState=dead\nMainPID=0\nMemoryCurrent=111\n"
                "MemoryPeak=222\nControlGroup=/missing.service\n",
                111,
                222,
            ),
            (
                "cgroup-wins",
                fixture("systemd-gost.txt").replace("MemoryCurrent=8388608\n", "").replace("MemoryPeak=12582912\n", ""),
                8_388_608,
                12_582_912,
            ),
        )
        for label, properties, current, peak in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)

                def command(parts):
                    if parts[0] == "ss":
                        return CommandResult("", "", 0)
                    return properties

                db = str(root / "metrics.sqlite3")
                collect_once(
                    db,
                    str(root / "env"),
                    1000,
                    integration_sources(root, command),
                    CollectorConfig(filesystem_paths=(Path("/"),)),
                )
                conn = init_db(db)
                self.assertEqual(
                    stored_metric(conn, "nginx.service", "cgroup_memory_current_bytes"),
                    (float(current), None, "exact"),
                )
                self.assertEqual(
                    stored_metric(conn, "nginx.service", "cgroup_memory_peak_bytes"),
                    (float(peak), None, "exact"),
                )
                if label == "cgroup-wins":
                    conn.close()
                    collect_once(
                        db,
                        str(root / "env"),
                        1005,
                        integration_sources(root, command, [15.0]),
                        CollectorConfig(filesystem_paths=(Path("/"),)),
                    )
                    conn = init_db(db)
                    self.assertEqual(
                        stored_metric(conn, "nginx.service", "cgroup_memory_current_bytes"),
                        (float(current), None, "estimated"),
                    )

    def test_slow_checkpoint_is_included_in_duration_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mono = [10.0]

            def command(parts):
                if parts[0] == "ss":
                    return CommandResult("", "", 0)
                return "ActiveState=inactive\nSubState=dead\nMainPID=0\nNRestarts=0\n"

            def checkpoint(_path):
                mono[0] += 2.5
                return (0, 4, 4)

            db = str(root / "metrics.sqlite3")
            collect_once(
                db,
                str(root / "env"),
                1000,
                integration_sources(root, command, mono),
                CollectorConfig(filesystem_paths=(Path("/"),)),
                maintenance=True,
                checkpoint=checkpoint,
            )
            conn = init_db(db)
            self.assertEqual(stored_metric(conn, "local", "checkpoint_duration_seconds")[0], 2.5)
            self.assertGreaterEqual(stored_metric(conn, "local", "duration_seconds")[0], 2.5)
            self.assertGreaterEqual(stored_metric(conn, "local", "maintenance_duration_seconds")[0], 2.5)
            self.assertEqual(stored_metric(conn, "local", "metrics_written")[2], "estimated")
            self.assertEqual(stored_metric(conn, "local", "events_written")[2], "estimated")
            self.assertGreaterEqual(
                conn.execute("SELECT duration_seconds FROM sample_cycles").fetchone()[0],
                2.5,
            )

    def test_fast_and_slow_process_sources_have_deterministic_call_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mono = [10.0]
            process_stat_reads = [0]
            process_status_reads = [0]
            process_limits_reads = [0]
            cgroup_memory_reads = [0]
            fd_lists = [0]
            statvfs_calls = [0]
            command_calls: list[tuple[str, ...]] = []

            def reader(path):
                if path.name == "stat" and path.parent.name.isdigit():
                    process_stat_reads[0] += 1
                elif path.name == "status" and path.parent.name.isdigit():
                    process_status_reads[0] += 1
                elif path.name == "limits" and path.parent.name.isdigit():
                    process_limits_reads[0] += 1
                elif path.name in {"memory.current", "memory.peak"}:
                    cgroup_memory_reads[0] += 1
                return path.read_text(encoding="utf-8")

            def list_dir(_path):
                fd_lists[0] += 1
                return [str(index) for index in range(10_000)]

            def statvfs(_path):
                statvfs_calls[0] += 1
                return integration_sources(root, command).statvfs(_path)

            def command(parts):
                command_calls.append(tuple(parts))
                if parts[0] == "ss":
                    return CommandResult("", "", 0)
                return fixture("systemd-nginx.txt")

            base = integration_sources(root, command, mono, reader)
            sources = dataclasses.replace(base, list_dir=list_dir, statvfs=statvfs)
            config = CollectorConfig(
                sample_interval=5.0,
                tcp_snapshot_interval=30.0,
                slow_sample_interval=60.0,
                filesystem_paths=(Path("/"),),
            )
            db = str(root / "metrics.sqlite3")
            for offset in range(0, 30, 5):
                collect_once(db, str(root / "env"), 1000 + offset, sources, config)
                mono[0] += 5
            self.assertEqual(process_stat_reads[0], 18)
            self.assertEqual(process_status_reads[0], 3)
            self.assertEqual(process_limits_reads[0], 3)
            self.assertEqual(cgroup_memory_reads[0], 2)
            self.assertEqual(fd_lists[0], 3)
            self.assertEqual(statvfs_calls[0], 1)
            self.assertEqual(
                len([call for call in command_calls if call[:3] == ("ss", "-H", "-tanp")]),
                1,
            )

    def test_historical_source_error_keys_are_bounded_and_not_rewritten(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = init_db(str(Path(temp) / "metrics.sqlite3"))
            set_state(conn, "counter.source_errors_total", "100")
            set_json_state(
                conn,
                "counter.source_errors_by_source",
                {
                    f"transient:{index}": {"count": 1, "last_seen": 1000}
                    for index in range(100)
                },
            )
            set_json_state(
                conn,
                "counter.source_errors",
                {f"legacy:{index}": 1 for index in range(100)},
            )
            capture = Capture(values={"healthy": True})
            metrics, _events, active = collector_module._source_status(conn, capture, 1000)
            retained = get_json_state(conn, "counter.source_errors_by_source")
            self.assertEqual(len(retained), MAX_TRACKED_SOURCE_ERRORS)
            self.assertEqual(active, {})
            self.assertEqual(metric(metrics, "source_errors_total").value, 100)
            self.assertFalse(any(item.name == "source_errors" for item in metrics))
            self.assertEqual(get_json_state(conn, "counter.source_errors"), {})


if __name__ == "__main__":
    unittest.main()
