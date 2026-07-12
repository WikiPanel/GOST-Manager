from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from gateway.errors import ConflictError, StateError, ValidationError
from gateway.models import Route, StatePair, Strategy
from gateway.nginx_apply import NginxManager
from gateway.nginx_readiness import GostBackendReadiness
from gateway.runtime_apply import select_desired
from gateway.nginx_dependency import NginxDependencyManager
from gateway.nginx_inspection import (
    NginxInspector,
    parse_cgroup_pids,
    parse_stub_status,
)
from gateway.nginx_manifest import parse_manifest, render_manifest
from gateway.nginx_models import NginxServiceState
from gateway.nginx_paths import NGINX_SERVICE_NAME, NginxPaths
from gateway.nginx_render import (
    build_candidate,
    render_config,
    upstream_name,
    validate_nginx_path,
)
from gateway.nginx_store import NginxStore
from gateway.nginx_apply import FAILURE_PHASES
from gateway.runtime_inspection import CommandResult
from gateway.runtime_models import Listener, ServiceState
from gateway.runtime_paths import RuntimePaths, service_name
from gateway.runtime_render import make_entry, render_env, render_manifest as render_gost_manifest, render_unit
from gateway.runtime_store import RuntimeStore
from gateway.runtime_models import Credentials
from gateway.secrets import SecretStore
from gateway.serialization import serialize_node, serialize_shared
from gateway.store import GatewayStateStore
from test_gateway_support import TemporaryStore, add_secondary, make_pair


STATUS_TEXT = """Active connections: 4
server accepts handled requests
 10 10 12
Reading: 1 Writing: 2 Waiting: 1
"""


class ReadyBackends:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def ready_ports(self, pair, exit_ids, listeners):
        self.calls += 1
        if self.fail:
            raise ConflictError("backend_not_ready:ee-primary:service_state")
        bindings = {item.exit_id: item for item in pair.node.bindings}
        return {
            exit_id: bindings[exit_id].listen_port
            for exit_id in exit_ids
            if exit_id in bindings and bindings[exit_id].enabled
        }


