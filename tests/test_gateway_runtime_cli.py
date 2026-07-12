from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gateway.runtime_cli import main


class RuntimeCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()
        (root / "systemd").mkdir()
        self.global_args = [
            "--state-file", str(root / "state.json"),
            "--node-file", str(root / "node.json"),
            "--state-backup-dir", str(root / "state-backups"),
            "--state-lock-file", str(root / "state.lock"),
            "--secret-dir", str(root / "secrets"),
            "--generated-dir", str(root / "generated"),
            "--runtime-backup-dir", str(root / "runtime-backups"),
            "--runtime-lock-file", str(root / "runtime.lock"),
            "--systemd-dir", str(root / "systemd"),
            "--runner-path", str(root / "runner"),
            "--gost-bin", str(root / "gost"),
        ]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, args: list[str], stdin: bytes = b"") -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        stream = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), mock.patch("sys.stdin", stream):
            result = main([*self.global_args, *args])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_stdin_secret_create_and_list_never_expose_canary(self) -> None:
        suffix = os.urandom(8).hex()
        username, password = f"test-user-{suffix}", f"test-pass-{suffix}"
        payload = json.dumps({"username": username, "password": password}).encode()
        code, stdout, stderr = self.run_cli(
            ["--format", "json", "secret", "set", "--ref", "secret-primary", "--stdin-json"],
            payload,
        )
        self.assertEqual(0, code)
        self.assertNotIn(username, stdout + stderr)
        self.assertNotIn(password, stdout + stderr)
        code, stdout, stderr = self.run_cli(["secret", "list"])
        self.assertEqual(0, code)
        self.assertIn("secret-primary", stdout)
        self.assertNotIn(username, stdout + stderr)
        self.assertNotIn(password, stdout + stderr)

    def test_interactive_mode_uses_hidden_prompts_and_confirmation(self) -> None:
        suffix = os.urandom(8).hex()
        username, password = f"interactive-{suffix}", f"password-{suffix}"
        with mock.patch("gateway.runtime_cli.getpass.getpass", side_effect=(username, password, password)) as prompt:
            code, stdout, stderr = self.run_cli(["secret", "set", "--ref", "secret-interactive"])
        self.assertEqual(0, code)
        self.assertEqual(3, prompt.call_count)
        self.assertNotIn(username, stdout + stderr)
        self.assertNotIn(password, stdout + stderr)

    def test_forbidden_secret_options_are_generic_and_do_not_echo_value(self) -> None:
        canary = f"forbidden-{os.urandom(8).hex()}"
        code, stdout, stderr = self.run_cli(
            ["secret", "set", "--ref", "secret-primary", "--username", canary]
        )
        self.assertEqual(2, code)
        self.assertNotIn(canary, stdout + stderr)
    def test_apply_requires_yes_before_any_runtime_operation(self) -> None:
        code, _stdout, stderr = self.run_cli(["runtime", "apply"])
        self.assertEqual(4, code)
        self.assertIn("confirmation", stderr)

    def test_invalid_json_error_does_not_echo_input(self) -> None:
        canary = f"bad-{os.urandom(8).hex()}"
        code, stdout, stderr = self.run_cli(
            ["secret", "set", "--ref", "secret-primary", "--stdin-json"],
            f'{{"username":"user","password":"{canary}:"}}'.encode(),
        )
        self.assertEqual(2, code)
        self.assertNotIn(canary, stdout + stderr)
