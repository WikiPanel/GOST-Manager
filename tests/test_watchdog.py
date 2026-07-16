from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from gost_watchdog.commands import SystemdController, run_ping_checks
from gost_watchdog.config import (
    ConfigError,
    atomic_write_config,
    default_global_config_text,
    load_global_config,
    parse_global_config,
    parse_profile_config,
)
from gost_watchdog.daemon import WatchdogDaemon, advance_deadline
from gost_watchdog.engine import MaintenanceController, WatchdogEngine
from gost_watchdog.models import (
    CHECK_INTERVAL_SECONDS,
    FAILURE_THRESHOLD,
    PING_TIMEOUT_SECONDS,
    RECOVERY_HOLD_SECONDS,
    RECOVERY_JITTER_MAX_SECONDS,
    SUCCESS_THRESHOLD,
    Clock,
    GlobalConfig,
    ManagedProfile,
    ProfileConfig,
    WatchdogEvent,
)
from gost_watchdog.profiles import (
    ProfileError,
    discover_profiles,
    parse_kharej_ip_text,
    validate_service_name,
)
from gost_watchdog.storage import EVENT_RETENTION_SECONDS, WatchdogStore


class FakeClock:
    def __init__(self, value: int = 1_000) -> None:
        self.value = value

    def wall(self) -> float:
        return float(self.value)

    def monotonic(self) -> float:
        return float(self.value)

    def advance(self, seconds: int) -> None:
        self.value += seconds

    def model(self) -> Clock:
        return Clock(self.wall, self.monotonic)


class FakeSystemd:
    def __init__(self, active: bool = True) -> None:
        self.active = active
        self.calls: list[tuple[str, str]] = []
        self.stop_success = True
        self.start_success = True

    def is_active(self, service: str) -> bool:
        validate_service_name(service)
        self.calls.append(("is-active", service))
        return self.active

    def stop(self, service: str) -> bool:
        validate_service_name(service)
        self.calls.append(("stop", service))
        if self.stop_success:
            self.active = False
            return True
        return False

    def start(self, service: str) -> bool:
        validate_service_name(service)
        self.calls.append(("start", service))
        if self.start_success:
            self.active = True
            return True
        return False


def profile(
    *,
    profile_id: str = "iran-1",
    ip: str = "203.0.113.10",
    mode: str = "auto",
    failures: int = 10,
    successes: int = 10,
    hold: int = 10,
    jitter: int = 10,
) -> ManagedProfile:
    number = profile_id.split("-")[1]
    return ManagedProfile(
        profile_id,
        f"gost-iran-{number}.service",
        ip,
        f"/etc/gost/{profile_id}.env",
        f"/etc/systemd/system/gost-{profile_id}.service",
        f"/etc/gost-manager/watchdog.d/{profile_id}.conf",
        ProfileConfig(
            mode=mode,
            failure_threshold=failures,
            success_threshold=successes,
            recovery_hold_seconds=hold,
            recovery_jitter_max_seconds=jitter,
        ),
    )


