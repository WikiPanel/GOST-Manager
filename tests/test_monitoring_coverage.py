#!/usr/bin/env python3
"""Acceptance coverage for GitHub issue #11."""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

import monitoring.collector as collector_module
from monitoring.collector import (
    SERVICE_METRIC_SPECS,
    CollectorConfig,
    CollectorSources,
    CommandExecutionError,
    command_stdout,
    collect_once,
    run_command,
)
from monitoring.entities import tunnel_from_env
from monitoring.event_state import EventState
from monitoring.gost_monitoring import listener_quality
from monitoring.models import (
    Clock,
    CommandResult,
    Event,
    Metric,
    MetricSample,
    ProcessSnapshot,
    SocketRecord,
    Tunnel,
)
from monitoring.network_readers import (
    aggregate_external,
    interface_metrics,
    parse_net_dev,
    parse_protocol_table,
    read_interface_link,
    selected_tcp_counters,
    tcp_counter_metrics,
)
from monitoring.proc_readers import (
    conntrack_metrics,
    cpu_metrics,
    database_size_metrics,
    disk_metrics,
    file_handle_metrics,
    filesystem_metrics,
    memory_metrics,
    parse_diskstats,
    parse_proc_stat,
    process_metrics,
    read_process_snapshot,
    service_process_metrics,
)
from monitoring.schema import (
    init_db,
    insert_event,
    insert_metric,
    insert_sample,
    rollup_completed_minutes,
    set_json_state,
)
from monitoring.socket_readers import (
    established_socket_count,
    listener_ownership_exact,
    owned_listener_ports,
    parse_ss_sockets,
    process_tcp_states,
    tcp_state_counts,
)
from monitoring.systemd_readers import (
    cgroup_memory_metrics,
    discover_managed_services,
    parse_systemd_properties,
    read_cgroup_memory,
    service_metrics,
)

FIXTURES = Path(__file__).parent / "fixtures/monitoring"


def fixture(relative: str) -> str:
    return (FIXTURES / relative).read_text(encoding="utf-8")


def metric(metrics, name: str):
    matches = [item for item in metrics if item.name == name]
    if not matches:
        raise AssertionError(f"missing metric: {name}")
    return matches[-1]


def fake_statvfs():
    return SimpleNamespace(
        f_frsize=4096,
        f_bsize=4096,
        f_blocks=1000,
        f_bfree=300,
        f_bavail=250,
        f_files=200,
        f_ffree=60,
        f_favail=50,
    )


def integration_sources(root: Path, command, monotonic=None, read_text=None):
    mono = monotonic or [10.0]
    reader = read_text or (lambda path: path.read_text(encoding="utf-8"))
    return CollectorSources(
        clock=Clock(lambda: 1000.0, lambda: mono[0]),
        command=command,
        read_text=reader,
        list_dir=lambda _path: ["0", "1", "2"],
        statvfs=lambda _path: fake_statvfs(),
        file_size=lambda path: 0 if str(path).endswith("-wal") else 8192,
        proc_root=FIXTURES / "proc",
        sys_root=FIXTURES / "sys",
        cgroup_root=FIXTURES / "cgroup",
        systemd_unit_root=root / "units",
        ticks_per_second=100,
        page_size=4096,
    )


def write_tunnel_env(env_dir: Path, number: int = 1):
    env_dir.mkdir(exist_ok=True)
    port = 2051 + number
    (env_dir / f"iran-{number}.env").write_text(
        "KHAREJ_IP=198.51.100.20\n"
        "TUNNEL_PORT=28420\n"
        f"MAPPINGS={port}:{port}\n",
        encoding="utf-8",
    )


def stored_metric(conn, entity_id: str, name: str):
    return conn.execute(
        "SELECT p.numeric_value,p.text_value,p.quality FROM metric_points p "
        "JOIN entities e ON e.entity_pk=p.entity_pk "
        "WHERE e.entity_id=? AND p.metric_name=? ORDER BY p.ts DESC LIMIT 1",
        (entity_id, name),
    ).fetchone()


class CpuCoverageTests(unittest.TestCase):
    def test_complete_cpu_counters_and_derived_percentages(self):
        previous = parse_proc_stat(fixture("proc/stat.1"))
        current = parse_proc_stat(fixture("proc/stat.2"))
        metrics = cpu_metrics(current, previous, 5.0, 12.5)

        self.assertEqual(metric(metrics, "cpu_logical_count").value, 2)
        for field in ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"):
            self.assertEqual(metric(metrics, f"cpu_{field}_jiffies").quality, "exact")
            self.assertEqual(metric(metrics, f"cpu_{field}_percent").quality, "derived")
        self.assertAlmostEqual(metric(metrics, "cpu_user_percent").value, 10000 / 410, places=4)
        self.assertAlmostEqual(metric(metrics, "cpu_softirq_percent").value, 2500 / 410, places=4)
        self.assertAlmostEqual(metric(metrics, "cpu_utilization_percent").value, 19000 / 410, places=4)

    def test_cpu_reset_and_gap_do_not_create_a_spike(self):
        high = parse_proc_stat(fixture("proc/stat.2"))
        low = parse_proc_stat(fixture("proc/stat.1"))
        metrics = cpu_metrics(low, high, 20.0, 12.5)

        utilization = metric(metrics, "cpu_utilization_percent")
        self.assertIsNone(utilization.value)
        self.assertEqual(utilization.quality, "unavailable")
        self.assertTrue(utilization.reset)
        self.assertTrue(utilization.gap)


