"""Data contracts and production defaults for Upstream Watchdog v1."""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable


CHECK_MODE = "ping"
CHECK_INTERVAL_SECONDS = 2
PING_TIMEOUT_SECONDS = 1
FAILURE_THRESHOLD = 10
SUCCESS_THRESHOLD = 10
RECOVERY_HOLD_SECONDS = 10
RECOVERY_JITTER_MAX_SECONDS = 10
EVENT_RETENTION_SECONDS = 24 * 60 * 60
MAX_PING_WORKERS = 32

MODES = ("disabled", "monitor", "auto")
HEALTH_STATES = ("unknown", "healthy", "degraded", "down", "recovering")
DISPLAY_STATES = HEALTH_STATES + ("maintenance",)


@dataclasses.dataclass(frozen=True)
class Clock:
    wall: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic


@dataclasses.dataclass(frozen=True)
class GlobalConfig:
    check_mode: str = CHECK_MODE
    check_interval_seconds: int = CHECK_INTERVAL_SECONDS
    ping_timeout_seconds: int = PING_TIMEOUT_SECONDS
    failure_threshold: int = FAILURE_THRESHOLD
    success_threshold: int = SUCCESS_THRESHOLD
    recovery_hold_seconds: int = RECOVERY_HOLD_SECONDS
    recovery_jitter_max_seconds: int = RECOVERY_JITTER_MAX_SECONDS


@dataclasses.dataclass(frozen=True)
class ProfileConfig:
    mode: str = "disabled"
    check_interval_seconds: int = CHECK_INTERVAL_SECONDS
    ping_timeout_seconds: int = PING_TIMEOUT_SECONDS
    failure_threshold: int = FAILURE_THRESHOLD
    success_threshold: int = SUCCESS_THRESHOLD
    recovery_hold_seconds: int = RECOVERY_HOLD_SECONDS
    recovery_jitter_max_seconds: int = RECOVERY_JITTER_MAX_SECONDS


@dataclasses.dataclass(frozen=True)
class ManagedProfile:
    profile_id: str
    service_name: str
    kharej_ip: str
    env_path: str
    unit_path: str
    config_path: str
    config: ProfileConfig


@dataclasses.dataclass
class ProfileState:
    profile_id: str
    service_name: str
    kharej_ip: str
    health_state: str = "unknown"
    maintenance: bool = False
    stopped_by_watchdog: bool = False
    stopped_by_maintenance: bool = False
    manual_override: bool = False
    failure_count: int = 0
    success_count: int = 0
    last_check_at: int | None = None
    last_transition_at: int | None = None
    outage_started_at: int | None = None
    recovery_started_at: int | None = None
    recovery_ready_at: int | None = None
    recovery_jitter_seconds: int = 0
    last_service_active: bool | None = None

    @property
    def display_state(self) -> str:
        return "maintenance" if self.maintenance else self.health_state


@dataclasses.dataclass(frozen=True)
class WatchdogEvent:
    ts: int
    code: str
    profile_id: str | None = None
    service_name: str | None = None
    kharej_ip: str | None = None
    previous_state: str | None = None
    new_state: str | None = None
    failure_count: int = 0
    success_count: int = 0
    action_result: str | None = None
    outage_duration: int | None = None
    error_category: str | None = None
