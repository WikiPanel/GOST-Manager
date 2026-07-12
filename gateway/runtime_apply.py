"""Gateway desired-runtime selection, planning, activation, and rollback."""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gateway.errors import ConflictError, OperationalError, StateError, ValidationError
from gateway.models import StatePair
from gateway.runtime_inspection import RuntimeInspector
from gateway.runtime_models import (
    DesiredExitRuntime, Listener, PlanAction, RuntimeEntry, RuntimePlan, ServiceState,
)
from gateway.runtime_paths import RuntimePaths, service_name
from gateway.runtime_render import (
    MAX_RUNTIME_MANIFEST_BYTES, make_entry, parse_manifest, render_env,
    parse_env, render_manifest, render_unit, sha256, validate_unit,
)
from gateway.runtime_store import RuntimeStore
from gateway.secrets import SecretStore
from gateway.store import GatewayStateStore

FAILURE_PHASES = (
    "after_state_lock_acquisition", "after_runtime_lock_acquisition", "after_state_read",
    "after_secret_validation", "after_listener_snapshot", "after_service_state_capture",
    "after_candidate_rendering", "after_candidate_validation", "after_staging",
    "after_backup_creation", "after_first_file_replacement", "after_unit_replacement",
    "after_stale_service_stop", "after_daemon_reload", "after_new_service_enable",
    "after_changed_service_restart", "after_listener_verification",
    "after_manifest_replacement", "after_parent_fsync", "after_backup_pruning",
)


@dataclass(frozen=True)
class ApplyResult:
    plan: RuntimePlan
    restarted: tuple[str, ...]
    started: tuple[str, ...]
    removed: tuple[str, ...]
    changed: bool


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def secret_references(pair: StatePair) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    for binding in pair.node.bindings:
        if binding.secret_ref:
            result.setdefault(binding.secret_ref, []).append(binding.exit_id)
    return {key: tuple(sorted(value)) for key, value in result.items()}


def select_desired(pair: StatePair, secrets: SecretStore) -> tuple[DesiredExitRuntime, ...]:
    exits = {item.id: item for item in pair.shared.exits}
    result: list[DesiredExitRuntime] = []
    for binding in sorted(pair.node.bindings, key=lambda item: item.exit_id):
        exit_node = exits.get(binding.exit_id)
        if exit_node is None or not exit_node.enabled or not binding.enabled:
            continue
        if binding.listen_address != "127.0.0.1" or not binding.secret_ref:
            raise ValidationError("enabled binding is not runtime-safe")
        _credentials, mtime_ns = secrets.read(binding.secret_ref)
        result.append(
            DesiredExitRuntime(
                exit_id=exit_node.id,
                service_name=service_name(exit_node.id),
                listen_address="127.0.0.1",
                listen_port=binding.listen_port,
                exit_host=exit_node.host,
                socks_port=exit_node.socks_port,
                target_address="127.0.0.1",
                target_port=exit_node.target_port,
                secret_ref=binding.secret_ref,
                secret_mtime_ns=mtime_ns,
            )
        )
    return tuple(result)


