from __future__ import annotations

import json
import unittest
from unittest import mock

import gateway.cli
from test_gateway_support import TemporaryStore, cli_paths, run_cli


class GatewayCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryStore()
        self.paths = cli_paths(self.temporary.paths)

    def tearDown(self) -> None:
        self.temporary.close()

    def run_command(
        self, *arguments: str, output_format: str = "json"
    ) -> tuple[int, dict[str, object], str]:
        args = [*self.paths, "--format", output_format, *arguments]
        code, stdout, stderr = run_cli(args)
        payload = json.loads(stdout) if stdout and output_format == "json" else {}
        return code, payload, stderr

    def initialize(self) -> dict[str, object]:
        code, payload, error = self.run_command(
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
            "Gateway.Example.Org",
        )
        self.assertEqual((code, error), (0, ""))
        return payload

    def test_init_json_contract(self) -> None:
        payload = self.initialize()
        self.assertEqual(payload["action"], "initialized")
        self.assertEqual(payload["shared_revision"], 1)
        self.assertEqual(payload["node_revision"], 1)
        self.assertEqual(payload["state_file"], str(self.temporary.paths.state_file))
        self.assertEqual(payload["node_file"], str(self.temporary.paths.node_file))

    def test_global_format_option_is_accepted_after_command(self) -> None:
        self.initialize()
        code, stdout, stderr = run_cli([*self.paths, "show", "--format", "json"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("shared", json.loads(stdout))

    def test_show_validate_and_gateway_show(self) -> None:
        self.initialize()
        for command in (("show",), ("validate",), ("gateway", "show")):
            with self.subTest(command=command):
                code, payload, error = self.run_command(*command)
                self.assertEqual((code, error), (0, ""))
                self.assertTrue(payload)

    def test_full_state_only_smoke_and_runtime_readiness(self) -> None:
        self.initialize()
        commands = (
            (
                "exit",
                "add",
                "--id",
                "ee-primary",
                "--display-name",
                "Estonia primary",
                "--host",
                "192.0.2.10",
                "--socks-port",
                "28420",
                "--target-port",
                "18081",
            ),
            (
                "exit",
                "add",
                "--id",
                "de-backup",
                "--display-name",
                "Germany backup",
                "--host",
                "198.51.100.20",
                "--socks-port",
                "28421",
                "--target-port",
                "18082",
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
                "binding",
                "set",
                "--exit-id",
                "de-backup",
                "--listen-port",
                "18082",
                "--secret-ref",
                "secret-de-backup",
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
                "--exit-id",
                "de-backup",
            ),
            ("gateway", "set", "--enable"),
            ("route", "edit", "--id", "route-estonia", "--enable"),
        )
        for command in commands:
            with self.subTest(command=command):
                code, payload, error = self.run_command(*command)
                self.assertEqual((code, error), (0, ""))
                self.assertTrue(payload["changed"])
        code, payload, error = self.run_command("validate", "--runtime-ready")
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(payload["runtime_ready"])
        code, payload, error = self.run_command("show")
        self.assertEqual((code, error), (0, ""))
        route = payload["shared"]["routes"][0]
        self.assertEqual(route["exit_ids"], ["ee-primary", "de-backup"])

    def test_list_commands_are_deterministic(self) -> None:
        self.initialize()
        for identifier in ("z-exit", "a-exit"):
            self.run_command(
                "exit",
                "add",
                "--id",
                identifier,
                "--display-name",
                identifier,
                "--host",
                f"{identifier}.example.org",
                "--socks-port",
                "28420",
                "--target-port",
                "18081",
            )
        code, payload, error = self.run_command("exit", "list")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual([item["id"] for item in payload["exits"]], ["a-exit", "z-exit"])

    def test_noop_is_success_and_reports_unchanged(self) -> None:
        self.initialize()
        code, payload, error = self.run_command("gateway", "set", "--disable")
        self.assertEqual((code, error), (0, ""))
        self.assertFalse(payload["changed"])
        self.assertEqual(payload["shared_revision"], 1)

    def test_revision_conflict_uses_exit_code_four(self) -> None:
        self.initialize()
        code, _stdout, stderr = run_cli(
            [
                *self.paths,
                "gateway",
                "set",
                "--enable",
                "--expect-revision",
                "2",
            ]
        )
        self.assertEqual(code, 4)
        self.assertIn("expected 2, current 1", stderr)

    def test_missing_state_uses_exit_code_three(self) -> None:
        code, _stdout, stderr = run_cli([*self.paths, "show"])
        self.assertEqual(code, 3)
        self.assertIn("missing", stderr)

    def test_invalid_input_uses_exit_code_two(self) -> None:
        code, _stdout, stderr = run_cli([*self.paths, "init", "--gateway-id", "BAD"])
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "error: invalid command arguments\n")

    def test_unknown_argument_does_not_echo_secret_canary(self) -> None:
        canary = "SECRET-CANARY-DO-NOT-PRINT"
        code, stdout, stderr = run_cli(
            [*self.paths, "show", "--password", canary, "--format", "json"]
        )
        self.assertEqual(code, 2)
        self.assertNotIn(canary, stdout + stderr)
        error = json.loads(stderr)["error"]
        self.assertEqual(error["code"], 2)

    def test_corrupt_state_does_not_echo_secret_canary(self) -> None:
        self.initialize()
        canary = "SECRET-CANARY-DO-NOT-PRINT"
        value = json.loads(self.temporary.paths.state_file.read_text())
        value["password"] = canary
        self.temporary.paths.state_file.write_text(json.dumps(value))
        for output_format in ("human", "json"):
            with self.subTest(output_format=output_format):
                code, stdout, stderr = run_cli(
                    [*self.paths, "show", "--format", output_format]
                )
                self.assertEqual(code, 3)
                self.assertNotIn(canary, stdout + stderr)

    def test_dependency_protected_delete_uses_conflict_code(self) -> None:
        self.initialize()
        self.run_command(
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
        )
        self.run_command(
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
        )
        code, _stdout, stderr = run_cli(
            [*self.paths, "exit", "delete", "--id", "ee-primary"]
        )
        self.assertEqual(code, 4)
        self.assertIn("route-estonia", stderr)

    def test_edit_ids_have_no_replacement_option(self) -> None:
        self.initialize()
        canary = "SECRET-CANARY-DO-NOT-PRINT"
        code, stdout, stderr = run_cli(
            [
                *self.paths,
                "exit",
                "edit",
                "--id",
                "missing",
                "--new-id",
                canary,
            ]
        )
        self.assertEqual(code, 2)
        self.assertNotIn(canary, stdout + stderr)

    def test_human_noop_message_is_clear(self) -> None:
        self.initialize()
        code, stdout, stderr = run_cli(
            [*self.paths, "gateway", "set", "--disable"]
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout, "No gateway state change was needed.\n")

    def test_interruption_has_no_traceback(self) -> None:
        self.initialize()
        with mock.patch.object(gateway.cli, "_dispatch", side_effect=KeyboardInterrupt):
            code, stdout, stderr = run_cli([*self.paths, "show"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("interrupted", stderr)
        self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
