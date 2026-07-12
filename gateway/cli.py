"""Command-line interface for state-only gateway desired-state CRUD."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections.abc import Sequence

from gateway.crud import GatewayCRUD
from gateway.errors import GatewayError, OperationalError, ValidationError
from gateway.models import Binding, ExitNode, Gateway, Route
from gateway.paths import (
    DEFAULT_BACKUP_DIR,
    DEFAULT_LOCK_FILE,
    DEFAULT_NODE_FILE,
    DEFAULT_STATE_FILE,
    StatePaths,
)
from gateway.serialization import pair_primitive
from gateway.store import GatewayStateStore, MutationResult


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ValidationError("invalid command arguments")


def _positive_revision(value: str) -> int:
    try:
        revision = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid revision") from exc
    if revision < 1:
        raise argparse.ArgumentTypeError("invalid revision")
    return revision


def _port(value: str) -> int:
    try:
        return int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid port") from exc


def _enabled_group(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--enable", dest="enabled", action="store_true")
    group.add_argument("--disable", dest="enabled", action="store_false")
    parser.set_defaults(enabled=None)


def _expect_revision(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--expect-revision", type=_positive_revision)


def _command_parser() -> SafeArgumentParser:
    parser = SafeArgumentParser(prog="python3 -m gateway.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("--gateway-id", required=True)
    init.add_argument("--node-id", required=True)
    init.add_argument("--listen-address", required=True)
    init.add_argument("--listen-port", required=True, type=_port)
    init.add_argument("--server-name", required=True, action="append")
    init.add_argument("--status-port", type=_port, default=18000)

    commands.add_parser("show")
    validate = commands.add_parser("validate")
    validate.add_argument("--runtime-ready", action="store_true")

    gateway = commands.add_parser("gateway").add_subparsers(
        dest="gateway_command", required=True
    )
    gateway.add_parser("show")
    gateway_set = gateway.add_parser("set")
    _enabled_group(gateway_set)
    gateway_set.add_argument("--listen-address")
    gateway_set.add_argument("--listen-port", type=_port)
    gateway_set.add_argument("--server-name", action="append")
    gateway_set.add_argument("--status-port", type=_port)
    _expect_revision(gateway_set)

    exits = commands.add_parser("exit").add_subparsers(
        dest="exit_command", required=True
    )
    exit_add = exits.add_parser("add")
    exit_add.add_argument("--id", required=True)
    exit_add.add_argument("--display-name", required=True)
    exit_add.add_argument("--host", required=True)
    exit_add.add_argument("--socks-port", required=True, type=_port)
    exit_add.add_argument("--target-port", required=True, type=_port)
    _enabled_group(exit_add)
    _expect_revision(exit_add)
    exit_edit = exits.add_parser("edit")
    exit_edit.add_argument("--id", required=True)
    exit_edit.add_argument("--display-name")
    exit_edit.add_argument("--host")
    exit_edit.add_argument("--socks-port", type=_port)
    exit_edit.add_argument("--target-port", type=_port)
    _enabled_group(exit_edit)
    _expect_revision(exit_edit)
    exit_delete = exits.add_parser("delete")
    exit_delete.add_argument("--id", required=True)
    _expect_revision(exit_delete)
    exits.add_parser("list")

    bindings = commands.add_parser("binding").add_subparsers(
        dest="binding_command", required=True
    )
    binding_set = bindings.add_parser("set")
    binding_set.add_argument("--exit-id", required=True)
    binding_set.add_argument("--listen-port", required=True, type=_port)
    binding_set.add_argument("--secret-ref", required=True)
    _enabled_group(binding_set, required=True)
    _expect_revision(binding_set)
    binding_remove = bindings.add_parser("remove")
    binding_remove.add_argument("--exit-id", required=True)
    _expect_revision(binding_remove)
    bindings.add_parser("list")

    routes = commands.add_parser("route").add_subparsers(
        dest="route_command", required=True
    )
    route_add = routes.add_parser("add")
    route_add.add_argument("--id", required=True)
    route_add.add_argument("--display-name", required=True)
    route_add.add_argument("--host", required=True)
    route_add.add_argument("--path", required=True)
    route_add.add_argument(
        "--strategy", required=True, choices=("active-passive", "active-active")
    )
    route_add.add_argument("--exit-id", required=True, action="append")
    _enabled_group(route_add)
    _expect_revision(route_add)
    route_edit = routes.add_parser("edit")
    route_edit.add_argument("--id", required=True)
    route_edit.add_argument("--display-name")
    route_edit.add_argument("--host")
    route_edit.add_argument("--path")
    route_edit.add_argument(
        "--strategy", choices=("active-passive", "active-active")
    )
    route_edit.add_argument("--exit-id", action="append")
    _enabled_group(route_edit)
    _expect_revision(route_edit)
    route_delete = routes.add_parser("delete")
    route_delete.add_argument("--id", required=True)
    _expect_revision(route_delete)
    routes.add_parser("list")
    return parser


def _global_parser() -> SafeArgumentParser:
    parser = SafeArgumentParser(add_help=False)
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--node-file", default=DEFAULT_NODE_FILE)
    parser.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--lock-file", default=DEFAULT_LOCK_FILE)
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument("--debug", action="store_true")
    return parser


def _gateway_primitive(value: Gateway) -> dict[str, object]:
    return {
        "id": value.id,
        "enabled": value.enabled,
        "listen_address": value.listen_address,
        "listen_port": value.listen_port,
        "server_names": list(value.server_names),
        "status_port": value.status_port,
    }


def _exit_primitive(value: ExitNode) -> dict[str, object]:
    return {
        "id": value.id,
        "display_name": value.display_name,
        "enabled": value.enabled,
        "host": value.host,
        "socks_port": value.socks_port,
        "target_port": value.target_port,
    }


def _binding_primitive(value: Binding) -> dict[str, object]:
    return {
        "exit_id": value.exit_id,
        "enabled": value.enabled,
        "listen_address": value.listen_address,
        "listen_port": value.listen_port,
        "secret_ref": value.secret_ref,
    }


def _route_primitive(value: Route) -> dict[str, object]:
    return {
        "id": value.id,
        "display_name": value.display_name,
        "enabled": value.enabled,
        "host": value.host,
        "path": value.path,
        "strategy": value.strategy.value,
        "exit_ids": list(value.exit_ids),
    }


def _mutation_payload(result: MutationResult, entity: str) -> dict[str, object]:
    return {
        "entity": entity,
        "changed": result.changed,
        "shared_revision": result.pair.shared.revision,
        "node_revision": result.pair.node.revision,
    }


def _dispatch(args: argparse.Namespace, store: GatewayStateStore) -> dict[str, object]:
    crud = GatewayCRUD(store)
    if args.command == "init":
        pair = store.initialize(
            gateway_id=args.gateway_id,
            node_id=args.node_id,
            listen_address=args.listen_address,
            listen_port=args.listen_port,
            server_names=args.server_name,
            status_port=args.status_port,
        )
        return {
            "action": "initialized",
            "document_id": pair.shared.document_id,
            "shared_revision": pair.shared.revision,
            "node_revision": pair.node.revision,
            "state_file": str(store.paths.state_file),
            "node_file": str(store.paths.node_file),
        }
    if args.command == "show":
        return pair_primitive(crud.show())
    if args.command == "validate":
        pair = crud.show(runtime_ready=args.runtime_ready)
        return {
            "valid": True,
            "runtime_ready": args.runtime_ready,
            "shared_revision": pair.shared.revision,
            "node_revision": pair.node.revision,
        }
    if args.command == "gateway" and args.gateway_command == "show":
        pair = crud.show()
        return {
            "shared_revision": pair.shared.revision,
            "gateway": _gateway_primitive(pair.shared.gateway),
        }
    if args.command == "gateway" and args.gateway_command == "set":
        result = crud.set_gateway(
            enabled=args.enabled,
            listen_address=args.listen_address,
            listen_port=args.listen_port,
            server_names=args.server_name,
            status_port=args.status_port,
            expected_revision=args.expect_revision,
        )
        return _mutation_payload(result, "gateway")
    if args.command == "exit":
        if args.exit_command == "list":
            pair = crud.show()
            return {
                "shared_revision": pair.shared.revision,
                "exits": [_exit_primitive(item) for item in pair.shared.exits],
            }
        if args.exit_command == "add":
            result = crud.add_exit(
                exit_id=args.id,
                display_name=args.display_name,
                host=args.host,
                socks_port=args.socks_port,
                target_port=args.target_port,
                enabled=True if args.enabled is None else args.enabled,
                expected_revision=args.expect_revision,
            )
        elif args.exit_command == "edit":
            result = crud.edit_exit(
                exit_id=args.id,
                display_name=args.display_name,
                host=args.host,
                socks_port=args.socks_port,
                target_port=args.target_port,
                enabled=args.enabled,
                expected_revision=args.expect_revision,
            )
        else:
            result = crud.delete_exit(
                exit_id=args.id, expected_revision=args.expect_revision
            )
        return _mutation_payload(result, "exit")
    if args.command == "binding":
        if args.binding_command == "list":
            pair = crud.show()
            return {
                "node_revision": pair.node.revision,
                "bindings": [
                    _binding_primitive(item) for item in pair.node.bindings
                ],
            }
        if args.binding_command == "set":
            result = crud.set_binding(
                exit_id=args.exit_id,
                listen_port=args.listen_port,
                secret_ref=args.secret_ref,
                enabled=args.enabled,
                expected_revision=args.expect_revision,
            )
        else:
            result = crud.remove_binding(
                exit_id=args.exit_id, expected_revision=args.expect_revision
            )
        return _mutation_payload(result, "binding")
    if args.command == "route":
        if args.route_command == "list":
            pair = crud.show()
            return {
                "shared_revision": pair.shared.revision,
                "routes": [_route_primitive(item) for item in pair.shared.routes],
            }
        if args.route_command == "add":
            result = crud.add_route(
                route_id=args.id,
                display_name=args.display_name,
                host=args.host,
                path=args.path,
                strategy=args.strategy,
                exit_ids=args.exit_id,
                enabled=False if args.enabled is None else args.enabled,
                expected_revision=args.expect_revision,
            )
        elif args.route_command == "edit":
            result = crud.edit_route(
                route_id=args.id,
                display_name=args.display_name,
                host=args.host,
                path=args.path,
                strategy=args.strategy,
                exit_ids=args.exit_id,
                enabled=args.enabled,
                expected_revision=args.expect_revision,
            )
        else:
            result = crud.delete_route(
                route_id=args.id, expected_revision=args.expect_revision
            )
        return _mutation_payload(result, "route")
    raise ValidationError("invalid gateway command")


def _print_payload(payload: dict[str, object], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    if payload.get("changed") is False:
        print("No gateway state change was needed.")
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
        else:
            print(f"{key}: {value}")


def _print_error(error: GatewayError, output_format: str) -> None:
    if output_format == "json":
        print(
            json.dumps(
                {"error": {"code": error.exit_code, "message": str(error)}},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
    else:
        print(f"error: {error}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    output_format = "human"
    debug = False
    try:
        global_args, remaining = _global_parser().parse_known_args(values)
        output_format = global_args.format
        debug = global_args.debug
        args = _command_parser().parse_args(remaining)
        paths = StatePaths.from_values(
            state_file=global_args.state_file,
            node_file=global_args.node_file,
            backup_dir=global_args.backup_dir,
            lock_file=global_args.lock_file,
        )
        payload = _dispatch(args, GatewayStateStore(paths))
        _print_payload(payload, output_format)
        return 0
    except GatewayError as exc:
        _print_error(exc, output_format)
        return exc.exit_code
    except KeyboardInterrupt:
        error = OperationalError("gateway operation interrupted")
        _print_error(error, output_format)
        return error.exit_code
    except SystemExit:
        raise
    except Exception:
        error = GatewayError("unexpected gateway operation failure")
        _print_error(error, output_format)
        if debug:
            stack = traceback.extract_tb(sys.exc_info()[2])
            for frame in stack:
                print(
                    f"debug: {frame.filename}:{frame.lineno} in {frame.name}",
                    file=sys.stderr,
                )
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
