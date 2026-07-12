"""Strict deterministic NGINX runtime manifest serialization."""

from __future__ import annotations

import json
import re

from gateway.errors import StateError, ValidationError
from gateway.nginx_models import (
    MAX_NGINX_BACKENDS,
    MAX_NGINX_MANIFEST_BYTES,
    MAX_NGINX_ROUTES,
    NGINX_MANIFEST_SCHEMA_VERSION,
    NginxCandidate,
    NginxManifest,
    NginxManifestRoute,
)
from gateway.nginx_paths import NGINX_SERVICE_NAME, NginxPaths
from gateway.nginx_render import validate_nginx_path, upstream_name
from gateway.runtime_render import sha256
from gateway.validation import (
    canonical_host,
    canonical_ipv4,
    require_int,
    require_string,
    validate_slug,
    validate_timestamp,
    validate_uuid,
)


SHA_RE = re.compile(r"^[0-9a-f]{64}$")
UPSTREAM_RE = re.compile(r"^gmgw_route_[0-9a-f]{20}$")
TOP_KEYS = frozenset(
    {
        "schema_version", "applied_at", "document_id", "shared_revision",
        "node_revision", "service_name", "config_path", "config_sha256",
        "listen_address", "listen_port", "status_address", "status_port", "routes",
    }
)
ROUTE_KEYS = frozenset(
    {
        "route_id", "host", "path", "strategy", "upstream_name",
        "backend_exit_ids", "backend_addresses",
    }
)


def render_manifest(
    candidate: NginxCandidate,
    config: bytes,
    paths: NginxPaths,
    applied_at: str,
) -> bytes:
    validate_timestamp(applied_at)
    value = {
        "schema_version": NGINX_MANIFEST_SCHEMA_VERSION,
        "applied_at": applied_at,
        "document_id": candidate.document_id,
        "shared_revision": candidate.shared_revision,
        "node_revision": candidate.node_revision,
        "service_name": NGINX_SERVICE_NAME,
        "config_path": str(paths.config_file),
        "config_sha256": sha256(config),
        "listen_address": candidate.listen_address,
        "listen_port": candidate.listen_port,
        "status_address": candidate.status_address,
        "status_port": candidate.status_port,
        "routes": [
            {
                "route_id": route.route_id,
                "host": route.host,
                "path": route.path,
                "strategy": route.strategy,
                "upstream_name": route.upstream_name,
                "backend_exit_ids": [item.exit_id for item in route.backends],
                "backend_addresses": [item.endpoint for item in route.backends],
            }
            for route in candidate.routes
        ],
    }
    data = (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(data) > MAX_NGINX_MANIFEST_BYTES:
        raise ValidationError("NGINX runtime manifest exceeds 1 MiB")
    parse_manifest(data, paths)
    return data


def _strict_json(data: bytes) -> object:
    def unique(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    return json.loads(data.decode("utf-8"), object_pairs_hook=unique)


def _endpoint(value: object) -> tuple[str, int]:
    text = require_string(value, "backend address")
    if ":" not in text:
        raise ValueError
    address, raw_port = text.rsplit(":", 1)
    if canonical_ipv4(address, "backend address") != "127.0.0.1":
        raise ValueError
    port = require_int(int(raw_port), "backend port", 1024, 65535)
    if text != f"127.0.0.1:{port}":
        raise ValueError
    return address, port


def parse_manifest(data: bytes, paths: NginxPaths) -> NginxManifest:
    if not data or len(data) > MAX_NGINX_MANIFEST_BYTES:
        raise StateError("NGINX runtime manifest exceeds its size limit")
    try:
        value = _strict_json(data)
        if not isinstance(value, dict) or frozenset(value) != TOP_KEYS:
            raise ValueError
        schema = require_int(value["schema_version"], "manifest schema", 1, 1)
        if schema != NGINX_MANIFEST_SCHEMA_VERSION:
            raise ValueError
        applied_at = validate_timestamp(value["applied_at"])
        document_id = validate_uuid(value["document_id"])
        shared_revision = require_int(value["shared_revision"], "shared revision", 1, 2**63 - 1)
        node_revision = require_int(value["node_revision"], "node revision", 1, 2**63 - 1)
        service = require_string(value["service_name"], "service name")
        config_path = require_string(value["config_path"], "config path")
        config_hash = require_string(value["config_sha256"], "config SHA-256")
        listen_address = canonical_ipv4(value["listen_address"], "listen address")
        listen_port = require_int(value["listen_port"], "listen port", 1, 65535)
        status_address = canonical_ipv4(value["status_address"], "status address")
        status_port = require_int(value["status_port"], "status port", 1024, 65535)
        if (
            service != NGINX_SERVICE_NAME
            or config_path != str(paths.config_file)
            or not SHA_RE.fullmatch(config_hash)
            or status_address != "127.0.0.1"
            or listen_port == status_port
        ):
            raise ValueError
        raw_routes = value["routes"]
        if not isinstance(raw_routes, list) or len(raw_routes) > MAX_NGINX_ROUTES:
            raise ValueError
        routes: list[NginxManifestRoute] = []
        seen: set[str] = set()
        seen_host_paths: set[tuple[str, str]] = set()
        for item in raw_routes:
            if not isinstance(item, dict) or frozenset(item) != ROUTE_KEYS:
                raise ValueError
            route_id = validate_slug(item["route_id"], "route ID")
            host = canonical_host(item["host"], "route host")
            path = validate_nginx_path(require_string(item["path"], "route path"))
            strategy = require_string(item["strategy"], "route strategy")
            name = require_string(item["upstream_name"], "upstream name")
            ids = item["backend_exit_ids"]
            addresses = item["backend_addresses"]
            if (
                route_id in seen
                or strategy not in {"active-active", "active-passive"}
                or not UPSTREAM_RE.fullmatch(name)
                or name != upstream_name(route_id)
                or not isinstance(ids, list)
                or not isinstance(addresses, list)
                or not 1 <= len(ids) <= MAX_NGINX_BACKENDS
                or len(ids) != len(addresses)
                or len(set(addresses)) != len(addresses)
                or (host, path) in seen_host_paths
            ):
                raise ValueError
            backend_ids = tuple(validate_slug(value, "exit ID") for value in ids)
            if len(set(backend_ids)) != len(backend_ids):
                raise ValueError
            for endpoint in addresses:
                _endpoint(endpoint)
            routes.append(
                NginxManifestRoute(
                    route_id, host, path, strategy, name, backend_ids,
                    tuple(require_string(value, "backend address") for value in addresses),
                )
            )
            seen.add(route_id)
            seen_host_paths.add((host, path))
        if routes != sorted(routes, key=lambda item: (item.host, item.path, item.route_id)):
            raise ValueError
        return NginxManifest(
            schema, applied_at, document_id, shared_revision, node_revision,
            service, config_path, config_hash, listen_address, listen_port,
            status_address, status_port, tuple(routes),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError, ValidationError) as exc:
        raise StateError("NGINX runtime manifest is corrupt or unsupported") from exc