class NetworkCoverageTests(unittest.TestCase):
    def test_errors_drops_rates_and_loopback_exclusion(self):
        previous = parse_net_dev(fixture("proc/net/dev.1"))
        current = parse_net_dev(fixture("proc/net/dev.2"))
        eth0 = interface_metrics(current["eth0"], previous["eth0"], 5.0)

        self.assertEqual(metric(eth0, "rx_errors").value, 3)
        self.assertAlmostEqual(metric(eth0, "rx_errors_per_second").value, 0.2)
        self.assertAlmostEqual(metric(eth0, "rx_drops_per_second").value, 0.4)
        external = aggregate_external(current)
        old_external = aggregate_external(previous)
        external_metrics = interface_metrics(external, old_external, 5.0)
        self.assertEqual(metric(external_metrics, "rx_bytes").value, 52000)
        self.assertAlmostEqual(metric(external_metrics, "rx_bytes_per_second").value, 2400.0)
        self.assertNotEqual(external.rx_bytes, current["lo"].rx_bytes + external.rx_bytes)
        loopback = interface_metrics(current["lo"], previous["lo"], 5.0)
        self.assertEqual(loopback[0].scope, "net.loopback")

    def test_link_state_mtu_and_speed_from_injected_sysfs(self):
        values = read_interface_link("eth0", FIXTURES / "sys")
        self.assertEqual(values, {"state": "up", "link_up": 1, "mtu": 1500, "speed_mbps": 1000})
        loopback = read_interface_link("lo", FIXTURES / "sys")
        self.assertIsNone(loopback["speed_mbps"])

    def test_interface_add_remove_events_are_transition_only(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = init_db(str(Path(temp) / "metrics.sqlite3"))
            state = EventState(conn)
            self.assertEqual(state.set_transitions("interfaces", {"lo", "eth0"}, 1, "interface_added", "interface_removed", "interface"), [])
            events = state.set_transitions("interfaces", {"lo", "eth1"}, 2, "interface_added", "interface_removed", "interface")
            self.assertEqual({event.code for event in events}, {"interface_added", "interface_removed"})
            self.assertEqual(state.set_transitions("interfaces", {"lo", "eth1"}, 3, "interface_added", "interface_removed", "interface"), [])

    def test_paired_tcp_tables_rates_reset_and_gap(self):
        parsed = parse_protocol_table(fixture("proc/net/snmp.1"))
        self.assertEqual(parsed["Tcp"]["RetransSegs"], 25)
        previous = selected_tcp_counters(fixture("proc/net/snmp.1"), fixture("proc/net/netstat.1"))
        current = selected_tcp_counters(fixture("proc/net/snmp.2"), fixture("proc/net/netstat.2"))
        metrics = tcp_counter_metrics(current, previous, 5.0, 12.5)
        self.assertEqual(metric(metrics, "tcp_active_opens").value, 110)
        self.assertAlmostEqual(metric(metrics, "tcp_retransmitted_segments_per_second").value, 2.0)
        self.assertAlmostEqual(metric(metrics, "tcp_listen_drops_per_second").value, 1.0)

        reset_metrics = tcp_counter_metrics(previous, current, 20.0, 12.5)
        retransmit = metric(reset_metrics, "tcp_retransmitted_segments_per_second")
        self.assertIsNone(retransmit.value)
        self.assertTrue(retransmit.reset)
        self.assertTrue(retransmit.gap)

    def test_realistic_tcp_state_snapshot(self):
        records = parse_ss_sockets(fixture("ss.txt"))
        states = tcp_state_counts(records)
        self.assertEqual(states["ESTAB"], 1)
        self.assertEqual(states["SYN-SENT"], 1)
        self.assertEqual(states["CLOSE-WAIT"], 1)
        self.assertEqual(states["TIME-WAIT"], 1)


class HostStorageCoverageTests(unittest.TestCase):
    def test_memory_swap_dirty_and_writeback(self):
        metrics = memory_metrics(fixture("proc/meminfo"))
        self.assertEqual(metric(metrics, "memory_total_bytes").value, 8_000_000 * 1024)
        self.assertEqual(metric(metrics, "memory_used_bytes").value, 5_000_000 * 1024)
        self.assertEqual(metric(metrics, "swap_used_bytes").value, 1_500_000 * 1024)
        self.assertAlmostEqual(metric(metrics, "swap_used_percent").value, 75.0)
        self.assertEqual(metric(metrics, "memory_dirty_bytes").value, 12_000 * 1024)
        self.assertEqual(metric(metrics, "memory_writeback_bytes").value, 4_000 * 1024)

    def test_statvfs_space_inode_and_database_wal_sizes(self):
        stats = SimpleNamespace(
            f_frsize=4096,
            f_bsize=4096,
            f_blocks=1000,
            f_bfree=300,
            f_bavail=250,
            f_files=200,
            f_ffree=60,
            f_favail=50,
        )
        metrics = filesystem_metrics(Path("/data"), lambda _path: stats)
        self.assertEqual(metric(metrics, "filesystem_total_bytes").value, 4_096_000)
        self.assertEqual(metric(metrics, "filesystem_used_bytes").value, 2_867_200)
        self.assertEqual(metric(metrics, "filesystem_free_bytes").value, 1_228_800)
        self.assertEqual(metric(metrics, "filesystem_available_bytes").value, 1_024_000)
        self.assertAlmostEqual(metric(metrics, "filesystem_used_percent").value, 70.0)
        self.assertEqual(metric(metrics, "filesystem_inode_used").value, 140)
        self.assertEqual(metric(metrics, "filesystem_inode_free").value, 60)
        self.assertEqual(metric(metrics, "filesystem_inode_available").value, 50)

        def size(path: Path) -> int:
            if str(path).endswith("-wal"):
                return 4096
            return 8192

        db_metrics = database_size_metrics("/tmp/metrics.sqlite3", size)
        self.assertEqual(metric(db_metrics, "database_size_bytes").value, 8192)
        self.assertEqual(metric(db_metrics, "database_wal_size_bytes").value, 4096)

    def test_diskstats_deltas_and_device_replacement(self):
        previous = parse_diskstats(fixture("proc/diskstats.1"))["sda"]
        current = parse_diskstats(fixture("proc/diskstats.2"))["sda"]
        metrics = disk_metrics(current, previous, 10.0, 12.5)
        self.assertAlmostEqual(metric(metrics, "disk_read_bytes_per_second").value, 20_480.0)
        self.assertAlmostEqual(metric(metrics, "disk_written_bytes_per_second").value, 30_720.0)
        self.assertAlmostEqual(metric(metrics, "disk_utilization_percent").value, 2.0)

        replacement = parse_diskstats(fixture("proc/diskstats.replaced"))["sda"]
        replaced_metrics = disk_metrics(replacement, current, 5.0, 12.5)
        self.assertTrue(metric(replaced_metrics, "disk_read_bytes_per_second").reset)
        self.assertIsNone(metric(replaced_metrics, "disk_read_bytes_per_second").value)

    def test_conntrack_and_file_handle_utilization(self):
        conntrack = conntrack_metrics("250\n", "1000\n")
        handles = file_handle_metrics("100 0 1000\n", "2000\n")
        self.assertAlmostEqual(metric(conntrack, "conntrack_utilization_percent").value, 25.0)
        self.assertAlmostEqual(metric(handles, "file_handles_utilization_percent").value, 5.0)


class ServiceProcessCoverageTests(unittest.TestCase):
    def test_discovery_keeps_multiple_direct_services_and_ignores_nginx(self):
        services = discover_managed_services(
            ("gost-iran-1.service",),
            "nginx.service loaded active running\n"
            "gost-kharej-2.service loaded active running\n",
        )
        self.assertEqual(
            services,
            ("gost-iran-1.service", "gost-kharej-2.service"),
        )

    def test_gost_properties_and_optional_ip_accounting(self):
        gost = service_metrics("gost-iran-1.service", parse_systemd_properties(fixture("systemd-gost.txt")))
        self.assertEqual(metric(gost, "service_main_pid").value, 4242)
        self.assertEqual(metric(gost, "systemd_ip_ingress_bytes").quality, "exact")

    def test_process_cpu_rss_threads_fds_and_pid_identity(self):
        snapshot = read_process_snapshot(
            4242,
            FIXTURES / "proc",
            list_dir=lambda _path: ["0", "1", "2"],
            page_size=4096,
        )
        self.assertEqual(snapshot.start_ticks, 12345)
        self.assertEqual(snapshot.cpu_ticks, 150)
        self.assertEqual(snapshot.rss_bytes, 8192 * 1024)
        self.assertEqual(snapshot.threads, 4)
        self.assertEqual(snapshot.fd_count, 3)
        self.assertEqual(snapshot.fd_soft_limit, 4096)
        self.assertEqual(snapshot.fd_hard_limit, 8192)

        previous = dataclasses.replace(snapshot, cpu_ticks=100)
        metrics = process_metrics("gost-iran-1.service", snapshot, previous, 5.0, 100)
        self.assertAlmostEqual(metric(metrics, "process_cpu_percent").value, 10.0)
        replacement = dataclasses.replace(previous, start_ticks=999)
        replaced = process_metrics("gost-iran-1.service", snapshot, replacement, 5.0, 100)
        self.assertTrue(metric(replaced, "process_cpu_percent").reset)
        self.assertIsNone(metric(replaced, "process_cpu_percent").value)

    def test_cgroup_memory_available_and_unavailable(self):
        values = read_cgroup_memory(
            "/system.slice/gost-iran-1.service",
            FIXTURES / "cgroup",
        )
        self.assertEqual(values["memory_current"], 8_388_608)
        self.assertEqual(values["memory_peak"], 12_582_912)
        missing = cgroup_memory_metrics("gost-iran-2.service", {})
        self.assertTrue(all(item.quality == "unavailable" for item in missing))

    def test_listener_ownership_established_count_and_process_states(self):
        records = parse_ss_sockets(fixture("ss.txt"))
        self.assertEqual(owned_listener_ports(records, (2052,), 4242), {2052})
        self.assertTrue(listener_ownership_exact(records, (2052,), 4242))
        self.assertFalse(listener_ownership_exact(records, (2052,), 3131))
        self.assertEqual(established_socket_count(records, 4242), 1)
        self.assertEqual(process_tcp_states(records, 4242)["SYN-SENT"], 1)


class EventAndCollectorCoverageTests(unittest.TestCase):
    def test_persistence_rejects_secret_fields_without_dropping_cpu_user_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = init_db(str(Path(temp) / "metrics.sqlite3"))
            sample_id = insert_sample(conn, MetricSample(None, 1, 1, 1, 0, 0, 0, 0))
            insert_metric(
                conn,
                sample_id,
                Metric("host", "cpu_user_jiffies", 10, "jiffies", "exact", entity_type="host", entity_id="local"),
            )
            insert_metric(
                conn,
                sample_id,
                Metric("host", "password", "secret-canary", "text", "exact", entity_type="host", entity_id="local"),
            )
            insert_event(
                conn,
                Event(1, "warning", "test", "sanitizer test", {"password": "secret-canary", "diagnostic": "token=secret-canary"}),
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM metric_points WHERE metric_name='cpu_user_jiffies'").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM metric_points WHERE metric_name='password'").fetchone()[0],
                0,
            )
            self.assertNotIn(
                "secret-canary",
                conn.execute("SELECT details_json FROM events WHERE code='test'").fetchone()[0],
            )

    def test_source_failure_is_deduplicated_and_recovery_is_emitted(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = init_db(str(Path(temp) / "metrics.sqlite3"))
            state = EventState(conn)
            first = state.availability("proc_net_snmp", False, 1)
            repeated = state.availability("proc_net_snmp", False, 2)
            recovered = state.availability("proc_net_snmp", True, 3)
            self.assertEqual([event.code for event in first], ["metric_source_unavailable"])
            self.assertEqual(repeated, [])
            self.assertEqual([event.code for event in recovered], ["metric_source_available"])

    def test_tunnel_endpoint_metadata_never_contains_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "iran-1.env"
            path.write_text(
                "GOST_USER=fixture-user\n"
                "GOST_PASS=fixture-password\n"
                "KHAREJ_IP=198.51.100.20\n"
                "TUNNEL_PORT=28420\n"
                "MAPPINGS=2052:2052\n",
                encoding="utf-8",
            )
            tunnel = tunnel_from_env(path)
            self.assertEqual(tunnel.remote_endpoint, "198.51.100.20:28420")
            serialized = repr(tunnel)
            self.assertNotIn("fixture-user", serialized)
            self.assertNotIn("fixture-password", serialized)

    def test_full_cycle_self_metrics_failure_isolation_dedup_and_no_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_dir = root / "env"
            env_dir.mkdir()
            (env_dir / "iran-1.env").write_text(
                "GOST_USER=fixture-user\n"
                "GOST_PASS=fixture-password\n"
                "KHAREJ_IP=198.51.100.20\n"
                "TUNNEL_PORT=28420\n"
                "MAPPINGS=2052:2052\n",
                encoding="utf-8",
            )
            (env_dir / "iran-2.env").write_text(
                "KHAREJ_IP=198.51.100.21\n"
                "TUNNEL_PORT=28421\n"
                "MAPPINGS=2053:2053\n",
                encoding="utf-8",
            )
            db = str(root / "metrics.sqlite3")
            second_service_available = [False]
            monotonic = [10.0]

            def command(parts):
                if parts[0] == "ss":
                    return fixture("ss.txt")
                service = parts[3]
                if service == "gost-iran-1.service":
                    return fixture("systemd-gost.txt")
                if service == "gost-iran-2.service" and second_service_available[0]:
                    return (
                        fixture("systemd-gost.txt")
                        .replace("MainPID=4242", "MainPID=0")
                        .replace(
                            "ControlGroup=/system.slice/gost-iran-1.service",
                            "ControlGroup=/missing.service",
                        )
                    )
                raise OSError("source unavailable")

            stats = SimpleNamespace(
                f_frsize=4096,
                f_bsize=4096,
                f_blocks=1000,
                f_bfree=300,
                f_bavail=250,
                f_files=200,
                f_ffree=60,
                f_favail=50,
            )
            sources = CollectorSources(
                clock=Clock(lambda: 1000.0, lambda: monotonic[0]),
                command=command,
                list_dir=lambda _path: ["0", "1", "2"],
                statvfs=lambda _path: stats,
                file_size=lambda path: 0 if str(path).endswith("-wal") else 8192,
                proc_root=FIXTURES / "proc",
                sys_root=FIXTURES / "sys",
                cgroup_root=FIXTURES / "cgroup",
                systemd_unit_root=root / "units",
                ticks_per_second=100,
                page_size=4096,
            )
            config = CollectorConfig(
                sample_interval=5.0,
                tcp_snapshot_interval=30.0,
                filesystem_paths=(Path("/"),),
            )

            collect_once(db, str(env_dir), 1000, sources, config)
            monotonic[0] += 5.0
            collect_once(db, str(env_dir), 1005, sources, config)
            conn = init_db(db)
            metric_names = {
                row[0]
                for row in conn.execute("SELECT metric_name FROM metric_points")
            }
            for expected in (
                "cpu_utilization_percent",
                "cpu_user_jiffies",
                "rx_errors_per_second",
                "tcp_retransmitted_segments_per_second",
                "memory_used_percent",
                "database_size_bytes",
                "process_cpu_percent",
                "listener_ownership_exact",
                "metrics_written",
                "events_written",
                "rows_write_attempted",
                "last_successful_cycle_timestamp",
                "source_errors_total",
            ):
                self.assertIn(expected, metric_names)
            unavailable_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE code='metric_source_unavailable' "
                "AND details_json LIKE '%systemd:gost-iran-2.service%'"
            ).fetchone()[0]
            self.assertEqual(unavailable_count, 1)
            self.assertEqual(
                conn.execute(
                    "SELECT numeric_value FROM metric_points p JOIN entities e "
                    "ON e.entity_pk=p.entity_pk WHERE e.entity_id='gost-iran-1.service' "
                    "AND p.metric_name='service_active' ORDER BY p.ts DESC LIMIT 1"
                ).fetchone()[0],
                1,
            )
            tunnel_metadata = conn.execute(
                "SELECT metadata_json FROM entities WHERE entity_type='tunnel' AND entity_id='iran-1'"
            ).fetchone()[0]
            self.assertEqual(json.loads(tunnel_metadata)["remote_endpoint"], "198.51.100.20:28420")

            persisted_text = "\n".join(
                str(value)
                for table, column in (
                    ("entities", "metadata_json"),
                    ("events", "details_json"),
                    ("metric_points", "text_value"),
                    ("collector_state", "value"),
                )
                for (value,) in conn.execute(f"SELECT {column} FROM {table}")
                if value is not None
            )
            self.assertNotIn("fixture-user", persisted_text)
            self.assertNotIn("fixture-password", persisted_text)
            conn.close()

            second_service_available[0] = True
            monotonic[0] += 5.0
            collect_once(db, str(env_dir), 1010, sources, config)
            conn = init_db(db)
            recovered = conn.execute(
                "SELECT COUNT(*) FROM events WHERE code='metric_source_available' "
                "AND details_json LIKE '%systemd:gost-iran-2.service%'"
            ).fetchone()[0]
            self.assertEqual(recovered, 1)


