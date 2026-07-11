#!/usr/bin/env python3
"""Deterministic capacity and synthetic high-cardinality checks."""

from __future__ import annotations

import time
import tempfile
import unittest
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
from monitoring.socket_readers import established_socket_count, parse_ss_sockets

try:
    from test_monitoring_coverage import fixture, integration_sources, write_tunnel_env
except ModuleNotFoundError:
    from tests.test_monitoring_coverage import fixture, integration_sources, write_tunnel_env

LARGE_SOCKET_ROWS = 20_000
SOCKET_PARSE_BUDGET_SECONDS = 5.0


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
            for timestamp in (1000, 1005, 1030, 1060):
                collect_once(db, str(env_dir), timestamp, sources, config)
            conn = init_db(db)
            counts = dict(
                conn.execute(
                    "SELECT ts,COUNT(*) FROM metric_points GROUP BY ts ORDER BY ts"
                ).fetchall()
            )
            fast = counts[1005]
            full_extra = counts[1030] - fast
            slow_extra = counts[1060] - counts[1030]
            self.assertEqual(fast, REPRESENTATIVE_FAST_POINTS_PER_CYCLE)
            self.assertEqual(full_extra, REPRESENTATIVE_FULL_SOCKET_EXTRA_POINTS)
            self.assertEqual(slow_extra, REPRESENTATIVE_SLOW_EXTRA_POINTS)

    def test_representative_storage_budget_is_explicit_and_bounded(self):
        budget = representative_storage_budget()
        self.assertEqual(budget.metric_points_per_day, 9_120_960)
        self.assertEqual(budget.raw_metric_points, 18_241_920)
        self.assertEqual(budget.minute_rollup_rows, 25_185_600)
        self.assertEqual(budget.sample_cycle_rows, 34_560)
        self.assertEqual(budget.metric_sample_rows, 241_920)
        self.assertEqual(budget.event_rows, 150_000)
        self.assertEqual(budget.entity_rows, 2_048)
        self.assertAlmostEqual(budget.estimated_total_database_gib, 10.885, places=3)
        self.assertEqual(budget.recommended_disk_reservation_bytes, 12 * GIB)
        self.assertEqual(budget.recommended_disk_reservation_gib, 12.0)

    def test_thirty_day_rollups_are_included_in_total_database_bytes(self):
        budget = estimate_storage_budget()
        table_bytes = (
            budget.estimated_raw_table_bytes
            + budget.estimated_rollup_table_bytes
            + budget.estimated_auxiliary_table_bytes
        )
        self.assertEqual(
            budget.minute_rollup_rows,
            REPRESENTATIVE_ROLLUP_SERIES_PER_MINUTE * 60 * 24 * 30,
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

    def test_twenty_thousand_socket_snapshot_stays_within_one_cycle_budget(self):
        rows = "\n".join(
            "ESTAB 0 0 10.0.0.10:{local} 198.51.100.20:28420 "
            'users:(("gost",pid={pid},fd={fd}))'.format(
                local=10_000 + (index % 50_000),
                pid=4242 if index % 2 == 0 else 5252,
                fd=index + 3,
            )
            for index in range(LARGE_SOCKET_ROWS)
        )
        started = time.perf_counter()
        records = parse_ss_sockets(rows)
        owned = established_socket_count(records, (4242,))
        elapsed = time.perf_counter() - started
        self.assertEqual(len(records), LARGE_SOCKET_ROWS)
        self.assertEqual(owned, LARGE_SOCKET_ROWS // 2)
        self.assertLess(elapsed, SOCKET_PARSE_BUDGET_SECONDS)


if __name__ == "__main__":
    unittest.main()
