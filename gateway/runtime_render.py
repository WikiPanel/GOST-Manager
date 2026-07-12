"""Deterministic non-secret runtime rendering and strict manifest parsing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from gateway.errors import StateError, ValidationError
from gateway.runtime_models import DesiredExitRuntime, RuntimeEntry
from gateway.runtime_paths import RuntimePaths, service_name
from gateway.validation import canonical_host, validate_slug

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


def parse_manifest(data: bytes) -> dict[str, RuntimeEntry]:
    if len(data) > MAX_RUNTIME_MANIFEST_BYTES:
        raise StateError("runtime manifest exceeds its size limit")
    try:
        value = json.loads(data.decode("utf-8"))
        if (
            not isinstance(value, dict)
            or set(value) != {
                "schema_version", "applied_at", "document_id", "shared_revision",
                "node_revision", "services",
            }
            or value.get("schema_version") != 1
        ):
            raise ValueError
        services = value["services"]
        if not isinstance(services, list):
            raise ValueError
        result: dict[str, RuntimeEntry] = {}
        for item in services:
            entry = RuntimeEntry(**item)
            if entry.exit_id in result or entry.service_name != service_name(entry.exit_id):
                raise ValueError
            result[entry.exit_id] = entry
        return result
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise StateError("runtime manifest is corrupt or unsupported") from exc
