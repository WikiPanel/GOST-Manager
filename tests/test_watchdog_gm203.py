from __future__ import annotations

import errno
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from gost_watchdog.admin_cli import (
    Context,
    command_events,
    command_set_check_mode,
    command_summary,
)
from gost_watchdog.commands import TcpConnectExecutor, run_checks
from gost_watchdog.config import GlobalConfig, parse_profile_config, render_global_config
from gost_watchdog.engine import WatchdogEngine
from gost_watchdog.models import (
    Clock,
    ManagedProfile,
    ProbeResult,
    ProfileConfig,
    WatchdogEvent,
)
from gost_watchdog.profiles import parse_safe_target_text
from gost_watchdog.storage import SCHEMA_VERSION, WatchdogStore


class FakeClock:
    def __init__(self, value: int = 10_000) -> None:
        self.value = value

    def now(self) -> float:
        return float(self.value)

    def advance(self, seconds: int) -> None:
        self.value += seconds


class FakeSystemd:
    def __init__(self) -> None:
        self.active: dict[str, bool] = {}
        self.actions: list[tuple[str, str]] = []

    def is_active(self, service: str) -> bool:
        return self.active.get(service, True)

    def stop(self, service: str) -> bool:
        self.actions.append(("stop", service))
        self.active[service] = False
        return True

    def start(self, service: str) -> bool:
        self.actions.append(("start", service))
        self.active[service] = True
        return True


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

def profile(
    profile_id: str = "iran-1",
    *,
    port: int | None = 28420,
    mode: str = "monitor",
    failures: int = 2,
    successes: int = 2,
    check_mode: str = "tcp",
    timeout: int = 1,
) -> ManagedProfile:
    number = profile_id.split("-")[1]
    return ManagedProfile(
        profile_id=profile_id,
        service_name=f"gost-iran-{number}.service",
        kharej_ip="127.0.0.1",
        env_path=f"/etc/gost/{profile_id}.env",
        unit_path=f"/etc/systemd/system/gost-iran-{number}.service",
        config_path=f"/etc/gost-manager/watchdog.d/{profile_id}.conf",
        config=ProfileConfig(
            mode=mode,
            check_mode=check_mode,
            ping_timeout_seconds=timeout,
            failure_threshold=failures,
            success_threshold=successes,
            recovery_hold_seconds=0,
            recovery_jitter_max_seconds=0,
        ),
        tunnel_port=port,
        target_error=None if port is not None else "missing_tunnel_port",
    )


class TcpProbeTests(unittest.TestCase):
    def test_tcp_success_and_refusal(self) -> None:
        connection = FakeConnection()
        self.assertEqual(
            TcpConnectExecutor(lambda *_args, **_kwargs: connection)(
                "127.0.0.1", 28420, 1
            ).status,
            "success",
        )
        self.assertTrue(connection.closed)

        def refused(*_args: object, **_kwargs: object) -> FakeConnection:
            raise OSError(errno.ECONNREFUSED, "refused")

        self.assertEqual(
            TcpConnectExecutor(refused)("127.0.0.1", 28420, 1).status,
            "unreachable",
        )

    def test_timeout_and_local_resource_failures_are_typed(self) -> None:
        def timeout(*_args: object, **_kwargs: object) -> FakeConnection:
            raise OSError(errno.ETIMEDOUT, "timed out")

        self.assertEqual(
            TcpConnectExecutor(timeout)("127.0.0.1", 1, 1).status,
            "unreachable",
        )
        for error_number in (errno.EMFILE, errno.ENFILE, errno.ENOMEM):
            def exhausted(*_args: object, **_kwargs: object) -> FakeConnection:
                raise OSError(error_number, "local resource failure")

            result = TcpConnectExecutor(exhausted)("127.0.0.1", 1, 1)
            self.assertEqual(
                (result.status, result.error_category),
                ("probe_error", "tcp_local_resource_error"),
            )

    def test_ping_failure_does_not_override_open_tcp_check(self) -> None:
        current = profile(port=28420)
        results = run_checks(
            [current],
            lambda _ip, _timeout: ProbeResult("unreachable"),
            TcpConnectExecutor(lambda *_args, **_kwargs: FakeConnection()),
        )
        self.assertEqual(results[current.profile_id].status, "success")

    def test_probe_deduplication_includes_mode_port_and_timeout(self) -> None:
        calls: list[tuple[str, int, int]] = []
        ping_calls: list[tuple[str, int]] = []

        def tcp(ip: str, port: int, timeout: int) -> ProbeResult:
            calls.append((ip, port, timeout))
            return ProbeResult("success")

        profiles = [
            profile("iran-1", port=28420),
            profile("iran-2", port=28420),
            profile("iran-3", port=28421),
            profile("iran-4", port=28420, timeout=2),
            profile("iran-5", port=28420, check_mode="ping"),
        ]

        def ping(ip: str, timeout: int) -> ProbeResult:
            ping_calls.append((ip, timeout))
            return ProbeResult("success")

        results = run_checks(profiles, ping, tcp)
        self.assertEqual(len(results), 5)
        self.assertCountEqual(
            calls,
            [
                ("127.0.0.1", 28420, 1),
                ("127.0.0.1", 28421, 1),
                ("127.0.0.1", 28420, 2),
            ],
        )
        self.assertEqual(ping_calls, [("127.0.0.1", 1)])


