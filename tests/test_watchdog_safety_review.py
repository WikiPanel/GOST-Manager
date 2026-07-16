from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gost_watchdog.admin_cli import (
    AdminInputError,
    AdminRuntimeError,
    Context,
    command_reset_profile,
    command_configure_profile,
    command_set_mode,
    command_status,
)
from gost_watchdog.commands import (
    CommandError,
    SubprocessPingExecutor,
    run_ping_checks,
)
from gost_watchdog.config import (
    atomic_write_config,
    default_global_config_text,
    parse_profile_config,
    render_profile_values,
    render_global_config,
)
from gost_watchdog.engine import DurableServiceActions, MaintenanceController, WatchdogEngine
from gost_watchdog.models import (
    EVENT_RETENTION_SECONDS,
    MAX_PING_WORKERS,
    Clock,
    GlobalConfig,
    ManagedProfile,
    ProbeResult,
    ProfileConfig,
    WatchdogEvent,
)
from gost_watchdog.profiles import validate_service_name
from gost_watchdog.storage import WatchdogStore


class FakeClock:
    def __init__(self, value: int = 100_000) -> None:
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


class FailingPersistStore:
    def __init__(self, store: WatchdogStore, fail_on: int) -> None:
        self.store = store
        self.fail_on = fail_on
        self.persist_calls = 0

    def persist(self, state: object, events: object, now: int) -> None:
        self.persist_calls += 1
        if self.persist_calls == self.fail_on:
            raise sqlite3.OperationalError("injected persistence failure")
        self.store.persist(state, events, now)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        return getattr(self.store, name)


def managed_profile(
    profile_id: str = "iran-1",
    *,
    ip: str = "203.0.113.10",
    mode: str = "auto",
    interval: int = 2,
    timeout: int = 1,
    failures: int = 1,
    successes: int = 1,
    hold: int = 0,
    jitter: int = 0,
) -> ManagedProfile:
    number = profile_id.split("-")[1]
    return ManagedProfile(
        profile_id=profile_id,
        service_name=f"gost-iran-{number}.service",
        kharej_ip=ip,
        env_path=f"/etc/gost/{profile_id}.env",
        unit_path=f"/etc/systemd/system/gost-{profile_id}.service",
        config_path=f"/etc/gost-manager/watchdog.d/{profile_id}.conf",
        config=ProfileConfig(
            mode=mode,
            check_interval_seconds=interval,
            ping_timeout_seconds=timeout,
            failure_threshold=failures,
            success_threshold=successes,
            recovery_hold_seconds=hold,
            recovery_jitter_max_seconds=jitter,
        ),
    )


class ProbeResultSafetyTests(unittest.TestCase):
    def test_ping_executor_classifies_return_codes_and_local_failures(self) -> None:
        class Result:
            def __init__(self, returncode: int) -> None:
                self.returncode = returncode

        for returncode, expected in ((0, "success"), (1, "unreachable"), (2, "probe_error")):
            with self.subTest(returncode=returncode):
                result = SubprocessPingExecutor(
                    runner=lambda *_args, **_kwargs: Result(returncode)  # type: ignore[arg-type]
                )("203.0.113.10", 1)
                self.assertEqual(result.status, expected)

        failures = (
            (FileNotFoundError(), "ping_binary_missing"),
            (PermissionError(), "ping_permission_denied"),
            (subprocess.TimeoutExpired(["ping"], 2), "ping_execution_timeout"),
            (OSError(), "ping_execution_failed"),
        )
        for error, category in failures:
            with self.subTest(category=category):
                def raise_error(*_args: object, **_kwargs: object) -> object:
                    raise error

                result = SubprocessPingExecutor(runner=raise_error)("203.0.113.10", 1)
                self.assertEqual(
                    (result.status, result.error_category),
                    ("probe_error", category),
                )

    def test_probe_errors_never_change_counters_or_services_and_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = WatchdogStore(str(Path(temporary) / "watchdog.sqlite3"))
            clock = FakeClock()
            systemd = FakeSystemd(True)
            engine = WatchdogEngine(store, systemd, clock=clock.model())
            for category in (
                "ping_binary_missing",
                "ping_binary_missing",
                "ping_permission_denied",
                "ping_execution_timeout",
            ):
                state = engine.process(
                    managed_profile(), ProbeResult("probe_error", category)
                )
                clock.advance(2)
            self.assertEqual((state.failure_count, state.success_count), (0, 0))
            self.assertEqual(state.check_status, "probe_error")
            self.assertFalse(any(call[0] in {"stop", "start"} for call in systemd.calls))
            errors = [
                row
                for row in store.events(clock.value, limit=100)
                if row["code"] == "watchdog_probe_error"
            ]
            self.assertEqual(len(errors), 3)
            store.close()

    def test_unreachable_counts_and_probe_recovery_resumes_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = WatchdogStore(str(Path(temporary) / "watchdog.sqlite3"))
            clock = FakeClock()
            systemd = FakeSystemd(True)
            engine = WatchdogEngine(store, systemd, clock=clock.model())
            current = managed_profile(failures=3)
            state = engine.process(current, ProbeResult("unreachable"))
            self.assertEqual(state.failure_count, 1)
            clock.advance(2)
            state = engine.process(
                current, ProbeResult("probe_error", "ping_binary_missing")
            )
            self.assertEqual(state.failure_count, 1)
            clock.advance(2)
            state = engine.process(current, ProbeResult("success"))
            self.assertEqual((state.failure_count, state.check_status), (0, "success"))
            recovered = [
                row
                for row in store.events(clock.value, limit=100)
                if row["code"] == "watchdog_probe_recovered"
            ]
            self.assertEqual(len(recovered), 1)
            store.close()


