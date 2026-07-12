from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from gateway.errors import ConflictError, OperationalError, StateError, ValidationError
from gateway.models import Binding, ExitNode, NodeState, SharedState, StatePair
from gateway.runtime_apply import RuntimeManager
from gateway.runtime_inspection import CommandResult, RuntimeInspector, parse_ss_listeners
from gateway.runtime_models import DesiredExitRuntime
from gateway.runtime_paths import RuntimePaths, service_name
from gateway.runtime_render import parse_manifest, render_env, render_manifest, render_unit
from gateway.serialization import serialize_node, serialize_shared
from gateway.secrets import SecretStore
from gateway.store import GatewayStateStore
from test_gateway_support import TemporaryStore, add_secondary, make_pair


def credentials():
    from gateway.runtime_models import Credentials
    suffix = os.urandom(8).hex()
    return Credentials(f"test-user-{suffix}", f"test-pass-{suffix}")


class FakeSystem:
    def __init__(self) -> None:
        self.states: dict[str, dict[str, object]] = {}
        self.ports: dict[str, int] = {}
        self.commands: list[tuple[str, ...]] = []
        self.fail_on: tuple[str, ...] | None = None
        self.listener_verification_enabled = True
        self.next_pid = 4000

    def runner(self, argv: Sequence[str]) -> CommandResult:
        command = tuple(argv)
        self.commands.append(command)
        if self.fail_on is not None and command[: len(self.fail_on)] == self.fail_on:
            return CommandResult(1, "", "failure")
        if command[:3] == ("ss", "-H", "-lntp"):
            lines = []
            for name, state in sorted(self.states.items()):
                if state.get("active") and name in self.ports:
                    pid = state["pid"]
                    lines.append(
                        f'LISTEN 0 4096 127.0.0.1:{self.ports[name]} 0.0.0.0:* users:(("gost",pid={pid},fd=3))'
                    )
            return CommandResult(0, "\n".join(lines) + ("\n" if lines else ""), "")
        if len(command) >= 4 and command[:3] == ("systemctl", "--no-pager", "show"):
            name = command[3]
            state = self.states.get(name)
            if state is None:
                return CommandResult(1)
            return CommandResult(
                0,
                "\n".join((
                    "LoadState=loaded",
                    f"UnitFileState={'enabled' if state['enabled'] else 'disabled'}",
                    f"ActiveState={'active' if state['active'] else 'inactive'}",
                    f"MainPID={state['pid'] if state['active'] else 0}",
                )) + "\n",
            )
        if command[:2] == ("systemd-analyze", "verify"):
            return CommandResult(0)
        if command[:2] == ("systemctl", "daemon-reload"):
            return CommandResult(0)
        if command[0] == "systemctl":
            action, name = command[1], command[-1]
            state = self.states.setdefault(name, {"enabled": False, "active": False, "pid": 0})
            if action == "enable":
                state["enabled"] = True
            elif action == "disable":
                state["enabled"] = False
                if "--now" in command:
                    state["active"] = False
            elif action in {"start", "restart"}:
                state["active"] = True
                if not state["pid"] or action == "restart":
                    self.next_pid += 1
                    state["pid"] = self.next_pid
            elif action == "stop":
                state["active"] = False
            return CommandResult(0)
        return CommandResult(1)

    def verify_listener(self, address: str, port: int, pid: int) -> bool:
        if not self.listener_verification_enabled:
            return False
        if address != "127.0.0.1":
            return False
        for name, state in self.states.items():
            if state.get("active") and state.get("pid") == pid and self.ports.get(name) == port:
                return True
        return False


