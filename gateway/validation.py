"""Primitive, model, and cross-document gateway validation."""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
import unicodedata
import uuid
from collections.abc import Mapping, Sequence

from gateway.errors import ValidationError
from gateway.models import (
    MAX_BINDINGS,
    MAX_EXITS,
    MAX_ROUTES,
    NODE_SCHEMA_VERSION,
    SHARED_SCHEMA_VERSION,
    Binding,
    ExitNode,
    Gateway,
    NodeState,
    Route,
    SharedState,
    StatePair,
    Strategy,
)

ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SECRET_REF_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
HEX_RE = re.compile(r"^[0-9A-Fa-f]{2}$")
UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)


def require_exact_keys(
    value: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    keys = frozenset(value)
    if keys != expected:
        raise ValidationError(f"{label} contains missing or unknown fields")


def require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object")
    return value


def require_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array")
    return value


def require_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a string")
    return value


def require_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValidationError(f"{label} must be a boolean")
    return value


def require_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationError(
            f"{label} must be an integer from {minimum} through {maximum}"
        )
    return value


def validate_slug(value: object, label: str = "ID") -> str:
    text = require_string(value, label)
    if not ID_RE.fullmatch(text):
        raise ValidationError(f"{label} must be a stable lowercase slug")
    return text


def validate_secret_ref(value: object, enabled: bool) -> str:
    text = require_string(value, "secret reference")
    if enabled and not text:
        raise ValidationError("enabled bindings require a secret reference")
    if text and not SECRET_REF_RE.fullmatch(text):
        raise ValidationError("secret reference must be a stable lowercase slug")
    return text


def validate_display_name(value: object) -> str:
    text = require_string(value, "display name")
    if not text or text != text.strip() or len(text) > 100:
        raise ValidationError("display name must be trimmed and 1 to 100 characters")
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise ValidationError("display name contains control characters")
    return text


def validate_uuid(value: object) -> str:
    text = require_string(value, "document ID")
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError) as exc:
        raise ValidationError("document ID must be a canonical UUID") from exc
    if str(parsed) != text:
        raise ValidationError("document ID must be a canonical UUID")
    return text


def validate_timestamp(value: object) -> str:
    text = require_string(value, "updated timestamp")
    if not UTC_TIMESTAMP_RE.fullmatch(text):
        raise ValidationError("updated timestamp must be UTC and end in Z")
    try:
        parsed = dt.datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ValidationError("updated timestamp must be a valid UTC ISO-8601 value") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValidationError("updated timestamp must be UTC and end in Z")
    return text


def canonical_ipv4(value: object, label: str) -> str:
    text = require_string(value, label)
    try:
        address = ipaddress.IPv4Address(text)
    except ipaddress.AddressValueError as exc:
        raise ValidationError(f"{label} must be an IPv4 address") from exc
    return str(address)


def canonical_host(value: object, label: str = "host") -> str:
    text = require_string(value, label)
    if not text or text != text.strip() or any(character.isspace() for character in text):
        raise ValidationError(f"{label} must be an exact DNS name or IPv4 address")
    lowered = text.lower()
    if (
        lowered.endswith(".")
        or "*" in lowered
        or "://" in lowered
        or "/" in lowered
        or "@" in lowered
        or ":" in lowered
    ):
        raise ValidationError(f"{label} must not contain a scheme, path, port, or wildcard")
    try:
        return str(ipaddress.IPv4Address(lowered))
    except ipaddress.AddressValueError:
        pass
    if re.fullmatch(r"[0-9.]+", lowered):
        raise ValidationError(f"{label} looks like an invalid IPv4 address")
    if len(lowered.encode("ascii", "ignore")) != len(lowered) or len(lowered) > 253:
        raise ValidationError(f"{label} must be a valid DNS name")
    labels = lowered.split(".")
    if len(labels) < 2 or any(not DNS_LABEL_RE.fullmatch(item) for item in labels):
        raise ValidationError(f"{label} must be a valid DNS name")
    return lowered