class SharedProbeTests(unittest.TestCase):
    def test_shared_ip_and_timeout_use_one_probe(self) -> None:
        calls: list[tuple[str, int]] = []
        profiles = [managed_profile("iran-1"), managed_profile("iran-2")]
        results = run_ping_checks(
            profiles,
            lambda ip, timeout: calls.append((ip, timeout)) or ProbeResult("success"),
        )
        self.assertEqual(calls, [("203.0.113.10", 1)])
        self.assertEqual({key: value.status for key, value in results.items()}, {
            "iran-1": "success",
            "iran-2": "success",
        })

    def test_shared_ip_with_different_timeouts_uses_independent_results(self) -> None:
        calls: list[tuple[str, int]] = []

        def probe(ip: str, timeout: int) -> ProbeResult:
            calls.append((ip, timeout))
            return ProbeResult("success" if timeout == 1 else "unreachable")

        profiles = [
            managed_profile("iran-1", timeout=1, interval=5),
            managed_profile("iran-2", timeout=4, interval=5),
        ]
        results = run_ping_checks(profiles, probe)
        self.assertEqual(sorted(calls), [("203.0.113.10", 1), ("203.0.113.10", 4)])
        self.assertEqual(results["iran-1"].status, "success")
        self.assertEqual(results["iran-2"].status, "unreachable")

    def test_worker_concurrency_never_exceeds_bound(self) -> None:
        lock = threading.Lock()
        active = 0
        maximum = 0

        def probe(_ip: str, _timeout: int) -> ProbeResult:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return ProbeResult("success")

        profiles = [
            managed_profile(f"iran-{index}", ip=f"203.0.113.{index}")
            for index in range(1, 41)
        ]
        run_ping_checks(profiles, probe, max_workers=1000)
        self.assertLessEqual(maximum, MAX_PING_WORKERS)
        self.assertGreater(maximum, 1)


