"""Validated state-only CRUD operations for the gateway documents."""

from __future__ import annotations

from dataclasses import replace

from gateway.errors import ConflictError, ValidationError
from gateway.models import (
    MAX_BINDINGS,
    MAX_EXITS,
    MAX_ROUTES,
    Binding,
    ExitNode,
    NodeState,
    Route,
    SharedState,
    StatePair,
    Strategy,
)
from gateway.store import GatewayStateStore, MutationResult
from gateway.validation import (
    canonical_host,
    canonical_ipv4,
    canonical_server_names,
    require_bool,
    require_int,
    validate_display_name,
    validate_exit_ids,
    validate_pair,
    validate_route_path,
    validate_secret_ref,
    validate_slug,
    validate_strategy,
)


def _find_exit(shared: SharedState, exit_id: str) -> ExitNode:
    for item in shared.exits:
        if item.id == exit_id:
            return item
    raise ConflictError(f"exit {exit_id} does not exist")


def _find_route(shared: SharedState, route_id: str) -> Route:
    for item in shared.routes:
        if item.id == route_id:
            return item
    raise ConflictError(f"route {route_id} does not exist")


def _usable_exit_ids(pair: StatePair, route: Route) -> tuple[str, ...]:
    exits = {item.id: item for item in pair.shared.exits}
    bindings = {item.exit_id: item for item in pair.node.bindings}
    return tuple(
        exit_id
        for exit_id in route.exit_ids
        if exits.get(exit_id) is not None
        and exits[exit_id].enabled
        and bindings.get(exit_id) is not None
        and bindings[exit_id].enabled
        and bool(bindings[exit_id].secret_ref)
    )


def _require_usable_backends(pair: StatePair) -> None:
    for route in pair.shared.routes:
        if route.enabled and not _usable_exit_ids(pair, route):
            raise ConflictError(
                f"mutation would leave enabled route {route.id} without a usable backend"
            )


def _require_unique_enabled_route(candidate: Route, routes: tuple[Route, ...]) -> None:
    if not candidate.enabled:
        return
    for route in routes:
        if (
            route.id != candidate.id
            and route.enabled
            and route.host == candidate.host
            and route.path == candidate.path
        ):
            raise ConflictError(
                f"enabled routes {route.id} and {candidate.id} conflict on Host + Path"
            )


def _require_unique_global_id(shared: SharedState, identifier: str) -> None:
    identifiers = {shared.gateway.id}
    identifiers.update(item.id for item in shared.exits)
    identifiers.update(item.id for item in shared.routes)
    if identifier in identifiers:
        raise ConflictError(f"gateway entity ID {identifier} already exists")


def _sort_exits(items: list[ExitNode]) -> tuple[ExitNode, ...]:
    return tuple(sorted(items, key=lambda item: item.id))


def _sort_routes(items: list[Route]) -> tuple[Route, ...]:
    return tuple(sorted(items, key=lambda item: item.id))


def _sort_bindings(items: list[Binding]) -> tuple[Binding, ...]:
    return tuple(sorted(items, key=lambda item: item.exit_id))