class WatchdogConfigTests(unittest.TestCase):
    def test_exact_production_defaults(self) -> None:
        config = parse_global_config(default_global_config_text())
        self.assertEqual(CHECK_INTERVAL_SECONDS, 2)
        self.assertEqual(PING_TIMEOUT_SECONDS, 1)
        self.assertEqual(FAILURE_THRESHOLD, 10)
        self.assertEqual(SUCCESS_THRESHOLD, 10)
        self.assertEqual(RECOVERY_HOLD_SECONDS, 10)
        self.assertEqual(RECOVERY_JITTER_MAX_SECONDS, 10)
        self.assertEqual(config.check_interval_seconds, 2)

    def test_profile_inherits_global_and_defaults_disabled(self) -> None:
        config = parse_profile_config("MODE=disabled\n", GlobalConfig())
        self.assertEqual(config.mode, "disabled")
        self.assertEqual(config.check_interval_seconds, 2)

    def test_profile_overrides(self) -> None:
        config = parse_profile_config(
            "MODE=monitor\nCHECK_INTERVAL_SECONDS=5\nPING_TIMEOUT_SECONDS=3\n"
            "FAILURE_THRESHOLD=4\nSUCCESS_THRESHOLD=6\nRECOVERY_HOLD_SECONDS=7\n"
            "RECOVERY_JITTER_MAX_SECONDS=8\n",
            GlobalConfig(),
        )
        self.assertEqual(dataclass_values(config), ("monitor", 5, 3, 4, 6, 7, 8))

    def test_invalid_mode_fails_closed(self) -> None:
        with self.assertRaises(ConfigError):
            parse_profile_config("MODE=restart-everything\n", GlobalConfig())

    def test_timeout_may_not_exceed_interval(self) -> None:
        with self.assertRaises(ConfigError):
            parse_profile_config(
                "MODE=auto\nCHECK_INTERVAL_SECONDS=2\nPING_TIMEOUT_SECONDS=3\n",
                GlobalConfig(),
            )

    def test_unknown_duplicate_and_shell_values_rejected(self) -> None:
        invalid = (
            "MODE=auto\nUNKNOWN=1\n",
            "MODE=auto\nMODE=monitor\n",
            "MODE=$(id)\n",
        )
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ConfigError):
                parse_profile_config(text, GlobalConfig())

    def test_symlinked_config_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_text(default_global_config_text(), encoding="ascii")
            target.chmod(0o600)
            link = root / "watchdog.conf"
            link.symlink_to(target)
            with self.assertRaises(ConfigError):
                load_global_config(link)

    def test_atomic_write_is_restricted_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "watchdog.conf"
            atomic_write_config(path, default_global_config_text(), boundary=root)
            self.assertEqual(path.read_text(encoding="ascii"), default_global_config_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(any(item.name.startswith(".watchdog.conf.") for item in root.iterdir()))


def dataclass_values(config: ProfileConfig) -> tuple[object, ...]:
    return (
        config.mode,
        config.check_interval_seconds,
        config.ping_timeout_seconds,
        config.failure_threshold,
        config.success_threshold,
        config.recovery_hold_seconds,
        config.recovery_jitter_max_seconds,
    )


class WatchdogDiscoveryTests(unittest.TestCase):
    def test_env_parser_reads_only_safe_kharej_value(self) -> None:
        text = "GOST_USER=secret-user\nGOST_PASS=secret-pass\nKHAREJ_IP=edge.example.test\n"
        self.assertEqual(parse_kharej_ip_text(text), "edge.example.test")

    def test_discovery_requires_matching_regular_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = root / "etc/gost"
            units = root / "etc/systemd/system"
            configs = root / "etc/gost-manager/watchdog.d"
            env.mkdir(parents=True)
            units.mkdir(parents=True)
            configs.mkdir(parents=True)
            (env / "iran-1.env").write_text("KHAREJ_IP=203.0.113.1\n", encoding="ascii")
            (env / "iran-1.env").chmod(0o600)
            profiles, _ = discover_profiles(env, units, configs, GlobalConfig())
            self.assertEqual(profiles, [])
            (units / "gost-iran-1.service").write_text("[Service]\n", encoding="ascii")
            profiles, errors = discover_profiles(env, units, configs, GlobalConfig())
            self.assertEqual(errors, [])
            self.assertEqual([item.profile_id for item in profiles], ["iran-1"])
            self.assertEqual(profiles[0].config.mode, "disabled")

    def test_kharej_and_malformed_profiles_are_never_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = root / "env"
            units = root / "units"
            configs = root / "configs"
            for directory in (env, units, configs):
                directory.mkdir()
            (env / "kharej-1.env").write_text("KHAREJ_IP=203.0.113.1\n")
            (env / "iran-0.env").write_text("KHAREJ_IP=203.0.113.2\n")
            profiles, errors = discover_profiles(env, units, configs, GlobalConfig())
            self.assertEqual(profiles, [])
            self.assertEqual(errors, [])

    def test_symlinked_env_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = root / "env"
            units = root / "units"
            configs = root / "configs"
            for directory in (env, units, configs):
                directory.mkdir()
            external = root / "external"
            external.write_text("KHAREJ_IP=203.0.113.1\n")
            (env / "iran-1.env").symlink_to(external)
            (units / "gost-iran-1.service").write_text("[Service]\n")
            profiles, errors = discover_profiles(env, units, configs, GlobalConfig())
            self.assertEqual(profiles, [])
            self.assertEqual(errors, [("iran-1", "invalid_profile")])


class WatchdogPingTests(unittest.TestCase):
    def test_ten_unique_destinations_are_concurrent(self) -> None:
        barrier = threading.Barrier(10, timeout=2.0)
        calls: list[str] = []
        lock = threading.Lock()

        def check(destination: str, _timeout: int) -> bool:
            with lock:
                calls.append(destination)
            barrier.wait()
            return True

        profiles = [profile(profile_id=f"iran-{index}", ip=f"203.0.113.{index}") for index in range(1, 11)]
        results = run_ping_checks(profiles, check)
        self.assertEqual(len(calls), 10)
        self.assertTrue(all(results.values()))

    def test_duplicate_destinations_are_checked_once(self) -> None:
        calls: list[str] = []
        profiles = [profile(profile_id="iran-1"), profile(profile_id="iran-2")]
        results = run_ping_checks(profiles, lambda ip, _timeout: calls.append(ip) is None)
        self.assertEqual(calls, ["203.0.113.10"])
        self.assertEqual(
            {key: value.status for key, value in results.items()},
            {"iran-1": "success", "iran-2": "success"},
        )

    def test_disabled_profile_performs_no_check(self) -> None:
        calls: list[str] = []
        results = run_ping_checks(
            [profile(mode="disabled")], lambda ip, _timeout: calls.append(ip) is None
        )
        self.assertEqual(results, {})
        self.assertEqual(calls, [])

    def test_arbitrary_and_kharej_units_are_rejected(self) -> None:
        for unit in ("ssh.service", "gost-kharej-1.service", "gost-iran-0.service"):
            with self.subTest(unit=unit), self.assertRaises(ProfileError):
                validate_service_name(unit)

    def test_systemd_uses_argv_without_shell(self) -> None:
        seen: list[tuple[list[str], bool]] = []

        class Result:
            returncode = 0

        def runner(argv: list[str], **kwargs: object) -> Result:
            seen.append((argv, bool(kwargs["shell"])))
            return Result()

        controller = SystemdController(runner=runner)
        self.assertTrue(controller.is_active("gost-iran-1.service"))
        self.assertEqual(seen[0][0], ["systemctl", "is-active", "--quiet", "gost-iran-1.service"])
        self.assertFalse(seen[0][1])


class WatchdogStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temporary.name) / "watchdog.sqlite3")
        self.store = WatchdogStore(self.db)
        self.clock = FakeClock()
        self.systemd = FakeSystemd(True)
        self.engine = WatchdogEngine(
            self.store,
            self.systemd,
            clock=self.clock.model(),
            jitter_source=lambda _maximum: 0,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _events(self) -> list[str]:
        return [row["code"] for row in reversed(self.store.events(self.clock.value, limit=1000))]

    def test_one_failure_increments_and_success_resets(self) -> None:
        state = self.engine.process(profile(), False)
        self.assertEqual((state.failure_count, state.success_count), (1, 0))
        self.assertEqual(state.health_state, "degraded")
        self.clock.advance(2)
        state = self.engine.process(profile(), True)
        self.assertEqual(state.failure_count, 0)
        self.assertEqual(state.health_state, "healthy")

    def test_no_stop_before_threshold_and_exactly_one_at_ten(self) -> None:
        current = profile()
        for _ in range(9):
            self.engine.process(current, False)
            self.clock.advance(2)
        self.assertEqual([call for call in self.systemd.calls if call[0] == "stop"], [])
        state = self.engine.process(current, False)
        self.assertTrue(state.stopped_by_watchdog)
        for _ in range(4):
            self.clock.advance(2)
            self.engine.process(current, False)
        stops = [call for call in self.systemd.calls if call[0] == "stop"]
        self.assertEqual(len(stops), 1)
        self.assertIn("watchdog_upstream_down", self._events())
        self.assertIn("watchdog_profile_stopped", self._events())

    def test_monitor_mode_never_stops_or_starts(self) -> None:
        current = profile(mode="monitor", failures=1, successes=1, hold=0, jitter=0)
        self.engine.process(current, False)
        self.clock.advance(2)
        self.engine.process(current, True)
        self.assertEqual(self.systemd.calls, [])

    def test_switching_down_monitor_profile_to_auto_stops_once(self) -> None:
        self.engine.process(profile(mode="monitor", failures=1), False)
        self.clock.advance(2)
        state = self.engine.process(profile(mode="auto", failures=1), False)
        self.assertTrue(state.stopped_by_watchdog)
        self.assertEqual(len([call for call in self.systemd.calls if call[0] == "stop"]), 1)

    def test_disabled_mode_performs_no_service_query(self) -> None:
        self.engine.process(profile(mode="disabled"), False)
        self.assertEqual(self.systemd.calls, [])

    def test_full_success_threshold_and_hold_are_required(self) -> None:
        current = profile(failures=1, successes=10, hold=10, jitter=0)
        self.engine.process(current, False)
        self.assertFalse(self.systemd.active)
        for _ in range(9):
            self.clock.advance(2)
            state = self.engine.process(current, True)
        self.assertEqual(state.success_count, 9)
        self.assertFalse(self.systemd.active)
        self.clock.advance(2)
        state = self.engine.process(current, True)
        self.assertEqual(state.health_state, "recovering")
        self.clock.advance(9)
        state = self.engine.process(current, True)
        self.assertEqual(state.health_state, "recovering")
        self.clock.advance(1)
        state = self.engine.process(current, True)
        self.assertEqual(state.health_state, "healthy")
        self.assertTrue(self.systemd.active)
        self.assertEqual(len([call for call in self.systemd.calls if call[0] == "start"]), 1)

    def test_failure_during_hold_resets_recovery(self) -> None:
        current = profile(failures=1, successes=1, hold=10, jitter=0)
        self.engine.process(current, False)
        self.clock.advance(2)
        state = self.engine.process(current, True)
        self.assertIsNotNone(state.recovery_ready_at)
        self.clock.advance(5)
        state = self.engine.process(current, False)
        self.assertEqual(state.health_state, "down")
        self.assertEqual(state.success_count, 0)
        self.assertIsNone(state.recovery_ready_at)

    def test_jitter_is_bounded_and_applied(self) -> None:
        engine = WatchdogEngine(
            self.store,
            self.systemd,
            clock=self.clock.model(),
            jitter_source=lambda maximum: maximum,
        )
        current = profile(failures=1, successes=1, hold=0, jitter=10)
        engine.process(current, False)
        self.clock.advance(2)
        state = engine.process(current, True)
        self.assertEqual(state.recovery_jitter_seconds, 10)
        self.assertEqual(state.recovery_ready_at, self.clock.value + 10)

    def test_already_inactive_service_is_never_claimed_or_started(self) -> None:
        self.systemd.active = False
        current = profile(failures=1, successes=1, hold=0, jitter=0)
        state = self.engine.process(current, False)
        self.assertFalse(state.stopped_by_watchdog)
        self.clock.advance(2)
        state = self.engine.process(current, True)
        self.assertTrue(state.manual_override)
        self.assertFalse(any(call[0] == "start" for call in self.systemd.calls))

    def test_manual_start_clears_ownership_and_suspends_actions(self) -> None:
        current = profile(failures=1)
        state = self.store.get_state(current.profile_id, current.service_name, current.kharej_ip)
        state.health_state = "down"
        state.stopped_by_watchdog = True
        state.last_service_active = False
        self.store.persist(state, [], self.clock.value)
        self.systemd.active = True
        state = self.engine.process(current, False)
        self.assertTrue(state.manual_override)
        self.assertFalse(state.stopped_by_watchdog)
        self.assertIn("watchdog_manual_override", self._events())

    def test_stop_failure_is_not_retried_every_cycle(self) -> None:
        self.systemd.stop_success = False
        current = profile(failures=1)
        state = self.engine.process(current, False)
        self.assertTrue(state.manual_override)
        for _ in range(4):
            self.clock.advance(2)
            self.engine.process(current, False)
        self.assertEqual(len([call for call in self.systemd.calls if call[0] == "stop"]), 1)

    def test_no_per_ping_event_spam(self) -> None:
        current = profile()
        for _ in range(25):
            self.engine.process(current, True)
            self.clock.advance(2)
        self.assertEqual(self._events(), ["watchdog_upstream_healthy"])

    def test_state_survives_store_restart(self) -> None:
        state = self.engine.process(profile(), False)
        self.store.close()
        self.store = WatchdogStore(self.db)
        restored = self.store.get_state(state.profile_id, state.service_name, state.kharej_ip)
        self.assertEqual(restored.failure_count, 1)
        self.assertEqual(restored.health_state, "degraded")

    def test_maintenance_never_auto_starts(self) -> None:
        current = profile(failures=1, successes=1, hold=0, jitter=0)
        self.engine.process(current, False)
        maintenance = MaintenanceController(
            self.store, self.systemd, clock=self.clock.model()
        )
        maintenance.apply(current, "enter-keep")
        self.clock.advance(2)
        state = self.engine.process(current, True)
        self.assertTrue(state.maintenance)
        self.assertFalse(self.systemd.active)

    def test_maintenance_exit_never_starts_unowned_manual_stop(self) -> None:
        current = profile()
        state = self.store.get_state(current.profile_id, current.service_name, current.kharej_ip)
        state.health_state = "healthy"
        state.maintenance = True
        state.last_service_active = False
        self.store.persist(state, [], self.clock.value)
        self.systemd.active = False
        maintenance = MaintenanceController(
            self.store, self.systemd, clock=self.clock.model()
        )
        with self.assertRaises(ValueError):
            maintenance.apply(current, "exit-start")
        self.assertFalse(any(call[0] == "start" for call in self.systemd.calls))

    def test_maintenance_owned_start_clears_all_stop_ownership(self) -> None:
        current = profile()
        state = self.store.get_state(current.profile_id, current.service_name, current.kharej_ip)
        state.health_state = "healthy"
        state.maintenance = True
        state.stopped_by_watchdog = True
        state.stopped_by_maintenance = True
        state.last_service_active = False
        self.store.persist(state, [], self.clock.value)
        self.systemd.active = False
        maintenance = MaintenanceController(
            self.store, self.systemd, clock=self.clock.model()
        )
        state = maintenance.apply(current, "exit-start")
        self.assertTrue(self.systemd.active)
        self.assertFalse(state.stopped_by_watchdog)
        self.assertFalse(state.stopped_by_maintenance)
        self.clock.advance(2)
        state = self.engine.process(current, True)
        self.assertFalse(state.manual_override)

    def test_secret_canary_never_reaches_database(self) -> None:
        secret = "PASSWORD-CANARY-DO-NOT-STORE"
        parse_kharej_ip_text(f"GOST_PASS={secret}\nKHAREJ_IP=203.0.113.10\n")
        self.engine.process(profile(), False)
        self.store.close()
        self.assertNotIn(secret.encode(), Path(self.db).read_bytes())
        self.store = WatchdogStore(self.db)


class WatchdogStorageTests(unittest.TestCase):
    def test_event_retention_is_exactly_24_hours_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = WatchdogStore(str(Path(temporary) / "watchdog.sqlite3"))
            now = 200_000
            store.record_event(WatchdogEvent(now - EVENT_RETENTION_SECONDS - 1, "watchdog_daemon_started"))
            store.record_event(WatchdogEvent(now - EVENT_RETENTION_SECONDS, "watchdog_daemon_started"))
            self.assertEqual(store.prune_events(now), 1)
            self.assertEqual(store.prune_events(now), 0)
            rows = store.events(now, limit=10)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["ts"], now - EVENT_RETENTION_SECONDS)
            store.close()

    def test_storage_uses_wal_and_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "watchdog.sqlite3")
            store = WatchdogStore(path)
            mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
            indexes = {
                row[0]
                for row in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            self.assertEqual(str(mode).lower(), "wal")
            self.assertIn("idx_watchdog_events_ts", indexes)
            self.assertIn("idx_watchdog_events_profile_ts", indexes)
            store.close()

    def test_state_and_event_persistence_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "watchdog.sqlite3")
            store = WatchdogStore(path)
            state = store.get_state("iran-1", "gost-iran-1.service", "203.0.113.1")
            state.failure_count = 1
            store.persist(state, [], 100)
            state.failure_count = 2
            with self.assertRaises(ValueError):
                store.persist(state, [WatchdogEvent(101, "unsupported_event")], 101)
            restored = store.get_state(
                "iran-1", "gost-iran-1.service", "203.0.113.1"
            )
            self.assertEqual(restored.failure_count, 1)
            store.close()

    def test_symlinked_database_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.sqlite3"
            sqlite3.connect(target).close()
            link = root / "watchdog.sqlite3"
            link.symlink_to(target)
            with self.assertRaises(RuntimeError):
                WatchdogStore(str(link))

    def test_monotonic_deadline_skips_drift_without_overlap(self) -> None:
        self.assertEqual(advance_deadline(10.0, 2.0, 11.0), 12.0)
        self.assertEqual(advance_deadline(10.0, 2.0, 17.5), 18.0)


class WatchdogDaemonTests(unittest.TestCase):
    def test_persistent_runtime_error_is_transition_deduplicated(self) -> None:
        class Loader:
            def load(self) -> tuple[list[ManagedProfile], list[tuple[str | None, str]], int]:
                return [profile(mode="monitor")], [], 2

        class FailingEngine:
            def process(self, _profile: ManagedProfile, _success: bool) -> None:
                raise RuntimeError("fixed failure")

        with tempfile.TemporaryDirectory() as temporary:
            store = WatchdogStore(str(Path(temporary) / "watchdog.sqlite3"))
            clock = FakeClock()
            daemon = WatchdogDaemon(
                store,
                Loader(),  # type: ignore[arg-type]
                engine=FailingEngine(),  # type: ignore[arg-type]
                ping_executor=lambda _ip, _timeout: True,
                clock=clock.model(),
            )
            for _ in range(6):
                daemon.run_cycle()
                clock.advance(2)
            errors = [
                row
                for row in store.events(clock.value, limit=100)
                if row["code"] == "watchdog_config_error"
            ]
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["error_category"], "runtime_error")
            store.close()


if __name__ == "__main__":
    unittest.main()