class TcpConfigAndEngineTests(unittest.TestCase):
    def test_safe_parser_retains_only_target_fields(self) -> None:
        ip, port, error = parse_safe_target_text(
            "GOST_USER=private-user\nGOST_PASS=private\x00-pass\n"
            "unrelated malformed content\n"
            "MAPPINGS=1:2\nKHAREJ_IP=edge.example.test\nTUNNEL_PORT=28420\n"
        )
        self.assertEqual((ip, port, error), ("edge.example.test", 28420, None))
        self.assertNotIn("private", repr((ip, port, error)))

    def test_tcp_is_explicit_and_invalid_port_fails_closed(self) -> None:
        self.assertEqual(parse_profile_config("MODE=monitor\n", GlobalConfig()).check_mode, "ping")
        self.assertEqual(
            parse_profile_config("MODE=monitor\nCHECK_MODE=tcp\n", GlobalConfig()).check_mode,
            "tcp",
        )
        current = profile(port=None, mode="auto")
        result = run_checks([current], lambda *_args: True, lambda *_args: True)[current.profile_id]
        self.assertEqual(
            (result.status, result.error_category),
            ("probe_error", "invalid_tcp_target"),
        )
        self.assertEqual(
            parse_safe_target_text("KHAREJ_IP=127.0.0.1\n"),
            ("127.0.0.1", None, "missing_tunnel_port"),
        )
        for value in ("0", "65536", "not-a-port"):
            self.assertEqual(
                parse_safe_target_text(
                    f"KHAREJ_IP=127.0.0.1\nTUNNEL_PORT={value}\n"
                ),
                ("127.0.0.1", None, "invalid_tunnel_port"),
            )

    def test_check_mode_change_preserves_manual_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            env_dir = root / "etc/gost"
            unit_dir = root / "etc/systemd/system"
            config_dir = root / "etc/gost-manager/watchdog.d"
            state_dir = root / "var/lib/gost-manager/watchdog"
            for directory in (env_dir, unit_dir, config_dir, state_dir):
                directory.mkdir(parents=True, exist_ok=True)
            global_path = root / "etc/gost-manager/watchdog.conf"
            global_path.write_text(render_global_config(GlobalConfig()), encoding="ascii")
            global_path.chmod(0o600)
            (env_dir / "iran-1.env").write_text(
                "GOST_USER=secret-user\nGOST_PASS=secret-pass\n"
                "KHAREJ_IP=127.0.0.1\nTUNNEL_PORT=28420\n",
                encoding="ascii",
            )
            (unit_dir / "gost-iran-1.service").write_text(
                "[Service]\nExecStart=/bin/true\n",
                encoding="ascii",
            )
            profile_path = config_dir / "iran-1.conf"
            profile_path.write_text("MODE=auto\n", encoding="ascii")
            for path in (
                env_dir / "iran-1.env",
                unit_dir / "gost-iran-1.service",
                profile_path,
            ):
                path.chmod(0o600)
            database = state_dir / "watchdog.sqlite3"
            store = WatchdogStore(str(database))
            state = store.get_state("iran-1", "gost-iran-1.service", "127.0.0.1")
            state.manual_override = True
            store.persist(state, [], 10_000)
            store.close()
            context = Context(
                global_config_path=global_path,
                profile_config_dir=config_dir,
                env_dir=env_dir,
                unit_dir=unit_dir,
                db_path=database,
                boundary=root,
                expected_uid=None,
                owner_uid=os.getuid(),
            )
            with redirect_stdout(io.StringIO()):
                command_set_check_mode(context, "iran-1", "tcp")
            self.assertEqual(
                parse_profile_config(profile_path.read_text(encoding="ascii"), GlobalConfig()).check_mode,
                "tcp",
            )
            store = WatchdogStore(str(database))
            try:
                restored = store.get_state(
                    "iran-1", "gost-iran-1.service", "127.0.0.1"
                )
                self.assertTrue(restored.manual_override)
            finally:
                store.close()

    def test_probe_error_never_changes_counters_or_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = WatchdogStore(str(Path(temporary) / "watchdog.sqlite3"))
            clock = FakeClock()
            systemd = FakeSystemd()
            engine = WatchdogEngine(
                store,
                systemd,  # type: ignore[arg-type]
                clock=Clock(clock.now, clock.now),
                jitter_source=lambda _maximum: 0,
            )
            current = profile(mode="auto", failures=1)
            first = engine.process(current, ProbeResult("probe_error", "tcp_local_resource_error"))
            second = engine.process(current, ProbeResult("probe_error", "tcp_local_resource_error"))
            self.assertEqual((first.failure_count, second.failure_count), (0, 0))
            self.assertEqual(systemd.actions, [])
            self.assertEqual(
                len([row for row in store.events(clock.value) if row["code"] == "watchdog_probe_error"]),
                1,
            )
            clock.advance(2)
            engine.process(current, ProbeResult("success"))
            self.assertEqual(
                len(
                    [
                        row
                        for row in store.events(clock.value)
                        if row["code"] == "watchdog_probe_recovered"
                    ]
                ),
                1,
            )
            store.close()

    def test_tcp_timeout_advances_the_failure_threshold(self) -> None:
        def timeout(*_args: object, **_kwargs: object) -> FakeConnection:
            raise OSError(errno.ETIMEDOUT, "timed out")

        result = TcpConnectExecutor(timeout)("127.0.0.1", 28420, 1)
        with tempfile.TemporaryDirectory() as temporary:
            store = WatchdogStore(str(Path(temporary) / "watchdog.sqlite3"))
            clock = FakeClock()
            systemd = FakeSystemd()
            engine = WatchdogEngine(
                store,
                systemd,  # type: ignore[arg-type]
                clock=Clock(clock.now, clock.now),
                jitter_source=lambda _maximum: 0,
            )
            current = profile(mode="auto", failures=2)
            engine.process(current, result)
            self.assertEqual(systemd.actions, [])
            clock.advance(2)
            engine.process(current, result)
            self.assertEqual(systemd.actions, [("stop", current.service_name)])
            store.close()

    def test_outage_stops_only_target_and_recovery_starts_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = WatchdogStore(str(Path(temporary) / "watchdog.sqlite3"))
            clock = FakeClock()
            systemd = FakeSystemd()
            engine = WatchdogEngine(
                store,
                systemd,  # type: ignore[arg-type]
                clock=Clock(clock.now, clock.now),
                jitter_source=lambda _maximum: 0,
            )
            first = profile("iran-1", mode="auto", failures=2, successes=2)
            other = profile("iran-2", mode="auto", failures=2, successes=2)
            engine.process(first, ProbeResult("unreachable"))
            clock.advance(2)
            engine.process(first, ProbeResult("unreachable"))
            self.assertEqual(systemd.actions, [("stop", first.service_name)])
            self.assertNotIn(("stop", other.service_name), systemd.actions)
            clock.advance(2)
            engine.process(first, ProbeResult("success"))
            clock.advance(2)
            engine.process(first, ProbeResult("success"))
            self.assertEqual(systemd.actions.count(("start", first.service_name)), 1)
            store.close()


