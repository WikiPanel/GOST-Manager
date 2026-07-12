"""Deterministic non-secret runtime rendering and strict manifest parsing."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict

from gateway.errors import StateError, ValidationError
from gateway.models import MAX_EXITS
from gateway.runtime_models import DesiredExitRuntime, RuntimeEntry, RuntimeManifest
from gateway.runtime_paths import RuntimePaths, service_name
from gateway.validation import (
    require_bool, require_int, require_string, validate_secret_ref,
    validate_slug, validate_timestamp, validate_uuid, canonical_host,
)

MAX_RUNTIME_MANIFEST_BYTES = 512 * 1024


def render_env(desired: DesiredExitRuntime) -> bytes:
    lines = (
        f"GATEWAY_EXIT_ID={desired.exit_id}",
        "GATEWAY_LISTEN_ADDRESS=127.0.0.1",
        f"GATEWAY_LISTEN_PORT={desired.listen_port}",
        f"GATEWAY_EXIT_HOST={desired.exit_host}",
        f"GATEWAY_SOCKS_PORT={desired.socks_port}",
        "GATEWAY_TARGET_ADDRESS=127.0.0.1",
        f"GATEWAY_TARGET_PORT={desired.target_port}",
    )
    return ("\n".join(lines) + "\n").encode("ascii")


def parse_env(data: bytes, exit_id: str) -> dict[str, str]:
    expected_keys = (
        "GATEWAY_EXIT_ID", "GATEWAY_LISTEN_ADDRESS", "GATEWAY_LISTEN_PORT",
        "GATEWAY_EXIT_HOST", "GATEWAY_SOCKS_PORT", "GATEWAY_TARGET_ADDRESS",
        "GATEWAY_TARGET_PORT",
    )
    if not data.endswith(b"\n") or len(data) > 64 * 1024:
        raise StateError("generated gateway environment is corrupt")
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise StateError("generated gateway environment is corrupt") from exc
    if len(lines) != len(expected_keys):
        raise StateError("generated gateway environment is corrupt")
    values: dict[str, str] = {}
    for expected, line in zip(expected_keys, lines):
        if "=" not in line:
            raise StateError("generated gateway environment is corrupt")
        key, value = line.split("=", 1)
        if key != expected or not value:
            raise StateError("generated gateway environment is corrupt")
        values[key] = value
    try:
        if validate_slug(values["GATEWAY_EXIT_ID"], "exit ID") != exit_id:
            raise ValidationError("exit mismatch")
        if values["GATEWAY_LISTEN_ADDRESS"] != "127.0.0.1":
            raise ValidationError("listen address")
        if values["GATEWAY_TARGET_ADDRESS"] != "127.0.0.1":
            raise ValidationError("target address")
        if canonical_host(values["GATEWAY_EXIT_HOST"], "exit host") != values["GATEWAY_EXIT_HOST"]:
            raise ValidationError("exit host")
        for key in ("GATEWAY_LISTEN_PORT", "GATEWAY_SOCKS_PORT", "GATEWAY_TARGET_PORT"):
            port = int(values[key], 10)
            if not 1 <= port <= 65535 or str(port) != values[key]:
                raise ValidationError("port")
    except (KeyError, ValueError, ValidationError) as exc:
        raise StateError("generated gateway environment is corrupt") from exc
    return values


def render_unit(desired: DesiredExitRuntime, paths: RuntimePaths) -> bytes:
    if desired.service_name != service_name(desired.exit_id):
        raise ValidationError("runtime service name is invalid")
    text = f"""[Unit]
