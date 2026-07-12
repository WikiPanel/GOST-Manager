from __future__ import annotations

import fcntl
import json
import os
import stat
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from gateway.crud import GatewayCRUD
from gateway.errors import ConflictError, ValidationError
from gateway.locking import GatewayStateLock
from gateway.models import ExitNode
from gateway.paths import StatePaths
from gateway.serialization import parse_node, parse_shared
from gateway.store import (
    BACKUP_LIMIT,
    BACKUP_RE,
    FAILURE_PHASES,
    FileOperations,
    GatewayStateStore,
)
from test_gateway_support import TemporaryStore


class GatewayStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryStore()

    def tearDown(self) -> None:
        self.temporary.close()

    def test_init_creates_private_files_and_directories(self) -> None:
        self.temporary.initialize()
        paths = self.temporary.paths
        self.assertEqual(stat.S_IMODE(paths.state_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths.node_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths.lock_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths.backup_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(paths.state_file.parent.stat().st_mode), 0o700)

    def test_init_uses_one_document_id_timestamp_and_revision(self) -> None:
        pair = self.temporary.initialize().load_pair()
        self.assertEqual(pair.shared.document_id, pair.node.document_id)
        self.assertEqual(pair.shared.updated_at, pair.node.updated_at)
        self.assertEqual(pair.shared.revision, 1)
        self.assertEqual(pair.node.revision, 1)

    def test_clock_uuid_replace_and_fsync_are_injectable(self) -> None:
        replacements: list[tuple[str, str]] = []
        fsync_calls: list[int] = []

        def injected_replace(source: str, destination: str) -> None:
            replacements.append((source, destination))
            os.replace(source, destination)

        def injected_fsync(descriptor: int) -> None:
            fsync_calls.append(descriptor)
            os.fsync(descriptor)

        operations = replace(
            FileOperations(), replace=injected_replace, fsync=injected_fsync
        )
        pair = self.temporary.initialize(file_operations=operations).load_pair()
        self.assertEqual(pair.shared.document_id, "00000000-0000-4000-8000-000000000001")
        self.assertEqual(pair.shared.updated_at, "2026-07-12T00:00:00Z")
        self.assertGreaterEqual(len(replacements), 2)
        self.assertGreaterEqual(len(fsync_calls), 4)

    def test_init_conflict_does_not_create_backup_or_node_state(self) -> None:
        self.temporary.paths.state_file.write_text("operator state\n")
        with self.assertRaises(ConflictError):
            self.temporary.store().initialize(
                gateway_id="gateway-main",
                node_id="iran-gateway-1",
                listen_address="0.0.0.0",
                listen_port=80,
                server_names=["gateway.example.org"],
            )
        self.assertEqual(self.temporary.paths.state_file.read_text(), "operator state\n")
        self.assertFalse(self.temporary.paths.node_file.exists())
        self.assertFalse(self.temporary.paths.backup_dir.exists())

    def test_init_rolls_back_at_both_document_boundaries(self) -> None:
        for phase in ("after_first_document_init", "after_second_document_init"):
            with self.subTest(phase=phase):
                temporary = TemporaryStore()

                def fail(current: str) -> None:
                    if current == phase:
                        raise OSError("injected failure")

                with self.assertRaises(OSError):
                    temporary.initialize(failure_hook=fail)
                self.assertFalse(temporary.paths.state_file.exists())
                self.assertFalse(temporary.paths.node_file.exists())
                self.assertEqual(
                    list(temporary.root.glob(".*.new.*")), []
                )
                temporary.close()

    def test_init_failure_boundaries_never_leave_a_partial_pair(self) -> None:
        phases = (
            "after_candidate_serialization",
            "after_lock_acquisition",
            "after_temporary_creation",
            "after_temporary_write",
            "after_file_fsync",
            "after_atomic_replacement",
            "after_parent_fsync",
            "after_post_replace_validation",
            "after_backup_pruning",
            "after_first_document_init",
            "after_second_document_init",
        )
        for phase in phases:
            with self.subTest(phase=phase):
                temporary = TemporaryStore()
                fired = False

                def fail(current: str) -> None:
                    nonlocal fired
                    if current == phase and not fired:
                        fired = True
                        raise OSError("injected failure")

                with self.assertRaises(OSError):
                    temporary.initialize(failure_hook=fail)
                self.assertTrue(fired)
                self.assertFalse(temporary.paths.state_file.exists())
                self.assertFalse(temporary.paths.node_file.exists())
                temporary_files = [
                    path
                    for path in temporary.root.rglob(".*")
                    if path.is_file()
                    and (".new." in path.name or ".restore." in path.name)
                ]
                self.assertEqual(temporary_files, [])
                temporary.close()

    def test_all_mutation_failure_boundaries_preserve_active_state(self) -> None:
        initial = self.temporary.initialize()
        original = self.temporary.paths.state_file.read_bytes()
        mutation_phases = tuple(
            phase
            for phase in FAILURE_PHASES
            if phase
            not in {"after_first_document_init", "after_second_document_init"}
        )
        for phase in mutation_phases:
            with self.subTest(phase=phase):
                fired = False

                def fail(current: str) -> None:
                    nonlocal fired
                    if current == phase and not fired:
                        fired = True
                        raise OSError("injected failure")

                store = self.temporary.store(failure_hook=fail)
                with self.assertRaises(OSError):
                    GatewayCRUD(store).set_gateway(enabled=True)
                self.assertTrue(fired)
                self.assertEqual(self.temporary.paths.state_file.read_bytes(), original)
                self.assertEqual(parse_shared(original).revision, 1)
                self.assertEqual(initial.load_pair().shared.revision, 1)
                temporary_files = [
                    path
                    for path in self.temporary.root.rglob(".*")
                    if path.is_file() and (".new." in path.name or ".restore." in path.name)
                ]
                self.assertEqual(temporary_files, [])

    def test_failed_node_mutation_preserves_node_and_shared_pair(self) -> None:
        store = self.temporary.initialize()
        GatewayCRUD(store).add_exit(
            exit_id="ee-primary",
            display_name="Estonia",
            host="192.0.2.10",
            socks_port=28420,
            target_port=18081,
        )
        shared_before = self.temporary.paths.state_file.read_bytes()
        node_before = self.temporary.paths.node_file.read_bytes()

        def fail(phase: str) -> None:
            if phase == "after_atomic_replacement":
                raise OSError("injected failure")

        with self.assertRaises(OSError):
            GatewayCRUD(self.temporary.store(failure_hook=fail)).set_binding(
                exit_id="ee-primary",
                listen_port=18081,
                secret_ref="secret-ee-primary",
                enabled=True,
            )
        self.assertEqual(self.temporary.paths.state_file.read_bytes(), shared_before)
        self.assertEqual(self.temporary.paths.node_file.read_bytes(), node_before)
        parse_shared(shared_before)
        parse_node(node_before)

    def test_backup_and_rollback_preserve_exact_previous_bytes(self) -> None:
        store = self.temporary.initialize()
        value = json.loads(self.temporary.paths.state_file.read_text())
        previous = (json.dumps(value, separators=(",", ":")) + "\n").encode()
        self.temporary.paths.state_file.write_bytes(previous)
        self.temporary.paths.state_file.chmod(0o600)

        GatewayCRUD(store).set_gateway(enabled=True)
        backup = next(self.temporary.paths.backup_dir.glob("shared-r1-*.json"))
        self.assertEqual(backup.read_bytes(), previous)

        current = self.temporary.paths.state_file.read_bytes()

        def fail(phase: str) -> None:
            if phase == "after_atomic_replacement":
                raise OSError("injected failure")

        with self.assertRaises(OSError):
            GatewayCRUD(self.temporary.store(failure_hook=fail)).set_gateway(
                enabled=False
            )
        self.assertEqual(self.temporary.paths.state_file.read_bytes(), current)

    def test_backups_are_bounded_per_document_and_unmanaged_files_survive(self) -> None:
        store = self.temporary.initialize()
        crud = GatewayCRUD(store)
        unmanaged = self.temporary.paths.backup_dir / "operator-note.txt"
        unmanaged.write_text("keep\n")
        symlink = self.temporary.paths.backup_dir / "shared-r1-not-managed.json"
        symlink.symlink_to(unmanaged)
        for index in range(BACKUP_LIMIT + 4):
            crud.set_gateway(status_port=19000 + index)
        shared_backups = [
            path
            for path in self.temporary.paths.backup_dir.iterdir()
            if BACKUP_RE.fullmatch(path.name)
            and path.name.startswith("shared-")
            and path.is_file()
        ]
        self.assertEqual(len(shared_backups), BACKUP_LIMIT)
        self.assertTrue(unmanaged.exists())
        self.assertTrue(symlink.is_symlink())
        self.assertEqual(unmanaged.read_text(), "keep\n")
        self.assertTrue(
            all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in shared_backups)
        )

    def test_node_backups_are_bounded_independently(self) -> None:
        store = self.temporary.initialize()
        crud = GatewayCRUD(store)
        crud.add_exit(
            exit_id="ee-primary",
            display_name="Estonia",
            host="192.0.2.10",
            socks_port=28420,
            target_port=18081,
        )
        for index in range(BACKUP_LIMIT + 3):
            crud.set_binding(
                exit_id="ee-primary",
                listen_port=20000 + index,
                secret_ref="secret-ee-primary",
                enabled=True,
            )
        node_backups = [
            path
            for path in self.temporary.paths.backup_dir.iterdir()
            if BACKUP_RE.fullmatch(path.name)
            and path.name.startswith("node-")
            and path.is_file()
        ]
        self.assertEqual(len(node_backups), BACKUP_LIMIT)

    def test_expected_revision_conflict_creates_no_backup(self) -> None:
        store = self.temporary.initialize()
        with self.assertRaises(ConflictError):
            GatewayCRUD(store).set_gateway(enabled=True, expected_revision=2)
        self.assertEqual(list(self.temporary.paths.backup_dir.iterdir()), [])

    def test_store_refuses_gateway_and_node_identity_changes(self) -> None:
        store = self.temporary.initialize()
        with self.assertRaisesRegex(ConflictError, "gateway ID"):
            store.mutate_shared(
                lambda shared: replace(
                    shared,
                    gateway=replace(shared.gateway, id="other-gateway"),
                )
            )
        with self.assertRaisesRegex(ConflictError, "node ID"):
            store.mutate_node(
                lambda node: replace(node, node_id="other-node")
            )
        pair = store.load_pair()
        self.assertEqual(pair.shared.gateway.id, "gateway-main")
        self.assertEqual(pair.node.node_id, "iran-gateway-1")

    def test_stale_unheld_lock_file_is_reused(self) -> None:
        self.temporary.paths.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.temporary.paths.lock_file.write_text("stale-holder\n")
        store = self.temporary.initialize()
        self.assertEqual(store.load_pair().shared.revision, 1)
        self.assertEqual(self.temporary.paths.lock_file.read_text(), "gateway-state\n")

    def test_lock_contention_has_bounded_conflict(self) -> None:
        self.temporary.initialize()
        descriptor = os.open(self.temporary.paths.lock_file, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            store = GatewayStateStore(self.temporary.paths, lock_timeout=0)
            with self.assertRaisesRegex(ConflictError, "busy"):
                store.load_pair()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def test_lock_releases_after_validation_failure_and_interrupt(self) -> None:
        path = self.temporary.paths.lock_file
        for exception in (ValidationError("invalid"), KeyboardInterrupt()):
            with self.subTest(exception=type(exception).__name__):
                with self.assertRaises(type(exception)):
                    with GatewayStateLock(path, timeout=0):
                        raise exception
                with GatewayStateLock(path, timeout=0):
                    pass

    def test_concurrent_writers_do_not_lose_updates(self) -> None:
        store = self.temporary.initialize()
        barrier = threading.Barrier(12)
        errors: list[BaseException] = []

        def writer(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                GatewayCRUD(store).add_exit(
                    exit_id=f"exit-{index:02d}",
                    display_name=f"Exit {index}",
                    host=f"exit-{index:02d}.example.org",
                    socks_port=20000 + index,
                    target_port=30000 + index,
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(index,)) for index in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        pair = store.load_pair()
        self.assertEqual(len(pair.shared.exits), 12)
        self.assertEqual(pair.shared.revision, 13)

    def test_state_file_symlink_is_rejected_and_target_unchanged(self) -> None:
        outside = self.temporary.root / "outside.json"
        outside.write_text("outside\n")
        self.temporary.paths.state_file.symlink_to(outside)
        with self.assertRaises(ValidationError):
            self.temporary.initialize()
        self.assertEqual(outside.read_text(), "outside\n")
        self.assertTrue(self.temporary.paths.state_file.is_symlink())

    def test_parent_symlink_is_rejected(self) -> None:
        outside = self.temporary.root / "outside"
        outside.mkdir()
        linked = self.temporary.root / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        paths = StatePaths.from_values(
            linked / "state.json",
            linked / "node.json",
            linked / "backups",
            self.temporary.root / "lock",
        )
        with self.assertRaises(ValidationError):
            GatewayStateStore(paths).initialize(
                gateway_id="gateway-main",
                node_id="iran-gateway-1",
                listen_address="0.0.0.0",
                listen_port=80,
                server_names=["gateway.example.org"],
            )
        self.assertEqual(list(outside.iterdir()), [])

    def test_node_backup_and_lock_symlinks_are_rejected_without_following(self) -> None:
        for kind in ("node", "backup", "lock"):
            with self.subTest(kind=kind):
                temporary = TemporaryStore()
                outside_file = temporary.root / "outside-file"
                outside_file.write_text("outside\n")
                outside_dir = temporary.root / "outside-dir"
                outside_dir.mkdir()
                if kind == "node":
                    temporary.paths.node_file.symlink_to(outside_file)
                elif kind == "backup":
                    temporary.paths.backup_dir.symlink_to(
                        outside_dir, target_is_directory=True
                    )
                else:
                    temporary.paths.lock_file.symlink_to(outside_file)
                with self.assertRaises(ValidationError):
                    temporary.initialize()
                self.assertEqual(outside_file.read_text(), "outside\n")
                self.assertEqual(list(outside_dir.iterdir()), [])
                temporary.close()

    def test_existing_state_parent_mode_is_not_changed(self) -> None:
        public_parent = self.temporary.root / "public-state"
        public_parent.mkdir(mode=0o755)
        public_parent.chmod(0o755)
        paths = StatePaths.from_values(
            public_parent / "state.json",
            public_parent / "node.json",
            self.temporary.root / "private-backups",
            self.temporary.root / "gateway-state.lock",
        )
        GatewayStateStore(paths).initialize(
            gateway_id="gateway-main",
            node_id="iran-gateway-1",
            listen_address="0.0.0.0",
            listen_port=80,
            server_names=["gateway.example.org"],
        )
        self.assertEqual(stat.S_IMODE(public_parent.stat().st_mode), 0o755)

    def test_existing_public_lock_parent_is_refused_without_mode_change(self) -> None:
        public_parent = self.temporary.root / "public-lock"
        public_parent.mkdir(mode=0o755)
        public_parent.chmod(0o755)
        paths = StatePaths.from_values(
            self.temporary.root / "state.json",
            self.temporary.root / "node.json",
            self.temporary.root / "backups",
            public_parent / "gateway-state.lock",
        )
        with self.assertRaisesRegex(ValidationError, "mode 0700"):
            GatewayStateStore(paths).initialize(
                gateway_id="gateway-main",
                node_id="iran-gateway-1",
                listen_address="0.0.0.0",
                listen_port=80,
                server_names=["gateway.example.org"],
            )
        self.assertEqual(stat.S_IMODE(public_parent.stat().st_mode), 0o755)

    def test_non_regular_active_file_is_rejected(self) -> None:
        self.temporary.paths.state_file.mkdir()
        with self.assertRaises((ValidationError, ConflictError)):
            self.temporary.initialize()

    def test_paths_must_be_absolute_normalized_and_distinct(self) -> None:
        invalid = (
            ("relative/state.json", "absolute normalized"),
            (str(self.temporary.root / "a" / ".." / "state.json"), "normalized"),
            (str(self.temporary.root / "bad\nstate.json"), "forbidden"),
        )
        for value, message in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(ValidationError, message):
                StatePaths.from_values(
                    value,
                    self.temporary.paths.node_file,
                    self.temporary.paths.backup_dir,
                    self.temporary.paths.lock_file,
                )
        with self.assertRaisesRegex(ValidationError, "different"):
            StatePaths.from_values(
                self.temporary.paths.state_file,
                self.temporary.paths.state_file,
                self.temporary.paths.backup_dir,
                self.temporary.paths.lock_file,
            )


if __name__ == "__main__":
    unittest.main()