class GatewayCRUD:
    """Apply canonicalized CRUD mutations through a revisioned state store."""

    def __init__(self, store: GatewayStateStore) -> None:
        self.store = store

    def show(self, *, runtime_ready: bool = False) -> StatePair:
        return self.store.load_pair(runtime_ready=runtime_ready)

    def set_gateway(
        self,
        *,
        enabled: bool | None = None,
        listen_address: str | None = None,
        listen_port: int | None = None,
        server_names: list[str] | tuple[str, ...] | None = None,
        status_port: int | None = None,
        expected_revision: int | None = None,
    ) -> MutationResult:
        if all(
            value is None
            for value in (
                enabled,
                listen_address,
                listen_port,
                server_names,
                status_port,
            )
        ):
            raise ValidationError("gateway set requires at least one field option")
        canonical_enabled = (
            require_bool(enabled, "gateway enabled") if enabled is not None else None
        )
        canonical_address = (
            canonical_ipv4(listen_address, "gateway listen address")
            if listen_address is not None
            else None
        )
        canonical_port = (
            require_int(listen_port, "gateway listen port", 1, 65535)
            if listen_port is not None
            else None
        )
        canonical_names = (
            canonical_server_names(list(server_names))
            if server_names is not None
            else None
        )
        canonical_status = (
            require_int(status_port, "gateway status port", 1024, 65535)
            if status_port is not None
            else None
        )

        node = self.store.load_pair().node

        def mutate(shared: SharedState) -> SharedState:
            gateway = replace(
                shared.gateway,
                enabled=(
                    shared.gateway.enabled
                    if canonical_enabled is None
                    else canonical_enabled
                ),
                listen_address=canonical_address or shared.gateway.listen_address,
                listen_port=(
                    shared.gateway.listen_port
                    if canonical_port is None
                    else canonical_port
                ),
                server_names=(
                    shared.gateway.server_names
                    if canonical_names is None
                    else canonical_names
                ),
                status_port=(
                    shared.gateway.status_port
                    if canonical_status is None
                    else canonical_status
                ),
            )
            route_hosts = {route.host for route in shared.routes}
            missing_hosts = route_hosts.difference(gateway.server_names)
            if missing_hosts:
                raise ConflictError(
                    "gateway server names cannot remove a host used by a route"
                )
            binding_ports = {binding.listen_port for binding in node.bindings}
            if gateway.listen_port in binding_ports or gateway.status_port in binding_ports:
                raise ConflictError("gateway listener port conflicts with a local binding")
            candidate = replace(shared, gateway=gateway)
            validate_pair(StatePair(candidate, node))
            return candidate

        return self.store.mutate_shared(
            mutate, expected_revision=expected_revision
        )

    def add_exit(
        self,
        *,
        exit_id: str,
        display_name: str,
        host: str,
        socks_port: int,
        target_port: int,
        enabled: bool = True,
        expected_revision: int | None = None,
    ) -> MutationResult:
        candidate = ExitNode(
            id=validate_slug(exit_id, "exit ID"),
            display_name=validate_display_name(display_name),
            enabled=require_bool(enabled, "exit enabled"),
            host=canonical_host(host, "exit host"),
            socks_port=require_int(socks_port, "SOCKS port", 1, 65535),
            target_port=require_int(target_port, "target port", 1, 65535),
        )

        def mutate(shared: SharedState) -> SharedState:
            _require_unique_global_id(shared, candidate.id)
            if len(shared.exits) >= MAX_EXITS:
                raise ConflictError("shared state has reached the exit limit")
            return replace(shared, exits=_sort_exits([*shared.exits, candidate]))

        return self.store.mutate_shared(
            mutate, expected_revision=expected_revision
        )

    def edit_exit(
        self,
        *,
        exit_id: str,
        display_name: str | None = None,
        host: str | None = None,
        socks_port: int | None = None,
        target_port: int | None = None,
        enabled: bool | None = None,
        expected_revision: int | None = None,
    ) -> MutationResult:
        identifier = validate_slug(exit_id, "exit ID")
        if all(
            value is None
            for value in (display_name, host, socks_port, target_port, enabled)
        ):
            raise ValidationError("exit edit requires at least one field option")
        canonical_name = (
            validate_display_name(display_name) if display_name is not None else None
        )
        canonical_host_value = (
            canonical_host(host, "exit host") if host is not None else None
        )
        canonical_socks = (
            require_int(socks_port, "SOCKS port", 1, 65535)
            if socks_port is not None
            else None
        )
        canonical_target = (
            require_int(target_port, "target port", 1, 65535)
            if target_port is not None
            else None
        )
        canonical_enabled = (
            require_bool(enabled, "exit enabled") if enabled is not None else None
        )
        node = self.store.load_pair().node

        def mutate(shared: SharedState) -> SharedState:
            current = _find_exit(shared, identifier)
            edited = replace(
                current,
                display_name=canonical_name or current.display_name,
                host=canonical_host_value or current.host,
                socks_port=(current.socks_port if canonical_socks is None else canonical_socks),
                target_port=(
                    current.target_port if canonical_target is None else canonical_target
                ),
                enabled=current.enabled if canonical_enabled is None else canonical_enabled,
            )
            exits = [edited if item.id == identifier else item for item in shared.exits]
            candidate = replace(shared, exits=_sort_exits(exits))
            _require_usable_backends(StatePair(candidate, node))
            return candidate

        return self.store.mutate_shared(
            mutate, expected_revision=expected_revision
        )

    def delete_exit(
        self, *, exit_id: str, expected_revision: int | None = None
    ) -> MutationResult:
        identifier = validate_slug(exit_id, "exit ID")

        def mutate(shared: SharedState) -> SharedState:
            _find_exit(shared, identifier)
            references = sorted(
                route.id for route in shared.routes if identifier in route.exit_ids
            )
            if references:
                raise ConflictError(
                    f"exit {identifier} is referenced by route {references[0]}"
                )
            exits = [item for item in shared.exits if item.id != identifier]
            return replace(shared, exits=_sort_exits(exits))

        return self.store.mutate_shared(
            mutate, expected_revision=expected_revision
        )

    def set_binding(
        self,
        *,
        exit_id: str,
        listen_port: int,
        secret_ref: str,
        enabled: bool,
        expected_revision: int | None = None,
    ) -> MutationResult:
        identifier = validate_slug(exit_id, "binding exit ID")
        canonical_enabled = require_bool(enabled, "binding enabled")
        binding = Binding(
            exit_id=identifier,
            enabled=canonical_enabled,
            listen_address="127.0.0.1",
            listen_port=require_int(
                listen_port, "binding listen port", 1024, 65535
            ),
            secret_ref=validate_secret_ref(secret_ref, canonical_enabled),
        )
        shared = self.store.load_pair().shared
        _find_exit(shared, identifier)

        def mutate(node: NodeState) -> NodeState:
            existing = {item.exit_id: item for item in node.bindings}
            if identifier not in existing and len(node.bindings) >= MAX_BINDINGS:
                raise ConflictError("node state has reached the binding limit")
            for item in node.bindings:
                if item.exit_id != identifier and item.listen_port == binding.listen_port:
                    raise ConflictError("binding listen port is already in use")
            if binding.listen_port in {
                shared.gateway.listen_port,
                shared.gateway.status_port,
            }:
                raise ConflictError("binding port conflicts with a gateway listener")
            bindings = [
                binding if item.exit_id == identifier else item
                for item in node.bindings
            ]
            if identifier not in existing:
                bindings.append(binding)
            candidate = replace(node, bindings=_sort_bindings(bindings))
            _require_usable_backends(StatePair(shared, candidate))
            return candidate

        return self.store.mutate_node(mutate, expected_revision=expected_revision)

    def remove_binding(
        self, *, exit_id: str, expected_revision: int | None = None
    ) -> MutationResult:
        identifier = validate_slug(exit_id, "binding exit ID")
        shared = self.store.load_pair().shared

        def mutate(node: NodeState) -> NodeState:
            if not any(item.exit_id == identifier for item in node.bindings):
                raise ConflictError(f"binding for exit {identifier} does not exist")
            bindings = [item for item in node.bindings if item.exit_id != identifier]
            candidate = replace(node, bindings=_sort_bindings(bindings))
            _require_usable_backends(StatePair(shared, candidate))
            return candidate

        return self.store.mutate_node(mutate, expected_revision=expected_revision)

    def add_route(
        self,
        *,
        route_id: str,
        display_name: str,
        host: str,
        path: str,
        strategy: str | Strategy,
        exit_ids: list[str] | tuple[str, ...],
        enabled: bool = False,
        expected_revision: int | None = None,
    ) -> MutationResult:
        strategy_value = strategy.value if isinstance(strategy, Strategy) else strategy
        candidate = Route(
            id=validate_slug(route_id, "route ID"),
            display_name=validate_display_name(display_name),
            enabled=require_bool(enabled, "route enabled"),
            host=canonical_host(host, "route host"),
            path=validate_route_path(path),
            strategy=validate_strategy(strategy_value),
            exit_ids=validate_exit_ids(list(exit_ids)),
        )
        node = self.store.load_pair().node

        def mutate(shared: SharedState) -> SharedState:
            _require_unique_global_id(shared, candidate.id)
            if len(shared.routes) >= MAX_ROUTES:
                raise ConflictError("shared state has reached the route limit")
            _require_unique_enabled_route(candidate, shared.routes)
            result = replace(shared, routes=_sort_routes([*shared.routes, candidate]))
            validate_pair(StatePair(result, node))
            return result

        return self.store.mutate_shared(
            mutate, expected_revision=expected_revision
        )

    def edit_route(
        self,
        *,
        route_id: str,
        display_name: str | None = None,
        host: str | None = None,
        path: str | None = None,
        strategy: str | Strategy | None = None,
        exit_ids: list[str] | tuple[str, ...] | None = None,
        enabled: bool | None = None,
        expected_revision: int | None = None,
    ) -> MutationResult:
        identifier = validate_slug(route_id, "route ID")
        if all(
            value is None
            for value in (display_name, host, path, strategy, exit_ids, enabled)
        ):
            raise ValidationError("route edit requires at least one field option")
        canonical_name = (
            validate_display_name(display_name) if display_name is not None else None
        )
        canonical_host_value = (
            canonical_host(host, "route host") if host is not None else None
        )
        canonical_path = validate_route_path(path) if path is not None else None
        strategy_value = strategy.value if isinstance(strategy, Strategy) else strategy
        canonical_strategy = (
            validate_strategy(strategy_value) if strategy_value is not None else None
        )
        canonical_exit_ids = (
            validate_exit_ids(list(exit_ids)) if exit_ids is not None else None
        )
        canonical_enabled = (
            require_bool(enabled, "route enabled") if enabled is not None else None
        )
        node = self.store.load_pair().node

        def mutate(shared: SharedState) -> SharedState:
            current = _find_route(shared, identifier)
            edited = replace(
                current,
                display_name=canonical_name or current.display_name,
                host=canonical_host_value or current.host,
                path=current.path if canonical_path is None else canonical_path,
                strategy=(
                    current.strategy if canonical_strategy is None else canonical_strategy
                ),
                exit_ids=(
                    current.exit_ids
                    if canonical_exit_ids is None
                    else canonical_exit_ids
                ),
                enabled=current.enabled if canonical_enabled is None else canonical_enabled,
            )
            _require_unique_enabled_route(edited, shared.routes)
            routes = [edited if item.id == identifier else item for item in shared.routes]
            candidate = replace(shared, routes=_sort_routes(routes))
            validate_pair(StatePair(candidate, node))
            return candidate

        return self.store.mutate_shared(
            mutate, expected_revision=expected_revision
        )

    def delete_route(
        self, *, route_id: str, expected_revision: int | None = None
    ) -> MutationResult:
        identifier = validate_slug(route_id, "route ID")

        def mutate(shared: SharedState) -> SharedState:
            _find_route(shared, identifier)
            routes = [item for item in shared.routes if item.id != identifier]
            return replace(shared, routes=_sort_routes(routes))

        return self.store.mutate_shared(
            mutate, expected_revision=expected_revision
        )
