#!/usr/bin/env python3
"""Deterministic capacity and synthetic high-cardinality checks."""

from __future__ import annotations

import dataclasses
import resource
import sys
import time
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from monitoring.collector import CollectorConfig, CommandResult, collect_once
from monitoring.performance import (
    GIB,
    REPRESENTATIVE_FAST_POINTS_PER_CYCLE,
    REPRESENTATIVE_FULL_SOCKET_EXTRA_POINTS,
    REPRESENTATIVE_ROLLUP_SERIES_PER_MINUTE,
    REPRESENTATIVE_SLOW_EXTRA_POINTS,
    estimate_storage_budget,
    representative_storage_budget,
)
from monitoring.schema import init_db
from monitoring.socket_readers import (
    established_remote_socket_count,
    established_socket_count,
    parse_ss_sockets,
    tcp_state_counts,
)

try:
    from test_monitoring_coverage import (
        fixture,
        integration_sources,
        stored_metric,
        write_tunnel_env,
    )
except ModuleNotFoundError:
    from tests.test_monitoring_coverage import (
        fixture,
        integration_sources,
        stored_metric,
        write_tunnel_env,
    )

SYNTHETIC_ONLINE_USERS = 1_000
SYNTHETIC_GOST_SERVICE_COUNT = 6
SYNTHETIC_TCP_NOISE_ROWS = 20
SYNTHETIC_LISTENER_ROWS = SYNTHETIC_GOST_SERVICE_COUNT + 1
SYNTHETIC_SOCKET_ROWS = (
    SYNTHETIC_ONLINE_USERS * 3
    + SYNTHETIC_TCP_NOISE_ROWS
    + SYNTHETIC_LISTENER_ROWS
)
SOCKET_PARSE_BUDGET_SECONDS = 5.0
HEAVY_CYCLE_BUDGET_SECONDS = 5.0
MAX_RSS_BYTES = 256 * 1024 * 1024


def _maximum_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _synthetic_socket_fixture() -> tuple[str, str, Counter[int]]:
    listeners = [
        'LISTEN 0 511 0.0.0.0:443 0.0.0.0:* users:(("nginx",pid=3131,fd=7))'
    ]
    for number in range(1, SYNTHETIC_GOST_SERVICE_COUNT + 1):
        listeners.append(
            "LISTEN 0 4096 127.0.0.1:{port} 0.0.0.0:* "
            'users:(("gost",pid={pid},fd=7))'.format(
                port=2051 + number,
                pid=6000 + number,
            )
        )

    rows: list[str] = []
    users_by_pid: Counter[int] = Counter()
    for index in range(SYNTHETIC_ONLINE_USERS):
        number = index % SYNTHETIC_GOST_SERVICE_COUNT + 1
        gost_pid = 6000 + number
        nginx_pid = 3132 if index % 2 == 0 else 3133
        users_by_pid[gost_pid] += 1
        client_host = f"203.0.{index // 250}.{index % 250 + 1}"
        rows.append(
            "ESTAB 0 0 203.0.113.10:443 {client}:{port} "
            'users:(("nginx",pid={pid},fd={fd}))'.format(
                client=client_host,
                port=20_000 + index,
                pid=nginx_pid,
                fd=100 + index,
            )
        )
        rows.append(
            "ESTAB 0 0 127.0.0.1:{listener} 127.0.0.1:{peer} "
            'users:(("gost",pid={pid},fd={fd}))'.format(
                listener=2051 + number,
                peer=30_000 + index,
                pid=gost_pid,
                fd=2_000 + index,
            )
        )
        rows.append(
            "ESTAB 0 0 10.0.0.10:{local} 198.51.100.20:28420 "
            'users:(("gost",pid={pid},fd={fd}))'.format(
                local=40_000 + index,
                pid=gost_pid,
                fd=4_000 + index,
            )
        )

    for state_index, state in enumerate(
        ("SYN-SENT", "SYN-RECV", "CLOSE-WAIT", "TIME-WAIT")
    ):
        for index in range(5):
            rows.append(
                f"{state} 0 0 10.0.0.10:{50_000 + state_index * 10 + index} "
                f"198.51.100.30:{443 + index}"
            )
    return "\n".join(listeners), "\n".join([*rows, *listeners]), users_by_pid


