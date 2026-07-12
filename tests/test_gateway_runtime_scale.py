from __future__ import annotations

import tempfile
import time
import tracemalloc
import unittest
import os
from dataclasses import dataclass
from pathlib import Path

from gateway.models import Binding, ExitNode, NodeState, SharedState, StatePair
from gateway.paths import StatePaths
from gateway.runtime_apply import RuntimeManager
from gateway.runtime_models import RuntimeEntry, ServiceState
from gateway.runtime_paths import RuntimePaths
from gateway.runtime_render import make_entry, render_env, render_manifest, render_unit
from gateway.serialization import serialize_node, serialize_shared
from gateway.secrets import SecretStore
from gateway.store import GatewayStateStore
from test_gateway_support import make_pair


def credentials():
    from gateway.runtime_models import Credentials
    suffix = os.urandom(8).hex()
    return Credentials(f"test-user-{suffix}", f"test-pass-{suffix}")


class ScaleInspector:
    def listeners(self):
        return ()

    def discover_service_ids(self):
        return frozenset()

    def service_state(self, exit_id: str) -> ServiceState:
        from gateway.runtime_paths import service_name
        return ServiceState(service_name(exit_id), False, False, False, None)


@dataclass(frozen=True)
class ScaleResult:
    exits: int
    actions: int
    duration_seconds: float
    peak_bytes: int
    manifest_bytes: int


def benchmark_256_runtime() -> ScaleResult:
    base = make_pair()
    exits = tuple(
        ExitNode(
            id=f"exit-{index:03d}", display_name=f"Exit {index}", enabled=True,
            host=f"node-{index}.example.org", socks_port=20000 + index,
            target_port=30000 + index,
        )
        for index in range(256)
    )
    bindings = tuple(
        Binding(
            exit_id=item.id, enabled=True, listen_address="127.0.0.1",
            listen_port=30000 + index, secret_ref=f"secret-{index:03d}",
        )
        for index, item in enumerate(exits)
    )
    pair = StatePair(
        SharedState(
            base.shared.schema_version, base.shared.document_id, base.shared.revision,
            base.shared.updated_at, base.shared.gateway, exits, (),
        ),
        NodeState(
            base.node.schema_version, base.node.document_id, base.node.node_id,
            base.node.revision, base.node.updated_at, bindings,
        ),
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "systemd").mkdir()
        paths = RuntimePaths.from_values(
            root / "secrets", root / "generated", root / "backups", root / "runtime.lock",
            root / "systemd", root / "runner", root / "gost",
        )
        for dependency in (paths.runner_path, paths.gost_bin):
            dependency.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
            dependency.chmod(0o755)
        state_paths = StatePaths.from_values(
            root / "state.json", root / "node.json", root / "state-backups", root / "state.lock"
        )
        state_paths.state_file.write_bytes(serialize_shared(pair.shared))
        state_paths.node_file.write_bytes(serialize_node(pair.node))
        secret_store = SecretStore(paths)
        for index in range(256):
            secret_store.set(f"secret-{index:03d}", credentials())
        manager = RuntimeManager(
            GatewayStateStore(state_paths), secret_store, paths,
            inspector=ScaleInspector(), verify_units=False,
        )
        tracemalloc.start()
        started = time.monotonic()
        plan = manager.plan()
        entries: list[RuntimeEntry] = []
        for item in plan.desired:
            env, unit = render_env(item), render_unit(item, paths)
            entries.append(make_entry(item, paths, env, unit))
        manifest = render_manifest(
            applied_at="2026-07-12T00:00:00Z", document_id=pair.shared.document_id,
            shared_revision=1, node_revision=1, entries=tuple(entries),
        )
        duration = time.monotonic() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return ScaleResult(len(plan.desired), len(plan.actions), duration, peak, len(manifest))


class RuntimeScaleTests(unittest.TestCase):
    def test_256_exit_selection_render_and_manifest_are_bounded(self) -> None:
        result = benchmark_256_runtime()
        self.assertEqual(256, result.exits)
        self.assertEqual(256, result.actions)
        self.assertGreater(result.manifest_bytes, 1000)
        self.assertLess(result.duration_seconds, 5.0)
        self.assertLess(result.peak_bytes, 128 * 1024 * 1024)
