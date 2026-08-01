from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from monitoring import collector
from monitoring.admin_cli import AdminUnsafeError, apply_high_traffic_preset
from monitoring.collector import CollectorConfig, CollectorSources, collect_once
from monitoring.config import (
    GENERIC_POLICY,
    MonitoringConfig,
    load_config,
)
from monitoring.event_state import EventState
from monitoring.health import HealthPolicy, _required_event_codes
from monitoring.models import Clock, CommandResult, Event
from monitoring.proc_readers import read_process_slow_snapshot
from monitoring.scheduler import LoadSheddingState, run_daemon
from monitoring.schema import (
    EVENT_RETENTION_SECONDS,
    RAW_RETENTION_SECONDS,
    RETENTION_DELETE_BATCH_SIZE,
    ROLLUP_RETENTION_SECONDS,
    apply_retention,
    init_db,
    insert_event,
)


FIXTURES = Path(__file__).parent / "fixtures/monitoring"


def config_text(config: MonitoringConfig, *, include_load_shedding: bool = True) -> str:
    values = config.as_mapping()
    if not include_load_shedding:
        values.pop("GOST_MONITOR_LOAD_SHEDDING")
    return "".join(f"{key}={value}\n" for key, value in values.items())


class MonitoringConfigPresetTests(unittest.TestCase):
    def _write(self, root: Path, config: MonitoringConfig) -> Path:
        path = root / "monitoring.env"
        path.write_text(config_text(config), encoding="ascii")
        path.chmod(0o600)
        return path

    def test_legacy_config_loads_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "monitoring.env"
            original = config_text(
                MonitoringConfig(
                    sample_interval=11,
                    tcp_interval=33,
                    slow_interval=66,
                    maintenance_interval=900,
                ),
                include_load_shedding=False,
            )
            path.write_text(original, encoding="ascii")
            path.chmod(0o600)
            loaded = load_config(path)
            self.assertEqual((loaded.sample_interval, loaded.tcp_interval), (11, 33))
            self.assertTrue(loaded.load_shedding)
            self.assertEqual(path.read_text(encoding="ascii"), original)

    def test_preset_is_atomic_backed_up_and_restarts_only_collector(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            current = MonitoringConfig(
                sample_interval=10,
                tcp_interval=30,
                slow_interval=60,
                maintenance_interval=900,
                load_shedding=False,
            )
            path = self._write(root, current)
            calls: list[tuple[str, ...]] = []

            def runner(parts, **_kwargs):
                calls.append(tuple(parts))
                return SimpleNamespace(returncode=0)

            result = apply_high_traffic_preset(
                path,
                policy=GENERIC_POLICY,
                root=None,
                restart_collector=True,
                command_runner=runner,
            )
            self.assertTrue(result["changed"])
            self.assertTrue(result["collector_restarted"])
            self.assertEqual(calls, [("systemctl", "restart", "gost-monitor-collector.service")])
            self.assertFalse(any("gost-iran" in item or "gost-kharej" in item for call in calls for item in call))
            updated = load_config(path)
            self.assertEqual(
                (
                    updated.sample_interval,
                    updated.tcp_interval,
                    updated.slow_interval,
                    updated.maintenance_interval,
                ),
                (20, 60, 120, 1800),
            )
            self.assertFalse(updated.load_shedding)
            backup = Path(str(result["backup"]))
            self.assertEqual(backup.read_text(encoding="ascii"), config_text(current))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            calls.clear()
            no_op = apply_high_traffic_preset(
                path,
                policy=GENERIC_POLICY,
                root=None,
                restart_collector=True,
                command_runner=runner,
            )
            self.assertFalse(no_op["changed"])
            self.assertEqual(no_op["backup"], None)
            self.assertEqual(calls, [])

    def test_preset_rejects_symlink_unsafe_permissions_and_invalid_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = self._write(
                root,
                MonitoringConfig(sample_interval=10, tcp_interval=30, slow_interval=60),
            )
            link = root / "linked.env"
            link.symlink_to(target)
            with self.assertRaises(AdminUnsafeError):
                apply_high_traffic_preset(
                    link,
                    policy=GENERIC_POLICY,
                    root=None,
                    restart_collector=False,
                )
            target.chmod(0o644)
            with self.assertRaises(AdminUnsafeError):
                apply_high_traffic_preset(
                    target,
                    policy=GENERIC_POLICY,
                    root=None,
                    restart_collector=False,
                )
            target.chmod(0o600)
            target.write_text("invalid\n", encoding="ascii")
            with self.assertRaises(Exception):
                apply_high_traffic_preset(
                    target,
                    policy=GENERIC_POLICY,
                    root=None,
                    restart_collector=False,
                )


class LoadSheddingTests(unittest.TestCase):
    def test_state_machine_enters_once_and_recovers_after_three_cycles(self) -> None:
        state = LoadSheddingState()
        state.observe(overrun=True, missed_deadlines=1, overrun_seconds=1.0, enabled=True)
        self.assertTrue(state.active)
        self.assertEqual(state.consume_cycle_context(), ("entered", True, 1, 1.0))
        state.observe(overrun=True, missed_deadlines=2, overrun_seconds=6.0, enabled=True)
        self.assertEqual(state.consume_cycle_context()[0], None)
        for _ in range(2):
            state.observe(overrun=False, missed_deadlines=0, overrun_seconds=0.0, enabled=True)
            self.assertTrue(state.active)
            self.assertIsNone(state.consume_cycle_context()[0])
        state.observe(overrun=False, missed_deadlines=0, overrun_seconds=0.0, enabled=True)
        self.assertFalse(state.active)
        self.assertEqual(state.consume_cycle_context()[0], "recovered")

    def test_daemon_defers_heavy_work_without_queueing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = str(root / "metrics.sqlite3")
            env = root / "env"
            env.mkdir()
            monotonic = [0.0]
            wall = [1000.0]
            durations = iter((6.0, 1.0, 1.0, 1.0, 1.0))
            calls: list[dict[str, object]] = []

            def collect(*_args, **kwargs):
                calls.append(dict(kwargs))
                duration = next(durations)
                monotonic[0] += duration
                wall[0] += duration
                return int(wall[0])

            sources = CollectorSources(clock=Clock(lambda: wall[0], lambda: monotonic[0]))
            with mock.patch("monitoring.scheduler.signal.signal", return_value=None):
                run_daemon(
                    db,
                    str(env),
                    sources=sources,
                    sleeper=lambda seconds: monotonic.__setitem__(0, monotonic[0] + seconds),
                    stop_requested=lambda: len(calls) >= 5,
                    collect=collect,
                    config=CollectorConfig(sample_interval=5.0),
                )
            self.assertEqual(
                [call["defer_heavy"] for call in calls],
                [False, True, True, True, False],
            )
            self.assertEqual(
                [call["load_shedding_transition"] for call in calls],
                [None, "entered", None, None, "recovered"],
            )

    def test_load_shedding_events_are_persisted_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "metrics.sqlite3")
            conn = init_db(database)
            codes: list[str] = []
            for ts, active in ((100, False), (101, True), (102, True), (103, False), (104, False)):
                if ts == 103:
                    conn.close()
                    conn = init_db(database)
                conn.execute("BEGIN IMMEDIATE")
                events = EventState(conn).boolean_transition(
                    "collection_load_shedding",
                    active,
                    ts,
                    "collection_load_shedding_entered",
                    "entered",
                    "collection_load_shedding_recovered",
                    "recovered",
                )
                codes.extend(event.code for event in events)
                conn.commit()
            self.assertEqual(
                codes,
                [
                    "collection_load_shedding_entered",
                    "collection_load_shedding_recovered",
                ],
            )
            conn.close()

    def test_load_shedding_health_clears_on_recovery(self) -> None:
        entered = {
            "ts": 100,
            "severity": "warning",
            "code": "collection_load_shedding_entered",
            "details": {},
        }
        snapshot: dict[str, object] = {
            "events": [entered],
            "entities": [],
            "metrics": [],
        }
        self.assertIn(
            "collection_load_shedding_entered",
            _required_event_codes(snapshot, 105, HealthPolicy()),
        )
        snapshot["events"] = [
            entered,
            {
                "ts": 104,
                "severity": "info",
                "code": "collection_load_shedding_recovered",
                "details": {},
            },
        ]
        self.assertNotIn(
            "collection_load_shedding_entered",
            _required_event_codes(snapshot, 105, HealthPolicy()),
        )


