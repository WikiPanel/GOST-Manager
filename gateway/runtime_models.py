"""Immutable models shared by gateway runtime planning and activation."""

from __future__ import annotations

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


@dataclass(frozen=True)
class Listener:
    address: str
    port: int
    pids: tuple[int, ...]
    process_names: tuple[str, ...]

    @property
    def wildcard(self) -> bool:
        return self.address in {"*", "0.0.0.0", "::", "[::]"}


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