class ConfigRecoveryTests(unittest.TestCase):
    def test_profile_render_uses_active_global_and_keeps_partial_overrides(self) -> None:
        global_config = GlobalConfig(
            check_interval_seconds=5,
            ping_timeout_seconds=1,
            failure_threshold=17,
            success_threshold=19,
        )
        values = {"MODE": "monitor", "PING_TIMEOUT_SECONDS": "4"}
        rendered = render_profile_values(values, global_config)
        self.assertEqual(rendered, "MODE=monitor\nPING_TIMEOUT_SECONDS=4\n")
        parsed = parse_profile_config(rendered, global_config)
        self.assertEqual(parsed.check_interval_seconds, 5)
        self.assertEqual(parsed.ping_timeout_seconds, 4)
        self.assertEqual(parsed.failure_threshold, 17)
        self.assertEqual(parsed.success_threshold, 19)

    def _fixture(self, root: Path) -> Context:
        config_root = root / "etc/gost-manager"
        profile_root = config_root / "watchdog.d"
        env_root = root / "etc/gost"
        unit_root = root / "etc/systemd/system"
        state_root = root / "var/lib/gost-manager/watchdog"
        for directory in (profile_root, env_root, unit_root, state_root):
            directory.mkdir(parents=True, exist_ok=True)
        global_path = config_root / "watchdog.conf"
        atomic_write_config(global_path, default_global_config_text(), boundary=root)
        env_path = env_root / "iran-1.env"
        env_path.write_text("KHAREJ_IP=203.0.113.10\n", encoding="ascii")
        env_path.chmod(0o600)
        unit_path = unit_root / "gost-iran-1.service"
        unit_path.write_text("[Service]\n", encoding="ascii")
        unit_path.chmod(0o644)
        return Context(
            global_config_path=global_path,
            profile_config_dir=profile_root,
            env_dir=env_root,
            unit_dir=unit_root,
            db_path=state_root / "watchdog.sqlite3",
            boundary=root,
            expected_uid=None,
            owner_uid=None,
        )

    def test_reset_recovers_malformed_unknown_unsafe_and_incompatible_configs(self) -> None:
        broken_values = (
            "not-a-config\n",
            "MODE=auto\nUNKNOWN=1\n",
            "MODE=auto\nPING_TIMEOUT_SECONDS=3\n",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self._fixture(root)
            target = context.profile_config_dir / "iran-1.conf"
            for value in broken_values:
                with self.subTest(value=value):
                    target.write_text(value, encoding="ascii")
                    target.chmod(0o666 if value == broken_values[0] else 0o600)
                    command_reset_profile(context, "iran-1")
                    self.assertEqual(target.read_text(encoding="ascii"), "MODE=disabled\n")
                    self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_reset_replaces_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self._fixture(root)
            external = root / "external.conf"
            external.write_text("SECRET=do-not-touch\n", encoding="ascii")
            target = context.profile_config_dir / "iran-1.conf"
            target.symlink_to(external)
            command_reset_profile(context, "iran-1")
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.read_text(encoding="ascii"), "MODE=disabled\n")
            self.assertEqual(external.read_text(encoding="ascii"), "SECRET=do-not-touch\n")

    def test_reset_recovers_override_invalidated_by_global_timing_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self._fixture(root)
            atomic_write_config(
                context.global_config_path,
                render_global_config(
                    GlobalConfig(check_interval_seconds=5, ping_timeout_seconds=1)
                ),
                boundary=root,
            )
            target = context.profile_config_dir / "iran-1.conf"
            target.write_text(
                "MODE=monitor\nPING_TIMEOUT_SECONDS=4\n", encoding="ascii"
            )
            target.chmod(0o600)
            atomic_write_config(
                context.global_config_path,
                default_global_config_text(),
                boundary=root,
            )
            command_reset_profile(context, "iran-1")
            self.assertEqual(target.read_text(encoding="ascii"), "MODE=disabled\n")

    def test_reset_rejects_arbitrary_kharej_and_unknown_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = self._fixture(Path(temporary))
            for profile_id in ("ssh", "kharej-1", "iran-2"):
                with self.subTest(profile_id=profile_id), self.assertRaises(AdminInputError):
                    command_reset_profile(context, profile_id)


