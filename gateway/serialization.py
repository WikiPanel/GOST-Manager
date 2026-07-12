"""Strict JSON parsing and deterministic gateway serialization."""

from __future__ import annotations

import json
from collections.abc import Mapping

from gateway.errors import ValidationError
from gateway.models import (
    MAX_NODE_BYTES,
    MAX_SHARED_BYTES,
    Binding,
    ExitNode,
    Gateway,
    NodeState,
    Route,
    SharedState,
    StatePair,
)
from gateway.validation import (
    canonical_host,
    canonical_ipv4,
    canonical_server_names,
    require_bool,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    validate_display_name,
    validate_exit_ids,
    validate_pair,
    validate_route_path,
    validate_secret_ref,
    validate_shared,
    validate_slug,
    validate_strategy,
    validate_timestamp,
    validate_uuid,
    validate_node,
)


class _DuplicateKeyError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _decode(data: bytes, maximum: int, label: str) -> Mapping[str, object]:
    if len(data) > maximum:
        raise ValidationError(f"{label} exceeds its serialized size limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{label} must be UTF-8 JSON") from exc
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except _DuplicateKeyError as exc:
        raise ValidationError("JSON contains duplicate object keys") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} is not valid JSON") from exc
    return require_mapping(value, label)


def _parse_gateway(value: object) -> Gateway:
    item = require_mapping(value, "gateway")
    require_exact_keys(
        item,
        frozenset(
            {
                "id",
                "enabled",
                "listen_address",
                "listen_port",
                "server_names",
                "status_port",
            }
        ),
        "gateway",
    )
    return Gateway(
        id=validate_slug(item["id"], "gateway ID"),
        enabled=require_bool(item["enabled"], "gateway enabled"),
        listen_address=canonical_ipv4(item["listen_address"], "gateway listen address"),
        listen_port=require_int(item["listen_port"], "gateway listen port", 1, 65535),
        server_names=canonical_server_names(item["server_names"]),
        status_port=require_int(item["status_port"], "gateway status port", 1024, 65535),
    )


def _parse_exit(value: object) -> ExitNode:
    item = require_mapping(value, "exit")
    require_exact_keys(
        item,
        frozenset(
            {"id", "display_name", "enabled", "host", "socks_port", "target_port"}
        ),
        "exit",
    )
    return ExitNode(
        id=validate_slug(item["id"], "exit ID"),
        display_name=validate_display_name(item["display_name"]),
        enabled=require_bool(item["enabled"], "exit enabled"),
        host=canonical_host(item["host"], "exit host"),
        socks_port=require_int(item["socks_port"], "SOCKS port", 1, 65535),
        target_port=require_int(item["target_port"], "target port", 1, 65535),
    )


def _parse_route(value: object) -> Route:
    item = require_mapping(value, "route")
    require_exact_keys(
        item,
        frozenset(
            {"id", "display_name", "enabled", "host", "path", "strategy", "exit_ids"}
        ),
        "route",
    )
    return Route(
        id=validate_slug(item["id"], "route ID"),
        display_name=validate_display_name(item["display_name"]),
        enabled=require_bool(item["enabled"], "route enabled"),
        host=canonical_host(item["host"], "route host"),
        path=validate_route_path(item["path"]),
        strategy=validate_strategy(item["strategy"]),
        exit_ids=validate_exit_ids(item["exit_ids"]),
    )


def parse_shared(data: bytes) -> SharedState:
    item = _decode(data, MAX_SHARED_BYTES, "shared state")
    require_exact_keys(
        item,
        frozenset(
            {
                "schema_version",
                "document_id",
                "revision",
                "updated_at",
                "gateway",
                "exits",
                "routes",
            }
        ),
        "shared state",
    )
    exits = require_list(item["exits"], "exits")
    routes = require_list(item["routes"], "routes")
    shared = SharedState(
        schema_version=require_int(item["schema_version"], "shared schema version", 1, 1),
        document_id=validate_uuid(item["document_id"]),
        revision=require_int(item["revision"], "shared revision", 1, 2**63 - 1),
        updated_at=validate_timestamp(item["updated_at"]),
        gateway=_parse_gateway(item["gateway"]),
        exits=tuple(sorted((_parse_exit(value) for value in exits), key=lambda value: value.id)),
        routes=tuple(sorted((_parse_route(value) for value in routes), key=lambda value: value.id)),
    )
    validate_shared(shared)
    return shared