def run_monitoring_lite_benchmark() -> dict[str, int | float]:
    listener_text, socket_text, users_by_pid = _synthetic_socket_fixture()
    parse_started = time.perf_counter()
    records = parse_ss_sockets(socket_text)
    parse_duration = time.perf_counter() - parse_started

    attribution_started = time.perf_counter()
    nginx_total = established_socket_count(records, (3131, 3132, 3133))
    service_totals = {
        pid: established_socket_count(records, (pid,)) for pid in users_by_pid
    }
    remote_totals = {
        pid: established_remote_socket_count(
            records,
            (pid,),
            "198.51.100.20",
            28420,
        )
        for pid in users_by_pid
    }
    states = tcp_state_counts(records)
    attribution_duration = time.perf_counter() - attribution_started

    if nginx_total != SYNTHETIC_ONLINE_USERS:
        raise AssertionError("synthetic NGINX socket attribution mismatch")
    if sum(service_totals.values()) != SYNTHETIC_ONLINE_USERS * 2:
        raise AssertionError("synthetic GOST socket attribution mismatch")
    if sum(value or 0 for value in remote_totals.values()) != SYNTHETIC_ONLINE_USERS:
        raise AssertionError("synthetic remote socket attribution mismatch")
    if states.get("ESTAB") != SYNTHETIC_ONLINE_USERS * 3:
        raise AssertionError("synthetic TCP state count mismatch")

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        env_dir = root / "env"
        for number in range(1, SYNTHETIC_GOST_SERVICE_COUNT + 1):
            write_tunnel_env(env_dir, number)
        command_calls: list[tuple[str, ...]] = []

        def command(parts):
            command_calls.append(tuple(parts))
            if parts[0] == "ss":
                return CommandResult(
                    listener_text if parts[-1] == "-lntp" else socket_text,
                    "",
                    0,
                )
            service = parts[3]
            if service == "nginx.service":
                return fixture("systemd-nginx.txt")
            number = int(service.removeprefix("gost-iran-").removesuffix(".service"))
            return (
                fixture("systemd-gost.txt")
                .replace("MainPID=4242", f"MainPID={6000 + number}")
                .replace(
                    "ControlGroup=/system.slice/gost-iran-1.service",
                    f"ControlGroup=/system.slice/{service}",
                )
            )

        def read_text(path: Path) -> str:
            if path.name == "cgroup.procs":
                service = path.parent.name
                if service == "nginx.service":
                    return "3131\n3132\n3133\n"
                number = int(
                    service.removeprefix("gost-iran-").removesuffix(".service")
                )
                return f"{6000 + number}\n"
            if path.name in {"memory.current", "memory.peak"}:
                return "16777216\n"
            if path.parent.name.isdigit() and int(path.parent.name) >= 6001:
                pid = int(path.parent.name)
                template = fixture(f"proc/4242/{path.name}")
                if path.name == "stat":
                    return template.replace("4242 (", f"{pid} (", 1)
                if path.name == "status":
                    return template.replace("Pid:\t4242", f"Pid:\t{pid}")
                return template
            return path.read_text(encoding="utf-8")

        def list_dir(path: Path) -> list[str]:
            pid = int(path.parent.name)
            count = 1_200 if pid in {3131, 3132, 3133} else 400
            return [str(index) for index in range(count)]

        sources = dataclasses.replace(
            integration_sources(root, command, [10.0], read_text),
            cgroup_root=root / "cgroup",
            list_dir=list_dir,
        )
        db = str(root / "metrics.sqlite3")
        cycle_started = time.perf_counter()
        collect_once(
            db,
            str(env_dir),
            1_000,
            sources,
            CollectorConfig(),
            missed_deadlines=0,
        )
        cycle_duration = time.perf_counter() - cycle_started
        conn = init_db(db)
        metric_points = int(
            conn.execute("SELECT COUNT(*) FROM metric_points WHERE ts=1000").fetchone()[0]
        )
        missed_deadlines = int(
            conn.execute(
                "SELECT missed_deadlines FROM sample_cycles WHERE collected_at=1000"
            ).fetchone()[0]
        )
        if stored_metric(conn, "nginx.service", "established_sockets_total")[0] != 1000:
            raise AssertionError("stored NGINX socket count mismatch")
        for number in range(1, SYNTHETIC_GOST_SERVICE_COUNT + 1):
            expected = users_by_pid[6000 + number]
            if stored_metric(
                conn,
                f"gost-iran-{number}.service",
                "established_sockets_total",
            )[0] != expected * 2:
                raise AssertionError("stored GOST socket count mismatch")
            if stored_metric(
                conn,
                f"iran-{number}",
                "established_remote_sockets",
            )[0] != expected:
                raise AssertionError("stored tunnel remote socket count mismatch")
        conn.close()

    budget = representative_storage_budget()
    return {
        "online_users": SYNTHETIC_ONLINE_USERS,
        "socket_rows": len(records),
        "socket_parse_seconds": parse_duration,
        "socket_attribution_seconds": attribution_duration,
        "heavy_cycle_seconds": cycle_duration,
        "maximum_rss_bytes": _maximum_rss_bytes(),
        "metric_points_per_heavy_cycle": metric_points,
        "projected_points_per_day": budget.metric_points_per_day,
        "projected_database_bytes": budget.estimated_total_database_bytes,
        "missed_deadlines": missed_deadlines,
        "ss_executions": sum(1 for call in command_calls if call[0] == "ss"),
    }