class OutageWindowTests(unittest.TestCase):
    def _store(self, temporary: str) -> WatchdogStore:
        return WatchdogStore(str(Path(temporary) / "watchdog.sqlite3"))

    def test_completed_outage_fully_inside_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            now = 200_000
            store.record_event(WatchdogEvent(now - 100, "watchdog_upstream_healthy", profile_id="iran-1", outage_duration=200))
            summary = store.summary(now, "iran-1")
            self.assertEqual((summary["outage_count"], summary["total_downtime_seconds"], summary["longest_outage_seconds"]), (1, 200, 200))
            store.close()

    def test_completed_outage_is_clipped_at_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            now = 200_000
            cutoff = now - EVENT_RETENTION_SECONDS
            store.record_event(WatchdogEvent(cutoff + 100, "watchdog_upstream_healthy", profile_id="iran-1", outage_duration=200))
            summary = store.summary(now, "iran-1")
            self.assertEqual((summary["outage_count"], summary["total_downtime_seconds"]), (1, 100))
            store.close()

    def test_ongoing_outage_is_clipped_to_24_hours(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            now = 200_000
            state = store.get_state("iran-1", "gost-iran-1.service", "203.0.113.10")
            state.outage_started_at = now - EVENT_RETENTION_SECONDS - 500
            store.persist(state, [], now)
            summary = store.summary(now, "iran-1")
            self.assertEqual((summary["outage_count"], summary["total_downtime_seconds"], summary["longest_outage_seconds"]), (1, EVENT_RETENTION_SECONDS, EVENT_RETENTION_SECONDS))
            store.close()

    def test_outage_ending_at_or_before_cutoff_has_no_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            now = 200_000
            cutoff = now - EVENT_RETENTION_SECONDS
            for end in (cutoff - 1, cutoff):
                store.record_event(WatchdogEvent(end, "watchdog_upstream_healthy", profile_id="iran-1", outage_duration=100))
            summary = store.summary(now, "iran-1")
            self.assertEqual((summary["outage_count"], summary["total_downtime_seconds"]), (0, 0))
            store.close()

    def test_multiple_outages_count_total_longest_and_boundary_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            now = 200_000
            cutoff = now - EVENT_RETENTION_SECONDS
            store.record_event(WatchdogEvent(cutoff + 50, "watchdog_upstream_healthy", profile_id="iran-1", outage_duration=50))
            store.record_event(WatchdogEvent(now - 10, "watchdog_upstream_healthy", profile_id="iran-1", outage_duration=30))
            summary = store.summary(now, "iran-1")
            self.assertEqual((summary["outage_count"], summary["total_downtime_seconds"], summary["longest_outage_seconds"]), (2, 80, 50))
            self.assertEqual(summary["last_outage_at"], now - 40)
            store.close()


class WatchdogSchemaMigrationTests(unittest.TestCase):
    def test_v1_state_is_migrated_with_safe_action_and_probe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "watchdog.sqlite3")
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
                INSERT INTO schema_migrations VALUES(1, 1);
                CREATE TABLE profile_state(
                    profile_id TEXT PRIMARY KEY, service_name TEXT NOT NULL,
                    kharej_ip TEXT NOT NULL, health_state TEXT NOT NULL,
                    maintenance INTEGER NOT NULL DEFAULT 0,
                    stopped_by_watchdog INTEGER NOT NULL DEFAULT 0,
                    stopped_by_maintenance INTEGER NOT NULL DEFAULT 0,
                    manual_override INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    last_check_at INTEGER, last_transition_at INTEGER,
                    outage_started_at INTEGER, recovery_started_at INTEGER,
                    recovery_ready_at INTEGER,
                    recovery_jitter_seconds INTEGER NOT NULL DEFAULT 0,
                    last_service_active INTEGER, updated_at INTEGER NOT NULL
                );
                INSERT INTO profile_state(
                    profile_id,service_name,kharej_ip,health_state,updated_at
                ) VALUES('iran-1','gost-iran-1.service','203.0.113.10','healthy',1);
                """
            )
            connection.commit()
            connection.close()
            store = WatchdogStore(path)
            state = store.all_states()["iran-1"]
            self.assertEqual(state.check_status, "unknown")
            self.assertIsNone(state.pending_action)
            version = store.conn.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            self.assertEqual(version, 2)
            store.close()


class DurableOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = WatchdogStore(str(Path(self.temporary.name) / "watchdog.sqlite3"))
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_stop_pending_before_action_is_cleared_without_service_action(self) -> None:
        current = managed_profile()
        state = self.store.get_state(current.profile_id, current.service_name, current.kharej_ip)
        state.pending_action = "stop_watchdog"
        state.pending_action_at = self.clock.value
        self.store.persist(state, [], self.clock.value)
        systemd = FakeSystemd(True)
        WatchdogEngine(self.store, systemd, clock=self.clock.model()).process(
            current, ProbeResult("success")
        )
        restored = self.store.get_state(current.profile_id, current.service_name, current.kharej_ip)
        self.assertIsNone(restored.pending_action)
        self.assertFalse(restored.stopped_by_watchdog)
        self.assertFalse(any(call[0] in {"stop", "start"} for call in systemd.calls))

    def test_successful_stop_with_failed_final_persist_is_compensated(self) -> None:
        systemd = FakeSystemd(True)
        failing = FailingPersistStore(self.store, fail_on=2)
        engine = WatchdogEngine(failing, systemd, clock=self.clock.model())  # type: ignore[arg-type]
        state = engine.process(managed_profile(), ProbeResult("unreachable"))
        self.assertTrue(systemd.active)
        self.assertTrue(state.manual_override)
        self.assertFalse(state.stopped_by_watchdog)
        self.assertIsNone(state.pending_action)
        self.assertEqual(len([call for call in systemd.calls if call[0] == "start"]), 1)

    def test_failed_compensation_leaves_durable_intent_for_restart_claim(self) -> None:
        systemd = FakeSystemd(True)
        systemd.start_success = False
        failing = FailingPersistStore(self.store, fail_on=2)
        engine = WatchdogEngine(failing, systemd, clock=self.clock.model())  # type: ignore[arg-type]
        with self.assertRaises(CommandError):
            engine.process(managed_profile(), ProbeResult("unreachable"))
        pending = self.store.get_state("iran-1", "gost-iran-1.service", "203.0.113.10")
        self.assertEqual(pending.pending_action, "stop_watchdog")
        self.assertFalse(systemd.active)

        recovered = WatchdogEngine(self.store, systemd, clock=self.clock.model()).process(
            managed_profile(), ProbeResult("unreachable")
        )
        self.assertTrue(recovered.stopped_by_watchdog)
        self.assertIsNone(recovered.pending_action)
        self.assertEqual(len([call for call in systemd.calls if call[0] == "stop"]), 1)

    def test_crash_after_stop_is_claimed_without_repeating_action(self) -> None:
        current = managed_profile()
        state = self.store.get_state(current.profile_id, current.service_name, current.kharej_ip)
        state.health_state = "down"
        state.pending_action = "stop_watchdog"
        state.pending_action_at = self.clock.value
        self.store.persist(state, [], self.clock.value)
        systemd = FakeSystemd(False)
        restored = WatchdogEngine(self.store, systemd, clock=self.clock.model()).process(
            current, ProbeResult("unreachable")
        )
        self.assertTrue(restored.stopped_by_watchdog)
        self.assertFalse(any(call[0] in {"stop", "start"} for call in systemd.calls))

    def test_pending_stop_reconciles_even_while_probe_is_unavailable(self) -> None:
        current = managed_profile()
        state = self.store.get_state(current.profile_id, current.service_name, current.kharej_ip)
        state.health_state = "down"
        state.pending_action = "stop_watchdog"
        state.pending_action_at = self.clock.value
        self.store.persist(state, [], self.clock.value)
        systemd = FakeSystemd(False)
        restored = WatchdogEngine(self.store, systemd, clock=self.clock.model()).process(
            current, ProbeResult("probe_error", "ping_binary_missing")
        )
        self.assertTrue(restored.stopped_by_watchdog)
        self.assertEqual(restored.check_status, "probe_error")
        self.assertFalse(any(call[0] in {"stop", "start"} for call in systemd.calls))

    def test_maintenance_stop_uses_durable_owner(self) -> None:
        current = managed_profile()
        state = self.store.get_state(current.profile_id, current.service_name, current.kharej_ip)
        systemd = FakeSystemd(True)
        events: list[WatchdogEvent] = []
        actions = DurableServiceActions(self.store, systemd)
        self.assertTrue(actions.stop(current, state, events, self.clock.value, owner="maintenance"))
        restored = self.store.get_state(current.profile_id, current.service_name, current.kharej_ip)
        self.assertTrue(restored.stopped_by_maintenance)
        self.assertFalse(restored.stopped_by_watchdog)
        self.assertIsNone(restored.pending_action)

    def test_maintenance_stop_persist_failure_is_compensated(self) -> None:
        current = managed_profile()
        state = self.store.get_state(current.profile_id, current.service_name, current.kharej_ip)
        systemd = FakeSystemd(True)
        failing = FailingPersistStore(self.store, fail_on=2)
        actions = DurableServiceActions(failing, systemd)  # type: ignore[arg-type]
        self.assertFalse(
            actions.stop(
                current,
                state,
                [],
                self.clock.value,
                owner="maintenance",
            )
        )
        restored = self.store.get_state(current.profile_id, current.service_name, current.kharej_ip)
        self.assertTrue(systemd.active)
        self.assertFalse(restored.stopped_by_maintenance)
        self.assertIsNone(restored.pending_action)

    def test_recovery_start_final_persist_retry_preserves_events(self) -> None:
        current = managed_profile()
        state = self.store.get_state(current.profile_id, current.service_name, current.kharej_ip)
        state.health_state = "healthy"
        state.check_status = "success"
        state.last_check_at = int(time.time())
        state.stopped_by_watchdog = True
        state.last_service_active = False
        self.store.persist(state, [], self.clock.value)
        systemd = FakeSystemd(False)
        failing = FailingPersistStore(self.store, fail_on=2)
        events: list[WatchdogEvent] = []
        actions = DurableServiceActions(failing, systemd)  # type: ignore[arg-type]
        self.assertTrue(
            actions.start(
                current,
                state,
                events,
                self.clock.value,
                owner="watchdog",
            )
        )
        restored = self.store.get_state(current.profile_id, current.service_name, current.kharej_ip)
        self.assertFalse(restored.stopped_by_watchdog)
        self.assertIsNone(restored.pending_action)
        codes = [row["code"] for row in self.store.events(self.clock.value, limit=100)]
        self.assertIn("watchdog_profile_started", codes)
        self.assertIn("watchdog_action_error", codes)

    def test_rearm_records_exactly_one_safe_event(self) -> None:
        current = managed_profile()
        state = self.store.get_state(current.profile_id, current.service_name, current.kharej_ip)
        state.manual_override = True
        state.last_service_active = True
        self.store.persist(state, [], self.clock.value)
        controller = MaintenanceController(
            self.store, FakeSystemd(True), clock=self.clock.model()
        )
        controller.rearm(current)
        events = [
            row
            for row in self.store.events(self.clock.value, limit=100)
            if row["code"] == "watchdog_manual_override"
            and row["action_result"] == "rearmed"
        ]
        self.assertEqual(len(events), 1)
        with self.assertRaises(ValueError):
            controller.rearm(current)
        events_after = [
            row
            for row in self.store.events(self.clock.value, limit=100)
            if row["code"] == "watchdog_manual_override"
            and row["action_result"] == "rearmed"
        ]
        self.assertEqual(len(events_after), 1)

    def test_systemd_reconciliation_calls_are_bounded_for_ten_profiles(self) -> None:
        systemd = FakeSystemd(True)
        engine = WatchdogEngine(self.store, systemd, clock=self.clock.model())
        profiles = [managed_profile(f"iran-{index}", failures=10) for index in range(1, 11)]
        for cycle in range(6):
            for current in profiles:
                engine.process(current, ProbeResult("success"))
            if cycle < 5:
                self.clock.advance(2)
        queries = [call for call in systemd.calls if call[0] == "is-active"]
        self.assertEqual(len(queries), 20)
        self.assertFalse(any(call[0] in {"stop", "start"} for call in systemd.calls))


class ModeChangeSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        profile_root = root / "etc/gost-manager/watchdog.d"
        env_root = root / "etc/gost"
        unit_root = root / "etc/systemd/system"
        state_root = root / "var/lib/gost-manager/watchdog"
        for directory in (profile_root, env_root, unit_root, state_root):
            directory.mkdir(parents=True)
        global_path = root / "etc/gost-manager/watchdog.conf"
        atomic_write_config(global_path, default_global_config_text(), boundary=root)
        (env_root / "iran-1.env").write_text("KHAREJ_IP=203.0.113.10\n", encoding="ascii")
        (env_root / "iran-1.env").chmod(0o600)
        (unit_root / "gost-iran-1.service").write_text("[Service]\n", encoding="ascii")
        (profile_root / "iran-1.conf").write_text("MODE=auto\n", encoding="ascii")
        (profile_root / "iran-1.conf").chmod(0o600)
        self.context = Context(
            global_config_path=global_path,
            profile_config_dir=profile_root,
            env_dir=env_root,
            unit_dir=unit_root,
            db_path=state_root / "watchdog.sqlite3",
            boundary=root,
            expected_uid=None,
            owner_uid=None,
        )
        store = WatchdogStore(str(self.context.db_path))
        state = store.get_state("iran-1", "gost-iran-1.service", "203.0.113.10")
        state.health_state = "healthy"
        state.check_status = "success"
        state.last_check_at = int(time.time())
        state.stopped_by_watchdog = True
        state.last_service_active = False
        store.persist(state, [], 100)
        store.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_leaving_auto_requires_explicit_owned_action(self) -> None:
        with self.assertRaises(AdminInputError):
            command_set_mode(self.context, "iran-1", "monitor", None)
        self.assertEqual(
            (self.context.profile_config_dir / "iran-1.conf").read_text(encoding="ascii"),
            "MODE=auto\n",
        )

    def test_configure_profile_cannot_bypass_safe_mode_change(self) -> None:
        arguments = SimpleNamespace(
            profile_id="iran-1",
            mode="monitor",
            check_interval=None,
            ping_timeout=None,
            failure_threshold=None,
            success_threshold=None,
            recovery_hold=None,
            recovery_jitter=None,
        )
        with self.assertRaises(AdminInputError):
            command_configure_profile(self.context, arguments)
        self.assertEqual(
            (self.context.profile_config_dir / "iran-1.conf").read_text(
                encoding="ascii"
            ),
            "MODE=auto\n",
        )

    def test_keep_stopped_changes_mode_without_service_action(self) -> None:
        command_set_mode(self.context, "iran-1", "monitor", "keep-stopped")
        self.assertEqual(
            (self.context.profile_config_dir / "iran-1.conf").read_text(encoding="ascii"),
            "MODE=monitor\n",
        )
        store = WatchdogStore(str(self.context.db_path))
        self.assertTrue(store.all_states()["iran-1"].stopped_by_watchdog)
        store.close()

    def test_start_if_healthy_is_explicit_and_clears_ownership(self) -> None:
        systemd = FakeSystemd(False)
        with mock.patch("gost_watchdog.admin_cli.SystemdController", return_value=systemd):
            command_set_mode(
                self.context, "iran-1", "disabled", "start-if-healthy"
            )
        self.assertTrue(systemd.active)
        self.assertEqual(
            (self.context.profile_config_dir / "iran-1.conf").read_text(encoding="ascii"),
            "MODE=disabled\n",
        )
        store = WatchdogStore(str(self.context.db_path))
        state = store.all_states()["iran-1"]
        self.assertFalse(state.stopped_by_watchdog)
        self.assertIsNone(state.pending_action)
        codes = [row["code"] for row in store.events(int(time.time()), limit=100)]
        self.assertIn("watchdog_mode_change_start", codes)
        self.assertEqual(
            store.summary(int(time.time()), "iran-1")["automatic_start_count"],
            0,
        )
        store.close()

    def test_start_choice_rejects_stale_or_unavailable_health(self) -> None:
        store = WatchdogStore(str(self.context.db_path))
        state = store.all_states()["iran-1"]
        state.check_status = "probe_error"
        state.last_probe_error_category = "ping_binary_missing"
        store.persist(state, [], int(time.time()))
        store.close()
        with self.assertRaises(AdminRuntimeError):
            command_set_mode(
                self.context, "iran-1", "disabled", "start-if-healthy"
            )
        self.assertEqual(
            (self.context.profile_config_dir / "iran-1.conf").read_text(
                encoding="ascii"
            ),
            "MODE=auto\n",
        )

    def test_human_status_surfaces_probe_error(self) -> None:
        store = WatchdogStore(str(self.context.db_path))
        state = store.all_states()["iran-1"]
        state.check_status = "probe_error"
        state.last_probe_error_category = "ping_binary_missing"
        store.persist(state, [], 101)
        store.close()
        output = StringIO()
        with mock.patch(
            "gost_watchdog.admin_cli.SystemdController",
            return_value=FakeSystemd(False),
        ), redirect_stdout(output):
            command_status(self.context, "iran-1", False)
        self.assertIn("probe_error", output.getvalue())


if __name__ == "__main__":
    unittest.main()
