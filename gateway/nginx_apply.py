"""Plan, apply, verify, and roll back the dedicated NGINX Gateway."""

from __future__ import annotations

import datetime as dt
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

from gateway.errors import ConflictError, OperationalError, StateError, ValidationError
from gateway.locking import NginxGatewayLock
from gateway.models import StatePair
from gateway.nginx_inspection import NginxInspector, listener_owned_by_service
from gateway.nginx_dependency import NginxDependencyManager
from gateway.nginx_manifest import parse_manifest, render_manifest
from gateway.nginx_models import (
    MAX_NGINX_CONFIG_BYTES,
    MAX_NGINX_MANIFEST_BYTES,
    NginxApplyResult,
    NginxCandidate,
    NginxManifest,
    NginxPlan,
    NginxServiceState,
)
from gateway.nginx_paths import NGINX_SERVICE_NAME, NginxPaths
from gateway.nginx_readiness import GostBackendReadiness
from gateway.nginx_render import build_candidate, render_config
from gateway.nginx_store import NginxFileSnapshot, NginxStore
from gateway.paths import reject_symlink_components
from gateway.runtime_models import Listener
from gateway.runtime_paths import RuntimePaths
from gateway.runtime_render import sha256
from gateway.secrets import SecretStore
from gateway.store import GatewayStateStore
from gateway.validation import validate_pair


