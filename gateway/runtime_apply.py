"""Gateway desired-runtime selection, planning, activation, and rollback."""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from gateway.errors import ConflictError, OperationalError, StateError, ValidationError
from gateway.models import StatePair
from gateway.runtime_inspection import RuntimeInspector
from gateway.runtime_models import (
    DesiredExitRuntime, Listener, ListenerDisposition, PlanAction, RuntimeDiscovery,
    RuntimeEntry, RuntimeManifest, RuntimePlan, ServiceState,
)
from gateway.runtime_paths import RuntimePaths, service_name
from gateway.runtime_render import (
    MAX_RUNTIME_MANIFEST_BYTES, make_entry, parse_manifest, parse_manifest_document,
    render_env, parse_env, render_manifest, render_unit, sha256, validate_unit,
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
    "after_final_verification", "after_current_backup_removal",
    "after_backup_parent_fsync", "after_service_manifest_replacement",
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
        return {} if data is None else parse_manifest(data, self.paths)

    def _load_manifest_document(self) -> RuntimeManifest | None:
        data = self.runtime_store.read_optional(
            self.paths.manifest_file, MAX_RUNTIME_MANIFEST_BYTES
        )
        return None if data is None else parse_manifest_document(data, self.paths)

    def _validate_runtime_dependencies(self) -> None:
        self.runtime_store.validate_dependency(self.paths.runner_path, "gateway runner")
        self.runtime_store.validate_dependency(self.paths.gost_bin, "GOST binary")

    def _discover_runtime(self, manifest: dict[str, RuntimeEntry]) -> RuntimeDiscovery:
        env_ids, unit_ids = self.runtime_store.managed_file_ids()
        systemd_ids = self.inspector.discover_service_ids()
        return RuntimeDiscovery(
            env_ids=env_ids,
            unit_ids=unit_ids,
            manifest_ids=frozenset(manifest),
            systemd_ids=systemd_ids,
        )

    def _plan_locked(self, pair: StatePair, selected: str | None) -> RuntimePlan:
        self._fail("after_state_read")
        desired_all = select_desired(pair, self.secret_store)
        self._fail("after_secret_validation")
        if selected is not None:
            from gateway.validation import validate_slug
            validate_slug(selected, "exit ID")
        self._validate_runtime_dependencies()
        manifest = self._load_manifest()
        discovery = self._discover_runtime(manifest)
        known_exit_ids = {item.id for item in pair.shared.exits}
        if selected is not None and selected not in known_exit_ids and selected not in discovery.all_ids:
            raise ConflictError("selected gateway Exit is unknown")
        desired = tuple(item for item in desired_all if selected is None or item.exit_id == selected)
        listeners = self.inspector.listeners()
        self._fail("after_listener_snapshot")
        managed_ids = set(discovery.all_ids)
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
            disposition = self._listener_disposition(
                item, states[item.exit_id], listeners
            )
            action, reason = self._desired_action(
                item, states[item.exit_id], manifest.get(item.exit_id), env, unit
            )
            if disposition is ListenerDisposition.MISSING_FOR_ACTIVE_SERVICE:
                action, reason = "restart", "expected_listener_missing"
            elif disposition is ListenerDisposition.OWNERSHIP_UNAVAILABLE:
                action, reason = "conflict", "listener_ownership_unavailable"
            elif disposition is ListenerDisposition.CONFLICT:
                action, reason = "conflict", "listen_port_conflict"
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

    def _listener_disposition(
        self, desired: DesiredExitRuntime, state: ServiceState,
        listeners: tuple[Listener, ...],
    ) -> ListenerDisposition:
        matching = tuple(item for item in listeners if item.port == desired.listen_port)
        if not matching:
            return (
                ListenerDisposition.MISSING_FOR_ACTIVE_SERVICE
                if state.active
                else ListenerDisposition.FREE
            )
        for listener in matching:
            if listener.wildcard or listener.address not in {
                "127.0.0.1",
            }:
                return ListenerDisposition.CONFLICT
            if not listener.pids:
                return ListenerDisposition.OWNERSHIP_UNAVAILABLE
            if not state.active or state.main_pid is None:
                return ListenerDisposition.CONFLICT
            if listener.pids != (state.main_pid,):
                return ListenerDisposition.CONFLICT
        return ListenerDisposition.EXACT_SAME_SERVICE

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
        parse_manifest_document(candidate_manifest, self.paths)
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
        mutation_attempted: set[str] = set()
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
                    changed_units = True
                    self.runtime_store.write_atomic(self.paths.unit_file(action.exit_id), unit, 0o644)
                    self._fail("after_unit_replacement")
                elif action.action == "remove":
                    state = previous_states[action.exit_id]
                    if state.loaded or state.active or state.enabled:
                        mutation_attempted.add(action.exit_id)
                        self.inspector.systemctl("disable", "--now", action.service_name)
                    self._fail("after_stale_service_stop")
                    if self.runtime_store.read_optional(
                        self.paths.unit_file(action.exit_id), 64 * 1024
                    ) is not None or state.loaded:
                        changed_units = True
                    self.runtime_store.remove_exact(self.paths.unit_file(action.exit_id))
                    self.runtime_store.remove_exact(self.paths.env_file(action.exit_id))
                    removed.append(action.exit_id)
            if changed_units:
                self.inspector.systemctl("daemon-reload")
            self._fail("after_daemon_reload")
            for action in plan.actions:
                if action.action in {"create", "start"}:
                    mutation_attempted.add(action.exit_id)
                    self.inspector.systemctl("enable", action.service_name)
                    self.inspector.systemctl("start", action.service_name)
                    started.append(action.exit_id)
                    self._fail("after_new_service_enable")
                elif action.action == "update":
                    mutation_attempted.add(action.exit_id)
                    self.inspector.systemctl("enable", action.service_name)
                    self.inspector.systemctl("start", action.service_name)
                    started.append(action.exit_id)
                elif action.action == "restart":
                    mutation_attempted.add(action.exit_id)
                    self.inspector.systemctl("enable", action.service_name)
                    self.inspector.systemctl("restart", action.service_name)
                    restarted.append(action.exit_id)
                    self._fail("after_changed_service_restart")
            self._verify_applied_runtime(plan, verify_manifest=None)
            self._fail("after_listener_verification")
            self.runtime_store.write_atomic(self.paths.manifest_file, candidate_manifest)
            self._fail("after_manifest_replacement")
            self.runtime_store.fsync_directory(self.paths.generated_dir)
            self._fail("after_parent_fsync")
            self.runtime_store.prune_backups(exclude=backup)
            self._fail("after_backup_pruning")
            self._verify_applied_runtime(plan, verify_manifest=candidate_manifest)
            self._fail("after_final_verification")
            self.runtime_store.remove_backup_tree(backup)
            self._fail("after_current_backup_removal")
            self.runtime_store.fsync_backup_parent()
            self._fail("after_backup_parent_fsync")
            return ApplyResult(plan, tuple(restarted), tuple(started), tuple(removed), True)
        except Exception:
            try:
                self._rollback(
                    snapshots, previous_states, mutation_attempted, changed_units
                )
            except Exception as exc:
                if backup.exists():
                    detail = f"backup retained at {backup}"
                else:
                    detail = "recovery backup is unavailable"
                raise OperationalError(
                    f"gateway runtime rollback could not be verified; {detail}"
                ) from exc
            if backup.exists():
                try:
                    self.runtime_store.remove_backup(backup)
                except Exception as cleanup_error:
                    raise OperationalError(
                        f"gateway runtime rollback succeeded; backup retained at {backup}"
                    ) from cleanup_error
            raise

    def _verify_applied_runtime(
        self, plan: RuntimePlan, verify_manifest: bytes | None,
    ) -> None:
        for item in plan.desired:
            state = self.inspector.service_state(item.exit_id)
            if not state.loaded or not state.enabled or not state.active or state.main_pid is None:
                raise OperationalError("gateway service did not become active and enabled")
            self.inspector.verify_service_listener(
                item.listen_address, item.listen_port, state.main_pid
            )
        for action in plan.actions:
            if action.action != "remove":
                continue
            state = self.inspector.service_state(action.exit_id)
            if state.loaded or state.enabled or state.active or state.main_pid is not None:
                raise OperationalError("removed gateway service is still present")
        if verify_manifest is not None:
            installed = self.runtime_store.read_optional(
                self.paths.manifest_file, MAX_RUNTIME_MANIFEST_BYTES
            )
            if installed != verify_manifest:
                raise OperationalError("installed runtime manifest verification failed")
            parse_manifest_document(installed, self.paths)

    def _rollback(
        self, snapshots: tuple, previous_states: dict[str, ServiceState],
        mutation_attempted: set[str], changed_units: bool,
    ) -> None:
        for exit_id in sorted(mutation_attempted):
            self.inspector.systemctl("stop", service_name(exit_id))
        for exit_id in sorted(mutation_attempted):
            if not previous_states[exit_id].enabled:
                self.inspector.systemctl("disable", service_name(exit_id))
        self.runtime_store.restore(snapshots)
        if changed_units:
            self.inspector.systemctl("daemon-reload")
        for exit_id, state in sorted(previous_states.items()):
            name = service_name(exit_id)
            if state.enabled:
                self.inspector.systemctl("enable", name)
            if state.active:
                self.inspector.systemctl("start", name)
        snapshot_by_path = {item.path: item for item in snapshots}
        for exit_id, expected in sorted(previous_states.items()):
            restored = self.inspector.service_state(exit_id)
            if (
                restored.loaded != expected.loaded
                or restored.enabled != expected.enabled
                or restored.active != expected.active
                or (restored.active and restored.main_pid is None)
                or (not restored.active and restored.main_pid is not None)
            ):
                raise OperationalError("gateway rollback service-state verification failed")
            env_snapshot = snapshot_by_path.get(self.paths.env_file(exit_id))
            unit_snapshot = snapshot_by_path.get(self.paths.unit_file(exit_id))
            if (
                expected.active
                and expected.loaded
                and restored.main_pid is not None
                and env_snapshot is not None
                and env_snapshot.data is not None
                and unit_snapshot is not None
                and unit_snapshot.data is not None
            ):
                values = parse_env(env_snapshot.data, exit_id)
                self.inspector.verify_service_listener(
                    "127.0.0.1", int(values["GATEWAY_LISTEN_PORT"]),
                    restored.main_pid,
                )

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
            values: dict[str, str] | None = None
            previous: ServiceState | None = None
            document: RuntimeManifest | None = None
            secret_generation: int | None = None
            if action in {"start", "restart"}:
                self._validate_runtime_dependencies()
                document = self._load_manifest_document()
                if document is None:
                    raise StateError("gateway runtime manifest is missing")
                manifest = {item.exit_id: item for item in document.entries}
                entry = manifest.get(exit_id)
                if entry is None:
                    raise StateError("gateway runtime entry is missing")
                _credentials, secret_generation = self.secret_store.read(entry.secret_ref)
                env_data = self.runtime_store.read_optional(self.paths.env_file(exit_id), 64 * 1024)
                unit_data = self.runtime_store.read_optional(self.paths.unit_file(exit_id), 64 * 1024)
                if env_data is None or unit_data is None:
                    raise StateError("generated gateway runtime is incomplete")
                if sha256(env_data) != entry.env_sha256 or sha256(unit_data) != entry.unit_sha256:
                    raise StateError("generated gateway runtime does not match its manifest")
                values = parse_env(env_data, exit_id)
                listeners = self.inspector.listeners()
                previous = self.inspector.service_state(exit_id)
                desired = DesiredExitRuntime(
                    exit_id, service_name(exit_id), "127.0.0.1",
                    int(values["GATEWAY_LISTEN_PORT"]), values["GATEWAY_EXIT_HOST"],
                    int(values["GATEWAY_SOCKS_PORT"]), "127.0.0.1",
                    int(values["GATEWAY_TARGET_PORT"]),
                    entry.secret_ref, secret_generation,
                )
                validate_unit(unit_data, desired, self.paths)
                disposition = self._listener_disposition(desired, previous, listeners)
                if disposition not in {
                    ListenerDisposition.FREE,
                    ListenerDisposition.EXACT_SAME_SERVICE,
                    ListenerDisposition.MISSING_FOR_ACTIVE_SERVICE,
                }:
                    raise ConflictError("gateway service listen port conflicts")
            try:
                self.inspector.systemctl(action, service_name(exit_id))
                result = self.inspector.service_state(exit_id)
                if action in {"start", "restart"}:
                    self._verify_control_active(exit_id, values, result)
            except Exception:
                if action in {"start", "restart"} and previous is not None:
                    try:
                        self._recover_service_control(exit_id, values, previous)
                    except Exception as recovery_error:
                        raise OperationalError(
                            "gateway service operation failed and recovery could not be verified"
                        ) from recovery_error
                raise
            if (
                action == "restart"
                and document is not None
                and secret_generation is not None
            ):
                self._update_manifest_generation(
                    document, exit_id, secret_generation
                )
            return result

    def _verify_control_active(
        self, exit_id: str, values: dict[str, str] | None, state: ServiceState,
    ) -> None:
        if values is None or not state.loaded or not state.active or state.main_pid is None:
            raise OperationalError("gateway service did not become active")
        self.inspector.verify_service_listener(
            "127.0.0.1", int(values["GATEWAY_LISTEN_PORT"]), state.main_pid
        )

    def _recover_service_control(
        self, exit_id: str, values: dict[str, str] | None, previous: ServiceState,
    ) -> None:
        name = service_name(exit_id)
        self.inspector.systemctl("stop", name)
        if previous.active:
            self.inspector.systemctl("start", name)
            restored = self.inspector.service_state(exit_id)
            self._verify_control_active(exit_id, values, restored)
            if restored.enabled != previous.enabled:
                raise OperationalError("gateway service enabled state changed during recovery")
            return
        restored = self.inspector.service_state(exit_id)
        if (
            restored.loaded != previous.loaded
            or restored.enabled != previous.enabled
            or restored.active
            or restored.main_pid is not None
        ):
            raise OperationalError("gateway inactive service state was not restored")

    def _update_manifest_generation(
        self, document: RuntimeManifest, exit_id: str, generation: int,
    ) -> None:
        updated: list[RuntimeEntry] = []
        found = False
        for entry in document.entries:
            if entry.exit_id == exit_id:
                entry = replace(entry, secret_mtime_ns=generation)
                found = True
            updated.append(entry)
        if not found:
            raise StateError("gateway runtime entry is missing")
        data = render_manifest(
            applied_at=document.applied_at,
            document_id=document.document_id,
            shared_revision=document.shared_revision,
            node_revision=document.node_revision,
            entries=tuple(updated),
        )
        parse_manifest_document(data, self.paths)
        self.runtime_store.write_atomic(self.paths.manifest_file, data)
        self._fail("after_service_manifest_replacement")
        self.runtime_store.fsync_directory(self.paths.generated_dir)
        installed = self.runtime_store.read_optional(
            self.paths.manifest_file, MAX_RUNTIME_MANIFEST_BYTES
        )
        if installed != data:
            raise OperationalError("gateway runtime manifest update was not durable")
        parse_manifest_document(installed, self.paths)
