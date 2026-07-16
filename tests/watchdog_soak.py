"""Deterministic ten-profile Watchdog process-rate benchmark."""

from __future__ import annotations

import resource
import threading
import time
from pathlib import Path

from gost_watchdog.commands import SubprocessPingExecutor
from gost_watchdog.daemon import WatchdogDaemon
from gost_watchdog.engine import WatchdogEngine
from gost_watchdog.models import Clock, ManagedProfile, ProbeResult, ProfileConfig
from gost_watchdog.profiles import validate_service_name
from gost_watchdog.storage import WatchdogStore


class BenchmarkClock:
    def __init__(self) -> None:
        self.value = 10_000.0

    def advance(self, seconds: float) -> None:
        self.value += seconds

    def model(self) -> Clock:
        return Clock(lambda: self.value, lambda: self.value)


class CountingSystemd:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def is_active(self, service: str) -> bool:
        validate_service_name(service)
        self.calls.append(("is-active", service))
        return True

    def stop(self, service: str) -> bool:
        self.calls.append(("stop", service))
        return False

    def start(self, service: str) -> bool:
        self.calls.append(("start", service))
        return False


class StaticLoader:
    def __init__(self, profiles: list[ManagedProfile]) -> None:
        self.profiles = profiles

    def load(self) -> tuple[list[ManagedProfile], list[tuple[str | None, str]], int]:
        return self.profiles, [], 2


def _profile(number: int) -> ManagedProfile:
    profile_id = f"iran-{number}"
    return ManagedProfile(
        profile_id=profile_id,
        service_name=f"gost-iran-{number}.service",
        kharej_ip="127.0.0.1",
        env_path=f"/etc/gost/{profile_id}.env",
        unit_path=f"/etc/systemd/system/gost-{profile_id}.service",
        config_path=f"/etc/gost-manager/watchdog.d/{profile_id}.conf",
        config=ProfileConfig(mode="auto", failure_threshold=10),
    )


def run_benchmark(state_directory: Path) -> dict[str, float | int]:
    clock = BenchmarkClock()
    profiles = [_profile(number) for number in range(1, 11)]
    store = WatchdogStore(str(state_directory / "soak.sqlite3"))
    systemd = CountingSystemd()
    engine = WatchdogEngine(store, systemd, clock=clock.model())  # type: ignore[arg-type]
    ping = SubprocessPingExecutor()
    lock = threading.Lock()
    active = 0
    maximum_overlap = 0
    ping_calls = 0

    def counted_ping(destination: str, timeout: int) -> ProbeResult:
        nonlocal active, maximum_overlap, ping_calls
        with lock:
            active += 1
            ping_calls += 1
            maximum_overlap = max(maximum_overlap, active)
        try:
            return ping(destination, timeout)
        finally:
            with lock:
                active -= 1

    daemon = WatchdogDaemon(
        store,
        StaticLoader(profiles),  # type: ignore[arg-type]
        engine=engine,
        ping_executor=counted_ping,
        clock=clock.model(),
    )
    wall_start = time.monotonic()
    cpu_start = time.process_time()
    deadlines: list[float] = []
    try:
        for cycle in range(6):
            deadlines.append(daemon.run_cycle())
            if cycle < 5:
                clock.advance(2.0)
    finally:
        store.close()
    elapsed = max(0.000001, time.monotonic() - wall_start)
    cpu_seconds = max(0.0, time.process_time() - cpu_start)
    systemd_queries = sum(call[0] == "is-active" for call in systemd.calls)
    service_actions = sum(call[0] in {"stop", "start"} for call in systemd.calls)
    expected_deadlines = [10_002.0, 10_004.0, 10_006.0, 10_008.0, 10_010.0, 10_012.0]
    if deadlines != expected_deadlines:
        raise AssertionError(f"unstable scheduler deadlines: {deadlines}")
    if ping_calls != 6 or systemd_queries != 20:
        raise AssertionError(
            f"unexpected process bounds: ping={ping_calls} systemd={systemd_queries}"
        )
    if maximum_overlap != 1 or service_actions != 0:
        raise AssertionError(
            f"overlap/action violation: overlap={maximum_overlap} actions={service_actions}"
        )
    return {
        "cycles": 6,
        "profiles": 10,
        "cpu_seconds": cpu_seconds,
        "rss_kib": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
        "task_count": threading.active_count(),
        "ping_calls": ping_calls,
        "ping_rate": ping_calls / elapsed,
        "systemd_calls": systemd_queries,
        "systemd_rate": systemd_queries / elapsed,
        "maximum_overlap": maximum_overlap,
        "service_actions": service_actions,
    }