class FakeNginxSystem:
    def __init__(self, root: Path, paths: NginxPaths) -> None:
        self.root = root
        self.paths = paths
        self.commands: list[tuple[str, ...]] = []
        self.loaded = True
        self.enabled = False
        self.active = False
        self.main_pid = 5100
        self.worker_pids = (5101, 5102)
        self.public = ("0.0.0.0", 18080)
        self.status_port = 19000
        self.pending_public = self.public
        self.pending_status = self.status_port
        self.fail_reload = False
        self.conflict_listener = ""
        self.distro_loaded = False
        self.distro_enabled = False
        self.distro_active = False
        self.version = "nginx version: nginx/1.24.0"
        self.cgroup = root / "cgroup/system.slice/gost-nginx-gateway.service"
        self.cgroup.mkdir(parents=True)
        self._write_pids()

    def _write_pids(self) -> None:
        self.cgroup.joinpath("cgroup.procs").write_text(
            "\n".join(str(item) for item in (self.main_pid, *self.worker_pids)) + "\n",
            encoding="ascii",
        )

    def _parse_config(self, path: Path) -> None:
        if not path.exists():
            return
        text = path.read_text(encoding="ascii")
        public = re.search(r"listen ([0-9.]+):([0-9]+) default_server;", text)
        status = re.search(r"listen 127\.0\.0\.1:([0-9]+);", text)
        if public:
            self.pending_public = (public.group(1), int(public.group(2)))
        if status:
            self.pending_status = int(status.group(1))

    def runner(self, argv: Sequence[str]) -> CommandResult:
        command = tuple(argv)
        self.commands.append(command)
        if command and command[0].endswith("nginx"):
            if "-v" in command:
                return CommandResult(0, "", self.version)
            if "-c" in command:
                self._parse_config(Path(command[command.index("-c") + 1]))
            return CommandResult(0)
        if command[:3] == ("ss", "-H", "-lntp"):
            lines: list[str] = []
            if self.active:
                owners = ",".join(
                    f'(\"nginx\",pid={pid},fd=5)' for pid in self.worker_pids
                )
                lines.extend(
                    (
                        f"LISTEN 0 511 {self.public[0]}:{self.public[1]} 0.0.0.0:* users:({owners})",
                        f"LISTEN 0 511 127.0.0.1:{self.status_port} 0.0.0.0:* users:({owners})",
                    )
                )
            if self.conflict_listener:
                lines.append(self.conflict_listener)
            return CommandResult(0, "\n".join(lines) + ("\n" if lines else ""))
        if len(command) >= 4 and command[:3] == ("systemctl", "--no-pager", "show"):
            name = command[3]
            if name == "nginx.service":
                if not self.distro_loaded:
                    return CommandResult(1)
                return self._show(
                    self.distro_loaded, self.distro_enabled, self.distro_active, 6100,
                    "/system.slice/nginx.service",
                )
            return self._show(
                self.loaded, self.enabled, self.active, self.main_pid,
                "/system.slice/gost-nginx-gateway.service",
            )
        if command and command[0] == "systemctl":
            action, name = command[1], command[-1]
            if name == "nginx.service":
                if action == "stop":
                    self.distro_active = False
                elif action == "disable":
                    self.distro_enabled = False
                return CommandResult(0)
            if name != NGINX_SERVICE_NAME:
                return CommandResult(1)
            if action == "enable":
                self.enabled = True
            elif action == "disable":
                self.enabled = False
            elif action == "start":
                self.active = True
                self.public = self.pending_public
                self.status_port = self.pending_status
            elif action == "stop":
                self.active = False
            elif action == "reload":
                if self.fail_reload:
                    self.fail_reload = False
                    return CommandResult(1)
                self.public = self.pending_public
                self.status_port = self.pending_status
            elif action == "restart":
                self.main_pid += 100
                self.worker_pids = (self.main_pid + 1, self.main_pid + 2)
                self._write_pids()
                self.active = True
            return CommandResult(0)
        if command[:2] == ("apt-get", "update"):
            return CommandResult(0)
        if command[:2] == ("apt-get", "install"):
            self.paths.nginx_bin.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
            self.paths.nginx_bin.chmod(0o755)
            self.distro_loaded = True
            self.distro_enabled = True
            self.distro_active = True
            return CommandResult(0)
        return CommandResult(1)

    @staticmethod
    def _show(loaded, enabled, active, pid, cgroup):
        return CommandResult(
            0,
            "\n".join(
                (
                    f"LoadState={'loaded' if loaded else 'not-found'}",
                    f"UnitFileState={'enabled' if enabled else 'disabled'}",
                    f"ActiveState={'active' if active else 'inactive'}",
                    f"SubState={'running' if active else 'dead'}",
                    f"MainPID={pid if active else 0}",
                    f"ControlGroup={cgroup if active else ''}",
                    "FragmentPath=/etc/systemd/system/gost-nginx-gateway.service",
                )
            )
            + "\n",
        )

    def status_reader(self, port: int, timeout: float) -> str:
        if not self.active or port != self.status_port:
            raise OSError("status unavailable")
        return STATUS_TEXT


class NginxFixture:
    def __init__(self) -> None:
        self.state_temp = TemporaryStore()
        self.state = self.state_temp.initialize()
        self.pair = add_secondary(
            make_pair(gateway_enabled=True, route_enabled=True)
        )
        self.replace_pair(self.pair)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        for directory in ("systemd", "bin", "run"):
            self.root.joinpath(directory).mkdir()
        self.runtime_paths = RuntimePaths.from_values(
            self.root / "secrets",
            self.root / "generated",
            self.root / "runtime-backups",
            self.root / "runtime.lock",
            self.root / "systemd",
            self.root / "gost-runner",
            self.root / "gost",
        )
        self.secrets = SecretStore(self.runtime_paths)
        self.paths = NginxPaths.from_values(
            self.root / "nginx",
            self.root / "nginx-backups",
            self.root / "nginx.lock",
            self.root / "run",
            self.root / "systemd/gost-nginx-gateway.service",
            self.root / "nginx-runner",
            self.root / "bin/nginx",
            self.root / "nginx-launcher",
        )
        self.paths.nginx_bin.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        self.paths.nginx_bin.chmod(0o755)
        self.system = FakeNginxSystem(self.root, self.paths)
        self.inspector = NginxInspector(
            self.system.runner,
            cgroup_root=self.root / "cgroup",
            status_reader=self.system.status_reader,
        )
        self.ready = ReadyBackends()
        self.manager = NginxManager(
            self.state,
            self.secrets,
            self.runtime_paths,
            self.paths,
            inspector=self.inspector,
            backend_readiness=self.ready,
            clock=lambda: "2026-07-12T18:00:00Z",
        )

    def replace_pair(self, pair: StatePair) -> None:
        self.pair = pair
        self.state.paths.state_file.write_bytes(serialize_shared(pair.shared))
        self.state.paths.node_file.write_bytes(serialize_node(pair.node))

    def close(self) -> None:
        self.temporary.cleanup()
        self.state_temp.close()