class RuntimeFixture:
    def __init__(self) -> None:
        self.state_temp = TemporaryStore()
        self.state = self.state_temp.initialize()
        pair = make_pair(gateway_enabled=False, route_enabled=False)
        self.state.mutate_shared(lambda _shared: pair.shared)
        self.state.mutate_node(lambda _node: pair.node)
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()
        (root / "systemd").mkdir()
        self.paths = RuntimePaths.from_values(
            root / "secrets", root / "generated", root / "backups", root / "runtime.lock",
            root / "systemd", root / "runner", root / "gost",
        )
        self.secrets = SecretStore(self.paths)
        self.secrets.set("secret-ee-primary", credentials())
        self.system = FakeSystem()
        self.system.ports[service_name("ee-primary")] = 18081
        self.inspector = RuntimeInspector(self.system.runner, self.system.verify_listener)
        self.manager = RuntimeManager(
            self.state, self.secrets, self.paths,
            inspector=self.inspector, clock=lambda: "2026-07-12T00:00:00Z",
        )

    def close(self) -> None:
        self.temporary.cleanup()
        self.state_temp.close()

    def replace_pair(self, pair: StatePair) -> None:
        self.state.paths.state_file.write_bytes(serialize_shared(pair.shared))
        self.state.paths.node_file.write_bytes(serialize_node(pair.node))


class RuntimeRenderAndInspectionTests(unittest.TestCase):
    def desired(self) -> DesiredExitRuntime:
        return DesiredExitRuntime(
            "ee-primary", service_name("ee-primary"), "127.0.0.1", 18081,
            "192.0.2.10", 28420, "127.0.0.1", 18081,
            "secret-ee-primary", 123,
        )

    def test_golden_non_secret_env_and_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            paths = RuntimePaths.from_values(
                root / "secrets", root / "generated", root / "backups", root / "lock",
                root / "systemd", root / "runner", root / "gost",
            )
            desired = self.desired()
            env = render_env(desired).decode()
            unit = render_unit(desired, paths).decode()
            self.assertEqual(1, env.count("GATEWAY_LISTEN_ADDRESS=127.0.0.1"))
            self.assertNotIn("GOST_USER", env)
            self.assertNotIn("GOST_PASS", env)
            self.assertEqual(2, unit.count("EnvironmentFile="))
            self.assertIn("LimitNOFILE=200000", unit)
            self.assertNotIn("nginx", unit.lower())
            self.assertNotIn("PrivateNetwork", unit)

    def test_manifest_is_deterministic_and_non_secret(self) -> None:
        data = render_manifest(
            applied_at="2026-07-12T00:00:00Z", document_id="doc",
            shared_revision=2, node_revision=3, entries=(),
        )
        self.assertEqual({}, parse_manifest(data))
        self.assertNotIn(b"password", data.lower())

    def test_ss_parser_handles_ipv4_ipv6_and_rejects_malformed(self) -> None:
        parsed = parse_ss_listeners(
            'LISTEN 0 128 127.0.0.1:18081 0.0.0.0:* users:(("gost",pid=42,fd=3))\n'
            'LISTEN 0 128 [::]:443 [::]:* users:(("nginx",pid=51,fd=4))\n'
        )
        self.assertEqual((42,), parsed[0].pids)
        self.assertTrue(parsed[1].wildcard)
        with self.assertRaises(ValidationError):
            parse_ss_listeners("malformed non-empty output")


class RuntimePlanApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RuntimeFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_first_apply_then_unchanged_is_noop_without_restart(self) -> None:
        plan = self.fixture.manager.plan()
        self.assertEqual(["create"], [item.action for item in plan.actions])
        self.assertEqual(1, self.fixture.inspector.listener_calls)
        result = self.fixture.manager.apply(yes=True)
        self.assertEqual(2, self.fixture.inspector.listener_calls)
        self.assertEqual(("ee-primary",), result.started)
        restart_before = sum(command[1] == "restart" for command in self.fixture.system.commands if command[0] == "systemctl")
        unchanged = self.fixture.manager.apply(yes=True)
        restart_after = sum(command[1] == "restart" for command in self.fixture.system.commands if command[0] == "systemctl")
        self.assertFalse(unchanged.changed)
        self.assertEqual(restart_before, restart_after)
        self.assertEqual(0o600, self.fixture.paths.env_file("ee-primary").stat().st_mode & 0o777)
        self.assertEqual(0o644, self.fixture.paths.unit_file("ee-primary").stat().st_mode & 0o777)
        self.assertEqual([], list(self.fixture.paths.runtime_backup_dir.iterdir()))

    def test_same_service_main_pid_owns_unchanged_port(self) -> None:
        self.fixture.manager.apply(yes=True)
        plan = self.fixture.manager.plan()
        self.assertEqual("no-op", plan.actions[0].action)

    def test_unknown_or_direct_mode_port_owner_is_conflict(self) -> None:
        self.fixture.system.states["gost-iran-1.service"] = {"enabled": True, "active": True, "pid": 9001}
        self.fixture.system.ports["gost-iran-1.service"] = 18081
        plan = self.fixture.manager.plan()
        self.assertEqual("conflict", plan.actions[0].action)
        with self.assertRaises(ConflictError):
            self.fixture.manager.apply(yes=True)

    def test_endpoint_change_restarts_only_selected_exit(self) -> None:
        self.fixture.manager.apply(yes=True)
        self.fixture.state.mutate_shared(
            lambda shared: replace(
                shared,
                exits=(replace(shared.exits[0], host="192.0.2.11"),),
            )
        )
        result = self.fixture.manager.apply(yes=True)
        self.assertEqual(("ee-primary",), result.restarted)
        restart_commands = [command for command in self.fixture.system.commands if command[:2] == ("systemctl", "restart")]
        self.assertEqual([("systemctl", "restart", "gost-gateway-exit-ee-primary.service")], restart_commands)

    def test_missing_secret_fails_before_listener_or_mutation(self) -> None:
        self.fixture.paths.secret_file("secret-ee-primary").unlink()
        before = len(self.fixture.system.commands)
        with self.assertRaises(StateError):
            self.fixture.manager.apply(yes=True)
        self.assertEqual(before, len(self.fixture.system.commands))
        self.assertFalse(self.fixture.paths.generated_dir.exists())

    def test_failure_restores_files_and_service_state(self) -> None:
        self.fixture.manager.apply(yes=True)
        env_before = self.fixture.paths.env_file("ee-primary").read_bytes()
        unit_before = self.fixture.paths.unit_file("ee-primary").read_bytes()
        state_before = dict(self.fixture.system.states[service_name("ee-primary")])
        self.fixture.state.mutate_shared(
            lambda shared: replace(shared, exits=(replace(shared.exits[0], socks_port=28421),))
        )
        self.fixture.manager.failure_hook = lambda phase: (_ for _ in ()).throw(RuntimeError("injected")) if phase == "after_changed_service_restart" else None
        with self.assertRaises(RuntimeError):
            self.fixture.manager.apply(yes=True)
        self.assertEqual(env_before, self.fixture.paths.env_file("ee-primary").read_bytes())
        self.assertEqual(unit_before, self.fixture.paths.unit_file("ee-primary").read_bytes())
        restored = self.fixture.system.states[service_name("ee-primary")]
        self.assertEqual(state_before["enabled"], restored["enabled"])
        self.assertEqual(state_before["active"], restored["active"])

    def test_unverifiable_rollback_retains_non_secret_diagnostic_backup(self) -> None:
        self.fixture.system.fail_on = ("systemctl", "daemon-reload")
        with self.assertRaises(OperationalError) as caught:
            self.fixture.manager.apply(yes=True)
        backups = list(self.fixture.paths.runtime_backup_dir.glob("txn-*"))
        self.assertEqual(1, len(backups))
        self.assertIn(str(backups[0]), str(caught.exception))
        contents = b"".join(
            path.read_bytes() for path in backups[0].iterdir() if path.is_file()
        )
        self.assertNotIn(b"GOST_PASS", contents)

    def test_listener_ownership_verification_failure_rolls_back_activation(self) -> None:
        self.fixture.system.listener_verification_enabled = False
        with self.assertRaises(OperationalError):
            self.fixture.manager.apply(yes=True)
        self.assertFalse(self.fixture.paths.env_file("ee-primary").exists())
        self.assertFalse(self.fixture.paths.unit_file("ee-primary").exists())
        state = self.fixture.system.states[service_name("ee-primary")]
        self.assertFalse(state["active"])
        self.assertFalse(state["enabled"])
        self.assertEqual(1, self.fixture.inspector.listener_calls)

    def test_service_control_is_exact_and_confirmation_is_required(self) -> None:
        self.fixture.manager.apply(yes=True)
        with self.assertRaises(ConflictError):
            self.fixture.manager.service_control("restart", "ee-primary")
        self.fixture.manager.service_control("restart", "ee-primary", yes=True)
        targeted = [command[-1] for command in self.fixture.system.commands if command[:2] == ("systemctl", "restart")]
        self.assertEqual([service_name("ee-primary")], targeted)

    def test_six_independent_exits_activate_with_one_daemon_reload(self) -> None:
        base = make_pair(route_enabled=False)
        exits = tuple(
            ExitNode(
                id=f"exit-{index}", display_name=f"Exit {index}", enabled=True,
                host=f"exit-{index}.example.org", socks_port=28420 + index,
                target_port=18080 + index,
            )
            for index in range(1, 7)
        )
        bindings = tuple(
            Binding(
                exit_id=item.id, enabled=True, listen_address="127.0.0.1",
                listen_port=18080 + index, secret_ref=f"secret-{index}",
            )
            for index, item in enumerate(exits, 1)
        )
        pair = StatePair(
            replace(base.shared, exits=exits, routes=()),
            replace(base.node, bindings=bindings),
        )
        self.fixture.replace_pair(pair)
        for index, item in enumerate(exits, 1):
            self.fixture.secrets.set(f"secret-{index}", credentials())
            self.fixture.system.ports[service_name(item.id)] = 18080 + index
        result = self.fixture.manager.apply(yes=True)
        self.assertEqual(tuple(item.id for item in exits), result.started)
        reloads = [command for command in self.fixture.system.commands if command[:2] == ("systemctl", "daemon-reload")]
        self.assertEqual(1, len(reloads))
        self.assertEqual(6, len(parse_manifest(self.fixture.paths.manifest_file.read_bytes())))

    def test_selected_apply_does_not_remove_unrelated_stale_runtime(self) -> None:
        self.fixture.manager.apply(yes=True)
        stale_id = "de-stale"
        stale_env = self.fixture.paths.env_file(stale_id)
        stale_unit = self.fixture.paths.unit_file(stale_id)
        stale_env.write_text("stale\n", encoding="ascii")
        stale_unit.write_text("stale\n", encoding="ascii")
        self.fixture.state.mutate_node(
            lambda node: replace(node, bindings=(replace(node.bindings[0], enabled=False),))
        )
        selected = self.fixture.manager.apply(yes=True, exit_id="ee-primary")
        self.assertEqual(("ee-primary",), selected.removed)
        self.assertTrue(stale_env.exists())
        self.assertTrue(stale_unit.exists())
        full = self.fixture.manager.apply(yes=True)
        self.assertEqual((stale_id,), full.removed)
        self.assertFalse(stale_env.exists())
        self.assertFalse(stale_unit.exists())

    def test_secret_mtime_change_recommends_only_one_restart(self) -> None:
        self.fixture.manager.apply(yes=True)
        path = self.fixture.paths.secret_file("secret-ee-primary")
        before = path.stat().st_mtime_ns
        os.utime(path, ns=(before + 1_000_000, before + 1_000_000))
        plan = self.fixture.manager.plan()
        self.assertEqual(["restart"], [item.action for item in plan.actions])

    def test_runtime_apply_leaves_direct_mode_bytes_and_commands_unchanged(self) -> None:
        direct_env = Path(self.fixture.temporary.name) / "direct.env"
        direct_unit = self.fixture.paths.systemd_dir / "gost-iran-1.service"
        direct_env.write_bytes(b"MAPPINGS=2052:2052\n")
        direct_unit.write_bytes(b"[Unit]\nDescription=Direct\n")
        before = (direct_env.read_bytes(), direct_unit.read_bytes(), direct_env.stat().st_mode, direct_unit.stat().st_mode)
        self.fixture.manager.apply(yes=True)
        after = (direct_env.read_bytes(), direct_unit.read_bytes(), direct_env.stat().st_mode, direct_unit.stat().st_mode)
        self.assertEqual(before, after)
        flattened = "\n".join(" ".join(command) for command in self.fixture.system.commands)
        self.assertNotIn("gost-iran-1.service", flattened)
        self.assertNotIn("nginx", flattened.lower())
        self.assertNotIn("iptables", flattened.lower())

    def test_state_lock_is_acquired_before_runtime_lock(self) -> None:
        events: list[str] = []

        class Lock:
            def __init__(self, name: str) -> None:
                self.name = name

            def __enter__(self):
                events.append(f"enter-{self.name}")
                return self

            def __exit__(self, *_args):
                events.append(f"exit-{self.name}")

        state = GatewayStateStore(
            self.fixture.state.paths,
            lock_factory=lambda _path, _timeout: Lock("state"),
        )
        secrets = SecretStore(
            self.fixture.paths,
            lock_factory=lambda _path, _timeout: Lock("runtime"),
        )
        manager = RuntimeManager(
            state, secrets, self.fixture.paths,
            inspector=RuntimeInspector(
                self.fixture.system.runner, self.fixture.system.verify_listener
            ),
        )
        manager.plan()
        self.assertEqual(
            ["enter-state", "enter-runtime", "exit-runtime", "exit-state"],
            events,
        )

    def test_two_exit_temporary_root_smoke_flow(self) -> None:
        pair = add_secondary(make_pair(route_enabled=False))
        self.fixture.replace_pair(pair)
        self.fixture.secrets.set("secret-de-backup", credentials())
        self.fixture.system.ports[service_name("de-backup")] = 18082

        direct_unit = self.fixture.paths.systemd_dir / "gost-kharej-1.service"
        direct_unit.write_bytes(b"[Unit]\nDescription=Direct\n")
        direct_before = direct_unit.read_bytes()

        plan = self.fixture.manager.plan()
        self.assertEqual(["create", "create"], [item.action for item in plan.actions])
        first = self.fixture.manager.apply(yes=True)
        self.assertEqual(("de-backup", "ee-primary"), first.started)
        for exit_id in ("de-backup", "ee-primary"):
            env = self.fixture.paths.env_file(exit_id).read_bytes()
            self.assertNotIn(b"GOST_USER", env)
            self.assertNotIn(b"GOST_PASS", env)
            self.assertTrue(self.fixture.system.states[service_name(exit_id)]["active"])

        restart_before = len([
            command for command in self.fixture.system.commands
            if command[:2] == ("systemctl", "restart")
        ])
        self.assertFalse(self.fixture.manager.apply(yes=True).changed)
        restart_after = len([
            command for command in self.fixture.system.commands
            if command[:2] == ("systemctl", "restart")
        ])
        self.assertEqual(restart_before, restart_after)

        self.fixture.state.mutate_shared(
            lambda shared: replace(
                shared,
                exits=tuple(
                    replace(item, host="192.0.2.44") if item.id == "ee-primary" else item
                    for item in shared.exits
                ),
            )
        )
        changed = self.fixture.manager.apply(yes=True)
        self.assertEqual(("ee-primary",), changed.restarted)

        self.fixture.secrets.set("secret-ee-primary", credentials())
        self.assertEqual("restart", next(
            item.action for item in self.fixture.manager.plan().actions
            if item.exit_id == "ee-primary"
        ))
        self.fixture.manager.service_control("restart", "ee-primary", yes=True)

        self.fixture.state.mutate_node(
            lambda node: replace(
                node,
                bindings=tuple(
                    replace(item, enabled=False) if item.exit_id == "de-backup" else item
                    for item in node.bindings
                ),
            )
        )
        removed = self.fixture.manager.apply(yes=True)
        self.assertEqual(("de-backup",), removed.removed)

        env_before = self.fixture.paths.env_file("ee-primary").read_bytes()
        self.fixture.state.mutate_shared(
            lambda shared: replace(
                shared,
                exits=tuple(
                    replace(item, socks_port=29999) if item.id == "ee-primary" else item
                    for item in shared.exits
                ),
            )
        )
        self.fixture.manager.failure_hook = lambda phase: (
            (_ for _ in ()).throw(RuntimeError("smoke failure"))
            if phase == "after_changed_service_restart" else None
        )
        with self.assertRaises(RuntimeError):
            self.fixture.manager.apply(yes=True)
        self.assertEqual(env_before, self.fixture.paths.env_file("ee-primary").read_bytes())
        self.assertEqual(direct_before, direct_unit.read_bytes())
