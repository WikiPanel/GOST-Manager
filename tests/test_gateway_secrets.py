from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from gateway.errors import ConflictError, OperationalError, StateError, ValidationError
from gateway.runtime_models import Credentials
from gateway.runtime_paths import RuntimePaths
from gateway.secrets import SecretStore, parse_secret, parse_secret_json, render_secret


def credentials() -> Credentials:
    # Values are generated at test runtime and never used as production fixtures.
    suffix = os.urandom(8).hex()
    return Credentials(f"test-user-{suffix}", f"test-pass-{suffix}")


class SecretParserTests(unittest.TestCase):
    def test_exact_two_line_round_trip(self) -> None:
        value = credentials()
        self.assertEqual(value, parse_secret(render_secret(value)))

    def test_rejects_malformed_files_without_echoing_values(self) -> None:
        canary = credentials().password
        invalid = (
            b"GOST_USER=user\nGOST_PASS=pass",
            b"GOST_USER=user\nGOST_PASS=pass\n\n",
            b"GOST_USER=user\nUNKNOWN=pass\n",
            b"GOST_USER=user\nGOST_USER=pass\n",
            b"GOST_PASS=pass\nGOST_USER=user\n",
            f"GOST_USER=user\nGOST_PASS={canary}:bad\n".encode(),
            b"GOST_USER='user'\nGOST_PASS=pass\n",
        )
        for data in invalid:
            with self.subTest(data=data[:20]), self.assertRaises(ValidationError) as caught:
                parse_secret(data)
            self.assertNotIn(canary, str(caught.exception))

    def test_stdin_json_is_strict_and_bounded(self) -> None:
        value = credentials()
        data = json.dumps({"username": value.username, "password": value.password}).encode()
        self.assertEqual(value, parse_secret_json(data))
        for invalid in (
            b'{"username":"a","username":"b","password":"c"}',
            b'{"username":"a","password":"b","extra":true}',
            b"x" * 1025,
        ):
            with self.assertRaises(ValidationError):
                parse_secret_json(invalid)

    def test_forbidden_credential_characters(self) -> None:
        for character in ":@/\\ '" + '"`$;&|<>(){}':
            with self.subTest(character=character), self.assertRaises(ValidationError):
                parse_secret_json(
                    json.dumps({"username": "user", "password": f"bad{character}value"}).encode()
                )


class SecretStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()
        self.paths = RuntimePaths.from_values(
            root / "secrets", root / "generated", root / "backups", root / "runtime.lock",
            root / "systemd", root / "runner", root / "gost",
        )
        self.store = SecretStore(self.paths)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_atomic_create_update_permissions_and_no_backup(self) -> None:
        first, second = credentials(), credentials()
        self.assertEqual("created", self.store.set("secret-primary", first))
        path = self.paths.secret_file("secret-primary")
        self.assertEqual(0o700, stat.S_IMODE(self.paths.secret_dir.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual("updated", self.store.set("secret-primary", second))
        self.assertEqual(second, self.store.read("secret-primary")[0])
        self.assertEqual([path.name], sorted(item.name for item in self.paths.secret_dir.iterdir()))

    def test_failed_post_write_validation_restores_old_bytes(self) -> None:
        first = credentials()
        self.store.set("secret-primary", first)
        path = self.paths.secret_file("secret-primary")
        before = path.read_bytes()
        failing = SecretStore(
            self.paths,
            failure_hook=lambda phase: (_ for _ in ()).throw(RuntimeError("boom"))
            if phase == "after_secret_replacement" else None,
        )
        with self.assertRaises(RuntimeError):
            failing.set("secret-primary", credentials())
        self.assertEqual(before, path.read_bytes())

    def test_coarse_timestamp_rotations_advance_generation_each_time(self) -> None:
        self.store.set("secret-primary", credentials())
        path = self.paths.secret_file("secret-primary")

        def coarse_replace(source: str, destination: str) -> None:
            previous = Path(destination).stat().st_mtime_ns
            os.replace(source, destination)
            os.utime(destination, ns=(previous, previous))

        coarse = SecretStore(self.paths, replace=coarse_replace)
        generations = [self.store.read("secret-primary")[1]]
        for _index in range(2):
            coarse.set("secret-primary", credentials())
            generations.append(coarse.read("secret-primary")[1])
        self.assertEqual(sorted(set(generations)), generations)
        self.assertEqual(generations[0] + 2, generations[-1])

    def test_unadvanceable_generation_restores_previous_secret(self) -> None:
        original = credentials()
        self.store.set("secret-primary", original)
        path = self.paths.secret_file("secret-primary")
        generation = path.stat().st_mtime_ns

        def coarse_replace(source: str, destination: str) -> None:
            os.replace(source, destination)
            os.utime(destination, ns=(generation, generation))

        def frozen_utime(target, *args, **kwargs) -> None:
            os.utime(target, ns=(generation, generation), follow_symlinks=False)

        frozen = SecretStore(
            self.paths, replace=coarse_replace, utime=frozen_utime
        )
        with self.assertRaises(OperationalError):
            frozen.set("secret-primary", credentials())
        self.assertEqual(original, self.store.read("secret-primary")[0])
        self.assertEqual(generation, path.stat().st_mtime_ns)

    def test_delete_refuses_references_and_removes_only_exact_file(self) -> None:
        self.store.set("secret-primary", credentials())
        with self.store.lock():
            with self.assertRaises(ConflictError):
                self.store.delete_unlocked("secret-primary", ("ee-primary",))
        sibling = self.paths.secret_dir / "unmanaged.txt"
        sibling.write_text("safe", encoding="ascii")
        with self.store.lock():
            self.store.delete_unlocked("secret-primary", ())
        self.assertFalse(self.paths.secret_file("secret-primary").exists())
        self.assertEqual("safe", sibling.read_text(encoding="ascii"))

    def test_symlink_and_hardlink_are_rejected_without_target_change(self) -> None:
        self.paths.secret_dir.mkdir(mode=0o700)
        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(render_secret(credentials()))
        link = self.paths.secret_file("secret-link")
        link.symlink_to(outside)
        with self.assertRaises(ValidationError):
            self.store.read("secret-link")
        before = outside.read_bytes()
        link.unlink()
        os.link(outside, link)
        with self.assertRaises(ValidationError):
            self.store.read("secret-link")
        self.assertEqual(before, outside.read_bytes())

    def test_list_never_exposes_credentials(self) -> None:
        value = credentials()
        self.store.set("secret-primary", value)
        rendered = repr(self.store.list({"secret-primary": ("ee-primary",)}))
        self.assertNotIn(value.username, rendered)
        self.assertNotIn(value.password, rendered)
        self.assertIn("ee-primary", rendered)

    def test_concurrent_writers_leave_one_complete_valid_secret(self) -> None:
        values = [credentials() for _ in range(8)]
        errors: list[Exception] = []

        def write(value: Credentials) -> None:
            try:
                self.store.set("secret-primary", value)
            except Exception as exc:  # pragma: no cover - diagnostic collection
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(value,)) for value in values]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        self.assertIn(self.store.read("secret-primary")[0], values)