class MonitoringPerformanceTests(unittest.TestCase):
    def test_representative_profile_matches_budget_cycle_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_dir = root / "env"
            for number in range(1, 7):
                write_tunnel_env(env_dir, number)
            sockets = fixture("ss-gost-inbound-outbound.txt") + fixture(
                "ss-nginx-multiprocess.txt"
            )

            def command(parts):
                if parts[0] == "ss":
                    return CommandResult(sockets, "", 0)
                if parts[3] == "nginx.service":
                    return fixture("systemd-nginx.txt")
                return fixture("systemd-gost.txt")

            db = str(root / "metrics.sqlite3")
            sources = integration_sources(root, command, [10.0])
            config = CollectorConfig()
            for timestamp in (1000, 1010, 1030, 1060):
                collect_once(db, str(env_dir), timestamp, sources, config)
            conn = init_db(db)
            counts = dict(
                conn.execute(
                    "SELECT ts,COUNT(*) FROM metric_points GROUP BY ts ORDER BY ts"
                ).fetchall()
            )
            fast = counts[1010]
            full_extra = counts[1030] - fast
            slow_extra = counts[1060] - counts[1030]
            self.assertEqual(fast, REPRESENTATIVE_FAST_POINTS_PER_CYCLE)
            self.assertEqual(full_extra, REPRESENTATIVE_FULL_SOCKET_EXTRA_POINTS)
            self.assertEqual(slow_extra, REPRESENTATIVE_SLOW_EXTRA_POINTS)

    def test_representative_storage_budget_is_explicit_and_bounded(self):
        budget = representative_storage_budget()
        self.assertEqual(budget.raw_retention_hours, 6)
        self.assertEqual(budget.rollup_retention_days, 1)
        self.assertEqual(budget.event_retention_days, 1)
        self.assertEqual(budget.fast_metric_points_per_day, 4_510_080)
        self.assertEqual(budget.full_socket_metric_points_per_day, 25_920)
        self.assertEqual(budget.slow_metric_points_per_day, 74_880)
        self.assertEqual(budget.metric_points_per_day, 4_610_880)
        self.assertEqual(budget.raw_metric_points, 1_152_720)
        self.assertEqual(budget.minute_rollup_rows, 839_520)
        self.assertEqual(budget.sample_cycle_rows, 2_160)
        self.assertEqual(budget.metric_sample_rows, 15_120)
        self.assertEqual(budget.event_rows, 5_000)
        self.assertEqual(budget.entity_rows, 2_048)
        self.assertAlmostEqual(budget.estimated_total_database_gib, 0.484, places=3)
        self.assertLess(budget.estimated_total_database_bytes, GIB)
        self.assertEqual(budget.recommended_disk_reservation_bytes, GIB)
        self.assertEqual(budget.recommended_disk_reservation_gib, 1.0)

    def test_one_day_rollups_are_included_in_total_database_bytes(self):
        budget = estimate_storage_budget()
        table_bytes = (
            budget.estimated_raw_table_bytes
            + budget.estimated_rollup_table_bytes
            + budget.estimated_auxiliary_table_bytes
        )
        self.assertEqual(
            budget.minute_rollup_rows,
            REPRESENTATIVE_ROLLUP_SERIES_PER_MINUTE * 60 * 24,
        )
        self.assertEqual(
            budget.estimated_total_database_bytes,
            table_bytes
            + budget.estimated_indexes_and_free_pages_bytes
            + budget.wal_and_operational_headroom_bytes,
        )
        without_rollups = estimate_storage_budget(rollup_retention_days=0)
        self.assertGreater(
            budget.estimated_total_database_bytes,
            without_rollups.estimated_total_database_bytes
            + budget.estimated_rollup_table_bytes,
        )

    def test_event_retention_is_independent_from_rollup_retention(self):
        seven_day_events = estimate_storage_budget(event_retention_days=7)
        seven_day_rollups = estimate_storage_budget(
            rollup_retention_days=7,
            event_retention_days=30,
        )
        self.assertEqual(seven_day_events.event_rows, 35_000)
        self.assertEqual(seven_day_events.event_retention_days, 7)
        self.assertEqual(seven_day_events.minute_rollup_rows, 839_520)
        self.assertEqual(seven_day_rollups.event_rows, 150_000)
        self.assertEqual(seven_day_rollups.event_retention_days, 30)
        self.assertEqual(
            seven_day_rollups.minute_rollup_rows,
            REPRESENTATIVE_ROLLUP_SERIES_PER_MINUTE * 60 * 24 * 7,
        )
        self.assertEqual(
            seven_day_events.raw_metric_points,
            seven_day_rollups.raw_metric_points,
        )
        self.assertLess(
            seven_day_events.estimated_events_bytes,
            seven_day_rollups.estimated_events_bytes,
        )

    def test_monitoring_lite_thousand_user_cycle_is_bounded_and_exact(self):
        result = run_monitoring_lite_benchmark()
        self.assertEqual(result["online_users"], SYNTHETIC_ONLINE_USERS)
        self.assertEqual(result["socket_rows"], SYNTHETIC_SOCKET_ROWS)
        self.assertLess(result["socket_parse_seconds"], SOCKET_PARSE_BUDGET_SECONDS)
        self.assertLess(
            result["socket_attribution_seconds"], SOCKET_PARSE_BUDGET_SECONDS
        )
        self.assertLess(result["heavy_cycle_seconds"], HEAVY_CYCLE_BUDGET_SECONDS)
        self.assertLess(result["maximum_rss_bytes"], MAX_RSS_BYTES)
        self.assertEqual(
            result["metric_points_per_heavy_cycle"],
            REPRESENTATIVE_FAST_POINTS_PER_CYCLE
            + REPRESENTATIVE_FULL_SOCKET_EXTRA_POINTS
            + REPRESENTATIVE_SLOW_EXTRA_POINTS,
        )
        self.assertEqual(result["projected_points_per_day"], 4_610_880)
        self.assertLess(result["projected_database_bytes"], GIB)
        self.assertEqual(result["missed_deadlines"], 0)
        self.assertEqual(result["ss_executions"], 2)


if __name__ == "__main__":
    unittest.main()