class ProductionSnapshotCompatibilityTests(unittest.TestCase):
    script = Path(__file__).resolve().parents[1] / "scripts/gm-203-production-check.sh"

    def _canonicalize(self, payload: dict[str, object]) -> list[dict[str, object]]:
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; canonicalize_watchdog_policy',
                "gm203-snapshot-test",
                str(self.script),
            ],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_v201_and_v202_legacy_ping_snapshots_match_v203(self) -> None:
        policy = {
            "profile_id": "iran-1",
            "mode": "auto",
            "maintenance": True,
            "manual_override": True,
            "stopped_by_watchdog": False,
            "stopped_by_maintenance": True,
        }
        legacy_v201 = self._canonicalize({"profiles": [policy]})
        legacy_v202 = self._canonicalize({"profiles": [policy]})
        current_v203 = self._canonicalize(
            {"profiles": [{**policy, "check_mode": "ping"}]}
        )
        self.assertEqual(legacy_v201, current_v203)
        self.assertEqual(legacy_v202, current_v203)
        self.assertEqual(current_v203[0]["check_mode"], "ping")
        self.assertTrue(current_v203[0]["maintenance"])
        self.assertTrue(current_v203[0]["manual_override"])
        self.assertTrue(current_v203[0]["stopped_by_maintenance"])

    def test_explicit_tcp_is_preserved_and_unrelated_fields_are_omitted(self) -> None:
        rows = self._canonicalize(
            {
                "profiles": [
                    {
                        "profile_id": "iran-2",
                        "mode": "monitor",
                        "check_mode": "tcp",
                        "maintenance": False,
                        "manual_override": False,
                        "stopped_by_watchdog": False,
                        "stopped_by_maintenance": False,
                        "kharej_ip": "192.0.2.1",
                        "credential": "must-not-be-retained",
                    }
                ]
            }
        )
        self.assertEqual(rows[0]["check_mode"], "tcp")
        self.assertNotIn("kharej_ip", rows[0])
        self.assertNotIn("credential", rows[0])


