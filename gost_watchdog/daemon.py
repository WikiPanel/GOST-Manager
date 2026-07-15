"""One central monotonic Watchdog daemon for all Iran profiles."""

from __future__ import annotations

import signal
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from gost_watchdog.commands import SubprocessPingExecutor, SystemdController, run_ping_checks
from gost_watchdog.config import (
    DEFAULT_GLOBAL_CONFIG_PATH,
    DEFAULT_PROFILE_CONFIG_DIR,
    ConfigError,
    load_global_config,
    rooted_path,
)
from gost_watchdog.engine import WatchdogEngine
from gost_watchdog.models import Clock, EVENT_RETENTION_SECONDS, ManagedProfile, WatchdogEvent
from gost_watchdog.profiles import discover_profiles
from gost_watchdog.storage import DEFAULT_DB_PATH, WatchdogStore


DEFAULT_ENV_DIR = "/etc/gost"
DEFAULT_UNIT_DIR = "/etc/systemd/system"


def advance_deadline(previous: float, interval: float, current: float) -> float:
    deadline = previous + interval
    if deadline > current:
        return deadline
    missed = int((current - deadline) // interval) + 1
    return deadline + missed * interval


class RuntimeLoader:
    def __init__(
        self,
        *,
        global_config_path: str = DEFAULT_GLOBAL_CONFIG_PATH,
        profile_config_dir: str = DEFAULT_PROFILE_CONFIG_DIR,
        env_dir: str = DEFAULT_ENV_DIR,
        unit_dir: str = DEFAULT_UNIT_DIR,
        path_root: str | None = None,
        installed: bool = True,
    ) -> None:
        self.path_root = path_root
        self.boundary = Path(path_root) if path_root else None
        self.global_config_path = rooted_path(global_config_path, path_root)
        self.profile_config_dir = rooted_path(profile_config_dir, path_root)
        self.env_dir = rooted_path(env_dir, path_root)
        self.unit_dir = rooted_path(unit_dir, path_root)
        self.expected_uid = 0 if installed else None

    def load(self) -> tuple[list[ManagedProfile], list[tuple[str | None, str]], int]:
        config = load_global_config(
            self.global_config_path,
            expected_uid=self.expected_uid,
            boundary=self.boundary,
        )
        profiles, errors = discover_profiles(
            self.env_dir,
            self.unit_dir,
            self.profile_config_dir,
            config,
            expected_uid=self.expected_uid,
            boundary=self.boundary,
        )
        return profiles, errors, config.check_interval_seconds


class WatchdogDaemon:
    def __init__(
        self,
        store: WatchdogStore,
        loader: RuntimeLoader,
        *,
        engine: WatchdogEngine | None = None,
        ping_executor: Callable[[str, int], bool] | None = None,
        clock: Clock = Clock(),
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.loader = loader
        self.clock = clock
        self.sleeper = sleeper
        self.systemd = SystemdController()
        self.engine = engine or WatchdogEngine(store, self.systemd, clock=clock)
        self.ping_executor = ping_executor or SubprocessPingExecutor()
        self.next_due: dict[str, float] = {}
        self.reported_errors: set[tuple[str | None, str]] = set()
        self.next_prune = 0.0

    def _record_errors(self, errors: Sequence[tuple[str | None, str]], now: int) -> None:
        current = set(errors)
        for profile_id, category in current - self.reported_errors:
            self.store.record_event(
                WatchdogEvent(
                    ts=now,
                    code="watchdog_config_error",
                    profile_id=profile_id,
                    error_category=category,
                )
            )
        self.reported_errors = current

    def run_cycle(self) -> float:
        monotonic_now = self.clock.monotonic()
        wall_now = int(self.clock.wall())
        try:
            profiles, errors, fallback_interval = self.loader.load()
        except ConfigError:
            self._record_errors([(None, "invalid_global_config")], wall_now)
            return monotonic_now + 2.0
        cycle_errors = list(errors)
        active = [profile for profile in profiles if profile.config.mode != "disabled"]
        active_ids = {profile.profile_id for profile in active}
        self.next_due = {
            profile_id: deadline
            for profile_id, deadline in self.next_due.items()
            if profile_id in active_ids
        }
        due = [
            profile
            for profile in active
            if monotonic_now >= self.next_due.get(profile.profile_id, monotonic_now)
        ]
        results = run_ping_checks(due, self.ping_executor)
        for profile in due:
            try:
                self.engine.process(profile, results[profile.profile_id])
            except Exception:
                cycle_errors.append((profile.profile_id, "runtime_error"))
            previous = self.next_due.get(profile.profile_id, monotonic_now)
            self.next_due[profile.profile_id] = advance_deadline(
                previous,
                float(profile.config.check_interval_seconds),
                self.clock.monotonic(),
            )
        self._record_errors(cycle_errors, wall_now)
        if monotonic_now >= self.next_prune:
            self.store.prune_events(wall_now, retention_seconds=EVENT_RETENTION_SECONDS)
            self.next_prune = monotonic_now + 60.0
        if self.next_due:
            return min(self.next_due.values())
        return monotonic_now + float(fallback_interval)

    def run(self, stop_requested: Callable[[], bool] | None = None) -> int:
        stop = False

        def request_stop(_signum: int, _frame: object) -> None:
            nonlocal stop
            stop = True

        now = int(self.clock.wall())
        self.store.record_event(WatchdogEvent(now, "watchdog_daemon_started"))
        old_term = signal.signal(signal.SIGTERM, request_stop)
        old_int = signal.signal(signal.SIGINT, request_stop)
        try:
            deadline = self.clock.monotonic()
            while not stop and not (stop_requested and stop_requested()):
                current = self.clock.monotonic()
                if current < deadline:
                    self.sleeper(deadline - current)
                    continue
                deadline = self.run_cycle()
            return 0
        finally:
            self.store.record_event(
                WatchdogEvent(int(self.clock.wall()), "watchdog_daemon_stopped")
            )
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)


def main() -> int:
    store = WatchdogStore(DEFAULT_DB_PATH)
    try:
        return WatchdogDaemon(store, RuntimeLoader()).run()
    except Exception as exc:
        print(f"Watchdog failed safely: {exc.__class__.__name__}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
