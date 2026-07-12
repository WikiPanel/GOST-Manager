"""Safe CLI for private secrets and local gateway-exit runtime."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import traceback
from collections.abc import Sequence
from dataclasses import asdict

from gateway.cli import SafeArgumentParser
from gateway.errors import ConflictError, GatewayError, OperationalError, StateError, ValidationError
from gateway.paths import DEFAULT_BACKUP_DIR, DEFAULT_LOCK_FILE, DEFAULT_NODE_FILE, DEFAULT_STATE_FILE, StatePaths
from gateway.runtime_apply import RuntimeManager, secret_references
from gateway.runtime_paths import (
    DEFAULT_GENERATED_DIR, DEFAULT_GOST_BIN, DEFAULT_RUNNER_PATH,
    DEFAULT_RUNTIME_BACKUP_DIR, DEFAULT_RUNTIME_LOCK_FILE, DEFAULT_SECRET_DIR,
    DEFAULT_SYSTEMD_DIR, RuntimePaths,
)
from gateway.secrets import SecretStore, parse_secret_json, validate_credentials
from gateway.store import GatewayStateStore


def _global_parser() -> SafeArgumentParser:
    parser = SafeArgumentParser(add_help=False)
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--node-file", default=DEFAULT_NODE_FILE)
    parser.add_argument("--state-backup-dir", default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--state-lock-file", default=DEFAULT_LOCK_FILE)
    parser.add_argument("--secret-dir", default=DEFAULT_SECRET_DIR)
    parser.add_argument("--generated-dir", default=DEFAULT_GENERATED_DIR)
    parser.add_argument("--runtime-backup-dir", default=DEFAULT_RUNTIME_BACKUP_DIR)
    parser.add_argument("--runtime-lock-file", default=DEFAULT_RUNTIME_LOCK_FILE)
    parser.add_argument("--systemd-dir", default=DEFAULT_SYSTEMD_DIR)
    parser.add_argument("--runner-path", default=DEFAULT_RUNNER_PATH)
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument("--debug", action="store_true")
    return parser


def _command_parser() -> SafeArgumentParser:
    parser = SafeArgumentParser(prog="python3 -m gateway.runtime_cli")
    commands = parser.add_subparsers(dest="command", required=True)

    secrets = commands.add_parser("secret").add_subparsers(dest="secret_command", required=True)
    set_secret = secrets.add_parser("set")
    set_secret.add_argument("--ref", required=True)
    set_secret.add_argument("--stdin-json", action="store_true")
    delete_secret = secrets.add_parser("delete")
    delete_secret.add_argument("--ref", required=True)
    delete_secret.add_argument("--yes", action="store_true")
    secrets.add_parser("list")
    validate_secret = secrets.add_parser("validate")
    validate_secret.add_argument("--ref")

    runtime = commands.add_parser("runtime").add_subparsers(dest="runtime_command", required=True)
    plan = runtime.add_parser("plan")
    plan.add_argument("--exit-id")
    apply = runtime.add_parser("apply")
    apply.add_argument("--exit-id")
    apply.add_argument("--yes", action="store_true")
    status = runtime.add_parser("status")
    status.add_argument("--exit-id")

    service = commands.add_parser("service").add_subparsers(dest="service_command", required=True)
    for action in ("start", "stop", "restart", "status"):
        child = service.add_parser(action)
        child.add_argument("--exit-id", required=True)
        if action in {"stop", "restart"}:
            child.add_argument("--yes", action="store_true")
    return parser


def _stores(global_args: argparse.Namespace) -> tuple[GatewayStateStore, SecretStore, RuntimeManager]:
    state_paths = StatePaths.from_values(
        global_args.state_file, global_args.node_file,
        global_args.state_backup_dir, global_args.state_lock_file,
    )
    runtime_paths = RuntimePaths.from_values(
        global_args.secret_dir, global_args.generated_dir,
        global_args.runtime_backup_dir, global_args.runtime_lock_file,
        global_args.systemd_dir, global_args.runner_path, DEFAULT_GOST_BIN,
    )
    state_store = GatewayStateStore(state_paths)
    secret_store = SecretStore(runtime_paths)
    return state_store, secret_store, RuntimeManager(state_store, secret_store, runtime_paths)


def _read_stdin_limited() -> bytes:
    data = sys.stdin.buffer.read(1025)
    if len(data) > 1024:
        raise ValidationError("secret input is invalid")
    return data


def _references_if_available(store: GatewayStateStore) -> dict[str, tuple[str, ...]]:
    try:
        return secret_references(store.load_pair())
    except StateError:
        return {}


def _dispatch(
    args: argparse.Namespace, state_store: GatewayStateStore,
    secret_store: SecretStore, manager: RuntimeManager,
) -> dict[str, object]:
    if args.command == "secret":
        references = _references_if_available(state_store)
        if args.secret_command == "set":
            if args.stdin_json:
                credentials = parse_secret_json(_read_stdin_limited())
            else:
                username = getpass.getpass("GOST username: ")
                password = getpass.getpass("GOST password: ")
                confirmation = getpass.getpass("Confirm GOST password: ")
                if password != confirmation:
                    raise ValidationError("secret confirmation does not match")
                credentials = validate_credentials(username, password)
            result = secret_store.set(args.ref, credentials)
            affected = references.get(args.ref, ())
            return {
                "secret_ref": args.ref, "result": result,
                "referenced_exit_ids": list(affected),
                "restart_required": result == "updated" and bool(affected),
            }
        if args.secret_command == "delete":
            if not args.yes:
                raise ConflictError("secret deletion requires explicit confirmation")
            with state_store.locked_pair() as pair:
                refs = secret_references(pair)
                with secret_store.lock():
                    secret_store.delete_unlocked(args.ref, refs.get(args.ref, ()))
            return {"secret_ref": args.ref, "deleted": True}
        if args.secret_command == "list":
            return {"secrets": [
                {
                    "secret_ref": item.secret_ref, "valid": item.valid,
                    "referenced": item.referenced,
                    "referencing_exit_ids": list(item.referenced_exit_ids),
                }
                for item in secret_store.list(references)
            ]}
        return {"secrets": [asdict(item) for item in secret_store.validate(args.ref)]}

    if args.command == "runtime":
        if args.runtime_command == "plan":
            plan = manager.plan(args.exit_id)
            return {
                "actions": [asdict(item) for item in plan.actions],
                "listener_snapshot_count": plan.listener_snapshot_count,
            }
        if args.runtime_command == "apply":
            result = manager.apply(yes=args.yes, exit_id=args.exit_id)
            return {
                "changed": result.changed,
                "actions": [asdict(item) for item in result.plan.actions],
                "started_exit_ids": list(result.started),
                "restarted_exit_ids": list(result.restarted),
                "removed_exit_ids": list(result.removed),
                "connection_notice": (
                    "Established connections on restarted Exit services will reconnect."
                    if result.restarted else ""
                ),
            }
        return {"services": [asdict(item) for item in manager.status(args.exit_id)]}

    state = manager.service_control(
        args.service_command, args.exit_id, yes=getattr(args, "yes", False)
    )
    return {"service": asdict(state), "action": args.service_command}


def _print(payload: dict[str, object], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, ensure_ascii=True, sort_keys=True)}")
        elif value != "":
            print(f"{key}: {value}")


def _print_error(error: GatewayError, output_format: str) -> None:
    payload = {"error": {"code": error.exit_code, "message": str(error)}}
    if output_format == "json":
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
    else:
        print(f"error: {error}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    output_format = "human"
    debug = False
    try:
        global_args, remaining = _global_parser().parse_known_args(values)
        output_format, debug = global_args.format, global_args.debug
        args = _command_parser().parse_args(remaining)
        payload = _dispatch(args, *_stores(global_args))
        _print(payload, output_format)
        return 0
    except GatewayError as exc:
        _print_error(exc, output_format)
        return exc.exit_code
    except KeyboardInterrupt:
        error = OperationalError("gateway runtime operation interrupted")
        _print_error(error, output_format)
        return error.exit_code
    except SystemExit:
        raise
    except Exception:
        error = GatewayError("unexpected gateway runtime operation failure")
        _print_error(error, output_format)
        if debug:
            for frame in traceback.extract_tb(sys.exc_info()[2]):
                print(f"debug: {frame.filename}:{frame.lineno} in {frame.name}", file=sys.stderr)
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