class EventCheckModeHistoryTests(unittest.TestCase):
    @staticmethod
    def _context(root: Path, database: Path, *, check_mode: str = "tcp") -> Context:
        env_dir = root / "etc/gost"
        unit_dir = root / "etc/systemd/system"
        config_dir = root / "etc/gost-manager/watchdog.d"
        for directory in (env_dir, unit_dir, config_dir, database.parent):
            directory.mkdir(parents=True, exist_ok=True)
        global_path = root / "etc/gost-manager/watchdog.conf"
        global_path.write_text(render_global_config(GlobalConfig()), encoding="ascii")
        (env_dir / "iran-1.env").write_text(
            "KHAREJ_IP=127.0.0.1\nTUNNEL_PORT=28420\n", encoding="ascii"
        )
        (unit_dir / "gost-iran-1.service").write_text(
            "[Service]\nExecStart=/bin/true\n", encoding="ascii"
        )
        (config_dir / "iran-1.conf").write_text(
            f"MODE=monitor\nCHECK_MODE={check_mode}\n", encoding="ascii"
        )
        for path in (
            global_path,
            env_dir / "iran-1.env",
            unit_dir / "gost-iran-1.service",
            config_dir / "iran-1.conf",
        ):
            path.chmod(0o600)
        return Context(
            global_config_path=global_path,
            profile_config_dir=config_dir,
            env_dir=env_dir,
            unit_dir=unit_dir,
            db_path=database,
            boundary=root,
            expected_uid=None,
            owner_uid=os.getuid(),
        )

    def test_events_keep_event_time_mode_across_config_change_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            database = root / "var/lib/gost-manager/watchdog/watchdog.sqlite3"
            context = self._context(root, database)
            clock = FakeClock(int(time.time()))
            systemd = FakeSystemd()

            store = WatchdogStore(str(database))
            engine = WatchdogEngine(
                store,
                systemd,  # type: ignore[arg-type]
                clock=Clock(clock.now, clock.now),
                jitter_source=lambda _maximum: 0,
            )
            engine.process(profile(check_mode="ping"), ProbeResult("success"))
            store.close()

            clock.advance(2)
            store = WatchdogStore(str(database))
            engine = WatchdogEngine(
                store,
                systemd,  # type: ignore[arg-type]
                clock=Clock(clock.now, clock.now),
                jitter_source=lambda _maximum: 0,
            )
            engine.process(
                profile(check_mode="tcp"),
                ProbeResult("probe_error", "tcp_socket_error"),
            )
            store.close()

            reopened = WatchdogStore(str(database))
            try:
                event_modes = {
                    row["code"]: row["check_mode"]
                    for row in reopened.events(clock.value, limit=20)
                }
            finally:
                reopened.close()
            self.assertEqual(event_modes["watchdog_upstream_healthy"], "ping")
            self.assertEqual(event_modes["watchdog_probe_error"], "tcp")

            with mock.patch("gost_watchdog.admin_cli.time.time", return_value=clock.value):
                output = io.StringIO()
                with redirect_stdout(output):
                    command_events(context, None, 20, True)
                rows = json.loads(output.getvalue())
                json_modes = {row["code"]: row["check_mode"] for row in rows}
                self.assertEqual(json_modes, event_modes)

                output = io.StringIO()
                with redirect_stdout(output):
                    command_events(context, None, 20, False)
                text = output.getvalue()
                self.assertIn(" ping watchdog_upstream_healthy ", text)
                self.assertIn(" tcp watchdog_probe_error ", text)

                output = io.StringIO()
                with redirect_stdout(output):
                    command_summary(context, "iran-1", True)
                summary = json.loads(output.getvalue())["iran-1"]
                self.assertEqual(summary["current_check_mode"], "tcp")
                self.assertNotIn("check_mode", summary)

                output = io.StringIO()
                with redirect_stdout(output):
                    command_summary(context, "iran-1", False)
                self.assertIn("current_check=tcp", output.getvalue())

    def test_v2_event_history_migrates_to_allowlisted_event_time_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "watchdog.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE schema_migrations(
                    version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL
                );
                INSERT INTO schema_migrations VALUES(2, 1);
                CREATE TABLE events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL, code TEXT NOT NULL,
                    profile_id TEXT, service_name TEXT, kharej_ip TEXT,
                    previous_state TEXT, new_state TEXT,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    action_result TEXT, outage_duration INTEGER,
                    error_category TEXT
                );
                CREATE INDEX idx_watchdog_events_ts ON events(ts);
                CREATE INDEX idx_watchdog_events_profile_ts
                    ON events(profile_id,ts DESC);
                INSERT INTO events(ts,code,profile_id)
                    VALUES(1000,'watchdog_upstream_healthy','iran-1');
                INSERT INTO events(ts,code)
                    VALUES(1001,'watchdog_daemon_started');
                """
            )
            connection.commit()
            connection.close()

            store = WatchdogStore(str(path))
            try:
                self.assertEqual(SCHEMA_VERSION, 3)
                version = store.conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                self.assertEqual(version, 3)
                rows = store.events(1001, limit=10)
                by_code = {row["code"]: row for row in rows}
                self.assertEqual(
                    by_code["watchdog_upstream_healthy"]["check_mode"], "ping"
                )
                self.assertIsNone(
                    by_code["watchdog_daemon_started"]["check_mode"]
                )
                indexes = {
                    row[1]
                    for row in store.conn.execute("PRAGMA index_list(events)")
                }
                self.assertIn("idx_watchdog_events_ts", indexes)
                self.assertIn("idx_watchdog_events_profile_ts", indexes)
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        "INSERT INTO events(ts,code,check_mode) VALUES(?,?,?)",
                        (1002, "watchdog_daemon_started", "udp"),
                    )
            finally:
                store.close()

            reopened = WatchdogStore(str(path))
            try:
                rows = reopened.events(1001, limit=10)
                self.assertEqual(len(rows), 2)
                self.assertEqual(
                    next(
                        row["check_mode"]
                        for row in rows
                        if row["profile_id"] == "iran-1"
                    ),
                    "ping",
                )
            finally:
                reopened.close()

            with self.assertRaises(ValueError):
                WatchdogEvent(1002, "watchdog_daemon_started", check_mode="udp")


if __name__ == "__main__":
    unittest.main()
