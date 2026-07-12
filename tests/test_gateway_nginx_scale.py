from __future__ import annotations

import tempfile
import time
import tracemalloc
import unittest
from dataclasses import dataclass
from pathlib import Path

from gateway.models import Binding, ExitNode, Gateway, NodeState, Route, SharedState, StatePair, Strategy
from gateway.nginx_manifest import parse_manifest, render_manifest
from gateway.nginx_paths import NginxPaths
from gateway.nginx_render import build_candidate, render_config
from test_gateway_support import DOCUMENT_ID, TIMESTAMP


@dataclass(frozen=True)
class NginxScaleResult:
    routes: int
    backends: int
    duration_seconds: float
    peak_bytes: int
    config_bytes: int
    manifest_bytes: int


def benchmark_256_routes() -> NginxScaleResult:
    exits = tuple(
        ExitNode(
            f"exit-{index:03d}", f"Exit {index}", True,
            f"node-{index}.example.org", 30000 + index, 40000 + index,
        )
        for index in range(256)
    )
    bindings = tuple(
        Binding(item.id, True, "127.0.0.1", 20000 + index, f"secret-{index:03d}")
        for index, item in enumerate(exits)
    )
    routes = []
    for index in range(256):
        exit_ids = tuple(item.id for item in exits[:32]) if index == 0 else (exits[index].id,)
        routes.append(
            Route(
                f"route-{index:03d}", f"Route {index}", True,
                f"h{index % 32}.example.org", f"/route/{index:03d}",
                Strategy.ACTIVE_ACTIVE if index % 2 else Strategy.ACTIVE_PASSIVE,
                exit_ids,
            )
        )
    pair = StatePair(
        SharedState(
            1, DOCUMENT_ID, 1, TIMESTAMP,
            Gateway(
                "gateway-main", True, "0.0.0.0", 8080,
                tuple(f"h{index}.example.org" for index in range(32)), 18000,
            ),
            exits,
            tuple(routes),
        ),
        NodeState(1, DOCUMENT_ID, "iran-gateway-1", 1, TIMESTAMP, bindings),
    )
    ready = {item.exit_id: item.listen_port for item in bindings}
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        paths = NginxPaths.from_values(
            root / "generated", root / "backups", root / "lock", root / "run",
            root / "unit", root / "runner", root / "nginx", root / "launcher",
        )
        tracemalloc.start()
        started = time.monotonic()
        candidate = build_candidate(pair, ready)
        config = render_config(candidate, str(paths.pid_file))
        manifest = render_manifest(candidate, config, paths, "2026-07-12T18:00:00Z")
        parse_manifest(manifest, paths)
        duration = time.monotonic() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return NginxScaleResult(
        len(candidate.routes),
        sum(len(item.backends) for item in candidate.routes),
        duration,
        peak,
        len(config),
        len(manifest),
    )


class NginxScaleTests(unittest.TestCase):
    def test_maximum_route_render_is_bounded(self) -> None:
        result = benchmark_256_routes()
        self.assertEqual(256, result.routes)
        self.assertEqual(287, result.backends)
        self.assertLess(result.duration_seconds, 5.0)
        self.assertLess(result.peak_bytes, 128 * 1024 * 1024)
        self.assertLess(result.config_bytes, 4 * 1024 * 1024)
        self.assertLess(result.manifest_bytes, 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
