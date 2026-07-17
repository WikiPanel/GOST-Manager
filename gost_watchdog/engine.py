"""Transition-aware per-profile Watchdog state machine."""

from __future__ import annotations

import random
from collections.abc import Callable

from gost_watchdog.commands import CommandError, SystemdController
from gost_watchdog.models import (
    PENDING_ACTIONS,
    SERVICE_RECONCILIATION_INTERVAL_SECONDS,
    Clock,
    ManagedProfile,
    ProbeResult,
    ProfileState,
    WatchdogEvent,
)
from gost_watchdog.profiles import validate_profile_id, validate_service_name
from gost_watchdog.storage import WatchdogStore


STATE_EVENT = {
    "degraded": "watchdog_degraded",
    "down": "watchdog_upstream_down",
    "recovering": "watchdog_recovering",
    "healthy": "watchdog_upstream_healthy",
}


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


class DurableServiceActions:
    """Persist action intent before touching one exact managed service."""

    def __init__(
        self,
        store: WatchdogStore,
        systemd: SystemdController,
    ) -> None:
        self.store = store
        self.systemd = systemd

    def _persist(
        self, state: ProfileState, events: list[WatchdogEvent], now: int
    ) -> None:
        self.store.persist(state, events, now)
        events.clear()

    @staticmethod
    def _fail_pending(
        state: ProfileState,
        events: list[WatchdogEvent],
        now: int,
        error_category: str,
    ) -> None:
        state.manual_override = True
        state.pending_action = None
        state.pending_action_at = None
        events.append(
            _event(
                state,
                "watchdog_action_error",
                now,
                action_result="failed",
                error_category=error_category,
            )
        )

    @staticmethod
    def _finalize_pending_stop(
        action: str,
        state: ProfileState,
        events: list[WatchdogEvent],
        now: int,
    ) -> None:
        state.stopped_by_watchdog = action == "stop_watchdog"
        state.stopped_by_maintenance = action == "stop_maintenance"
        state.last_service_active = False
        if action == "stop_watchdog":
            events.append(
                _event(
                    state,
                    "watchdog_profile_stopped",
                    now,
                    action_result="intent_reconciled",
                )
            )
            return
        state.maintenance = True
        events.append(
            _event(
                state,
                "watchdog_maintenance_enabled",
                now,
                previous_state=state.health_state,
                new_state="maintenance",
                action_result="maintenance_stop",
            )
        )

    @staticmethod
    def _finalize_pending_start(
        action: str,
        state: ProfileState,
        events: list[WatchdogEvent],
        now: int,
    ) -> None:
        state.stopped_by_watchdog = False
        state.stopped_by_maintenance = False
        state.manual_override = False
        state.last_service_active = True
        if action == "start_watchdog":
            events.append(
                _event(
                    state,
                    "watchdog_profile_started",
                    now,
                    action_result="intent_reconciled",
                )
            )
            return
        if action == "start_maintenance":
            state.maintenance = False
            events.append(
                _event(
                    state,
                    "watchdog_maintenance_disabled",
                    now,
                    previous_state="maintenance",
                    new_state=state.health_state,
                    action_result="maintenance_exit_start",
                )
            )
            return
        events.append(
            _event(
                state,
                "watchdog_mode_change_start",
                now,
                action_result="operator_start",
            )
        )

    def reconcile_pending(
        self,
        profile: ManagedProfile,
        state: ProfileState,
        events: list[WatchdogEvent],
        now: int,
    ) -> None:
        action = state.pending_action
        if action is None:
            return
        if action not in PENDING_ACTIONS:
            raise ValueError("invalid pending Watchdog action")
        validate_profile_id(profile.profile_id)
        validate_service_name(profile.service_name)
        if profile.service_name != f"gost-{profile.profile_id}.service":
            raise ValueError("service does not match the managed Iran profile")
        try:
            active = self.systemd.is_active(profile.service_name)
        except CommandError:
            state.last_service_check_at = now
            self._fail_pending(state, events, now, "service_state_unavailable")
            self._persist(state, events, now)
            return
        state.last_service_active = active
        state.last_service_check_at = now
        if action in {"stop_watchdog", "stop_maintenance"}:
            if active:
                try:
                    active = not self.systemd.stop(profile.service_name)
                except CommandError:
                    active = True
            if active:
                state.stopped_by_watchdog = False
                state.stopped_by_maintenance = False
                state.last_service_active = True
                self._fail_pending(state, events, now, "stop_failed")
                self._persist(state, events, now)
                return
            self._finalize_pending_stop(action, state, events, now)
        else:
            if not active:
                try:
                    active = self.systemd.start(profile.service_name)
                except CommandError:
                    active = False
            if not active:
                state.last_service_active = False
                self._fail_pending(state, events, now, "start_failed")
                self._persist(state, events, now)
                return
            self._finalize_pending_start(action, state, events, now)
        state.pending_action = None
        state.pending_action_at = None
        self._persist(state, events, now)

    def stop(
        self,
        profile: ManagedProfile,
        state: ProfileState,
        events: list[WatchdogEvent],
        now: int,
        *,
        owner: str,
    ) -> bool:
        if owner not in {"watchdog", "maintenance"}:
            raise ValueError("invalid stop owner")
        state.pending_action = f"stop_{owner}"
        state.pending_action_at = now
        self._persist(state, events, now)

        active = self.systemd.is_active(profile.service_name)
        state.last_service_active = active
        state.last_service_check_at = now
        if not active:
            state.pending_action = None
            state.pending_action_at = None
            state.manual_override = True
            events.append(
                _event(
                    state,
                    "watchdog_manual_override",
                    now,
                    action_result="manual_stop",
                )
            )
            self._persist(state, events, now)
            return False

        try:
            stopped = self.systemd.stop(profile.service_name)
        except CommandError:
            stopped = False
        if not stopped:
            state.pending_action = None
            state.pending_action_at = None
            state.manual_override = True
            events.append(
                _event(
                    state,
                    "watchdog_stop_failed",
                    now,
                    action_result="failed",
                    error_category="stop_failed",
                )
            )
            self._persist(state, events, now)
            return False

        state.stopped_by_watchdog = owner == "watchdog"
        state.stopped_by_maintenance = owner == "maintenance"
        state.last_service_active = False
        state.pending_action = None
        state.pending_action_at = None
        if owner == "watchdog":
            events.append(
                _event(
                    state,
                    "watchdog_profile_stopped",
                    now,
                    action_result="stopped",
                )
            )
        try:
            self._persist(state, events, now)
            return True
        except Exception as persist_error:
            try:
                compensated = self.systemd.start(profile.service_name)
            except CommandError:
                compensated = False
            if not compensated:
                raise CommandError("compensation_failed") from persist_error
            state.stopped_by_watchdog = False
            state.stopped_by_maintenance = False
            state.last_service_active = True
            state.pending_action = None
            state.pending_action_at = None
            state.manual_override = True
            events.clear()
            events.append(
                _event(
                    state,
                    "watchdog_action_error",
                    now,
                    action_result="compensated",
                    error_category="persistence_failed",
                )
            )
            self._persist(state, events, now)
            return False

    def start(
        self,
        profile: ManagedProfile,
        state: ProfileState,
        events: list[WatchdogEvent],
        now: int,
        *,
        owner: str,
    ) -> bool:
        if owner not in {"watchdog", "maintenance", "operator"}:
            raise ValueError("invalid start owner")
        state.pending_action = f"start_{owner}"
        state.pending_action_at = now
        self._persist(state, events, now)

        active = self.systemd.is_active(profile.service_name)
        state.last_service_active = active
        state.last_service_check_at = now
        if not active:
            try:
                active = self.systemd.start(profile.service_name)
            except CommandError:
                active = False
        if not active:
            state.pending_action = None
            state.pending_action_at = None
            state.manual_override = True
            events.append(
                _event(
                    state,
                    "watchdog_start_failed",
                    now,
                    action_result="failed",
                    error_category="start_failed",
                )
            )
            self._persist(state, events, now)
            return False

        state.stopped_by_watchdog = False
        state.stopped_by_maintenance = False
        state.last_service_active = True
        state.manual_override = False
        state.pending_action = None
        state.pending_action_at = None
        if owner == "watchdog":
            events.append(
                _event(
                    state,
                    "watchdog_profile_started",
                    now,
                    action_result="started",
                )
            )
        elif owner == "operator":
            events.append(
                _event(
                    state,
                    "watchdog_mode_change_start",
                    now,
                    action_result="operator_start",
                )
            )
        try:
            self._persist(state, events, now)
        except Exception:
            events.append(
                _event(
                    state,
                    "watchdog_action_error",
                    now,
                    action_result="intent_reconciled",
                    error_category="persistence_failed",
                )
            )
            self._persist(state, events, now)
        return True


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
        self.actions = DurableServiceActions(store, systemd)

    _event = staticmethod(_event)

    def reconcile_pending(self, profile: ManagedProfile) -> ProfileState:
        now = int(self.clock.wall())
        state = self.store.get_state(
            profile.profile_id, profile.service_name, profile.kharej_ip
        )
        if state.pending_action is not None:
            self.actions.reconcile_pending(profile, state, [], now)
        return state

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
            _event(
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
    ) -> None:
        if state.pending_action is not None:
            self.actions.reconcile_pending(profile, state, events, now)
        if (
            state.last_service_check_at is not None
            and now - state.last_service_check_at
            < SERVICE_RECONCILIATION_INTERVAL_SECONDS
        ):
            return
        active = self.systemd.is_active(profile.service_name)
        owned = state.stopped_by_watchdog or state.stopped_by_maintenance
        if active and owned:
            state.stopped_by_watchdog = False
            state.stopped_by_maintenance = False
            if not state.manual_override:
                events.append(
                    _event(
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
            and (state.last_service_active is True or state.last_service_active is None)
        ):
            if not state.manual_override:
                events.append(
                    _event(
                        state,
                        "watchdog_manual_override",
                        now,
                        action_result="manual_stop",
                    )
                )
            state.manual_override = True
        state.last_service_active = active
        state.last_service_check_at = now

    def _stop_if_owned_transition(
        self,
        profile: ManagedProfile,
        state: ProfileState,
        now: int,
        events: list[WatchdogEvent],
    ) -> None:
        if profile.config.mode != "auto" or state.maintenance or state.manual_override:
            return
        self.actions.stop(profile, state, events, now, owner="watchdog")

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
        self.actions.start(profile, state, events, now, owner="watchdog")

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
                profile.config.mode == "auto" and not state.stopped_by_watchdog
            ):
                self._stop_if_owned_transition(profile, state, now, events)
            return
        if state.health_state in {"unknown", "healthy"}:
            self._transition(state, "degraded", now, events)

    def process(
        self, profile: ManagedProfile, result: ProbeResult | bool
    ) -> ProfileState:
        now = int(self.clock.wall())
        state = self.store.get_state(
            profile.profile_id, profile.service_name, profile.kharej_ip
        )
        events: list[WatchdogEvent] = []
        if state.pending_action is not None:
            self.actions.reconcile_pending(profile, state, events, now)
        if profile.config.mode == "disabled":
            return state
        if isinstance(result, bool):
            result = ProbeResult("success" if result else "unreachable")
        state.last_check_at = now
        if result.status == "probe_error":
            if (
                state.check_status != "probe_error"
                or state.last_probe_error_category != result.error_category
            ):
                events.append(
                    _event(
                        state,
                        "watchdog_probe_error",
                        now,
                        error_category=result.error_category,
                    )
                )
            state.check_status = "probe_error"
            state.last_probe_error_category = result.error_category
            self.store.persist(state, events, now)
            return state
        if state.check_status == "probe_error":
            events.append(
                _event(
                    state,
                    "watchdog_probe_recovered",
                    now,
                    error_category=state.last_probe_error_category,
                )
            )
        state.check_status = result.status
        state.last_probe_error_category = None
        if profile.config.mode == "auto":
            self._reconcile_service(profile, state, now, events)
        if result.status == "success":
            self._successful_check(profile, state, now, events)
        else:
            self._failed_check(profile, state, now, events)
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
        self.actions = DurableServiceActions(store, systemd)

    def apply(self, profile: ManagedProfile, action: str) -> ProfileState:
        if action not in {"enter-keep", "enter-stop", "exit-no-start", "exit-start"}:
            raise ValueError("unsupported maintenance action")
        now = int(self.clock.wall())
        state = self.store.get_state(
            profile.profile_id, profile.service_name, profile.kharej_ip
        )
        events: list[WatchdogEvent] = []
        self.actions.reconcile_pending(profile, state, events, now)
        active = self.systemd.is_active(profile.service_name)
        state.last_service_active = active
        state.last_service_check_at = now
        previous_display = state.display_state
        if action.startswith("enter-"):
            state.maintenance = True
            if action == "enter-stop" and active:
                self.actions.stop(
                    profile, state, events, now, owner="maintenance"
                )
            events.append(
                _event(
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
                        raise ValueError(
                            "service stop is not owned by Watchdog or maintenance"
                        )
                    if not self.actions.start(
                        profile, state, events, now, owner="maintenance"
                    ):
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
                _event(
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

    def start_owned_for_mode_change(self, profile: ManagedProfile) -> ProfileState:
        now = int(self.clock.wall())
        state = self.store.get_state(
            profile.profile_id, profile.service_name, profile.kharej_ip
        )
        age = None if state.last_check_at is None else now - state.last_check_at
        freshness_limit = max(10, profile.config.check_interval_seconds * 2)
        if (
            state.health_state != "healthy"
            or state.check_status != "success"
            or age is None
            or age < 0
            or age > freshness_limit
        ):
            raise ValueError("upstream is not healthy")
        if not state.stopped_by_watchdog:
            raise ValueError("service is not stopped by Watchdog")
        events: list[WatchdogEvent] = []
        if not self.actions.start(
            profile,
            state,
            events,
            now,
            owner="operator",
        ):
            raise CommandError("start_failed")
        return state

    def rearm(self, profile: ManagedProfile) -> ProfileState:
        now = int(self.clock.wall())
        state = self.store.get_state(
            profile.profile_id, profile.service_name, profile.kharej_ip
        )
        if not state.manual_override:
            raise ValueError("manual override is not active")
        active = self.systemd.is_active(profile.service_name)
        if not active and not state.stopped_by_watchdog:
            raise ValueError("start the manually stopped service before re-arming Auto Protect")
        state.manual_override = False
        state.last_service_active = active
        state.last_service_check_at = now
        event = _event(
            state,
            "watchdog_manual_override",
            now,
            action_result="rearmed",
        )
        self.store.persist(state, [event], now)
        return state