class NginxRenderTests(unittest.TestCase):
    def test_path_policy_accepts_exact_safe_paths(self) -> None:
        for value in ("/", "/api/v1", "/api/v1/", "/A-z_0.~"):
            self.assertEqual(value, validate_nginx_path(value))

    def test_path_policy_rejects_every_unsafe_form(self) -> None:
        values = (
            "/bad;return", "/bad{", "/bad}", "/$uri", "/($uri)",
            "/bad%2f", "/bad//path", "/./path", "/../path", "/%2e/path",
            "/path?q=1", "/path#x", "/café", "/line\nbreak", "/" + "a" * 512,
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_nginx_path(value)

    def test_golden_active_passive_render(self) -> None:
        pair = add_secondary(make_pair(gateway_enabled=True, route_enabled=True))
        candidate = build_candidate(pair, {"ee-primary": 18081, "de-backup": 18082})
        data = render_config(candidate, "/run/gost-manager-nginx/nginx.pid")
        text = data.decode("ascii")
        name = upstream_name("route-estonia")
        self.assertIn(f"upstream {name}", text)
        self.assertIn("server 127.0.0.1:18081 max_fails=1 fail_timeout=10s;", text)
        self.assertIn("server 127.0.0.1:18082 max_fails=1 fail_timeout=10s backup;", text)
        self.assertIn("location = /ee1/api/v1", text)
        self.assertIn("proxy_pass http://", text)
        self.assertNotIn("proxy_pass http://" + name + "/", text)
        self.assertNotIn("/etc/nginx", text)
        self.assertNotIn("secret", text.lower())

    def test_active_active_uses_least_connections_without_backup(self) -> None:
        pair = add_secondary(
            make_pair(
                gateway_enabled=True,
                route_enabled=True,
                strategy=Strategy.ACTIVE_ACTIVE,
            )
        )
        text = render_config(
            build_candidate(pair, {"ee-primary": 18081, "de-backup": 18082}),
            "/run/gost-manager-nginx/nginx.pid",
        ).decode("ascii")
        self.assertIn("least_conn;", text)
        self.assertNotIn(" backup;", text)

    def test_manifest_round_trip_and_duplicate_key_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            paths = NginxPaths.from_values(
                root / "generated", root / "backups", root / "lock", root / "run",
                root / "unit", root / "runner", root / "nginx", root / "launcher",
            )
            pair = make_pair(gateway_enabled=True, route_enabled=True)
            candidate = build_candidate(pair, {"ee-primary": 18081})
            config = render_config(candidate, str(paths.pid_file))
            data = render_manifest(candidate, config, paths, "2026-07-12T18:00:00Z")
            manifest = parse_manifest(data, paths)
            self.assertEqual(1, manifest.schema_version)
            self.assertEqual(("ee-primary",), manifest.routes[0].backend_exit_ids)
            self.assertNotIn(b"secret", data.lower())
            malformed = data.replace(b'{"applied_at"', b'{"schema_version":1,"applied_at"', 1)
            with self.assertRaises(StateError):
                parse_manifest(malformed, paths)

    def test_stub_status_and_cgroup_parsers(self) -> None:
        self.assertEqual(4, parse_stub_status(STATUS_TEXT).active)
        self.assertEqual((2, 9), parse_cgroup_pids("9\n2\n"))
        with self.assertRaises(ValidationError):
            parse_stub_status("empty")
        with self.assertRaises(ValidationError):
            parse_cgroup_pids("1\nbad\n")


class NginxPlanApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = NginxFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def lifecycle(self, action: str) -> list[tuple[str, ...]]:
        return [
            item for item in self.fixture.system.commands
            if item[:2] == ("systemctl", action) and item[-1] == NGINX_SERVICE_NAME
        ]

    def test_first_activation_and_unchanged_apply(self) -> None:
        self.assertEqual("create", self.fixture.manager.plan().action)
        result = self.fixture.manager.apply(yes=True)
        self.assertTrue(result.changed)
        self.assertEqual(0, result.restart_count)
        self.assertTrue(self.fixture.system.active)
        before = len(self.fixture.system.commands)
        unchanged = self.fixture.manager.apply(yes=True)
        self.assertFalse(unchanged.changed)
        commands = self.fixture.system.commands[before:]
        self.assertFalse(any(item[:2] in {("systemctl", "reload"), ("systemctl", "restart")} for item in commands))

    def test_metadata_only_change_never_reloads(self) -> None:
        self.fixture.manager.apply(yes=True)
        pair = self.fixture.pair
        route = replace(pair.shared.routes[0], display_name="Renamed route")
        self.fixture.replace_pair(
            StatePair(
                replace(pair.shared, revision=pair.shared.revision + 1, routes=(route,)),
                pair.node,
            )
        )
        plan = self.fixture.manager.plan()
        self.assertEqual("metadata-update", plan.action)
        before = len(self.lifecycle("reload"))
        self.fixture.manager.apply(yes=True)
        self.assertEqual(before, len(self.lifecycle("reload")))

    def test_effective_change_uses_one_reload_and_preserves_master_pid(self) -> None:
        self.fixture.manager.apply(yes=True)
        pair = self.fixture.pair
        route = replace(pair.shared.routes[0], path="/ee2/api/v1")
        self.fixture.replace_pair(
            StatePair(
                replace(pair.shared, revision=pair.shared.revision + 1, routes=(route,)),
                pair.node,
            )
        )
        result = self.fixture.manager.apply(yes=True)
        self.assertEqual(1, result.reload_count)
        self.assertEqual(0, result.restart_count)
        self.assertEqual(5100, self.fixture.system.main_pid)
        self.assertEqual(1, len(self.lifecycle("reload")))
        self.assertEqual(0, len(self.lifecycle("restart")))

    def test_failed_reload_restores_previous_files_and_service(self) -> None:
        self.fixture.manager.apply(yes=True)
        before_config = self.fixture.paths.config_file.read_bytes()
        before_manifest = self.fixture.paths.manifest_file.read_bytes()
        pair = self.fixture.pair
        route = replace(pair.shared.routes[0], path="/changed")
        self.fixture.replace_pair(
            StatePair(replace(pair.shared, revision=2, routes=(route,)), pair.node)
        )
        self.fixture.system.fail_reload = True
        with self.assertRaises(Exception):
            self.fixture.manager.apply(yes=True)
        self.assertEqual(before_config, self.fixture.paths.config_file.read_bytes())
        self.assertEqual(before_manifest, self.fixture.paths.manifest_file.read_bytes())
        self.assertTrue(self.fixture.system.active)

    def test_gateway_disable_removes_only_managed_runtime(self) -> None:
        self.fixture.manager.apply(yes=True)
        pair = self.fixture.pair
        self.fixture.replace_pair(
            StatePair(replace(pair.shared, gateway=replace(pair.shared.gateway, enabled=False)), pair.node)
        )
        result = self.fixture.manager.apply(yes=True)
        self.assertTrue(result.changed)
        self.assertFalse(self.fixture.paths.config_file.exists())
        self.assertFalse(self.fixture.paths.manifest_file.exists())
        self.assertFalse(self.fixture.system.active)

    def test_unmanaged_port_owner_is_conflict(self) -> None:
        self.fixture.system.conflict_listener = (
            'LISTEN 0 128 0.0.0.0:80 0.0.0.0:* users:(("other",pid=9000,fd=4))'
        )
        plan = self.fixture.manager.plan()
        self.assertEqual("conflict", plan.action)
        self.assertIn("listener_port_conflict", plan.reason_codes[0])

    def test_same_service_workers_are_accepted(self) -> None:
        self.fixture.manager.apply(yes=True)
        plan = self.fixture.manager.plan()
        self.assertEqual("no-op", plan.action)

    def test_backend_failure_is_conflict_without_gost_lifecycle(self) -> None:
        self.fixture.ready.fail = True
        plan = self.fixture.manager.plan()
        self.assertEqual("conflict", plan.action)
        self.assertFalse(
            any(
                item[0] == "systemctl" and item[-1].startswith("gost-gateway-exit-")
                for item in self.fixture.system.commands
            )
        )

    def test_plan_is_read_only(self) -> None:
        ignored = {"runtime.lock", "nginx.lock"}
        before = sorted(
            str(item.relative_to(self.fixture.root))
            for item in self.fixture.root.rglob("*")
            if item.name not in ignored
        )
        self.fixture.manager.plan()
        after = sorted(
            str(item.relative_to(self.fixture.root))
            for item in self.fixture.root.rglob("*")
            if item.name not in ignored
        )
        self.assertEqual(before, after)
        self.assertFalse(any(item[0] == "systemctl" and item[1] in {"start", "stop", "reload", "restart", "enable", "disable"} for item in self.fixture.system.commands))

    def test_config_without_manifest_is_conflict_and_untouched(self) -> None:
        self.fixture.paths.generated_dir.mkdir(mode=0o700)
        self.fixture.paths.config_file.write_text("unmanaged\n", encoding="ascii")
        plan = self.fixture.manager.plan()
        self.assertEqual("conflict", plan.action)
        self.assertEqual(b"unmanaged\n", self.fixture.paths.config_file.read_bytes())

    def test_service_restart_needs_both_acknowledgements(self) -> None:
        self.fixture.manager.apply(yes=True)
        with self.assertRaises(ConflictError):
            self.fixture.manager.service_control("restart", yes=True)
        self.fixture.manager.service_control(
            "restart", yes=True, acknowledge_disconnect=True
        )
        self.assertEqual(1, len(self.lifecycle("restart")))

    def test_failure_injection_restores_first_activation_boundaries(self) -> None:
        phases = (
            "after_backup_creation",
            "after_config_replacement",
            "after_installed_nginx_test",
            "after_service_enable",
            "after_service_start",
            "after_public_listener_verification",
            "after_status_listener_verification",
            "after_status_probe",
            "after_manifest_replacement",
            "after_parent_fsync",
            "after_backup_pruning",
            "after_final_verification",
            "after_current_backup_removal",
            "after_backup_parent_fsync",
        )
        self.assertTrue(set(phases).issubset(set(FAILURE_PHASES)))
        for phase in phases:
            with self.subTest(phase=phase):
                fixture = NginxFixture()

                def fail(current):
                    if current == phase:
                        raise RuntimeError("injected")

                fixture.manager.failure_hook = fail
                with self.assertRaises(Exception):
                    fixture.manager.apply(yes=True)
                self.assertFalse(fixture.paths.config_file.exists())
                self.assertFalse(fixture.paths.manifest_file.exists())
                self.assertFalse(fixture.system.active)
                self.assertFalse(fixture.system.enabled)
                fixture.close()

    def test_direct_mode_unmanaged_nginx_monitoring_and_firewall_are_isolated(self) -> None:
        direct = self.fixture.root / "etc/gost/iran-1.env"
        direct_unit = self.fixture.root / "etc/systemd/system/gost-iran-1.service"
        unmanaged = self.fixture.root / "etc/nginx/nginx.conf"
        monitoring = self.fixture.root / "var/lib/gost-manager/metrics.sqlite3"
        for path, data in (
            (direct, b"MAPPINGS=2052:2052\n"),
            (direct_unit, b"[Unit]\nDescription=Direct\n"),
            (unmanaged, b"events {}\n"),
            (monitoring, b"monitor-history\n"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o640)
        before = {
            path: (path.read_bytes(), path.stat().st_mode & 0o777)
            for path in (direct, direct_unit, unmanaged, monitoring)
        }
        self.fixture.manager.apply(yes=True)
        pair = self.fixture.pair
        changed = StatePair(
            replace(
                pair.shared,
                revision=2,
                routes=(replace(pair.shared.routes[0], path="/reload"),),
            ),
            pair.node,
        )
        self.fixture.replace_pair(changed)
        self.fixture.manager.apply(yes=True)
        for path, expected in before.items():
            self.assertEqual(expected, (path.read_bytes(), path.stat().st_mode & 0o777))
        command_text = "\n".join(" ".join(item) for item in self.fixture.system.commands)
        for forbidden in (
            "gost-iran-1.service",
            "gost-kharej-1.service",
            "gost-monitor-collector.service",
            "nginx.service",
            "iptables",
            "nft",
        ):
            self.assertNotIn(forbidden, command_text)

    def test_secret_canary_never_reaches_config_manifest_or_backup(self) -> None:
        canary = "secret-canary-should-not-appear"
        self.fixture.runtime_paths.secret_dir.mkdir(mode=0o700)
        secret_file = self.fixture.runtime_paths.secret_dir / "canary.env"
        secret_file.write_text(
            f"GOST_USER={canary}\nGOST_PASS={canary}-pass\n", encoding="ascii"
        )
        secret_file.chmod(0o600)
        self.fixture.manager.apply(yes=True)
        values = [
            self.fixture.paths.config_file.read_bytes(),
            self.fixture.paths.manifest_file.read_bytes(),
        ]
        values.extend(
            item.read_bytes()
            for item in self.fixture.paths.backup_dir.rglob("*")
            if item.is_file()
        )
        self.assertNotIn(canary.encode("ascii"), b"".join(values))

    def test_lock_order_is_state_then_runtime_then_nginx(self) -> None:
        events: list[str] = []

        class Lock:
            def __init__(self, name):
                self.name = name

            def __enter__(self):
                events.append(self.name)
                return self

            def __exit__(self, *_args):
                events.append(f"release-{self.name}")

        state = GatewayStateStore(
            self.fixture.state.paths,
            lock_factory=lambda _path, _timeout: Lock("state"),
        )
        secrets = SecretStore(
            self.fixture.runtime_paths,
            lock_factory=lambda _path, _timeout: Lock("runtime"),
        )
        manager = NginxManager(
            state,
            secrets,
            self.fixture.runtime_paths,
            self.fixture.paths,
            inspector=self.fixture.inspector,
            backend_readiness=self.fixture.ready,
            lock_factory=lambda _path, _timeout: Lock("nginx"),
        )
        manager.plan()
        self.assertEqual(["state", "runtime", "nginx"], events[:3])


class NginxDependencyTests(unittest.TestCase):
    def test_existing_binary_is_noop_and_never_touches_distro_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for name in ("generated", "backups", "run", "systemd", "bin"):
                root.joinpath(name).mkdir()
            paths = NginxPaths.from_values(
                root / "generated", root / "backups", root / "lock", root / "run",
                root / "systemd/unit", root / "runner", root / "bin/nginx", root / "launcher",
            )
            paths.nginx_bin.write_text("#!/bin/sh\n", encoding="ascii")
            paths.nginx_bin.chmod(0o755)
            system = FakeNginxSystem(root, paths)
            inspector = NginxInspector(system.runner, cgroup_root=root / "cgroup", status_reader=system.status_reader)
            manager = NginxDependencyManager(paths, inspector)
            self.assertEqual("no-op", manager.install(yes=True, is_root=True))
            self.assertFalse(any(item[-1] == "nginx.service" and item[1] in {"start", "stop", "enable", "disable"} for item in system.commands if item and item[0] == "systemctl"))

    def test_install_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for name in ("generated", "backups", "run", "systemd", "bin"):
                root.joinpath(name).mkdir()
            paths = NginxPaths.from_values(
                root / "generated", root / "backups", root / "lock", root / "run",
                root / "systemd/unit", root / "runner", root / "bin/nginx", root / "launcher",
            )
            system = FakeNginxSystem(root, paths)
            inspector = NginxInspector(system.runner, cgroup_root=root / "cgroup", status_reader=system.status_reader)
            with self.assertRaises(ConflictError):
                NginxDependencyManager(paths, inspector).install(yes=False, is_root=True)
            self.assertFalse(any(item and item[0] == "apt-get" for item in system.commands))

    def test_new_install_stops_and_disables_only_new_distro_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for name in ("generated", "backups", "run", "systemd", "bin"):
                root.joinpath(name).mkdir()
            paths = NginxPaths.from_values(
                root / "generated", root / "backups", root / "lock", root / "run",
                root / "systemd/unit", root / "runner", root / "bin/nginx", root / "launcher",
            )
            system = FakeNginxSystem(root, paths)
            inspector = NginxInspector(system.runner, cgroup_root=root / "cgroup", status_reader=system.status_reader)
            manager = NginxDependencyManager(paths, inspector)
            self.assertEqual("installed", manager.install(yes=True, is_root=True))
            self.assertFalse(system.distro_active)
            self.assertFalse(system.distro_enabled)
            self.assertFalse(system.active)
            self.assertFalse(paths.config_file.exists())


class NginxStoreTests(unittest.TestCase):
    def test_symlink_and_hardlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for name in ("generated", "backups", "run", "systemd", "bin"):
                root.joinpath(name).mkdir()
            paths = NginxPaths.from_values(
                root / "generated", root / "backups", root / "lock", root / "run",
                root / "systemd/unit", root / "runner", root / "bin/nginx", root / "launcher",
            )
            outside = root / "outside"
            outside.write_text("data", encoding="ascii")
            paths.config_file.symlink_to(outside)
            with self.assertRaises(ValidationError):
                NginxStore(paths).read_optional(paths.config_file, 100)


class GostBackendReadinessTests(unittest.TestCase):
    def test_exact_generated_runtime_cgroup_and_listener_are_required(self) -> None:
        temporary_state = TemporaryStore()
        state = temporary_state.initialize()
        pair = make_pair(gateway_enabled=True, route_enabled=True)
        state.paths.state_file.write_bytes(serialize_shared(pair.shared))
        state.paths.node_file.write_bytes(serialize_node(pair.node))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "systemd").mkdir()
            paths = RuntimePaths.from_values(
                root / "secrets", root / "generated", root / "backups", root / "lock",
                root / "systemd", root / "runner", root / "gost",
            )
            secrets = SecretStore(paths)
            secrets.set("secret-ee-primary", Credentials("docs-user", "docs-pass"))
            desired = select_desired(pair, secrets)[0]
            env, unit = render_env(desired), render_unit(desired, paths)
            store = RuntimeStore(paths)
            store.prepare()
            store.write_atomic(paths.env_file(desired.exit_id), env)
            store.write_atomic(paths.unit_file(desired.exit_id), unit, 0o644)
            entry = make_entry(desired, paths, env, unit)
            store.write_atomic(
                paths.manifest_file,
                render_gost_manifest(
                    applied_at="2026-07-12T18:00:00Z",
                    document_id=pair.shared.document_id,
                    shared_revision=pair.shared.revision,
                    node_revision=pair.node.revision,
                    entries=(entry,),
                ),
            )

            class Inspector:
                def service_state(self, exit_id):
                    return ServiceState(
                        service_name(exit_id), True, True, True, 7000,
                        "/system.slice/gost.service", (7000,), True,
                    )

            readiness = GostBackendReadiness(
                paths, secrets, inspector=Inspector(), runtime_store=store
            )
            listeners = (Listener("127.0.0.1", 18081, (7000,), ("gost",)),)
            self.assertEqual(
                {"ee-primary": 18081},
                readiness.ready_ports(pair, {"ee-primary"}, listeners),
            )
            unrelated = add_secondary(pair)
            unrelated = StatePair(
                replace(
                    unrelated.shared,
                    routes=(
                        replace(unrelated.shared.routes[0], exit_ids=("ee-primary",)),
                    ),
                ),
                unrelated.node,
            )
            self.assertEqual(
                {"ee-primary": 18081},
                readiness.ready_ports(unrelated, {"ee-primary"}, listeners),
            )
            with self.assertRaises(ConflictError):
                readiness.ready_ports(
                    pair,
                    {"ee-primary"},
                    (Listener("127.0.0.1", 18081, (9000,), ("other",)),),
                )
            secrets.set("secret-ee-primary", Credentials("rotated", "rotated-pass"))
            with self.assertRaises(ConflictError):
                readiness.ready_ports(pair, {"ee-primary"}, listeners)
        temporary_state.close()


if __name__ == "__main__":
    unittest.main()