class CollectorCadenceAndMaintenanceTests(unittest.TestCase):
    def test_ten_thousand_fds_are_enumerated_only_by_slow_reader(self) -> None:
        calls = [0]

        def read_text(path: Path) -> str:
            return (FIXTURES / "proc/4242" / path.name).read_text(encoding="utf-8")

        def list_dir(_path: Path) -> list[str]:
            calls[0] += 1
            return [str(number) for number in range(10_000)]

        snapshot = read_process_slow_snapshot(
            4242,
            Path("/proc"),
            read_text,
            list_dir,
        )
        self.assertEqual(snapshot.fd_count, 10_000)
        self.assertEqual(calls[0], 1)

    def test_fast_cycle_skips_ten_thousand_fd_enumeration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = root / "env"
            units = root / "units"
            cgroup = root / "cgroup/system.slice/gost-iran-1.service"
            env.mkdir()
            units.mkdir()
            cgroup.mkdir(parents=True)
            (units / "gost-iran-1.service").write_text("[Service]\n", encoding="ascii")
            (cgroup / "cgroup.procs").write_text("4242\n", encoding="ascii")
            calls = [0]

            def command(parts: list[str] | tuple[str, ...]) -> CommandResult:
                if parts[0] == "systemctl":
                    return CommandResult(
                        "Id=gost-iran-1.service\nActiveState=active\n"
                        "SubState=running\nMainPID=4242\nNRestarts=0\n"
                        "ControlGroup=/system.slice/gost-iran-1.service\n",
                        "",
                        0,
                    )
                return CommandResult("", "", 0)

            def list_dir(_path: Path) -> list[str]:
                calls[0] += 1
                return [str(number) for number in range(10_000)]

            sources = CollectorSources(
                command=command,
                read_text=lambda path: path.read_text(encoding="utf-8"),
                list_dir=list_dir,
                proc_root=FIXTURES / "proc",
                sys_root=FIXTURES / "sys",
                cgroup_root=root / "cgroup",
                systemd_unit_root=units,
            )
            config = CollectorConfig(filesystem_paths=())
            database = str(root / "metrics.sqlite3")
            collect_once(database, str(env), 1000, sources, config)
            calls[0] = 0
            collect_once(database, str(env), 1020, sources, config)
            self.assertEqual(calls[0], 0)
            collect_once(database, str(env), 1120, sources, config)
            self.assertEqual(calls[0], 1)

    def test_one_main_transaction_per_nonmaintenance_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = root / "env"
            env.mkdir()
            db = str(root / "metrics.sqlite3")
            init_db(db).close()
            statements: list[str] = []
            original = collector.open_runtime_database

            def traced(path: str) -> sqlite3.Connection:
                conn = original(path)
                conn.set_trace_callback(statements.append)
                return conn

            sources = CollectorSources(
                command=lambda _parts: CommandResult("", "", 0),
                read_text=lambda path: path.read_text(encoding="utf-8"),
                glob=lambda _root, _pattern: [],
                exists=Path.exists,
            )
            # Use the real fixture roots and no managed services; only the
            # collector connection is instrumented.
            sources = dataclasses.replace(
                sources,
                proc_root=FIXTURES / "proc",
                sys_root=FIXTURES / "sys",
                systemd_unit_root=root / "units",
            )
            (root / "units").mkdir()
            with mock.patch("monitoring.collector.open_runtime_database", side_effect=traced):
                collect_once(
                    db,
                    str(env),
                    1000,
                    sources=sources,
                    config=CollectorConfig(filesystem_paths=()),
                )
            begins = [sql for sql in statements if sql.strip().upper() == "BEGIN IMMEDIATE"]
            self.assertEqual(len(begins), 1)

    def test_retention_pruning_is_bounded_and_retention_values_unchanged(self) -> None:
        self.assertEqual((RAW_RETENTION_SECONDS, ROLLUP_RETENTION_SECONDS, EVENT_RETENTION_SECONDS), (21600, 86400, 86400))
        with tempfile.TemporaryDirectory() as temporary:
            conn = init_db(str(Path(temporary) / "metrics.sqlite3"))
            now = 200_000
            conn.execute("BEGIN IMMEDIATE")
            for index in range(RETENTION_DELETE_BATCH_SIZE + 5):
                insert_event(conn, Event(now - EVENT_RETENTION_SECONDS - index - 1, "info", "old", "old"))
            apply_retention(conn, now)
            conn.commit()
            remaining = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            self.assertEqual(remaining, 5)
            conn.execute("BEGIN IMMEDIATE")
            apply_retention(conn, now)
            conn.commit()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
