"""Shared deterministic fixtures for gateway desired-state tests."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

from gateway.cli import main
from gateway.models import (
    Binding,
    ExitNode,
    Gateway,
    NodeState,
    Route,
    SharedState,
    StatePair,
    Strategy,
)
from gateway.paths import StatePaths
from gateway.store import GatewayStateStore

DOCUMENT_ID = "00000000-0000-4000-8000-000000000001"
TIMESTAMP = "2026-07-12T00:00:00Z"


class StepClock:
    def __init__(self) -> None:
        self._second = 0

    def __call__(self) -> dt.datetime:
        value = dt.datetime(2026, 7, 12, tzinfo=dt.timezone.utc)
        result = value + dt.timedelta(seconds=self._second)
        self._second += 1
        return result


def make_pair(
    *,
    gateway_enabled: bool = False,
    route_enabled: bool = False,
    exit_enabled: bool = True,
    binding_enabled: bool = True,
    secret_ref: str = "secret-ee-primary",
    strategy: Strategy = Strategy.ACTIVE_PASSIVE,
) -> StatePair:
    shared = SharedState(
        schema_version=1,
        document_id=DOCUMENT_ID,
        revision=1,
        updated_at=TIMESTAMP,
        gateway=Gateway(
            id="gateway-main",
            enabled=gateway_enabled,
            listen_address="0.0.0.0",
            listen_port=80,
            server_names=("gateway.example.org",),
            status_port=18000,
        ),
        exits=(
            ExitNode(
                id="ee-primary",
                display_name="Estonia primary",
                enabled=exit_enabled,
                host="192.0.2.10",
                socks_port=28420,
                target_port=18081,
            ),
        ),
        routes=(
            Route(
                id="route-estonia",
                display_name="Estonia",
                enabled=route_enabled,
                host="gateway.example.org",
                path="/ee1/api/v1",
                strategy=strategy,
                exit_ids=("ee-primary",),
            ),
        ),
    )
    node = NodeState(
        schema_version=1,
        document_id=DOCUMENT_ID,
        node_id="iran-gateway-1",
        revision=1,
        updated_at=TIMESTAMP,
        bindings=(
            Binding(
                exit_id="ee-primary",
                enabled=binding_enabled,
                listen_address="127.0.0.1",
                listen_port=18081,
                secret_ref=secret_ref,
            ),
        ),
    )
    return StatePair(shared, node)


def add_secondary(pair: StatePair, *, route_enabled: bool | None = None) -> StatePair:
    secondary = ExitNode(
        id="de-backup",
        display_name="Germany backup",
        enabled=True,
        host="198.51.100.20",
        socks_port=28421,
        target_port=18082,
    )
    binding = Binding(
        exit_id="de-backup",
        enabled=True,
        listen_address="127.0.0.1",
        listen_port=18082,
        secret_ref="secret-de-backup",
    )
    route = replace(
        pair.shared.routes[0],
        enabled=(
            pair.shared.routes[0].enabled
            if route_enabled is None
            else route_enabled
        ),
        exit_ids=("ee-primary", "de-backup"),
    )
    return StatePair(
        replace(pair.shared, exits=(secondary, *pair.shared.exits), routes=(route,)),
        replace(pair.node, bindings=(binding, *pair.node.bindings)),
    )


class TemporaryStore:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.paths = StatePaths.from_values(
            self.root / "state.json",
            self.root / "node.json",
            self.root / "backups",
            self.root / "gateway.lock",
        )
        self.clock = StepClock()

    def store(self, **kwargs: object) -> GatewayStateStore:
        return GatewayStateStore(
            self.paths,
            clock=self.clock,
            uuid_factory=lambda: uuid.UUID(DOCUMENT_ID),
            **kwargs,
        )

    def initialize(self, **kwargs: object) -> GatewayStateStore:
        store = self.store(**kwargs)
        store.initialize(
            gateway_id="gateway-main",
            node_id="iran-gateway-1",
            listen_address="0.0.0.0",
            listen_port=80,
            server_names=["Gateway.Example.Org"],
        )
        return store

    def close(self) -> None:
        self.temporary.cleanup()


def cli_paths(paths: StatePaths) -> list[str]:
    return [
        "--state-file",
        str(paths.state_file),
        "--node-file",
        str(paths.node_file),
        "--backup-dir",
        str(paths.backup_dir),
        "--lock-file",
        str(paths.lock_file),
    ]


def run_cli(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = main(arguments)
    return result, stdout.getvalue(), stderr.getvalue()
