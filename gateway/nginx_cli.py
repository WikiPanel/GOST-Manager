"""Safe CLI for the dedicated NGINX Gateway runtime."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections.abc import Sequence
from dataclasses import asdict, is_dataclass

from gateway.cli import SafeArgumentParser
from gateway.errors import ConflictError, GatewayError, OperationalError, ValidationError
from gateway.nginx_apply import NginxManager
from gateway.nginx_paths import (
    DEFAULT_NGINX_BACKUP_DIR,
    DEFAULT_NGINX_BIN,
    DEFAULT_NGINX_DIR,
    DEFAULT_NGINX_LAUNCHER,
    DEFAULT_NGINX_LOCK_FILE,
    DEFAULT_NGINX_RUNNER,
    DEFAULT_NGINX_RUNTIME_DIR,
    DEFAULT_NGINX_UNIT,
    NginxPaths,
)
from gateway.paths import DEFAULT_BACKUP_DIR, DEFAULT_LOCK_FILE, DEFAULT_NODE_FILE, DEFAULT_STATE_FILE, StatePaths
from gateway.runtime_paths import (
    DEFAULT_GENERATED_DIR,
    DEFAULT_GOST_BIN,
    DEFAULT_RUNNER_PATH,
    DEFAULT_RUNTIME_BACKUP_DIR,
    DEFAULT_RUNTIME_LOCK_FILE,
    DEFAULT_SECRET_DIR,
    DEFAULT_SYSTEMD_DIR,
    RuntimePaths,
)
from gateway.secrets import SecretStore
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
    parser.add_argument("--gost-runner", default=DEFAULT_RUNNER_PATH)
    parser.add_argument("--gost-bin", default=DEFAULT_GOST_BIN)
    parser.add_argument("--nginx-generated-dir", default=DEFAULT_NGINX_DIR)
    parser.add_argument("--nginx-backup-dir", default=DEFAULT_NGINX_BACKUP_DIR)
    parser.add_argument("--nginx-lock-file", default=DEFAULT_NGINX_LOCK_FILE)
    parser.add_argument("--nginx-runtime-dir", default=DEFAULT_NGINX_RUNTIME_DIR)
    parser.add_argument("--nginx-unit", default=DEFAULT_NGINX_UNIT)
    parser.add_argument("--nginx-runner", default=DEFAULT_NGINX_RUNNER)
    parser.add_argument("--nginx-launcher", default=DEFAULT_NGINX_LAUNCHER)
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument("--debug", action="store_true")
    return parser


def _command_parser() -> SafeArgumentParser:
    parser = SafeArgumentParser(prog="python3 -m gateway.nginx_cli")
    commands = parser.add_subparsers(dest="command", required=True)
    dependency = commands.add_parser("dependency").add_subparsers(
        dest="dependency_command", required=True
    )
    dependency.add_parser("status")
    install = dependency.add_parser("install")
    install.add_argument("--yes", action="store_true")
    commands.add_parser("plan")
    apply = commands.add_parser("apply")
    apply.add_argument("--yes", action="store_true")
    commands.add_parser("status")
    commands.add_parser("test")
    service = commands.add_parser("service").add_subparsers(
        dest="service_command", required=True
    )
    service.add_parser("status")
    service.add_parser("start")
    for action in ("stop", "reload"):
        child = service.add_parser(action)
        child.add_argument("--yes", action="store_true")
    restart = service.add_parser("restart")
    restart.add_argument("--yes", action="store_true")
    restart.add_argument("--acknowledge-disconnect", action="store_true")
    return parser


def _manager(args: argparse.Namespace) -> NginxManager:
    state_paths = StatePaths.from_values(
        args.state_file, args.node_file, args.state_backup_dir, args.state_lock_file
    )
    runtime_paths = RuntimePaths.from_values(
        args.secret_dir,
        args.generated_dir,
        args.runtime_backup_dir,
        args.runtime_lock_file,
        args.systemd_dir,
        args.gost_runner,
        args.gost_bin,
    )
    nginx_paths = NginxPaths.from_values(
        args.nginx_generated_dir,
        args.nginx_backup_dir,
        args.nginx_lock_file,
        args.nginx_runtime_dir,
        args.nginx_unit,
        args.nginx_runner,
        DEFAULT_NGINX_BIN,
        args.nginx_launcher,
    )
    state_store = GatewayStateStore(state_paths)
    secret_store = SecretStore(runtime_paths)
    return NginxManager(state_store, secret_store, runtime_paths, nginx_paths)


def _require_root() -> None:
    if os.geteuid() != 0:
        raise ConflictError("NGINX Gateway mutation requires root")


def _primitive(value: object) -> object:
    if is_dataclass(value):
        return {key: _primitive(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_primitive(item) for item in value]
    if isinstance(value, bytes):
        return None
    return value


def _plan_payload(plan) -> dict[str, object]:
    return {
        "action": plan.action,
        "reason_codes": list(plan.reason_codes),
        "gateway_id": plan.gateway_id,
        "current_state": plan.current_state,
        "desired_state": plan.desired_state,
        "listen_address": plan.listen_address,
        "listen_port": plan.listen_port,
        "status_port": plan.status_port,
        "enabled_route_count": plan.enabled_route_count,
        "backend_count": plan.backend_count,
        "config_changed": plan.config_changed,
        "manifest_changed": plan.manifest_changed,
        "affected_route_ids": list(plan.affected_route_ids),
        "affected_exit_ids": list(plan.affected_exit_ids),
    }


def _dispatch(args: argparse.Namespace, manager: NginxManager) -> dict[str, object]:
    if args.command == "dependency":
        if args.dependency_command == "status":
            return {"dependency": _primitive(manager.dependency_status())}
        _require_root()
        return {
            "result": manager.dependency_install(yes=args.yes),
            "notice": "The NGINX package remains installed; Gateway apply is separate.",
        }
    if args.command == "plan":
        return {"plan": _plan_payload(manager.plan())}
    if args.command == "apply":
        _require_root()
        result = manager.apply(yes=args.yes)
        return {
            "changed": result.changed,
            "reload_count": result.reload_count,
            "restart_count": result.restart_count,
            "plan": _plan_payload(result.plan),
        }
    if args.command == "status":
        return {"status": _primitive(manager.status())}
    if args.command == "test":
        manager.test_installed()
        return {"valid": True}
    if args.command == "service":
        if args.service_command != "status":
            _require_root()
        state = manager.service_control(
            args.service_command,
            yes=getattr(args, "yes", False),
            acknowledge_disconnect=getattr(args, "acknowledge_disconnect", False),
        )
        return {"action": args.service_command, "service": _primitive(state)}
    raise ValidationError("invalid NGINX Gateway command")


def _print(payload: dict[str, object], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, ensure_ascii=True, sort_keys=True)}")
        else:
            print(f"{key}: {value}")


def _print_error(error: GatewayError, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps({"error": {"code": error.exit_code, "message": str(error)}}, sort_keys=True), file=sys.stderr)
    else:
        print(f"error: {error}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    output_format = "human"
    debug = False
    try:
        global_args, remaining = _global_parser().parse_known_args(values)
        output_format, debug = global_args.format, global_args.debug
        command_args = _command_parser().parse_args(remaining)
        payload = _dispatch(command_args, _manager(global_args))
        _print(payload, output_format)
        return 0
    except GatewayError as exc:
        _print_error(exc, output_format)
        return exc.exit_code
    except KeyboardInterrupt:
        error = OperationalError("NGINX Gateway operation interrupted")
        _print_error(error, output_format)
        return 130
    except SystemExit:
        raise
    except Exception:
        error = GatewayError("unexpected NGINX Gateway operation failure")
        _print_error(error, output_format)
        if debug:
            for frame in traceback.extract_tb(sys.exc_info()[2]):
                print(f"debug: {frame.filename}:{frame.lineno} in {frame.name}", file=sys.stderr)
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
