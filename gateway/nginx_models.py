"""Immutable models for the dedicated NGINX Gateway runtime."""

from __future__ import annotations

import enum
from dataclasses import dataclass


NGINX_CONFIG_SCHEMA_VERSION = 1
NGINX_MANIFEST_SCHEMA_VERSION = 1
MAX_NGINX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_NGINX_MANIFEST_BYTES = 1024 * 1024
MAX_NGINX_ROUTES = 256
MAX_NGINX_BACKENDS = 32


@dataclass(frozen=True)
class NginxBackend:
    exit_id: str
    address: str
    port: int

    @property
    def endpoint(self) -> str:
        return f"{self.address}:{self.port}"


@dataclass(frozen=True)
class NginxRoute:
    route_id: str
    host: str
    path: str
    strategy: str
    upstream_name: str
    backends: tuple[NginxBackend, ...]


@dataclass(frozen=True)
class NginxCandidate:
    document_id: str
    shared_revision: int
    node_revision: int
    gateway_id: str
    listen_address: str
    listen_port: int
    status_address: str
    status_port: int
    routes: tuple[NginxRoute, ...]


@dataclass(frozen=True)
class NginxManifestRoute:
    route_id: str
    host: str
    path: str
    strategy: str
    upstream_name: str
    backend_exit_ids: tuple[str, ...]
    backend_addresses: tuple[str, ...]


@dataclass(frozen=True)
class NginxManifest:
    schema_version: int
    applied_at: str
    document_id: str
    shared_revision: int
    node_revision: int
    service_name: str
    config_path: str
    config_sha256: str
    listen_address: str
    listen_port: int
    status_address: str
    status_port: int
    routes: tuple[NginxManifestRoute, ...]


@dataclass(frozen=True)
class NginxServiceState:
    loaded: bool
    enabled: bool
    active: bool
    substate: str
    main_pid: int | None
    control_group: str
    fragment_path: str
    pids: tuple[int, ...]
    pids_authoritative: bool


class ListenerOwnership(str, enum.Enum):
    FREE = "free"
    SAME_SERVICE = "same-service"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"
    MISSING = "missing"


@dataclass(frozen=True)
class NginxPlan:
    action: str
    reason_codes: tuple[str, ...]
    gateway_id: str
    current_state: str
    desired_state: str
    listen_address: str
    listen_port: int
    status_port: int
    enabled_route_count: int
    backend_count: int
    config_changed: bool
    manifest_changed: bool
    affected_route_ids: tuple[str, ...]
    affected_exit_ids: tuple[str, ...]
    config: bytes | None = None
    manifest: bytes | None = None

    @property
    def has_conflict(self) -> bool:
        return self.action in {"conflict", "dependency-missing"}


@dataclass(frozen=True)
class NginxApplyResult:
    plan: NginxPlan
    changed: bool
    reload_count: int = 0
    restart_count: int = 0


@dataclass(frozen=True)
class StubStatus:
    active: int
    accepted: int
    handled: int
    requests: int
    reading: int
    writing: int
    waiting: int


@dataclass(frozen=True)
class DependencyStatus:
    binary_path: str
    present: bool
    regular: bool
    symlink: bool
    executable: bool
    link_count_safe: bool
    version: str
    distro_loaded: bool
    distro_enabled: bool
    distro_active: bool
    gateway_loaded: bool
    gateway_enabled: bool
    gateway_active: bool