def _parse_binding(value: object) -> Binding:
    item = require_mapping(value, "binding")
    require_exact_keys(
        item,
        frozenset(
            {"exit_id", "enabled", "listen_address", "listen_port", "secret_ref"}
        ),
        "binding",
    )
    enabled = require_bool(item["enabled"], "binding enabled")
    address = canonical_ipv4(item["listen_address"], "binding listen address")
    if address != "127.0.0.1":
        raise ValidationError("binding listen address must be 127.0.0.1")
    return Binding(
        exit_id=validate_slug(item["exit_id"], "binding exit ID"),
        enabled=enabled,
        listen_address=address,
        listen_port=require_int(item["listen_port"], "binding listen port", 1024, 65535),
        secret_ref=validate_secret_ref(item["secret_ref"], enabled),
    )


def parse_node(data: bytes) -> NodeState:
    item = _decode(data, MAX_NODE_BYTES, "node state")
    require_exact_keys(
        item,
        frozenset(
            {
                "schema_version",
                "document_id",
                "node_id",
                "revision",
                "updated_at",
                "bindings",
            }
        ),
        "node state",
    )
    bindings = require_list(item["bindings"], "bindings")
    node = NodeState(
        schema_version=require_int(item["schema_version"], "node schema version", 1, 1),
        document_id=validate_uuid(item["document_id"]),
        node_id=validate_slug(item["node_id"], "node ID"),
        revision=require_int(item["revision"], "node revision", 1, 2**63 - 1),
        updated_at=validate_timestamp(item["updated_at"]),
        bindings=tuple(
            sorted((_parse_binding(value) for value in bindings), key=lambda value: value.exit_id)
        ),
    )
    validate_node(node)
    return node


def shared_primitive(shared: SharedState) -> dict[str, object]:
    return {
        "schema_version": shared.schema_version,
        "document_id": shared.document_id,
        "revision": shared.revision,
        "updated_at": shared.updated_at,
        "gateway": {
            "id": shared.gateway.id,
            "enabled": shared.gateway.enabled,
            "listen_address": shared.gateway.listen_address,
            "listen_port": shared.gateway.listen_port,
            "server_names": list(shared.gateway.server_names),
            "status_port": shared.gateway.status_port,
        },
        "exits": [
            {
                "id": item.id,
                "display_name": item.display_name,
                "enabled": item.enabled,
                "host": item.host,
                "socks_port": item.socks_port,
                "target_port": item.target_port,
            }
            for item in sorted(shared.exits, key=lambda value: value.id)
        ],
        "routes": [
            {
                "id": item.id,
                "display_name": item.display_name,
                "enabled": item.enabled,
                "host": item.host,
                "path": item.path,
                "strategy": item.strategy.value,
                "exit_ids": list(item.exit_ids),
            }
            for item in sorted(shared.routes, key=lambda value: value.id)
        ],
    }


def node_primitive(node: NodeState) -> dict[str, object]:
    return {
        "schema_version": node.schema_version,
        "document_id": node.document_id,
        "node_id": node.node_id,
        "revision": node.revision,
        "updated_at": node.updated_at,
        "bindings": [
            {
                "exit_id": item.exit_id,
                "enabled": item.enabled,
                "listen_address": item.listen_address,
                "listen_port": item.listen_port,
                "secret_ref": item.secret_ref,
            }
            for item in sorted(node.bindings, key=lambda value: value.exit_id)
        ],
    }


def _encode(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def serialize_shared(shared: SharedState) -> bytes:
    validate_shared(shared)
    data = _encode(shared_primitive(shared))
    if len(data) > MAX_SHARED_BYTES:
        raise ValidationError("shared state exceeds its serialized size limit")
    return data


def serialize_node(node: NodeState) -> bytes:
    validate_node(node)
    data = _encode(node_primitive(node))
    if len(data) > MAX_NODE_BYTES:
        raise ValidationError("node state exceeds its serialized size limit")
    return data


def pair_primitive(pair: StatePair) -> dict[str, object]:
    validate_pair(pair)
    return {"shared": shared_primitive(pair.shared), "node": node_primitive(pair.node)}