FAILURE_PHASES = (
    "after_state_lock_acquisition",
    "after_runtime_lock_acquisition",
    "after_nginx_lock_acquisition",
    "after_state_read",
    "after_backend_readiness",
    "after_dependency_validation",
    "after_current_manifest_read",
    "after_listener_snapshot",
    "after_service_state_capture",
    "after_candidate_render",
    "after_static_validation",
    "after_staged_nginx_test",
    "after_staging",
    "after_backup_creation",
    "after_config_replacement",
    "after_installed_nginx_test",
    "after_service_enable",
    "after_service_start",
    "after_reload_command",
    "after_public_listener_verification",
    "after_status_listener_verification",
    "after_status_probe",
    "after_manifest_replacement",
    "after_parent_fsync",
    "after_backup_pruning",
    "after_final_verification",
    "after_current_backup_removal",
    "after_backup_parent_fsync",
    "after_rollback_config_restore",
    "after_rollback_previous_config_test",
    "after_rollback_reload",
    "after_rollback_service_state_restore",
    "after_rollback_listener_verification",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class NginxManager:
    def __init__(
        self,
        state_store: GatewayStateStore,
        secret_store: SecretStore,
        runtime_paths: RuntimePaths,
        paths: NginxPaths,
        *,
        inspector: NginxInspector | None = None,
        store: NginxStore | None = None,
        backend_readiness: GostBackendReadiness | None = None,
        clock: Callable[[], str] = _utc_now,
        lock_timeout: float = 5.0,
        lock_factory: Callable[[Path, float], NginxGatewayLock] | None = None,
        failure_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.state_store = state_store
        self.secret_store = secret_store
        self.runtime_paths = runtime_paths
        self.paths = paths
        self.inspector = inspector or NginxInspector()
        self.store = store or NginxStore(paths)
        self.backend_readiness = backend_readiness or GostBackendReadiness(
            runtime_paths, secret_store
        )
        self.clock = clock
        self.lock_timeout = lock_timeout
        self.lock_factory = lock_factory or (
            lambda path, timeout: NginxGatewayLock(path, timeout=timeout)
        )
        self.failure_hook = failure_hook

    def _fail(self, phase: str) -> None:
        if self.failure_hook is not None:
            self.failure_hook(phase)

    def _lock(self) -> NginxGatewayLock:
        return self.lock_factory(self.paths.lock_file, self.lock_timeout)

    def plan(self) -> NginxPlan:
        with self.state_store.locked_pair() as pair:
            self._fail("after_state_lock_acquisition")
            with self.secret_store.lock():
                self._fail("after_runtime_lock_acquisition")
                with self._lock():
                    self._fail("after_nginx_lock_acquisition")
                    return self._plan_locked(pair)

    def _binary_status(self) -> tuple[bool, str]:
        reject_symlink_components(self.paths.nginx_bin.parent)
        try:
            metadata = self.paths.nginx_bin.lstat()
        except FileNotFoundError:
            return False, "nginx_binary_missing"
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not metadata.st_mode & 0o111
        ):
            return False, "nginx_binary_unsafe"
        return True, ""

    @staticmethod
    def _safe_conflict_plan(pair: StatePair, action: str, reason: str) -> NginxPlan:
        gateway = pair.shared.gateway
        routes = tuple(item for item in pair.shared.routes if item.enabled)
        return NginxPlan(
            action,
            (reason,),
            gateway.id,
            "unknown",
            "active" if gateway.enabled else "absent",
            gateway.listen_address,
            gateway.listen_port,
            gateway.status_port,
            len(routes),
            sum(len(item.exit_ids) for item in routes),
            False,
            False,
            tuple(item.id for item in routes),
            tuple(sorted({exit_id for item in routes for exit_id in item.exit_ids})),
        )

    def _plan_locked(self, pair: StatePair) -> NginxPlan:
        self._fail("after_state_read")
        gateway = pair.shared.gateway
        state = self.inspector.service_state()
        self._fail("after_service_state_capture")
        listeners = self.inspector.listeners()
        self._fail("after_listener_snapshot")
        try:
            current_config, current_manifest_data = self.store.inspect_owned()
            self._fail("after_current_manifest_read")
        except (ConflictError, StateError, ValidationError) as exc:
            return self._safe_conflict_plan(pair, "conflict", str(exc))
        current_manifest = (
            parse_manifest(current_manifest_data, self.paths)
            if current_manifest_data is not None
            else None
        )
        if not gateway.enabled:
            present = current_config is not None or current_manifest_data is not None
            action = "stop-remove" if present or state.loaded or state.active or state.enabled else "no-op"
            return NginxPlan(
                action,
                ("gateway_disabled",) if action == "stop-remove" else ("runtime_absent",),
                gateway.id,
                "active" if state.active else "inactive",
                "absent",
                gateway.listen_address,
                gateway.listen_port,
                gateway.status_port,
                0,
                0,
                present,
                present,
                (),
                (),
            )
        try:
            validate_pair(pair, runtime_ready=True)
        except ValidationError as exc:
            return self._safe_conflict_plan(pair, "conflict", str(exc))
        dependency_ok, dependency_reason = self._binary_status()
        self._fail("after_dependency_validation")
        if not dependency_ok:
            action = "dependency-missing" if dependency_reason == "nginx_binary_missing" else "conflict"
            return self._safe_conflict_plan(pair, action, dependency_reason)
        enabled_routes = tuple(item for item in pair.shared.routes if item.enabled)
        exits = {item.id: item for item in pair.shared.exits}
        bindings = {item.exit_id: item for item in pair.node.bindings}
        exit_ids = {
            exit_id
            for route in enabled_routes
            for exit_id in route.exit_ids
            if exits[exit_id].enabled
            and exit_id in bindings
            and bindings[exit_id].enabled
            and bool(bindings[exit_id].secret_ref)
        }
        try:
            ready = self.backend_readiness.ready_ports(pair, exit_ids, listeners)
            self._fail("after_backend_readiness")
            candidate = build_candidate(pair, ready)
            config = render_config(candidate, str(self.paths.pid_file))
            self._fail("after_candidate_render")
            self._fail("after_static_validation")
            reason = self._port_conflict_reason(candidate, state, listeners)
            if reason:
                return self._safe_conflict_plan(pair, "conflict", reason)
        except (ConflictError, StateError, ValidationError, OperationalError) as exc:
            return self._safe_conflict_plan(pair, "conflict", str(exc))
        applied_at = self._manifest_time(candidate, config, current_manifest)
        manifest_data = render_manifest(candidate, config, self.paths, applied_at)
        config_changed = current_config != config
        manifest_changed = current_manifest_data != manifest_data
        listener_health = self._expected_listener_health(candidate, state, listeners)
        status_ok = False
        if state.active and listener_health:
            try:
                self.inspector.status(candidate.status_port)
                status_ok = True
            except (OperationalError, ValidationError):
                status_ok = False
        if not state.active:
            action, reasons = (
                ("create", ("first_activation",))
                if current_config is None or current_manifest_data is None or config_changed
                else ("start", ("service_inactive",))
            )
        elif config_changed:
            action, reasons = "reload", ("effective_config_changed",)
        elif not listener_health or not status_ok:
            action, reasons = "reload", ("active_listener_or_status_missing",)
        elif manifest_changed:
            action, reasons = "metadata-update", ("manifest_metadata_changed",)
        elif not state.enabled:
            action, reasons = "start", ("service_disabled",)
        else:
            action, reasons = "no-op", ("runtime_matches",)
        return NginxPlan(
            action,
            reasons,
            gateway.id,
            "active" if state.active else "inactive",
            "active",
            candidate.listen_address,
            candidate.listen_port,
            candidate.status_port,
            len(candidate.routes),
            sum(len(item.backends) for item in candidate.routes),
            config_changed,
            manifest_changed,
            tuple(item.route_id for item in candidate.routes),
            tuple(sorted({backend.exit_id for route in candidate.routes for backend in route.backends})),
            config,
            manifest_data,
        )

    def _manifest_time(
        self,
        candidate: NginxCandidate,
        config: bytes,
        current: NginxManifest | None,
    ) -> str:
        if (
            current is not None
            and current.document_id == candidate.document_id
            and current.shared_revision == candidate.shared_revision
            and current.node_revision == candidate.node_revision
            and current.config_sha256 == sha256(config)
        ):
            return current.applied_at
        return self.clock()

    def _port_conflict_reason(
        self,
        candidate: NginxCandidate,
        state: NginxServiceState,
        listeners: tuple[Listener, ...],
    ) -> str:
        for listener in listeners:
            public_relevant = listener.port == candidate.listen_port and (
                candidate.listen_address == "0.0.0.0"
                or listener.wildcard
                or listener.address == candidate.listen_address
            )
            status_relevant = listener.port == candidate.status_port and (
                listener.wildcard or listener.address == "127.0.0.1"
            )
            if not public_relevant and not status_relevant:
                continue
            if not listener_owned_by_service(listener, state):
                return "listener_port_conflict_or_unknown_owner"
        return ""

    @staticmethod
    def _expected_listener_health(
        candidate: NginxCandidate,
        state: NginxServiceState,
        listeners: tuple[Listener, ...],
    ) -> bool:
        public = [
            item for item in listeners
            if item.address == candidate.listen_address and item.port == candidate.listen_port
        ]
        status = [
            item for item in listeners
            if item.address == "127.0.0.1" and item.port == candidate.status_port
        ]
        return bool(
            public and status
            and all(listener_owned_by_service(item, state) for item in (*public, *status))
        )

    def apply(self, *, yes: bool) -> NginxApplyResult:
        if not yes:
            raise ConflictError("NGINX apply requires explicit confirmation")
        with self.state_store.locked_pair() as pair:
            self._fail("after_state_lock_acquisition")
            with self.secret_store.lock():
                self._fail("after_runtime_lock_acquisition")
                with self._lock():
                    self._fail("after_nginx_lock_acquisition")
                    plan = self._plan_locked(pair)
                    if plan.has_conflict:
                        raise ConflictError("NGINX plan contains a dependency or ownership conflict")
                    return self._apply_locked(pair, plan)

    def _stage_and_test(self, config: bytes) -> None:
        with tempfile.TemporaryDirectory(prefix="gost-nginx-candidate-") as temporary:
            directory = Path(temporary)
            os.chmod(directory, 0o700)
            candidate = directory / "nginx.conf"
            candidate.write_bytes(config)
            os.chmod(candidate, 0o600)
            self._fail("after_staging")
            self.inspector.nginx_test(self.paths.nginx_bin, candidate)
            self._fail("after_staged_nginx_test")

    def _apply_locked(self, pair: StatePair, plan: NginxPlan) -> NginxApplyResult:
        if plan.action == "no-op":
            return NginxApplyResult(plan, False)
        if plan.action == "stop-remove":
            return self._disable_locked(plan)
        if plan.config is None or plan.manifest is None:
            raise OperationalError("NGINX plan omitted candidate bytes")
        if plan.action in {"create", "reload"}:
            self._stage_and_test(plan.config)
        self.store.prepare()
        snapshots = self.store.snapshot()
        previous_state = self.inspector.service_state()
        backup = self.store.create_backup(snapshots)
        self._fail("after_backup_creation")
        reload_count = 0
        try:
            if plan.action in {"create", "reload"} and plan.config_changed:
                self.store.write_atomic(self.paths.config_file, plan.config)
                self._fail("after_config_replacement")
                self.inspector.nginx_test(self.paths.nginx_bin, self.paths.config_file)
                self._fail("after_installed_nginx_test")
            if plan.action in {"create", "start"}:
                if not previous_state.enabled:
                    self.inspector.systemctl("enable")
                    self._fail("after_service_enable")
                if not previous_state.active:
                    self.inspector.systemctl("start")
                    self._fail("after_service_start")
            elif plan.action == "reload":
                self.inspector.systemctl("reload")
                reload_count = 1
                self._fail("after_reload_command")
            self._verify_active(plan, previous_state.main_pid if plan.action == "reload" else None)
            self.store.write_atomic(self.paths.manifest_file, plan.manifest)
            self._fail("after_manifest_replacement")
            self.store.fsync_directory(self.paths.generated_dir)
            self._fail("after_parent_fsync")
            self.store.prune_backups(exclude=backup)
            self._fail("after_backup_pruning")
            self._verify_files(plan)
            self._fail("after_final_verification")
            self.store.remove_backup(backup)
            self._fail("after_current_backup_removal")
            self.store.fsync_directory(self.paths.backup_dir)
            self._fail("after_backup_parent_fsync")
            return NginxApplyResult(plan, True, reload_count, 0)
        except Exception:
            try:
                self._rollback(plan, snapshots, previous_state)
            except Exception as rollback_error:
                raise OperationalError(
                    f"NGINX rollback could not be verified; backup retained at {backup}"
                ) from rollback_error
            if backup.exists():
                self.store.remove_backup(backup)
            raise

    def _disable_locked(self, plan: NginxPlan) -> NginxApplyResult:
        snapshots = self.store.snapshot()
        previous_state = self.inspector.service_state()
        previous_manifest_data = next(
            (item.data for item in snapshots if item.path == self.paths.manifest_file),
            None,
        )
        previous_manifest = (
            parse_manifest(previous_manifest_data, self.paths)
            if previous_manifest_data is not None
            else None
        )
        backup = self.store.create_backup(snapshots)
        try:
            if previous_state.active:
                self.inspector.systemctl("stop")
            if previous_state.enabled:
                self.inspector.systemctl("disable")
            self.store.remove_exact(self.paths.manifest_file)
            self.store.remove_exact(self.paths.config_file)
            state = self.inspector.service_state()
            if state.active or state.enabled:
                raise OperationalError("disabled NGINX Gateway service remains active or enabled")
            listeners = self.inspector.listeners()
            if previous_manifest is not None and any(
                item.pids
                and set(item.pids).intersection(previous_state.pids)
                and (
                    (item.address == previous_manifest.listen_address and item.port == previous_manifest.listen_port)
                    or (item.address == "127.0.0.1" and item.port == previous_manifest.status_port)
                )
                for item in listeners
            ):
                raise OperationalError("disabled NGINX Gateway listener was not released")
            self.store.remove_backup(backup)
            return NginxApplyResult(plan, True)
        except Exception:
            try:
                self._rollback(plan, snapshots, previous_state)
            except Exception as rollback_error:
                raise OperationalError(
                    f"NGINX disable rollback could not be verified; backup retained at {backup}"
                ) from rollback_error
            if backup.exists():
                self.store.remove_backup(backup)
            raise

    def _verify_active(self, plan: NginxPlan, expected_main_pid: int | None) -> None:
        state = self.inspector.service_state()
        if not (
            state.loaded and state.enabled and state.active and state.main_pid is not None
            and state.pids_authoritative and state.pids and state.main_pid in state.pids
        ):
            raise OperationalError("dedicated NGINX Gateway service is not healthy")
        if expected_main_pid is not None and state.main_pid != expected_main_pid:
            raise OperationalError("NGINX graceful reload changed the master PID")
        listeners = self.inspector.listeners()
        public = [
            item for item in listeners
            if item.address == plan.listen_address and item.port == plan.listen_port
        ]
        if not public or any(not listener_owned_by_service(item, state) for item in public):
            raise OperationalError("NGINX public listener ownership verification failed")
        unexpected_public = [
            item for item in listeners
            if item.port == plan.listen_port
            and item.address != plan.listen_address
            and listener_owned_by_service(item, state)
        ]
        if unexpected_public:
            raise OperationalError("NGINX added an unexpected public listener")
        self._fail("after_public_listener_verification")
        status = [
            item for item in listeners
            if item.address == "127.0.0.1" and item.port == plan.status_port
        ]
        if not status or any(not listener_owned_by_service(item, state) for item in status):
            raise OperationalError("NGINX status listener ownership verification failed")
        self._fail("after_status_listener_verification")
        self.inspector.status(plan.status_port)
        self._fail("after_status_probe")

    def _verify_files(self, plan: NginxPlan) -> None:
        config, manifest = self.store.inspect_owned()
        if config != plan.config or manifest != plan.manifest:
            raise OperationalError("installed NGINX runtime bytes do not match the candidate")

    def _rollback(
        self,
        plan: NginxPlan,
        snapshots: tuple[NginxFileSnapshot, ...],
        previous: NginxServiceState,
    ) -> None:
        if not previous.active:
            current = self.inspector.service_state()
            if current.active:
                self.inspector.systemctl("stop")
            if not previous.enabled and current.enabled:
                self.inspector.systemctl("disable")
        self.store.restore(snapshots)
        self._fail("after_rollback_config_restore")
        previous_config = next(
            (item.data for item in snapshots if item.path == self.paths.config_file), None
        )
        if previous_config is not None:
            self.inspector.nginx_test(self.paths.nginx_bin, self.paths.config_file)
            self._fail("after_rollback_previous_config_test")
        if previous.active:
            current = self.inspector.service_state()
            if current.active:
                self.inspector.systemctl("reload")
                self._fail("after_rollback_reload")
            else:
                self.inspector.systemctl("start")
        self._restore_service_state(previous)
        self._fail("after_rollback_service_state_restore")
        restored = self.inspector.service_state()
        if (
            restored.enabled != previous.enabled
            or restored.active != previous.active
            or restored.loaded != previous.loaded
            or (restored.active and restored.main_pid is None)
        ):
            raise OperationalError("NGINX rollback service-state verification failed")
        if previous.active:
            manifest_data = next(
                (item.data for item in snapshots if item.path == self.paths.manifest_file), None
            )
            if manifest_data is None:
                raise OperationalError("previous active NGINX manifest is unavailable")
            manifest = parse_manifest(manifest_data, self.paths)
            rollback_plan = NginxPlan(
                "rollback", (), plan.gateway_id, "active", "active",
                manifest.listen_address, manifest.listen_port, manifest.status_port,
                len(manifest.routes), sum(len(item.backend_exit_ids) for item in manifest.routes),
                False, False, (), (), previous_config, manifest_data,
            )
            self._verify_active(
                rollback_plan,
                previous.main_pid if plan.action == "reload" else None,
            )
            self._fail("after_rollback_listener_verification")

    def _restore_service_state(self, previous: NginxServiceState) -> None:
        current = self.inspector.service_state()
        if previous.enabled and not current.enabled:
            self.inspector.systemctl("enable")
        elif not previous.enabled and current.enabled:
            self.inspector.systemctl("disable")
        current = self.inspector.service_state()
        if previous.active and not current.active:
            self.inspector.systemctl("start")
        elif not previous.active and current.active:
            self.inspector.systemctl("stop")

    def test_installed(self) -> None:
        with self._lock():
            config, _manifest = self.store.inspect_owned()
            if config is None:
                raise StateError("managed NGINX configuration is missing")
            self.inspector.nginx_test(self.paths.nginx_bin, self.paths.config_file)

    def status(self) -> dict[str, object]:
        with self.state_store.locked_pair() as pair:
            with self.secret_store.lock():
                with self._lock():
                    state = self.inspector.service_state()
                    listeners = self.inspector.listeners()
                    config, manifest_data = self.store.inspect_owned()
                    manifest = parse_manifest(manifest_data, self.paths) if manifest_data else None
                    stub = None
                    reason_codes: list[str] = []
                    config_valid = False
                    if config is not None:
                        try:
                            self.inspector.nginx_test(
                                self.paths.nginx_bin, self.paths.config_file
                            )
                            config_valid = True
                        except (OperationalError, ValidationError):
                            reason_codes.append("installed_config_invalid")
                    if state.active and manifest is not None:
                        try:
                            stub = self.inspector.status(manifest.status_port)
                        except (OperationalError, ValidationError):
                            reason_codes.append("status_probe_failed")
                    public_owned = False
                    status_owned = False
                    if manifest is not None:
                        public_owned = any(
                            item.address == manifest.listen_address
                            and item.port == manifest.listen_port
                            and listener_owned_by_service(item, state)
                            for item in listeners
                        )
                        status_owned = any(
                            item.address == "127.0.0.1"
                            and item.port == manifest.status_port
                            and listener_owned_by_service(item, state)
                            for item in listeners
                        )
                    if state.active and not public_owned:
                        reason_codes.append("public_listener_unavailable")
                    if state.active and not status_owned:
                        reason_codes.append("status_listener_unavailable")
                    return {
                        "gateway_desired_enabled": pair.shared.gateway.enabled,
                        "shared_revision": pair.shared.revision,
                        "node_revision": pair.node.revision,
                        "applied_shared_revision": manifest.shared_revision if manifest else None,
                        "applied_node_revision": manifest.node_revision if manifest else None,
                        "service": state,
                        "master_pid": state.main_pid,
                        "authoritative_pid_count": len(state.pids) if state.pids_authoritative else 0,
                        "public_listener_owned": public_owned,
                        "status_listener_owned": status_owned,
                        "listen_address": manifest.listen_address if manifest else pair.shared.gateway.listen_address,
                        "listen_port": manifest.listen_port if manifest else pair.shared.gateway.listen_port,
                        "status_address": "127.0.0.1",
                        "status_port": manifest.status_port if manifest else pair.shared.gateway.status_port,
                        "config_present": config is not None,
                        "config_valid": config_valid,
                        "manifest_valid": manifest is not None,
                        "config_drift": bool(
                            config is not None
                            and manifest is not None
                            and sha256(config) != manifest.config_sha256
                        ),
                        "route_count": len(manifest.routes) if manifest else 0,
                        "backend_count": (
                            sum(len(item.backend_exit_ids) for item in manifest.routes)
                            if manifest else 0
                        ),
                        "stub_status": stub,
                        "last_applied_at": manifest.applied_at if manifest else None,
                        "reason_codes": tuple(reason_codes),
                    }

    def service_control(
        self,
        action: str,
        *,
        yes: bool = False,
        acknowledge_disconnect: bool = False,
    ) -> NginxServiceState:
        if action in {"stop", "reload", "restart"} and not yes:
            raise ConflictError(f"NGINX service {action} requires explicit confirmation")
        if action == "restart" and not acknowledge_disconnect:
            raise ConflictError("NGINX restart requires disconnect acknowledgement")
        if action == "status":
            return self.inspector.service_state()
        if action in {"start", "restart"}:
            with self.state_store.locked_pair() as pair:
                with self.secret_store.lock():
                    with self._lock():
                        self._validate_service_start_locked(pair)
                        return self._service_control_locked(
                            action,
                            yes=yes,
                            acknowledge_disconnect=acknowledge_disconnect,
                        )
        with self._lock():
            return self._service_control_locked(
                action,
                yes=yes,
                acknowledge_disconnect=acknowledge_disconnect,
            )

    def _validate_service_start_locked(self, pair: StatePair) -> None:
        validate_pair(pair, runtime_ready=True)
        config, manifest_data = self.store.inspect_owned()
        if config is None or manifest_data is None:
            raise StateError("managed NGINX runtime is incomplete")
        manifest = parse_manifest(manifest_data, self.paths)
        listeners = self.inspector.listeners()
        routes = tuple(item for item in pair.shared.routes if item.enabled)
        exits = {item.id: item for item in pair.shared.exits}
        bindings = {item.exit_id: item for item in pair.node.bindings}
        exit_ids = {
            exit_id
            for route in routes
            for exit_id in route.exit_ids
            if exits[exit_id].enabled
            and exit_id in bindings
            and bindings[exit_id].enabled
            and bool(bindings[exit_id].secret_ref)
        }
        ready = self.backend_readiness.ready_ports(pair, exit_ids, listeners)
        candidate = build_candidate(pair, ready)
        desired_config = render_config(candidate, str(self.paths.pid_file))
        if config != desired_config or sha256(config) != manifest.config_sha256:
            raise ConflictError("managed NGINX runtime is stale relative to desired state")
        current = self.inspector.service_state()
        reason = self._port_conflict_reason(candidate, current, listeners)
        if reason:
            raise ConflictError(reason)

    def _service_control_locked(
        self,
        action: str,
        *,
        yes: bool,
        acknowledge_disconnect: bool,
    ) -> NginxServiceState:
        _ = yes, acknowledge_disconnect
        config, manifest_data = self.store.inspect_owned()
        if config is None or manifest_data is None:
            raise StateError("managed NGINX runtime is incomplete")
        manifest = parse_manifest(manifest_data, self.paths)
        self.inspector.nginx_test(self.paths.nginx_bin, self.paths.config_file)
        previous = self.inspector.service_state()
        if action == "start" and not previous.enabled:
            self.inspector.systemctl("enable")
        self.inspector.systemctl(action)
        current = self.inspector.service_state()
        if action in {"start", "reload", "restart"}:
            plan = NginxPlan(
                action, (), "gateway", "unknown", "active",
                manifest.listen_address, manifest.listen_port, manifest.status_port,
                len(manifest.routes), sum(len(item.backend_exit_ids) for item in manifest.routes),
                False, False, (), (), config, manifest_data,
            )
            self._verify_active(
                plan, previous.main_pid if action == "reload" else None
            )
            current = self.inspector.service_state()
        elif action == "stop" and current.active:
            raise OperationalError("NGINX Gateway service did not stop")
        return current

    def dependency_status(self):
        with self._lock():
            return NginxDependencyManager(self.paths, self.inspector).status()

    def dependency_install(self, *, yes: bool, is_root: bool | None = None) -> str:
        with self._lock():
            return NginxDependencyManager(self.paths, self.inspector).install(
                yes=yes, is_root=is_root
            )