def canonical_server_names(value: object) -> tuple[str, ...]:
    items = require_list(value, "server names")
    if not 1 <= len(items) <= 32:
        raise ValidationError("server names must contain 1 to 32 entries")
    result = tuple(canonical_host(item, "server name") for item in items)
    if len(set(result)) != len(result):
        raise ValidationError("server names contain a duplicate after canonicalization")
    return result


def validate_route_path(value: object) -> str:
    text = require_string(value, "route path")
    if not text.startswith("/") or len(text.encode("utf-8")) > 512:
        raise ValidationError("route path must start with / and be at most 512 UTF-8 bytes")
    if any(character.isspace() for character in text):
        raise ValidationError("route path must not contain whitespace")
    if any(
        character in "?#\\\"'" or unicodedata.category(character).startswith("C")
        for character in text
    ):
        raise ValidationError("route path contains a forbidden character")
    for index, character in enumerate(text):
        if character == "%" and not HEX_RE.fullmatch(text[index + 1 : index + 3]):
            raise ValidationError("route path contains an invalid percent escape")
    return text


def validate_strategy(value: object) -> Strategy:
    text = require_string(value, "route strategy")
    try:
        return Strategy(text)
    except ValueError as exc:
        raise ValidationError(
            "route strategy must be active-passive or active-active"
        ) from exc


def validate_exit_ids(value: object) -> tuple[str, ...]:
    items = require_list(value, "route exit IDs")
    if not 1 <= len(items) <= 32:
        raise ValidationError("route exit IDs must contain 1 to 32 entries")
    result = tuple(validate_slug(item, "exit ID") for item in items)
    if len(set(result)) != len(result):
        raise ValidationError("route exit IDs contain a duplicate")
    return result


def _unique_ids(items: Sequence[object], label: str) -> None:
    identifiers = [getattr(item, "id") for item in items]
    if len(set(identifiers)) != len(identifiers):
        raise ValidationError(f"{label} IDs must be unique")


def validate_gateway(gateway: Gateway) -> None:
    validate_slug(gateway.id, "gateway ID")
    require_bool(gateway.enabled, "gateway enabled")
    if canonical_ipv4(gateway.listen_address, "gateway listen address") != gateway.listen_address:
        raise ValidationError("gateway listen address must be canonical")
    require_int(gateway.listen_port, "gateway listen port", 1, 65535)
    if canonical_server_names(list(gateway.server_names)) != gateway.server_names:
        raise ValidationError("gateway server names must be canonical")
    require_int(gateway.status_port, "gateway status port", 1024, 65535)
    if gateway.listen_port == gateway.status_port:
        raise ValidationError("gateway public and status ports must be different")


def validate_exit(exit_node: ExitNode) -> None:
    validate_slug(exit_node.id, "exit ID")
    validate_display_name(exit_node.display_name)
    require_bool(exit_node.enabled, "exit enabled")
    if canonical_host(exit_node.host, "exit host") != exit_node.host:
        raise ValidationError("exit host must be canonical")
    require_int(exit_node.socks_port, "SOCKS port", 1, 65535)
    require_int(exit_node.target_port, "target port", 1, 65535)


def validate_route(route: Route) -> None:
    validate_slug(route.id, "route ID")
    validate_display_name(route.display_name)
    require_bool(route.enabled, "route enabled")
    if canonical_host(route.host, "route host") != route.host:
        raise ValidationError("route host must be canonical")
    validate_route_path(route.path)
    if not isinstance(route.strategy, Strategy):
        raise ValidationError("route strategy must use a supported enum value")
    validate_strategy(route.strategy.value)
    validate_exit_ids(list(route.exit_ids))


def validate_binding(binding: Binding) -> None:
    validate_slug(binding.exit_id, "binding exit ID")
    require_bool(binding.enabled, "binding enabled")
    if binding.listen_address != "127.0.0.1":
        raise ValidationError("binding listen address must be 127.0.0.1")
    require_int(binding.listen_port, "binding listen port", 1024, 65535)
    validate_secret_ref(binding.secret_ref, binding.enabled)


