"""Immutable gateway desired-state models."""

from __future__ import annotations

import enum
from dataclasses import dataclass


SHARED_SCHEMA_VERSION = 1
NODE_SCHEMA_VERSION = 1
MAX_EXITS = 256
MAX_ROUTES = 256
MAX_BINDINGS = 256
MAX_SHARED_BYTES = 1024 * 1024
MAX_NODE_BYTES = 512 * 1024


class Strategy(str, enum.Enum):
    ACTIVE_PASSIVE = "active-passive"
    ACTIVE_ACTIVE = "active-active"


@dataclass(frozen=True)
class Gateway:
    id: str
    enabled: bool
    listen_address: str
    listen_port: int
    server_names: tuple[str, ...]
    status_port: int


@dataclass(frozen=True)
class ExitNode:
    id: str
    display_name: str
    enabled: bool
    host: str
    socks_port: int
    target_port: int


@dataclass(frozen=True)
class Route:
    id: str
    display_name: str
    enabled: bool
    host: str
    path: str
    strategy: Strategy
    exit_ids: tuple[str, ...]


@dataclass(frozen=True)
class SharedState:
    schema_version: int
    document_id: str
    revision: int
    updated_at: str
    gateway: Gateway
    exits: tuple[ExitNode, ...]
    routes: tuple[Route, ...]


@dataclass(frozen=True)
class Binding:
    exit_id: str
    enabled: bool
    listen_address: str
    listen_port: int
    secret_ref: str


@dataclass(frozen=True)
class NodeState:
    schema_version: int
    document_id: str
    node_id: str
    revision: int
    updated_at: str
    bindings: tuple[Binding, ...]


@dataclass(frozen=True)
class StatePair:
    shared: SharedState
    node: NodeState
