"""Read-only batch readiness proof for route-local GOST Exit backends."""

from __future__ import annotations

from gateway.errors import ConflictError
from gateway.models import StatePair
from gateway.runtime_inspection import RuntimeInspector
from gateway.runtime_models import DesiredExitRuntime, Listener
from gateway.runtime_paths import RuntimePaths, service_name
from gateway.runtime_render import (
    MAX_RUNTIME_MANIFEST_BYTES,
    make_entry,
    parse_manifest,
    render_env,
    render_unit,
    sha256,
)
from gateway.runtime_store import RuntimeStore
from gateway.secrets import SecretStore


class GostBackendReadiness:
    """Prove selected backends while the caller holds state/runtime locks."""

    def __init__(
        self,
        paths: RuntimePaths,
        secret_store: SecretStore,
        *,
        inspector: RuntimeInspector | None = None,
        runtime_store: RuntimeStore | None = None,
    ) -> None:
        self.paths = paths
        self.secret_store = secret_store
        self.inspector = inspector or RuntimeInspector()
        self.store = runtime_store or RuntimeStore(paths)

    def ready_ports(
        self,
        pair: StatePair,
        exit_ids: set[str],
        listeners: tuple[Listener, ...],
    ) -> dict[str, int]:
        exits = {item.id: item for item in pair.shared.exits}
        bindings = {item.exit_id: item for item in pair.node.bindings}
        desired: dict[str, DesiredExitRuntime] = {}
        for exit_id in sorted(exit_ids):
            exit_node = exits.get(exit_id)
            binding = bindings.get(exit_id)
            if (
                exit_node is None
                or binding is None
                or not exit_node.enabled
                or not binding.enabled
                or binding.listen_address != "127.0.0.1"
                or not binding.secret_ref
            ):
                raise ConflictError(f"backend_not_ready:{exit_id}:binding_missing")
            _credentials, generation = self.secret_store.read(binding.secret_ref)
            desired[exit_id] = DesiredExitRuntime(
                exit_id,
                service_name(exit_id),
                "127.0.0.1",
                binding.listen_port,
                exit_node.host,
                exit_node.socks_port,
                "127.0.0.1",
                exit_node.target_port,
                binding.secret_ref,
                generation,
            )
        manifest_data = self.store.read_optional(
            self.paths.manifest_file, MAX_RUNTIME_MANIFEST_BYTES
        )
        if manifest_data is None:
            raise ConflictError("backend_not_ready:runtime_manifest_missing")
        manifest = parse_manifest(manifest_data, self.paths)
        result: dict[str, int] = {}
        for exit_id in sorted(exit_ids):
            item = desired.get(exit_id)
            entry = manifest.get(exit_id)
            if item is None or entry is None:
                raise ConflictError(f"backend_not_ready:{exit_id}:runtime_missing")
            env = self.store.read_optional(self.paths.env_file(exit_id), 64 * 1024)
            unit = self.store.read_optional(self.paths.unit_file(exit_id), 64 * 1024)
            expected_env = render_env(item)
            expected_unit = render_unit(item, self.paths)
            expected_entry = make_entry(item, self.paths, expected_env, expected_unit)
            if (
                env != expected_env
                or unit != expected_unit
                or entry != expected_entry
                or sha256(env or b"") != entry.env_sha256
                or sha256(unit or b"") != entry.unit_sha256
            ):
                raise ConflictError(f"backend_not_ready:{exit_id}:runtime_stale")
            state = self.inspector.service_state(exit_id)
            if not (
                state.loaded
                and state.enabled
                and state.active
                and state.main_pid is not None
                and state.pids_authoritative
                and state.pids
                and state.main_pid in state.pids
            ):
                raise ConflictError(f"backend_not_ready:{exit_id}:service_state")
            matching = tuple(
                listener
                for listener in listeners
                if listener.address == "127.0.0.1" and listener.port == item.listen_port
            )
            if not matching or any(
                not listener.pids
                or not set(listener.pids).issubset(set(state.pids))
                for listener in matching
            ):
                raise ConflictError(f"backend_not_ready:{exit_id}:listener_ownership")
            result[exit_id] = item.listen_port
        return result