def validate_shared(shared: SharedState) -> None:
    require_int(shared.schema_version, "shared schema version", 1, 1)
    if shared.schema_version != SHARED_SCHEMA_VERSION:
        raise ValidationError("shared schema version is unsupported")
    validate_uuid(shared.document_id)
    require_int(shared.revision, "shared revision", 1, 2**63 - 1)
    validate_timestamp(shared.updated_at)
    validate_gateway(shared.gateway)
    if len(shared.exits) > MAX_EXITS or len(shared.routes) > MAX_ROUTES:
        raise ValidationError("shared state exceeds an entity limit")
    for item in shared.exits:
        validate_exit(item)
    for item in shared.routes:
        validate_route(item)
    _unique_ids(shared.exits, "exit")
    _unique_ids(shared.routes, "route")
    all_ids = [shared.gateway.id]
    all_ids.extend(item.id for item in shared.exits)
    all_ids.extend(item.id for item in shared.routes)
    if len(set(all_ids)) != len(all_ids):
        raise ValidationError("gateway, exit, and route IDs must be globally unique")
    exits = {item.id for item in shared.exits}
    server_names = set(shared.gateway.server_names)
    enabled_pairs: dict[tuple[str, str], str] = {}
    for route in shared.routes:
        if route.host not in server_names:
            raise ValidationError("route host is not present in gateway server names")
        if any(exit_id not in exits for exit_id in route.exit_ids):
            raise ValidationError("route references an unknown exit")
        if route.enabled:
            key = (route.host, route.path)
            conflict = enabled_pairs.get(key)
            if conflict is not None:
                raise ValidationError(
                    f"enabled routes {conflict} and {route.id} conflict on Host + Path"
                )
            enabled_pairs[key] = route.id


def validate_node(node: NodeState) -> None:
    require_int(node.schema_version, "node schema version", 1, 1)
    if node.schema_version != NODE_SCHEMA_VERSION:
        raise ValidationError("node schema version is unsupported")
    validate_uuid(node.document_id)
    validate_slug(node.node_id, "node ID")
    require_int(node.revision, "node revision", 1, 2**63 - 1)
    validate_timestamp(node.updated_at)
    if len(node.bindings) > MAX_BINDINGS:
        raise ValidationError("node state exceeds the binding limit")
    for binding in node.bindings:
        validate_binding(binding)
    exit_ids = [binding.exit_id for binding in node.bindings]
    ports = [binding.listen_port for binding in node.bindings]
    if len(set(exit_ids)) != len(exit_ids):
        raise ValidationError("binding exit IDs must be unique")
    if len(set(ports)) != len(ports):
        raise ValidationError("binding listen ports must be unique")


def validate_pair(pair: StatePair, runtime_ready: bool = False) -> None:
    shared, node = pair.shared, pair.node
    validate_shared(shared)
    validate_node(node)
    if shared.document_id != node.document_id:
        raise ValidationError("shared and node document IDs do not match")
    exits = {item.id: item for item in shared.exits}
    bindings = {item.exit_id: item for item in node.bindings}
    for binding in node.bindings:
        if binding.exit_id not in exits:
            raise ValidationError("binding references an unknown exit")
        if binding.listen_port in {
            shared.gateway.listen_port,
            shared.gateway.status_port,
        }:
            raise ValidationError("binding port conflicts with a gateway listener port")
    for route in shared.routes:
        if not route.enabled:
            continue
        usable = [
            exit_id
            for exit_id in route.exit_ids
            if exits[exit_id].enabled
            and exit_id in bindings
            and bindings[exit_id].enabled
            and bool(bindings[exit_id].secret_ref)
        ]
        if not usable:
            raise ValidationError(
                f"enabled route {route.id} has no usable exit and local binding"
            )
    if runtime_ready:
        if not shared.gateway.enabled:
            raise ValidationError("runtime readiness requires an enabled gateway")
        if not any(route.enabled for route in shared.routes):
            raise ValidationError("runtime readiness requires an enabled route")
