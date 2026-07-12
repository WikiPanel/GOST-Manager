from __future__ import annotations

import importlib
import os
import stat
import subprocess
import time
import tracemalloc
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from gateway.models import Binding, ExitNode, Route, StatePair, Strategy
from gateway.serialization import serialize_node, serialize_shared
from gateway.validation import validate_pair
from test_gateway_support import TemporaryStore, cli_paths, make_pair, run_cli


def _manifest(root: Path) -> tuple[tuple[str, bytes, int], ...]:
    result: list[tuple[str, bytes, int]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            result.append(
                (
                    str(path.relative_to(root)),
                    path.read_bytes(),
                    stat.S_IMODE(path.stat().st_mode),
                )
            )
    return tuple(result)


class DirectModeIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryStore()
        self.direct_root = self.temporary.root / "direct-fixture"
        files = {
            "etc/gost/iran-1.env": b"GOST_IRAN_PORT=443\n",
            "etc/gost/kharej-1.env": b"GOST_KHAREJ_PORT=28420\n",
            "etc/systemd/system/gost-iran-1.service": b"[Service]\nExecStart=/usr/local/bin/gost\n",
            "etc/systemd/system/gost-kharej-1.service": b"[Service]\nExecStart=/usr/local/bin/gost\n",
        }
        for relative, data in files.items():
            path = self.direct_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o640 if path.suffix == ".env" else 0o644)

    def tearDown(self) -> None:
        self.temporary.close()

    def test_all_cli_crud_leaves_direct_mode_and_command_log_unchanged(self) -> None:
        before = _manifest(self.direct_root)
        paths = cli_paths(self.temporary.paths)
        command_log: list[tuple[object, ...]] = []

        def forbidden(*args: object, **_kwargs: object) -> None:
            command_log.append(args)
            raise AssertionError("gateway state CRUD attempted a process command")

        commands = (
            (
                "init",
                "--gateway-id",
                "gateway-main",
                "--node-id",
                "iran-gateway-1",
                "--listen-address",
                "0.0.0.0",
                "--listen-port",
                "80",
                "--server-name",
                "gateway.example.org",
            ),
            (
                "exit",
                "add",
                "--id",
                "ee-primary",
                "--display-name",
                "Estonia",
                "--host",
                "192.0.2.10",
                "--socks-port",
                "28420",
                "--target-port",
                "18081",
            ),
            (
                "exit",
                "edit",
                "--id",
                "ee-primary",
                "--display-name",
                "Estonia primary",
            ),
            (
                "binding",
                "set",
                "--exit-id",
                "ee-primary",
                "--listen-port",
                "18081",
                "--secret-ref",
                "secret-ee-primary",
                "--enable",
            ),
            (
                "route",
                "add",
                "--id",
                "route-estonia",
                "--display-name",
                "Estonia",
                "--host",
                "gateway.example.org",
                "--path",
                "/ee1/api/v1",
                "--strategy",
                "active-passive",
                "--exit-id",
                "ee-primary",
            ),
            ("route", "edit", "--id", "route-estonia", "--path", "/ee1/api/v1/"),
            ("gateway", "set", "--enable"),
            ("show",),
            ("validate",),
            ("gateway", "show"),
            ("exit", "list"),
            ("binding", "list"),
            ("route", "list"),
            ("route", "delete", "--id", "route-estonia"),
            ("binding", "remove", "--exit-id", "ee-primary"),
            ("exit", "delete", "--id", "ee-primary"),
        )
        with mock.patch.object(subprocess, "run", side_effect=forbidden), mock.patch.object(
            subprocess, "Popen", side_effect=forbidden
        ), mock.patch.object(subprocess, "check_call", side_effect=forbidden), mock.patch.object(
            subprocess, "check_output", side_effect=forbidden
        ):
            for command in commands:
                code, _stdout, stderr = run_cli([*paths, *command])
                self.assertEqual((code, stderr), (0, ""), command)

        self.assertEqual(command_log, [])
        self.assertEqual(_manifest(self.direct_root), before)
        self.assertFalse((self.direct_root / "etc/nginx").exists())
        self.assertFalse((self.direct_root / "etc/iptables").exists())
        self.assertFalse((self.direct_root / "etc/nftables").exists())

    def test_gateway_package_does_not_import_subprocess(self) -> None:
        modules = (
            "gateway.cli",
            "gateway.crud",
            "gateway.locking",
            "gateway.paths",
            "gateway.serialization",
            "gateway.store",
            "gateway.validation",
        )
        for name in modules:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                source = Path(module.__file__).read_text()
                self.assertNotIn("import subprocess", source)


class GatewayPerformanceTests(unittest.TestCase):
    def test_maximum_cardinality_validation_and_serialization_are_bounded(self) -> None:
        pair = make_pair()
        exits = tuple(
            ExitNode(
                id=f"exit-{index:03d}",
                display_name=f"Exit {index}",
                enabled=True,
                host=f"exit-{index:03d}.example.org",
                socks_port=20000 + index,
                target_port=30000 + index,
            )
            for index in range(256)
        )
        bindings = tuple(
            Binding(
                exit_id=f"exit-{index:03d}",
                enabled=True,
                listen_address="127.0.0.1",
                listen_port=40000 + index,
                secret_ref=f"secret-exit-{index:03d}",
            )
            for index in range(256)
        )
        routes = tuple(
            Route(
                id=f"route-{index:03d}",
                display_name=f"Route {index}",
                enabled=False,
                host="gateway.example.org",
                path=f"/route/{index:03d}",
                strategy=(
                    Strategy.ACTIVE_PASSIVE
                    if index % 2 == 0
                    else Strategy.ACTIVE_ACTIVE
                ),
                exit_ids=(f"exit-{index:03d}",),
            )
            for index in range(256)
        )
        candidate = StatePair(
            replace(pair.shared, exits=exits, routes=routes),
            replace(pair.node, bindings=bindings),
        )
        tracemalloc.start()
        started = time.monotonic()
        for _ in range(3):
            validate_pair(candidate)
            shared_data = serialize_shared(candidate.shared)
            node_data = serialize_node(candidate.node)
        elapsed = time.monotonic() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(elapsed, 5.0)
        self.assertLess(peak, 64 * 1024 * 1024)
        self.assertLess(len(shared_data), 1024 * 1024)
        self.assertLess(len(node_data), 512 * 1024)


if __name__ == "__main__":
    unittest.main()
