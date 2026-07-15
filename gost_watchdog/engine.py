"""Transition-aware per-profile Watchdog state machine."""

from __future__ import annotations

import random
from collections.abc import Callable

from gost_watchdog.commands import CommandError, SystemdController
from gost_watchdog.models import Clock, ManagedProfile, ProfileState, WatchdogEvent
from gost_watchdog.storage import WatchdogStore


STATE_EVENT = {
    "degraded": "watchdog_degraded",
    "down": "watchdog_upstream_down",
    "recovering": "watchdog_recovering",
    "healthy": "watchdog_upstream_healthy",
}


class WatchdogEngine:
    def __init__(
        self,
        store: WatchdogStore,
        systemd: SystemdController,
        *,
        clock: Clock = Clock(),
        jitter_source: Callable[[int], int] | None = None,
    ) -> None:
        self.store = store
        self.systemd = systemd
        self.clock = clock
        self.jitter_source = jitter_source or (lambda maximum: random.randint(0, maximum))

    @staticmethod
    def _event(
        state: ProfileState,
        code: str,
        now: int,
        **values: object,
    ) -> WatchdogEvent:
        return WatchdogEvent(
            ts=now,
            code=code,
            profile_id=state.profile_id,
            service_name=state.service_name,
            kharej_ip=state.kharej_ip,
            previous_state=values.get("previous_state"),
            new_state=values.get("new_state"),
            failure_count=state.failure_count,
            success_count=state.success_count,
            action_result=values.get("action_result"),
            outage_duration=values.get("outage_duration"),
            error_category=values.get("error_category"),
        )

    def _transition(
        self,
        state: ProfileState,
        new_state: str,
        now: int,
        events: list[WatchdogEvent],
        *,
        outage_duration: int | None = None,
    ) -> None:
        previous = state.health_state
        if previous == new_state:
            return
        state.health_state = new_state
        state.last_transition_at = now
        events.append(
            self._event(
                state,
                STATE_EVENT[new_state],
                now,
                previous_state=previous,
                new_state=new_state,
                outage_duration=outage_duration,
            )
        )

    def _reconcile_service(
        self,
        profile: ManagedProfile,
        state: ProfileState,
        now: int,
        events: list[WatchdogEvent],
    ) -> bool:
        active = self.systemd.is_active(profile.service_name)
        owned = state.stopped_by_watchdog or state.stopped_by_maintenance
        if active and owned:
            state.stopped_by_watchdog = False
            state.stopped_by_maintenance = False
            if not state.manual_override:
                events.append(
                    self._event(
                        state,
                        "watchdog_manual_override",
                        now,
                        action_result="manual_start",
                    )
                )
            state.manual_override = True
        elif (
            not active
            and not owned
            and profile.config.mode == "auto"
            and (state.last_service_active is True or state.last_service_active is None)
        ):
            if not state.manual_override:
                events.append(
                    self._event(
                        state,
                        "watchdog_manual_override",
                        now,
                        action_result="manual_stop",
                    )
                )
            state.manual_override = True
        state.last_service_active = active
        return active

    def _stop_if_owned_transition(
        self,
        profile: ManagedProfile,
        state: ProfileState,
        now: int,
        active: bool,
        events: list[WatchdogEvent],
    ) -> None:
        if profile.config.mode != "auto" or state.maintenance or state.manual_override:
            return
        if not active:
            return
        try:
            stopped = self.systemd.stop(profile.service_name)
        except CommandError:
            stopped = False
        if stopped:
            state.stopped_by_watchdog = True
            state.last_service_active = False
            events.append(
                self._event(
                    state,
                    "watchdog_profile_stopped",
                    now,
                    action_result="stopped",
                )
            )
            return
        state.manual_override = True
        events.append(
            self._event(
                state,
                "watchdog_stop_failed",
                now,
                action_result="failed",
                error_category="stop_failed",
            )
        )

    def _start_after_recovery(
        self,
        profile: ManagedProfile,
        state: ProfileState,
        now: int,
        events: list[WatchdogEvent],
    ) -> None:
        if (
            profile.config.mode != "auto"
            or state.maintenance
            or state.manual_override
            or not state.stopped_by_watchdog
        ):
            return
        try:
            started = self.systemd.start(profile.service_name)
        except CommandError:
            started = False
        if started:
            state.stopped_by_watchdog = False
            state.last_service_active = True
            events.append(
                self._event(
                    state,
                    "watchdog_profile_started",
                    now,
                    action_result="started",
                )
            )
            return
        state.manual_override = True
        events.append(
            self._event(
                state,
                "watchdog_start_failed",
                now,
                action_result="failed",
                error_category="start_failed",
            )
        )

    def _complete_recovery(
        self,
        profile: ManagedProfile,
        state: ProfileState,
        now: int,
        events: list[WatchdogEvent],
    ) -> None:
        duration = None
        if state.outage_started_at is not None:
            duration = max(0, now - state.outage_started_at)
        self._transition(state, "healthy", now, events, outage_duration=duration)
        state.outage_started_at = None
        state.recovery_started_at = None
        state.recovery_ready_at = None
        state.recovery_jitter_seconds = 0
        self._start_after_recovery(profile, state, now, events)

    def _successful_check(
        self,
        profile: ManagedProfile,
        state: ProfileState,
        now: int,
        events: list[WatchdogEvent],
    ) -> None:
        config = profile.config
        state.failure_count = 0
        if state.health_state in {"unknown", "degraded"}:
            state.success_count = min(state.success_count + 1, config.success_threshold)
            self._transition(state, "healthy", now, events)
            return
        if state.health_state == "healthy":
            state.success_count = min(state.success_count + 1, config.success_threshold)
            self._start_after_recovery(profile, state, now, events)
            return
        if state.health_state == "down":
            state.success_count = 1
            state.recovery_started_at = now
            state.recovery_ready_at = None
            state.recovery_jitter_seconds = 0
            self._transition(state, "recovering", now, events)
        else:
            state.success_count = min(state.success_count + 1, config.success_threshold)
        if state.success_count < config.success_threshold:
            return
        if state.recovery_ready_at is None:
            jitter = int(self.jitter_source(config.recovery_jitter_max_seconds))
            if jitter < 0 or jitter > config.recovery_jitter_max_seconds:
                raise ValueError("jitter source returned an out-of-bounds value")
            state.recovery_jitter_seconds = jitter
            state.recovery_ready_at = now + config.recovery_hold_seconds + jitter
        if now >= state.recovery_ready_at:
            self._complete_recovery(profile, state, now, events)

    def _failed_check(
        self,
        profile: ManagedProfile,
        state: ProfileState,
        now: int,
        active: bool,
        events: list[WatchdogEvent],
    ) -> None:
        config = profile.config
        state.success_count = 0
        state.failure_count = min(state.failure_count + 1, config.failure_threshold)
        state.recovery_started_at = None
        state.recovery_ready_at = None
        state.recovery_jitter_seconds = 0
        if state.health_state == "recovering":
            self._transition(state, "down", now, events)
            return
        if state.failure_count >= config.failure_threshold:
            transitioned = state.health_state != "down"
            if state.outage_started_at is None:
                state.outage_started_at = now
            self._transition(state, "down", now, events)
            if transitioned or (
                profile.config.mode == "auto"
                and active
                and not state.stopped_by_watchdog
            ):
                self._stop_if_owned_transition(profile, state, now, active, events)
            return
        if state.health_state in {"unknown", "healthy"}:
            self._transition(state, "degraded", now, events)

    def process(self, profile: ManagedProfile, success: bool) -> ProfileState:
        if profile.config.mode == "disabled":
            return self.store.get_state(
                profile.profile_id, profile.service_name, profile.kharej_ip
            )
        now = int(self.clock.wall())
        state = self.store.get_state(
            profile.profile_id, profile.service_name, profile.kharej_ip
        )
        events: list[WatchdogEvent] = []
        active = False
        if profile.config.mode == "auto":
            active = self._reconcile_service(profile, state, now, events)
        state.last_check_at = now
        if success:
            self._successful_check(profile, state, now, events)
        else:
            self._failed_check(profile, state, now, active, events)
        self.store.persist(state, events, now)
        return state


