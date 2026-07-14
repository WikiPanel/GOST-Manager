#!/usr/bin/env python3
"""Regression coverage for optional conntrack capability detection."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from monitoring.collector import CollectorConfig, CommandResult, collect_once
from monitoring.health import evaluate_snapshot
from monitoring.query_db import ReadOnlyDatabase
from monitoring.query_engine import QueryEngine
from monitoring.renderers import render_snapshot_plain
from monitoring.schema import SCHEMA_VERSION, get_state, init_db, set_state

try:
    from test_monitoring_coverage import integration_sources, stored_metric
except ModuleNotFoundError:
    from tests.test_monitoring_coverage import integration_sources, stored_metric


MISSING = object()
COUNT_NAME = "nf_conntrack_count"
MAXIMUM_NAME = "nf_conntrack_max"


class OptionalConntrackTests(unittest.TestCase):
    @staticmethod
    def _source_metadata(conn, source: str) -> dict[str, object]:
        return json.loads(
            conn.execute(
                "SELECT metadata_json FROM entities WHERE entity_type='collector_source' "
                "AND entity_id=?",
                (source,),
            ).fetchone()[0]
        )

    def _sources(self, root: Path, state: dict[str, object], monotonic: list[float]):
        command_log: list[tuple[str, ...]] = []
        read_log: list[str] = []

        def command(parts):
            command_log.append(tuple(parts))
            if parts[0] == "ss":
                return CommandResult("", "", 0)
            raise AssertionError(f"unexpected command: {parts}")

        base = integration_sources(root, command, monotonic)

        def exists(path: Path) -> bool:
            if path.name in {COUNT_NAME, MAXIMUM_NAME}:
                return state[path.name] is not MISSING
            return path.exists()

        def read_text(path: Path) -> str:
            if path.name in {COUNT_NAME, MAXIMUM_NAME}:
                read_log.append(path.name)
                value = state[path.name]
                if value is MISSING:
                    raise FileNotFoundError(path)
                if isinstance(value, Exception):
                    raise value
                return str(value)
            if path == base.proc_root / "stat":
                ticks = int(monotonic[0] * 10)
                half = ticks // 2
                return (
                    f"cpu  {ticks} 0 {ticks // 10} {ticks} 0 0 0 0 0 0\n"
                    f"cpu0 {half} 0 {half // 10} {half} 0 0 0 0 0 0\n"
                    f"cpu1 {ticks - half} 0 {(ticks - half) // 10} "
                    f"{ticks - half} 0 0 0 0 0 0\n"
                )
            return path.read_text(encoding="utf-8")

        sources = dataclasses.replace(base, exists=exists, read_text=read_text)
        return sources, command_log, read_log

    def _collect(
        self,
        root: Path,
        state: dict[str, object],
        cycles: int = 1,
        initial_total: int | None = None,
        fail_required: bool = False,
    ):
        db = str(root / "metrics.sqlite3")
        env_dir = root / "env"
        env_dir.mkdir(exist_ok=True)
        if initial_total is not None:
            conn = init_db(db)
            set_state(conn, "counter.source_errors_total", str(initial_total))
            conn.commit()
            conn.close()
        monotonic = [10.0]
        sources, command_log, read_log = self._sources(root, state, monotonic)
        if fail_required:
            original_reader = sources.read_text

            def read_text(path: Path) -> str:
                if path == sources.proc_root / "stat":
                    raise OSError("required source failure")
                return original_reader(path)

            sources = dataclasses.replace(sources, read_text=read_text)
        config = CollectorConfig(
            sample_interval=10.0,
            tcp_snapshot_interval=30.0,
            slow_sample_interval=60.0,
            filesystem_paths=(Path("/"),),
        )
        for index in range(cycles):
            collect_once(
                db,
                str(env_dir),
                1000 + index * 10,
                sources,
                config,
            )
            monotonic[0] += 10.0
        return db, sources, command_log, read_log, monotonic, config

    @staticmethod
    def _snapshot(db: str, now: int) -> dict[str, object]:
        return QueryEngine(ReadOnlyDatabase(db), clock=lambda: now).snapshot()

    def test_both_absent_for_sixty_cycles_is_bounded_and_healthy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {COUNT_NAME: MISSING, MAXIMUM_NAME: MISSING}
            db, _sources, commands, reads, _monotonic, _config = self._collect(
                root,
                state,
                cycles=60,
                initial_total=3548,
            )
            conn = init_db(db)
            self.assertEqual(stored_metric(conn, "local", "source_error_codes")[0], 0)
            totals = conn.execute(
                "SELECT numeric_value FROM metric_points p JOIN entities e "
                "ON e.entity_pk=p.entity_pk WHERE e.entity_id='local' "
                "AND p.metric_name='source_errors_total' ORDER BY p.ts"
            ).fetchall()
            self.assertEqual(len(totals), 60)
            self.assertEqual({row[0] for row in totals}, {3548.0})
            self.assertEqual(get_state(conn, "counter.source_errors_total"), "3548")
            self.assertEqual(self._source_metadata(conn, "conntrack")["state"], "unsupported")
            self.assertEqual(
                stored_metric(conn, "conntrack", "source_conntrack_available")[0],
                0,
            )
            for name in (
                "conntrack_count",
                "conntrack_max",
                "conntrack_utilization_percent",
            ):
                self.assertEqual(stored_metric(conn, "local", name)[2], "unavailable")
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM metric_points p JOIN entities e "
                    "ON e.entity_pk=p.entity_pk WHERE e.entity_id='conntrack' "
                    "AND p.metric_name='source_errors'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE code='optional_source_unsupported'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE code='metric_source_unavailable' "
                    "AND details_json LIKE '%conntrack%'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE details_json LIKE '%conntrack%'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0],
                SCHEMA_VERSION,
            )
            conn.close()

            snapshot = self._snapshot(db, 1590)
            health = evaluate_snapshot(snapshot)["overall"]
            self.assertEqual(health["status"], "healthy")
            self.assertEqual(health["reason_codes"], ("observations_current",))
            self.assertIn("conntrack: unsupported", render_snapshot_plain(snapshot))
            self.assertEqual(reads, [])
            self.assertTrue(all(command[0] == "ss" for command in commands))
            self.assertEqual(
                sum(command[0] in {"systemctl", "service", "gost", "modprobe"} for command in commands),
                0,
            )

    def test_dynamic_available_and_unsupported_transitions_need_no_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {COUNT_NAME: MISSING, MAXIMUM_NAME: MISSING}
            db, sources, commands, _reads, monotonic, config = self._collect(root, state)
            state.update({COUNT_NAME: "25\n", MAXIMUM_NAME: "100\n"})
            collect_once(db, str(root / "env"), 1010, sources, config)
            monotonic[0] += 10.0
            available_snapshot = self._snapshot(db, 1010)
            self.assertEqual(
                available_snapshot["optional_sources"]["conntrack"]["state"],
                "available",
            )
            self.assertIn("conntrack: 25.00", render_snapshot_plain(available_snapshot))
            state.update({COUNT_NAME: MISSING, MAXIMUM_NAME: MISSING})
            collect_once(db, str(root / "env"), 1020, sources, config)
            monotonic[0] += 10.0
            collect_once(db, str(root / "env"), 1030, sources, config)

            conn = init_db(db)
            values = conn.execute(
                "SELECT p.ts,p.numeric_value,p.quality FROM metric_points p JOIN entities e "
                "ON e.entity_pk=p.entity_pk WHERE e.entity_id='local' "
                "AND p.metric_name='conntrack_utilization_percent' ORDER BY p.ts"
            ).fetchall()
            self.assertEqual(values[1], (1010, 25.0, "derived"))
            self.assertEqual(values[2][2], "unavailable")
            self.assertEqual(
                conn.execute(
                    "SELECT numeric_value FROM metric_points p JOIN entities e "
                    "ON e.entity_pk=p.entity_pk WHERE e.entity_id='local' "
                    "AND p.metric_name='conntrack_count' AND p.ts=1010"
                ).fetchone()[0],
                25,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT numeric_value FROM metric_points p JOIN entities e "
                    "ON e.entity_pk=p.entity_pk WHERE e.entity_id='local' "
                    "AND p.metric_name='conntrack_max' AND p.ts=1010"
                ).fetchone()[0],
                100,
            )
            self.assertEqual(stored_metric(conn, "local", "source_errors_total")[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE code='optional_source_available'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE code='optional_source_unsupported'"
                ).fetchone()[0],
                2,
            )
            conn.close()
            self.assertIn("conntrack: unsupported", render_snapshot_plain(self._snapshot(db, 1030)))
            self.assertEqual(
                sum(command[0] in {"systemctl", "service", "gost", "modprobe"} for command in commands),
                0,
            )

    def test_partial_missing_files_are_real_failures(self):
        cases = (
            {COUNT_NAME: MISSING, MAXIMUM_NAME: "100\n"},
            {COUNT_NAME: "25\n", MAXIMUM_NAME: MISSING},
        )
        for state in cases:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temp:
                db, _sources, _commands, _reads, _monotonic, _config = self._collect(
                    Path(temp),
                    state,
                )
                conn = init_db(db)
                self.assertEqual(stored_metric(conn, "local", "source_error_codes")[0], 1)
                self.assertEqual(stored_metric(conn, "local", "source_errors_total")[0], 1)
                self.assertEqual(self._source_metadata(conn, "conntrack")["state"], "failed")
                metadata = self._source_metadata(conn, "conntrack")
                self.assertEqual(metadata["error_kind"], "missing_file")
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM events WHERE code='metric_source_unavailable' "
                        "AND details_json LIKE '%conntrack%'"
                    ).fetchone()[0],
                    1,
                )
                conn.close()
                self.assertIn("conntrack: failed", render_snapshot_plain(self._snapshot(db, 1000)))

    def test_permission_and_invalid_values_remain_real_failures(self):
        cases = (
            (
                {COUNT_NAME: PermissionError("credential-canary"), MAXIMUM_NAME: "100\n"},
                "permission_denied",
            ),
            ({COUNT_NAME: "not-a-number\n", MAXIMUM_NAME: "100\n"}, "parse_error"),
            ({COUNT_NAME: "25\n", MAXIMUM_NAME: "0\n"}, "parse_error"),
        )
        for state, expected_kind in cases:
            with self.subTest(expected_kind=expected_kind), tempfile.TemporaryDirectory() as temp:
                db, _sources, _commands, _reads, _monotonic, _config = self._collect(
                    Path(temp),
                    state,
                )
                conn = init_db(db)
                self.assertEqual(stored_metric(conn, "local", "source_error_codes")[0], 1)
                self.assertEqual(stored_metric(conn, "local", "source_errors_total")[0], 1)
                metadata = self._source_metadata(conn, "conntrack")
                self.assertEqual(metadata["error_kind"], expected_kind)
                persisted = "\n".join(
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
                self.assertNotIn("credential-canary", persisted)
                self.assertNotIn("/proc/sys/net/netfilter", persisted)
                conn.close()

    def test_unsupported_conntrack_does_not_hide_required_source_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            state = {COUNT_NAME: MISSING, MAXIMUM_NAME: MISSING}
            db, _sources, _commands, _reads, _monotonic, _config = self._collect(
                Path(temp),
                state,
                fail_required=True,
            )
            conn = init_db(db)
            self.assertEqual(stored_metric(conn, "local", "source_error_codes")[0], 1)
            self.assertEqual(stored_metric(conn, "local", "source_errors_total")[0], 1)
            self.assertEqual(self._source_metadata(conn, "conntrack")["state"], "unsupported")
            self.assertEqual(self._source_metadata(conn, "proc_stat")["state"], "failed")
            conn.close()
            health = evaluate_snapshot(self._snapshot(db, 1000))["overall"]
            self.assertEqual(health["status"], "unknown")
            self.assertIn("required_data_unavailable", health["reason_codes"])


if __name__ == "__main__":
    unittest.main()