class RuntimeManager:
    def __init__(
        self,
        state_store: GatewayStateStore,
        secret_store: SecretStore,
        paths: RuntimePaths,
        *,
        inspector: RuntimeInspector | None = None,
        runtime_store: RuntimeStore | None = None,
        clock: Callable[[], str] = _utc_now,
        failure_hook: Callable[[str], None] | None = None,
        verify_units: bool = True,
    ) -> None:
        self.state_store = state_store
        self.secret_store = secret_store
        self.paths = paths
        self.inspector = inspector or RuntimeInspector()
        self.runtime_store = runtime_store or RuntimeStore(paths)
        self.clock = clock
        self.failure_hook = failure_hook
        self.verify_units = verify_units

    def _fail(self, phase: str) -> None:
        if self.failure_hook is not None:
            self.failure_hook(phase)

    def plan(self, exit_id: str | None = None) -> RuntimePlan:
        with self.state_store.locked_pair() as pair:
            self._fail("after_state_lock_acquisition")
            with self.secret_store.lock():
                self._fail("after_runtime_lock_acquisition")
                return self._plan_locked(pair, exit_id)

    def _load_manifest(self) -> dict[str, RuntimeEntry]:
        data = self.runtime_store.read_optional(self.paths.manifest_file, MAX_RUNTIME_MANIFEST_BYTES)
        return {} if data is None else parse_manifest(data)

    def _plan_locked(self, pair: StatePair, selected: str | None) -> RuntimePlan:
        self._fail("after_state_read")
        desired_all = select_desired(pair, self.secret_store)
        self._fail("after_secret_validation")
        if selected is not None:
            from gateway.validation import validate_slug
            validate_slug(selected, "exit ID")
        desired = tuple(item for item in desired_all if selected is None or item.exit_id == selected)
        listeners = self.inspector.listeners()
        self._fail("after_listener_snapshot")
        manifest = self._load_manifest()
        managed_ids = self.runtime_store.managed_exit_ids() | set(manifest)
        if selected is not None:
            managed_ids &= {selected}
        states = {
            exit_id: self.inspector.service_state(exit_id)
            for exit_id in sorted(managed_ids | {item.exit_id for item in desired})
        }
        self._fail("after_service_state_capture")
        actions: list[PlanAction] = []
        for item in desired:
            env = render_env(item)
            unit = render_unit(item, self.paths)
            conflict = self._port_conflict(item, states[item.exit_id], listeners)
            action, reason = self._desired_action(
                item, states[item.exit_id], manifest.get(item.exit_id), env, unit
            )
            if conflict is not None:
                action, reason = "conflict", conflict
            actions.append(self._action(item, states[item.exit_id], action, reason))
        desired_ids = {item.exit_id for item in desired_all}
        for stale_id in sorted(managed_ids - desired_ids):
            state = states[stale_id]
            entry = manifest.get(stale_id)
            actions.append(
                PlanAction(
                    stale_id, service_name(stale_id), "remove", "runtime is no longer desired",
                    "active" if state.active else "inactive", "absent", "127.0.0.1", 0,
                    entry.secret_ref if entry else "",
                )
            )
        self._fail("after_candidate_rendering")
        return RuntimePlan(tuple(actions), desired, 1)

    def _desired_action(
        self, desired: DesiredExitRuntime, state: ServiceState,
        previous: RuntimeEntry | None, env: bytes, unit: bytes,
    ) -> tuple[str, str]:
        current_env = self.runtime_store.read_optional(self.paths.env_file(desired.exit_id), 64 * 1024)
        current_unit = self.runtime_store.read_optional(self.paths.unit_file(desired.exit_id), 64 * 1024)
        entry = make_entry(desired, self.paths, env, unit)
        if previous is None or current_env is None or current_unit is None or not state.loaded:
            if state.active:
                return "restart", "active service runtime material is incomplete"
            return "create", "managed runtime is missing"
        effective_changed = (
            current_env != env or current_unit != unit
            or previous.secret_ref != entry.secret_ref
            or previous.secret_mtime_ns != entry.secret_mtime_ns
            or previous.env_sha256 != entry.env_sha256
            or previous.unit_sha256 != entry.unit_sha256
        )
        if effective_changed:
            return ("restart" if state.active else "update"), "effective runtime input changed"
        if not state.active:
            return "start", "desired service is inactive"
        if not state.enabled:
            return "start", "desired service is disabled"
        return "no-op", "effective runtime is unchanged"

    def _port_conflict(
        self, desired: DesiredExitRuntime, state: ServiceState,
        listeners: tuple[Listener, ...],
    ) -> str | None:
        for listener in listeners:
            if listener.port != desired.listen_port:
                continue
            if listener.wildcard or listener.address not in {"127.0.0.1", "::ffff:127.0.0.1"}:
                return "listen port is occupied by a wildcard or non-loopback listener"
            if state.active and state.main_pid and listener.pids == (state.main_pid,):
                continue
            return "listen port ownership is unavailable or belongs to another process"
        return None

    @staticmethod
    def _action(item: DesiredExitRuntime, state: ServiceState, action: str, reason: str) -> PlanAction:
        return PlanAction(
            item.exit_id, item.service_name, action, reason,
            "active" if state.active else "inactive", "active",
            item.listen_address, item.listen_port, item.secret_ref,
        )

    def apply(self, *, yes: bool, exit_id: str | None = None) -> ApplyResult:
        if not yes:
            raise ConflictError("runtime apply requires explicit confirmation")
        with self.state_store.locked_pair() as pair:
            self._fail("after_state_lock_acquisition")
            with self.secret_store.lock():
                self._fail("after_runtime_lock_acquisition")
                plan = self._plan_locked(pair, exit_id)
                if plan.has_conflict:
                    raise ConflictError("runtime plan contains a port conflict")
                return self._apply_locked(pair, plan)

    def _apply_locked(self, pair: StatePair, plan: RuntimePlan) -> ApplyResult:
        if all(item.action == "no-op" for item in plan.actions):
            return ApplyResult(plan, (), (), (), False)
        entries = self._load_manifest()
        rendered: dict[str, tuple[bytes, bytes, RuntimeEntry]] = {}
        for item in plan.desired:
            env, unit = render_env(item), render_unit(item, self.paths)
            rendered[item.exit_id] = (env, unit, make_entry(item, self.paths, env, unit))
        candidate_entries = dict(entries)
        for action in plan.actions:
            if action.action in {"create", "update", "restart"}:
                candidate_entries[action.exit_id] = rendered[action.exit_id][2]
            elif action.action == "remove":
                candidate_entries.pop(action.exit_id, None)
        candidate_manifest = render_manifest(
            applied_at=self.clock(), document_id=pair.shared.document_id,
            shared_revision=pair.shared.revision, node_revision=pair.node.revision,
            entries=tuple(candidate_entries.values()),
        )
        self._fail("after_candidate_validation")
        with tempfile.TemporaryDirectory(prefix="gost-gateway-runtime-") as temporary:
            staging = Path(temporary)
            os.chmod(staging, 0o700)
            for exit_id, (env, unit, _entry) in rendered.items():
                staged_env = staging / f"{exit_id}.env"
                staged_unit = staging / service_name(exit_id)
                staged_env.write_bytes(env)
                os.chmod(staged_env, 0o600)
                staged_unit.write_bytes(
                    unit.replace(
                        str(self.paths.env_file(exit_id)).encode("utf-8"),
                        str(staged_env).encode("utf-8"),
                    )
                )
                os.chmod(staged_unit, 0o600)
                if self.verify_units:
                    self.inspector.verify_unit(str(staged_unit))
            staged_manifest = staging / "runtime.json"
            staged_manifest.write_bytes(candidate_manifest)
            os.chmod(staged_manifest, 0o600)
            self._fail("after_staging")
        self.runtime_store.prepare()
        touched: set[Path] = {self.paths.manifest_file}
        for action in plan.actions:
            touched.add(self.paths.env_file(action.exit_id))
            touched.add(self.paths.unit_file(action.exit_id))
        snapshots = self.runtime_store.snapshot(touched)
        previous_states = {
            action.exit_id: self.inspector.service_state(action.exit_id)
            for action in plan.actions
        }
        backup = self.runtime_store.create_backup(snapshots)
        changed_units = False
        restarted: list[str] = []
        started: list[str] = []
        removed: list[str] = []
        mutated_services: set[str] = set()
        first_file_replaced = False
        try:
            self._fail("after_backup_creation")
            for action in plan.actions:
                if action.action in {"create", "update", "restart"}:
                    env, unit, _entry = rendered[action.exit_id]
                    self.runtime_store.write_atomic(self.paths.env_file(action.exit_id), env)
                    if not first_file_replaced:
                        first_file_replaced = True
                        self._fail("after_first_file_replacement")
                    self.runtime_store.write_atomic(self.paths.unit_file(action.exit_id), unit, 0o644)
                    self._fail("after_unit_replacement")
                    changed_units = True
                elif action.action == "remove":
                    state = previous_states[action.exit_id]
                    if state.loaded or state.active or state.enabled:
                        self.inspector.systemctl("disable", "--now", action.service_name)
                        mutated_services.add(action.exit_id)
                    self._fail("after_stale_service_stop")
                    self.runtime_store.remove_exact(self.paths.unit_file(action.exit_id))
                    self.runtime_store.remove_exact(self.paths.env_file(action.exit_id))
                    removed.append(action.exit_id)
                    changed_units = True
            if changed_units:
                self.inspector.systemctl("daemon-reload")
            self._fail("after_daemon_reload")
            for action in plan.actions:
                if action.action in {"create", "start"}:
                    self.inspector.systemctl("enable", action.service_name)
                    self.inspector.systemctl("start", action.service_name)
                    mutated_services.add(action.exit_id)
                    started.append(action.exit_id)
                    self._fail("after_new_service_enable")
                elif action.action == "update":
                    self.inspector.systemctl("enable", action.service_name)
                    self.inspector.systemctl("start", action.service_name)
                    mutated_services.add(action.exit_id)
                    started.append(action.exit_id)
                elif action.action == "restart":
                    self.inspector.systemctl("enable", action.service_name)
                    self.inspector.systemctl("restart", action.service_name)
                    mutated_services.add(action.exit_id)
                    restarted.append(action.exit_id)
                    self._fail("after_changed_service_restart")
            for item in plan.desired:
                state = self.inspector.service_state(item.exit_id)
                if not state.active or state.main_pid is None:
                    raise OperationalError("gateway service did not become active")
                self.inspector.verify_service_listener(
                    item.listen_address, item.listen_port, state.main_pid
                )
            self._fail("after_listener_verification")
            self.runtime_store.write_atomic(self.paths.manifest_file, candidate_manifest)
            self._fail("after_manifest_replacement")
            self.runtime_store.fsync_directory(self.paths.generated_dir)
            self._fail("after_parent_fsync")
            self.runtime_store.remove_backup(backup)
            self.runtime_store.prune_backups()
            self._fail("after_backup_pruning")
            return ApplyResult(plan, tuple(restarted), tuple(started), tuple(removed), True)
        except Exception:
            try:
                self._rollback(snapshots, previous_states, mutated_services, changed_units)
            except Exception as exc:
                raise OperationalError(
                    f"gateway runtime rollback could not be verified; backup retained at {backup}"
                ) from exc
            self.runtime_store.remove_backup(backup)
            raise

    def _rollback(
        self, snapshots: tuple, previous_states: dict[str, ServiceState],
        mutated_services: set[str], changed_units: bool,
    ) -> None:
        for exit_id in sorted(mutated_services):
            try:
                self.inspector.systemctl("stop", service_name(exit_id))
            except OperationalError:
                pass
        self.runtime_store.restore(snapshots)
        if changed_units:
            self.inspector.systemctl("daemon-reload")
        for exit_id, state in sorted(previous_states.items()):
            name = service_name(exit_id)
            if state.enabled:
                self.inspector.systemctl("enable", name)
            else:
                try:
                    self.inspector.systemctl("disable", name)
                except OperationalError:
                    pass
            if state.active:
                self.inspector.systemctl("start", name)

    def status(self, exit_id: str | None = None) -> tuple[ServiceState, ...]:
        manifest = self._load_manifest()
        ids = sorted(manifest)
        if exit_id is not None:
            ids = [exit_id]
        return tuple(self.inspector.service_state(item) for item in ids)

    def service_control(self, action: str, exit_id: str, *, yes: bool = False) -> ServiceState:
        from gateway.validation import validate_slug
        validate_slug(exit_id, "exit ID")
        if action in {"stop", "restart"} and not yes:
            raise ConflictError(f"service {action} requires explicit confirmation")
        unit = self.paths.unit_file(exit_id)
        if self.runtime_store.read_optional(unit, 64 * 1024) is None:
            raise StateError("gateway exit unit is missing")
        if action == "status":
            return self.inspector.service_state(exit_id)
        with self.secret_store.lock():
            if action in {"start", "restart"}:
                manifest = self._load_manifest()
                entry = manifest.get(exit_id)
                if entry is None:
                    raise StateError("gateway runtime entry is missing")
                self.secret_store.read(entry.secret_ref)
                env_data = self.runtime_store.read_optional(self.paths.env_file(exit_id), 64 * 1024)
                unit_data = self.runtime_store.read_optional(self.paths.unit_file(exit_id), 64 * 1024)
                if env_data is None or unit_data is None:
                    raise StateError("generated gateway runtime is incomplete")
                if sha256(env_data) != entry.env_sha256 or sha256(unit_data) != entry.unit_sha256:
                    raise StateError("generated gateway runtime does not match its manifest")
                values = parse_env(env_data, exit_id)
                listeners = self.inspector.listeners()
                state = self.inspector.service_state(exit_id)
                desired = DesiredExitRuntime(
                    exit_id, service_name(exit_id), "127.0.0.1",
                    int(values["GATEWAY_LISTEN_PORT"]), values["GATEWAY_EXIT_HOST"],
                    int(values["GATEWAY_SOCKS_PORT"]), "127.0.0.1",
                    int(values["GATEWAY_TARGET_PORT"]),
                    entry.secret_ref, entry.secret_mtime_ns,
                )
                validate_unit(unit_data, desired, self.paths)
                conflict = self._port_conflict(desired, state, listeners)
                if conflict:
                    raise ConflictError(conflict)
            self.inspector.systemctl(action, service_name(exit_id))
            result = self.inspector.service_state(exit_id)
            if action in {"start", "restart"}:
                if not result.active or result.main_pid is None:
                    raise OperationalError("gateway service did not become active")
                self.inspector.verify_service_listener(
                    "127.0.0.1", int(values["GATEWAY_LISTEN_PORT"]), result.main_pid
                )
            return result