class MaintenanceController:
    def __init__(
        self,
        store: WatchdogStore,
        systemd: SystemdController,
        *,
        clock: Clock = Clock(),
    ) -> None:
        self.store = store
        self.systemd = systemd
        self.clock = clock

    def apply(self, profile: ManagedProfile, action: str) -> ProfileState:
        if action not in {"enter-keep", "enter-stop", "exit-no-start", "exit-start"}:
            raise ValueError("unsupported maintenance action")
        now = int(self.clock.wall())
        state = self.store.get_state(
            profile.profile_id, profile.service_name, profile.kharej_ip
        )
        events: list[WatchdogEvent] = []
        active = self.systemd.is_active(profile.service_name)
        previous_display = state.display_state
        if action.startswith("enter-"):
            state.maintenance = True
            if action == "enter-stop" and active:
                if not self.systemd.stop(profile.service_name):
                    events.append(
                        WatchdogEngine._event(
                            state,
                            "watchdog_stop_failed",
                            now,
                            action_result="failed",
                            error_category="stop_failed",
                        )
                    )
                else:
                    state.stopped_by_watchdog = False
                    state.stopped_by_maintenance = True
                    state.last_service_active = False
            events.append(
                WatchdogEngine._event(
                    state,
                    "watchdog_maintenance_enabled",
                    now,
                    previous_state=previous_display,
                    new_state="maintenance",
                    action_result="maintenance_stop" if action == "enter-stop" else None,
                )
            )
        else:
            if action == "exit-start":
                if state.health_state != "healthy":
                    raise ValueError("upstream is not healthy")
                if not active:
                    if not (state.stopped_by_maintenance or state.stopped_by_watchdog):
                        raise ValueError("service stop is not owned by Watchdog or maintenance")
                    if not self.systemd.start(profile.service_name):
                        raise CommandError("start_failed")
                state.last_service_active = True
                state.manual_override = False
                state.stopped_by_watchdog = False
                state.stopped_by_maintenance = False
            elif not active:
                state.manual_override = True
                state.last_service_active = False
            state.maintenance = False
            state.stopped_by_maintenance = False
            if action == "exit-no-start":
                state.stopped_by_watchdog = False
            events.append(
                WatchdogEngine._event(
                    state,
                    "watchdog_maintenance_disabled",
                    now,
                    previous_state=previous_display,
                    new_state=state.health_state,
                    action_result=(
                        "maintenance_exit_start"
                        if action == "exit-start"
                        else "maintenance_exit_no_start"
                    ),
                )
            )
        state.last_transition_at = now
        self.store.persist(state, events, now)
        return state

    def rearm(self, profile: ManagedProfile) -> ProfileState:
        now = int(self.clock.wall())
        state = self.store.get_state(
            profile.profile_id, profile.service_name, profile.kharej_ip
        )
        active = self.systemd.is_active(profile.service_name)
        if not active and not state.stopped_by_watchdog:
            raise ValueError("start the manually stopped service before re-arming Auto Protect")
        state.manual_override = False
        state.last_service_active = active
        self.store.persist(state, [], now)
        return state
