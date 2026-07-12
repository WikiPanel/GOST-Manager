from __future__ import annotations

import unittest
from dataclasses import replace

from gateway.errors import ValidationError
from gateway.models import Binding, ExitNode, Route, StatePair, Strategy
from gateway.validation import (
    canonical_host,
    canonical_ipv4,
    validate_display_name,
    validate_pair,
    validate_route_path,
    validate_slug,
)
from test_gateway_support import add_secondary, make_pair


class IdentifierAndTextTests(unittest.TestCase):
    def test_valid_ids(self) -> None:
        for value in ("a", "gateway-main", "route-123"):
            with self.subTest(value=value):
                self.assertEqual(validate_slug(value), value)

    def test_invalid_ids(self) -> None:
        for value in ("A", "1route", "route_1", "-route", "a" * 64, "route one"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    validate_slug(value)

    def test_unicode_display_name_is_allowed(self) -> None:
        self.assertEqual(validate_display_name("خروجی آزمایشی"), "خروجی آزمایشی")

    def test_display_name_boundaries(self) -> None:
        self.assertEqual(validate_display_name("x" * 100), "x" * 100)
        for value in ("", " spaced", "spaced ", "x" * 101, "line\nbreak", "nul\x00"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    validate_display_name(value)


class HostAndPathTests(unittest.TestCase):
    def test_dns_is_canonicalized_to_lowercase(self) -> None:
        self.assertEqual(canonical_host("Gateway.Example.Org"), "gateway.example.org")

    def test_ipv4_host_is_canonical(self) -> None:
        self.assertEqual(canonical_host("192.0.2.10"), "192.0.2.10")
        self.assertEqual(canonical_ipv4("0.0.0.0", "listen"), "0.0.0.0")

    def test_unsafe_hosts_are_rejected(self) -> None:
        values = (
            "*.example.org",
            "https://example.org",
            "example.org:443",
            "example.org/path",
            "user@example.org",
            "example.org.",
            "example org",
            "::1",
            "192.0.2.999",
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    canonical_host(value)

    def test_route_path_is_preserved_exactly(self) -> None:
        for value in ("/api/v1", "/api/v1/", "/encoded/%2Fvalue"):
            with self.subTest(value=value):
                self.assertEqual(validate_route_path(value), value)

    def test_route_path_rejects_unsafe_values(self) -> None:
        values = (
            "api/v1",
            "/api?query=1",
            "/api#fragment",
            "/api path",
            "/api\\path",
            '/api"path',
            "/api%",
            "/api%2",
            "/api%GG",
            "/nul\x00",
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    validate_route_path(value)

    def test_route_path_utf8_byte_limit(self) -> None:
        self.assertEqual(validate_route_path("/" + "a" * 511), "/" + "a" * 511)
        with self.assertRaises(ValidationError):
            validate_route_path("/" + "é" * 256)


class CrossDocumentValidationTests(unittest.TestCase):
    def test_gateway_public_and_status_port_must_differ(self) -> None:
        pair = make_pair()
        gateway = replace(pair.shared.gateway, status_port=18000, listen_port=18000)
        with self.assertRaises(ValidationError):
            validate_pair(StatePair(replace(pair.shared, gateway=gateway), pair.node))

    def test_binding_ports_must_be_unique(self) -> None:
        pair = add_secondary(make_pair())
        duplicate = replace(pair.node.bindings[0], listen_port=18081)
        node = replace(pair.node, bindings=(duplicate, pair.node.bindings[1]))
        with self.assertRaisesRegex(ValidationError, "ports must be unique"):
            validate_pair(StatePair(pair.shared, node))

    def test_binding_port_cannot_match_public_or_status_port(self) -> None:
        pair = make_pair()
        public_gateway = replace(pair.shared.gateway, listen_port=18081)
        with self.assertRaisesRegex(ValidationError, "conflicts"):
            validate_pair(
                StatePair(replace(pair.shared, gateway=public_gateway), pair.node)
            )
        status_binding = replace(pair.node.bindings[0], listen_port=18000)
        with self.assertRaisesRegex(ValidationError, "conflicts"):
            validate_pair(
                StatePair(pair.shared, replace(pair.node, bindings=(status_binding,)))
            )

    def test_binding_must_be_loopback(self) -> None:
        pair = make_pair()
        binding = replace(pair.node.bindings[0], listen_address="0.0.0.0")
        with self.assertRaises(ValidationError):
            validate_pair(StatePair(pair.shared, replace(pair.node, bindings=(binding,))))

    def test_route_references_and_membership_are_validated(self) -> None:
        pair = make_pair()
        missing = replace(pair.shared.routes[0], exit_ids=("missing-exit",))
        duplicate = replace(
            pair.shared.routes[0], exit_ids=("ee-primary", "ee-primary")
        )
        for route in (missing, duplicate):
            with self.subTest(route=route):
                with self.assertRaises(ValidationError):
                    validate_pair(
                        StatePair(replace(pair.shared, routes=(route,)), pair.node)
                    )

    def test_route_host_must_be_a_gateway_server_name(self) -> None:
        pair = make_pair()
        route = replace(pair.shared.routes[0], host="other.example.org")
        with self.assertRaisesRegex(ValidationError, "server names"):
            validate_pair(StatePair(replace(pair.shared, routes=(route,)), pair.node))

    def test_enabled_host_path_collision_is_rejected_with_both_ids(self) -> None:
        pair = make_pair(route_enabled=True)
        route = replace(pair.shared.routes[0], id="route-copy")
        shared = replace(pair.shared, routes=(pair.shared.routes[0], route))
        with self.assertRaises(ValidationError) as caught:
            validate_pair(StatePair(shared, pair.node))
        message = str(caught.exception)
        self.assertIn("route-estonia", message)
        self.assertIn("route-copy", message)

    def test_disabled_host_path_collision_is_allowed(self) -> None:
        pair = make_pair()
        route = replace(pair.shared.routes[0], id="route-copy")
        validate_pair(
            StatePair(replace(pair.shared, routes=(pair.shared.routes[0], route)), pair.node)
        )

    def test_trailing_slash_paths_are_distinct(self) -> None:
        pair = make_pair(route_enabled=True)
        route = replace(pair.shared.routes[0], id="route-copy", path="/ee1/api/v1/")
        validate_pair(
            StatePair(replace(pair.shared, routes=(pair.shared.routes[0], route)), pair.node)
        )

    def test_global_ids_are_unique(self) -> None:
        pair = make_pair()
        duplicate = replace(pair.shared.exits[0], id="route-estonia")
        with self.assertRaisesRegex(ValidationError, "globally unique"):
            validate_pair(
                StatePair(replace(pair.shared, exits=(duplicate,)), pair.node)
            )

    def test_duplicate_exit_and_route_ids_are_rejected(self) -> None:
        pair = make_pair()
        duplicate_exit = replace(pair.shared.exits[0])
        duplicate_route = replace(pair.shared.routes[0])
        candidates = (
            replace(pair.shared, exits=(pair.shared.exits[0], duplicate_exit)),
            replace(pair.shared, routes=(pair.shared.routes[0], duplicate_route)),
        )
        for shared in candidates:
            with self.subTest(shared=shared), self.assertRaises(ValidationError):
                validate_pair(StatePair(shared, pair.node))

    def test_duplicate_binding_exit_ids_are_rejected(self) -> None:
        pair = make_pair()
        duplicate = replace(pair.node.bindings[0], listen_port=18082)
        with self.assertRaisesRegex(ValidationError, "exit IDs"):
            validate_pair(
                StatePair(
                    pair.shared,
                    replace(pair.node, bindings=(pair.node.bindings[0], duplicate)),
                )
            )

    def test_binding_must_reference_an_exit(self) -> None:
        pair = make_pair()
        binding = replace(pair.node.bindings[0], exit_id="missing-exit")
        with self.assertRaisesRegex(ValidationError, "unknown exit"):
            validate_pair(StatePair(pair.shared, replace(pair.node, bindings=(binding,))))


class RuntimeReadinessTests(unittest.TestCase):
    def test_runtime_ready_requires_enabled_gateway(self) -> None:
        with self.assertRaisesRegex(ValidationError, "enabled gateway"):
            validate_pair(make_pair(route_enabled=True), runtime_ready=True)

    def test_runtime_ready_requires_an_enabled_route(self) -> None:
        with self.assertRaisesRegex(ValidationError, "enabled route"):
            validate_pair(make_pair(gateway_enabled=True), runtime_ready=True)

    def test_enabled_route_requires_enabled_exit(self) -> None:
        with self.assertRaisesRegex(ValidationError, "no usable exit"):
            validate_pair(make_pair(route_enabled=True, exit_enabled=False))

    def test_enabled_route_requires_binding(self) -> None:
        pair = make_pair(route_enabled=True)
        with self.assertRaisesRegex(ValidationError, "no usable exit"):
            validate_pair(StatePair(pair.shared, replace(pair.node, bindings=())))

    def test_enabled_route_requires_enabled_binding(self) -> None:
        with self.assertRaisesRegex(ValidationError, "no usable exit"):
            validate_pair(make_pair(route_enabled=True, binding_enabled=False))

    def test_enabled_binding_requires_secret_reference(self) -> None:
        with self.assertRaisesRegex(ValidationError, "secret reference"):
            validate_pair(make_pair(secret_ref=""))

    def test_active_passive_order_is_preserved_and_one_backend_is_enough(self) -> None:
        pair = add_secondary(
            make_pair(gateway_enabled=True, route_enabled=True), route_enabled=True
        )
        first_disabled = replace(pair.shared.exits[1], enabled=False)
        shared = replace(pair.shared, exits=(pair.shared.exits[0], first_disabled))
        validate_pair(StatePair(shared, pair.node), runtime_ready=True)
        self.assertEqual(shared.routes[0].exit_ids, ("ee-primary", "de-backup"))

    def test_active_active_supports_multiple_usable_backends(self) -> None:
        pair = add_secondary(
            make_pair(
                gateway_enabled=True,
                route_enabled=True,
                strategy=Strategy.ACTIVE_ACTIVE,
            ),
            route_enabled=True,
        )
        validate_pair(pair, runtime_ready=True)
        self.assertEqual(pair.shared.routes[0].strategy, Strategy.ACTIVE_ACTIVE)


class PortBoundaryTests(unittest.TestCase):
    def test_public_port_boundaries(self) -> None:
        for port in (0, 65536, True):
            pair = make_pair()
            gateway = replace(pair.shared.gateway, listen_port=port)
            with self.subTest(port=port), self.assertRaises(ValidationError):
                validate_pair(StatePair(replace(pair.shared, gateway=gateway), pair.node))

    def test_binding_port_boundaries(self) -> None:
        for port in (1023, 65536, False):
            pair = make_pair()
            binding = replace(pair.node.bindings[0], listen_port=port)
            with self.subTest(port=port), self.assertRaises(ValidationError):
                validate_pair(StatePair(pair.shared, replace(pair.node, bindings=(binding,))))


if __name__ == "__main__":
    unittest.main()