class TechnicalReviewRegressionTests(unittest.TestCase):
    def test_command_result_distinguishes_empty_missing_permission_and_failure(self):
        self.assertEqual(command_stdout(CommandResult("", "", 0)), "")
        self.assertEqual(
            command_stdout(CommandResult(fixture("ss.txt"), "", 0)),
            fixture("ss.txt"),
        )
        with self.assertRaisesRegex(CommandExecutionError, "permission_denied"):
            command_stdout(CommandResult("", "Permission denied", 126))
        with self.assertRaisesRegex(CommandExecutionError, "nonzero_exit"):
            command_stdout(CommandResult("", "failed", 1))
        with patch("monitoring.collector.subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(CommandExecutionError, "missing_binary"):
                run_command(["missing-ss-binary"])
        with patch("monitoring.collector.subprocess.run", side_effect=PermissionError()):
            with self.assertRaisesRegex(CommandExecutionError, "permission_denied"):
                run_command(["ss"])
        with patch(
            "monitoring.collector.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["ss"], 10),
        ):
            with self.assertRaisesRegex(CommandExecutionError, "timeout"):
                run_command(["ss"])
        tunnel = Tunnel(
            "iran",
            1,
            "gost-iran-1.service",
            "iran-1.env",
            (2052,),
            (2052,),
        )

        def failed_socket(parts):
            if parts[0] == "systemctl":
                return CommandResult("MainPID=4242\n", "", 0)
            return CommandResult("", "failed", 1)

        self.assertEqual(listener_quality(tunnel, failed_socket), "unavailable")

    def test_failed_missing_permission_and_empty_ss_have_distinct_quality(self):
        cases = (
            ("nonzero", CommandResult("", "failed", 1), "unavailable", None),
            ("missing", CommandExecutionError("missing_binary"), "unavailable", None),
            ("permission", CommandResult("", "Permission denied", 126), "unavailable", None),
            (
                "missing-process-metadata",
                CommandResult(
                    "LISTEN 0 128 0.0.0.0:2052 0.0.0.0:*\n"
                    "ESTAB 0 0 127.0.0.1:2052 198.51.100.20:28420\n",
                    "",
                    0,
                ),
                "unavailable",
                None,
            ),
            ("empty", CommandResult("", "", 0), "exact", 0.0),
        )
        for label, socket_result, expected_quality, expected_value in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                env_dir = root / "env"
                write_tunnel_env(env_dir)

                def command(parts):
                    if parts[0] == "ss":
                        if isinstance(socket_result, Exception):
                            raise socket_result
                        return socket_result
                    return fixture("systemd-gost.txt")

                sources = integration_sources(root, command)
                collect_once(
                    str(root / "metrics.sqlite3"),
                    str(env_dir),
                    1000,
                    sources,
                    CollectorConfig(filesystem_paths=(Path("/"),)),
                )
                conn = init_db(str(root / "metrics.sqlite3"))
                ownership = stored_metric(conn, "iran-1", "listener_ownership_exact")
                established = stored_metric(conn, "iran-1", "established_remote_sockets")
                service_listener = stored_metric(
                    conn,
                    "gost-iran-1.service",
                    "listener_owned_count",
                )
                service_established = stored_metric(
                    conn,
                    "gost-iran-1.service",
                    "established_sockets_total",
                )
                self.assertEqual(ownership[2], expected_quality)
                self.assertEqual(ownership[0], expected_value)
                self.assertEqual(established[2], expected_quality)
                self.assertEqual(established[0], expected_value)
                self.assertEqual(service_listener[2], expected_quality)
                self.assertEqual(service_listener[0], expected_value)
                self.assertEqual(service_established[2], expected_quality)
                self.assertEqual(service_established[0], expected_value)
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM events WHERE code='listener_disappeared'"
                    ).fetchone()[0],
                    0,
                )

    def test_failed_listener_snapshot_does_not_emit_false_disappearance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_dir = root / "env"
            write_tunnel_env(env_dir)
            listener_fails = [False]
            mono = [10.0]

            def command(parts):
                if parts[0] == "ss":
                    if parts[-1] == "-lntp" and listener_fails[0]:
                        return CommandResult("", "failed", 1)
                    return CommandResult(fixture("ss.txt"), "", 0)
                return fixture("systemd-gost.txt")

            sources = integration_sources(root, command, mono)
            db = str(root / "metrics.sqlite3")
            config = CollectorConfig(filesystem_paths=(Path("/"),))
            collect_once(db, str(env_dir), 1000, sources, config)
            listener_fails[0] = True
            mono[0] += 5
            collect_once(db, str(env_dir), 1005, sources, config)
            conn = init_db(db)
            ownership = stored_metric(conn, "iran-1", "listener_ownership_exact")
            self.assertEqual(ownership[2], "unavailable")
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE code='listener_disappeared'"
                ).fetchone()[0],
                0,
            )

    def test_light_and_full_socket_command_cadence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_dir = root / "env"
            write_tunnel_env(env_dir)
            calls = []
            mono = [10.0]

            def command(parts):
                calls.append(tuple(parts))
                if parts[0] == "ss":
                    return CommandResult("", "", 0)
                return "ActiveState=inactive\nSubState=dead\nMainPID=0\nNRestarts=0\n"

            sources = integration_sources(root, command, mono)
            config = CollectorConfig(
                sample_interval=5.0,
                tcp_snapshot_interval=30.0,
                filesystem_paths=(Path("/"),),
            )
            db = str(root / "metrics.sqlite3")
            for offset in range(0, 30, 5):
                collect_once(db, str(env_dir), 1000 + offset, sources, config)
                mono[0] += 5
            light = [call for call in calls if call[:3] == ("ss", "-H", "-lntp")]
            full = [call for call in calls if call[:3] == ("ss", "-H", "-tanp")]
            self.assertEqual(len(light), 6)
            self.assertEqual(len(full), 1)

    def test_thirty_second_metric_rollup_has_full_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = init_db(str(Path(temp) / "metrics.sqlite3"))
            set_json_state(
                conn,
                "metric_cadence_seconds",
                {"host:tcp_state_*": 30.0},
            )
            for timestamp, value in ((60, 3), (90, 4)):
                sample_id = insert_sample(
                    conn,
                    MetricSample(None, timestamp, 1, 1, 0, 0, 0, 0),
                )
                insert_metric(
                    conn,
                    sample_id,
                    Metric(
                        "tcp",
                        "tcp_state_estab",
                        value,
                        "count",
                        "exact",
                        entity_type="host",
                        entity_id="local",
                    ),
                )
            rollup_completed_minutes(conn, 120)
            row = conn.execute(
                "SELECT samples,expected_samples,coverage FROM minute_rollups "
                "WHERE metric_name='tcp_state_estab' AND minute_start=60"
            ).fetchone()
            self.assertEqual(row, (2, 2, 1.0))

    def test_cached_socket_snapshot_is_not_reused_after_process_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = init_db(str(Path(temp) / "metrics.sqlite3"))
            records = [
                SocketRecord(
                    "ESTAB",
                    "127.0.0.1",
                    443,
                    "198.51.100.10",
                    443,
                    100,
                    "gost",
                )
            ]
            current = collector_module._service_socket_metrics(
                conn,
                "gost-iran-1.service",
                (100,),
                True,
                "100:10",
                True,
                records,
            )
            replaced = collector_module._service_socket_metrics(
                conn,
                "gost-iran-1.service",
                (101,),
                True,
                "101:20",
                False,
                None,
            )
            self.assertEqual(metric(current, "established_sockets_total").value, 1)
            self.assertEqual(metric(current, "established_sockets_total").quality, "exact")
            self.assertIsNone(metric(replaced, "established_sockets_total").value)
            self.assertEqual(metric(replaced, "established_sockets_total").quality, "unavailable")

    def test_service_metric_families_cover_runtime_and_unavailable_states(self):
        variants = (
            ("active", fixture("systemd-gost.txt"), False, False, "exact", "exact"),
            ("inactive", "ActiveState=inactive\nSubState=dead\nMainPID=0\nNRestarts=0\n", False, False, "unavailable", "unavailable"),
            ("failed", "ActiveState=failed\nSubState=failed\nMainPID=0\nNRestarts=2\n", False, False, "unavailable", "unavailable"),
            ("systemd-unavailable", None, False, False, "unavailable", "unavailable"),
            ("process-unavailable", fixture("systemd-gost.txt"), True, False, "exact", "exact"),
            ("socket-unavailable", fixture("systemd-gost.txt"), False, True, "unavailable", "unavailable"),
        )
        for label, properties, process_fails, socket_fails, ownership_quality, established_quality in variants:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                env_dir = root / "env"
                write_tunnel_env(env_dir)

                def command(parts):
                    if parts[0] == "ss":
                        if socket_fails:
                            return CommandResult("", "failed", 1)
                        return CommandResult(fixture("ss.txt"), "", 0)
                    if properties is None:
                        return CommandResult("", "failed", 1)
                    return properties

                def reader(path):
                    if process_fails and "4242" in path.parts:
                        raise PermissionError()
                    return path.read_text(encoding="utf-8")

                sources = integration_sources(root, command, read_text=reader)
                db = str(root / "metrics.sqlite3")
                collect_once(db, str(env_dir), 1000, sources, CollectorConfig(filesystem_paths=(Path("/"),)))
                conn = init_db(db)
                rows = {
                    row[0]: row[1]
                    for row in conn.execute(
                        "SELECT p.metric_name,p.quality FROM metric_points p "
                        "JOIN entities e ON e.entity_pk=p.entity_pk "
                        "WHERE e.entity_id='gost-iran-1.service'"
                    )
                }
                self.assertTrue({name for name, _unit in SERVICE_METRIC_SPECS}.issubset(rows))
                expected_process_quality = "unavailable" if process_fails or properties is None or "MainPID=0" in (properties or "") else "exact"
                self.assertEqual(rows["process_rss_bytes"], expected_process_quality)
                self.assertEqual(rows["listener_owned_count"], ownership_quality)
                self.assertEqual(rows["established_sockets_total"], established_quality)
                self.assertEqual(stored_metric(conn, "iran-1", "listener_ownership_exact")[2], ownership_quality)
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM events WHERE code='listener_disappeared'"
                    ).fetchone()[0],
                    0,
                )

    def test_processing_stage_errors_share_source_error_registry_and_recover(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_dir = root / "env"
            env_dir.mkdir()
            invalid = [True]

            def command(parts):
                if parts[0] == "ss":
                    return CommandResult("", "", 0)
                return "ActiveState=inactive\nSubState=dead\nMainPID=0\nNRestarts=0\n"

            def reader(path):
                if path.name == "loadavg" and invalid[0]:
                    return "invalid"
                return path.read_text(encoding="utf-8")

            sources = integration_sources(root, command, read_text=reader)
            db = str(root / "metrics.sqlite3")
            config = CollectorConfig(filesystem_paths=(Path("/"),))
            collect_once(db, str(env_dir), 1000, sources, config)
            conn = init_db(db)
            error = stored_metric(conn, "proc_loadavg", "source_errors")
            self.assertEqual(error[0], 1.0)
            self.assertEqual(
                stored_metric(conn, "local", "source_errors_total")[0] >= 1,
                True,
            )
            conn.close()
            invalid[0] = False
            collect_once(db, str(env_dir), 1005, sources, config)
            conn = init_db(db)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE code='metric_source_available' "
                    "AND details_json LIKE '%proc_loadavg%'"
                ).fetchone()[0],
                1,
            )

    def test_per_service_failure_does_not_abort_other_entities(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_dir = root / "env"
            write_tunnel_env(env_dir, 1)
            write_tunnel_env(env_dir, 2)

            def command(parts):
                if parts[0] == "ss":
                    return CommandResult("", "", 0)
                return fixture("systemd-gost.txt")

            original = service_process_metrics

            def fail_one(service, snapshot, previous, elapsed, ticks_per_second, max_gap, authoritative=True, slow_quality="unavailable"):
                if service == "gost-iran-1.service":
                    raise ValueError("injected service transform failure")
                return original(
                    service,
                    snapshot,
                    previous,
                    elapsed,
                    ticks_per_second,
                    max_gap,
                    authoritative,
                    slow_quality,
                )

            db = str(root / "metrics.sqlite3")
            with patch("monitoring.collector.service_process_metrics", side_effect=fail_one):
                collect_once(
                    db,
                    str(env_dir),
                    1000,
                    integration_sources(root, command),
                    CollectorConfig(filesystem_paths=(Path("/"),)),
                )
            conn = init_db(db)
            self.assertEqual(stored_metric(conn, "gost-iran-1.service", "service_active")[2], "unavailable")
            self.assertEqual(stored_metric(conn, "gost-iran-2.service", "service_active")[2], "exact")
            self.assertEqual(stored_metric(conn, "iran-1", "process_rss_bytes")[2], "unavailable")
            self.assertEqual(stored_metric(conn, "iran-2", "service_active")[2], "exact")
            self.assertIsNotNone(stored_metric(conn, "local", "cpu_jiffies_total"))
            self.assertEqual(stored_metric(conn, "service_processing:gost-iran-1.service", "source_errors")[0], 1.0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE code='metric_source_unavailable' "
                    "AND details_json LIKE '%service_processing:gost-iran-1.service%'"
                ).fetchone()[0],
                1,
            )

    def test_per_tunnel_failure_does_not_abort_other_tunnels_or_host(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_dir = root / "env"
            write_tunnel_env(env_dir, 1)
            write_tunnel_env(env_dir, 2)

            def command(parts):
                if parts[0] == "ss":
                    return CommandResult("", "", 0)
                return "ActiveState=active\nSubState=running\nMainPID=0\nNRestarts=0\n"

            original = collector_module.upsert_tunnel

            def fail_one(conn, tunnel, now):
                if tunnel.tunnel_id == "iran-1":
                    raise ValueError("injected tunnel transform failure")
                return original(conn, tunnel, now)

            db = str(root / "metrics.sqlite3")
            with patch("monitoring.collector.upsert_tunnel", side_effect=fail_one):
                collect_once(
                    db,
                    str(env_dir),
                    1000,
                    integration_sources(root, command),
                    CollectorConfig(filesystem_paths=(Path("/"),)),
                )
            conn = init_db(db)
            self.assertEqual(stored_metric(conn, "iran-1", "observed_listener_count")[2], "unavailable")
            self.assertEqual(stored_metric(conn, "iran-2", "service_active")[2], "exact")
            self.assertIsNotNone(stored_metric(conn, "local", "cpu_jiffies_total"))
            self.assertEqual(stored_metric(conn, "tunnel_processing:iran-1", "source_errors")[0], 1.0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE code='metric_source_unavailable' "
                    "AND details_json LIKE '%tunnel_processing:iran-1%'"
                ).fetchone()[0],
                1,
            )

    def test_rows_write_attempted_is_explicitly_estimated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def command(parts):
                if parts[0] == "ss":
                    return CommandResult("", "", 0)
                return "ActiveState=inactive\nSubState=dead\nMainPID=0\nNRestarts=0\n"

            db = str(root / "metrics.sqlite3")
            collect_once(
                db,
                str(root / "env"),
                1000,
                integration_sources(root, command),
                CollectorConfig(filesystem_paths=(Path("/"),)),
            )
            conn = init_db(db)
            attempted = stored_metric(conn, "local", "rows_write_attempted")
            self.assertGreater(attempted[0], 0)
            self.assertEqual(attempted[2], "estimated")
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM metric_points WHERE metric_name='rows_written'"
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
