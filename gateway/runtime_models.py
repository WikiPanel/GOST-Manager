"""Immutable models shared by gateway runtime planning and activation."""

from __future__ import annotations

import enum
from dataclasses import dataclass


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str


@dataclass(frozen=True)
class DesiredExitRuntime:
    exit_id: str
    service_name: str
    listen_address: str
    listen_port: int
    exit_host: str
    socks_port: int
    target_address: str
    target_port: int
    secret_ref: str
    secret_mtime_ns: int


@dataclass(frozen=True)
class ServiceState:
    service_name: str
    loaded: bool
    enabled: bool
    active: bool
    main_pid: int | None
    control_group: str = ""
    pids: tuple[int, ...] = ()
    pids_authoritative: bool = False


@dataclass(frozen=True)
class Listener:
    address: str
    port: int
    pids: tuple[int, ...]
    process_names: tuple[str, ...]

    @property
    def wildcard(self) -> bool:
        return self.address in {"*", "0.0.0.0", "::", "[::]"}


class ListenerDisposition(str, enum.Enum):
    EXACT_SAME_SERVICE = "exact_same_service"
    FREE = "free"
    MISSING_FOR_ACTIVE_SERVICE = "missing_for_active_service"
    CONFLICT = "conflict"
    OWNERSHIP_UNAVAILABLE = "ownership_unavailable"


@dataclass(frozen=True)
class RuntimeDiscovery:
    env_ids: frozenset[str]
    unit_ids: frozenset[str]
    manifest_ids: frozenset[str]
    systemd_ids: frozenset[str]

    @property
    def all_ids(self) -> frozenset[str]:
        return self.env_ids | self.unit_ids | self.manifest_ids | self.systemd_ids


@dataclass(frozen=True)
class RuntimeEntry:
    exit_id: str
    service_name: str
    env_path: str
    unit_path: str
    secret_ref: str
    secret_mtime_ns: int
    env_sha256: str
    unit_sha256: str
    desired_enabled: bool


@dataclass(frozen=True)
class RuntimeManifest:
    applied_at: str
    document_id: str
    shared_revision: int
    node_revision: int
    entries: tuple[RuntimeEntry, ...]


@dataclass(frozen=True)
class PlanAction:
    exit_id: str
    service_name: str
    action: str
    reason: str
    current_state: str
    desired_state: str
    listen_address: str
    listen_port: int
    secret_ref: str


@dataclass(frozen=True)
class RuntimePlan:
    actions: tuple[PlanAction, ...]
    desired: tuple[DesiredExitRuntime, ...]
    listener_snapshot_count: int = 1

    @property
    def has_conflict(self) -> bool:
        return any(item.action == "conflict" for item in self.actions)