Description=GOST Manager Gateway Exit {desired.exit_id}
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
EnvironmentFile={paths.env_file(desired.exit_id)}
EnvironmentFile={paths.secret_file(desired.secret_ref)}
ExecStart={paths.runner_path}
Restart=on-failure
RestartSec=3
TimeoutStopSec=30
KillSignal=SIGTERM
KillMode=control-group
UMask=0077
LimitNOFILE=200000
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
"""
    validate_unit(text.encode("utf-8"), desired, paths)
    return text.encode("utf-8")


def validate_unit(data: bytes, desired: DesiredExitRuntime, paths: RuntimePaths) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("generated unit is invalid") from exc
    required = (
        f"EnvironmentFile={paths.env_file(desired.exit_id)}",
        f"EnvironmentFile={paths.secret_file(desired.secret_ref)}",
        f"ExecStart={paths.runner_path}",
        "LimitNOFILE=200000",
        "KillMode=control-group",
    )
    forbidden = (
        "nginx.service", "gost-iran-", "gost-kharej-", "gost-monitor-collector.service",
        "PrivateNetwork=true", "network-online.target", "iptables", "nft",
    )
    if any(item not in text for item in required) or any(item in text for item in forbidden):
        raise ValidationError("generated unit violates the runtime contract")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_entry(desired: DesiredExitRuntime, paths: RuntimePaths, env: bytes, unit: bytes) -> RuntimeEntry:
    return RuntimeEntry(
        exit_id=desired.exit_id,
        service_name=desired.service_name,
        env_path=str(paths.env_file(desired.exit_id)),
        unit_path=str(paths.unit_file(desired.exit_id)),
        secret_ref=desired.secret_ref,
        secret_mtime_ns=desired.secret_mtime_ns,
        env_sha256=sha256(env),
        unit_sha256=sha256(unit),
        desired_enabled=True,
    )


def render_manifest(
    *, applied_at: str, document_id: str, shared_revision: int,
    node_revision: int, entries: tuple[RuntimeEntry, ...],
) -> bytes:
    value = {
        "schema_version": 1,
        "applied_at": applied_at,
        "document_id": document_id,
        "shared_revision": shared_revision,
        "node_revision": node_revision,
        "services": [asdict(item) for item in sorted(entries, key=lambda item: item.exit_id)],
    }
    data = (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > MAX_RUNTIME_MANIFEST_BYTES:
        raise ValidationError("runtime manifest exceeds its size limit")
    return data


MANIFEST_KEYS = frozenset(
    {"schema_version", "applied_at", "document_id", "shared_revision", "node_revision", "services"}
)
ENTRY_KEYS = frozenset(
    {
        "exit_id", "service_name", "env_path", "unit_path", "secret_ref",
        "secret_mtime_ns", "env_sha256", "unit_sha256", "desired_enabled",
    }
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _strict_json(data: bytes) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs)


def parse_manifest_document(data: bytes, paths: RuntimePaths) -> RuntimeManifest:
    if len(data) > MAX_RUNTIME_MANIFEST_BYTES:
        raise StateError("runtime manifest exceeds its size limit")
    try:
        value = _strict_json(data)
        if (
            not isinstance(value, dict)
            or frozenset(value) != MANIFEST_KEYS
            or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1
        ):
            raise ValueError
        validate_timestamp(value["applied_at"])
        validate_uuid(value["document_id"])
        require_int(value["shared_revision"], "shared revision", 1, 2**63 - 1)
        require_int(value["node_revision"], "node revision", 1, 2**63 - 1)
        services = value["services"]
        if not isinstance(services, list) or len(services) > MAX_EXITS:
            raise ValueError
        result: dict[str, RuntimeEntry] = {}
        for item in services:
            if not isinstance(item, dict) or frozenset(item) != ENTRY_KEYS:
                raise ValueError
            exit_id = validate_slug(item["exit_id"], "exit ID")
            name = require_string(item["service_name"], "service name")
            env_path = require_string(item["env_path"], "environment path")
            unit_path = require_string(item["unit_path"], "unit path")
            secret_ref = validate_secret_ref(item["secret_ref"], True)
            generation = require_int(item["secret_mtime_ns"], "secret generation", 0, 2**63 - 1)
            env_hash = require_string(item["env_sha256"], "environment SHA-256")
            unit_hash = require_string(item["unit_sha256"], "unit SHA-256")
            enabled = require_bool(item["desired_enabled"], "desired enabled")
            if (
                name != service_name(exit_id)
                or env_path != str(paths.env_file(exit_id))
                or unit_path != str(paths.unit_file(exit_id))
                or not SHA256_RE.fullmatch(env_hash)
                or not SHA256_RE.fullmatch(unit_hash)
            ):
                raise ValueError
            entry = RuntimeEntry(
                exit_id, name, env_path, unit_path, secret_ref, generation,
                env_hash, unit_hash, enabled,
            )
            if entry.exit_id in result:
                raise ValueError
            result[entry.exit_id] = entry
        return RuntimeManifest(
            applied_at=value["applied_at"],
            document_id=value["document_id"],
            shared_revision=value["shared_revision"],
            node_revision=value["node_revision"],
            entries=tuple(result[key] for key in sorted(result)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError, ValidationError) as exc:
        raise StateError("runtime manifest is corrupt or unsupported") from exc


def parse_manifest(data: bytes, paths: RuntimePaths) -> dict[str, RuntimeEntry]:
    document = parse_manifest_document(data, paths)
    return {item.exit_id: item for item in document.entries}
