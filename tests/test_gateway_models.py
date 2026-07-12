from __future__ import annotations

import dataclasses
import json
import unittest
from dataclasses import replace

from gateway.errors import ValidationError
from gateway.models import (
    MAX_NODE_BYTES,
    MAX_SHARED_BYTES,
    Binding,
    ExitNode,
    Route,
    Strategy,
)
from gateway.serialization import (
    parse_node,
    parse_shared,
    serialize_node,
    serialize_shared,
)
from gateway.validation import validate_pair, validate_shared
from test_gateway_support import DOCUMENT_ID, add_secondary, make_pair


class GatewayModelTests(unittest.TestCase):
    def test_valid_pair_round_trip_is_deterministic(self) -> None:
        pair = add_secondary(make_pair())
        shared = serialize_shared(pair.shared)
        node = serialize_node(pair.node)
        self.assertEqual(serialize_shared(parse_shared(shared)), shared)
        self.assertEqual(serialize_node(parse_node(node)), node)
        self.assertTrue(shared.endswith(b"\n"))
        self.assertTrue(node.endswith(b"\n"))

    def test_serialization_sorts_entities_but_preserves_route_exit_order(self) -> None:
        pair = add_secondary(make_pair())
        shared = parse_shared(serialize_shared(pair.shared))
        node = parse_node(serialize_node(pair.node))
        self.assertEqual([item.id for item in shared.exits], ["de-backup", "ee-primary"])
        self.assertEqual(shared.routes[0].exit_ids, ("ee-primary", "de-backup"))
        self.assertEqual(
            [item.exit_id for item in node.bindings], ["de-backup", "ee-primary"]
        )

    def test_parsed_models_are_frozen(self) -> None:
        shared = parse_shared(serialize_shared(make_pair().shared))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            shared.revision = 2  # type: ignore[misc]

    def test_duplicate_top_level_json_key_is_rejected(self) -> None:
        payload = serialize_shared(make_pair().shared).decode()
        payload = payload.replace('"schema_version": 1,', '"schema_version": 1,\n  "schema_version": 1,', 1)
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            parse_shared(payload.encode())

    def test_duplicate_nested_json_key_is_rejected(self) -> None:
        payload = serialize_shared(make_pair().shared).decode()
        payload = payload.replace('"enabled": false,', '"enabled": false,\n    "enabled": false,', 1)
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            parse_shared(payload.encode())

    def test_unknown_shared_key_is_rejected_without_value_echo(self) -> None:
        value = json.loads(serialize_shared(make_pair().shared))
        value["password"] = "SECRET-CANARY-DO-NOT-PRINT"
        with self.assertRaises(ValidationError) as caught:
            parse_shared(json.dumps(value).encode())
        self.assertNotIn("SECRET-CANARY", str(caught.exception))

    def test_unknown_nested_key_is_rejected(self) -> None:
        value = json.loads(serialize_shared(make_pair().shared))
        value["gateway"]["worker_count"] = 4
        with self.assertRaises(ValidationError):
            parse_shared(json.dumps(value).encode())

    def test_unknown_binding_key_is_rejected(self) -> None:
        value = json.loads(serialize_node(make_pair().node))
        value["bindings"][0]["token"] = "SECRET-CANARY"
        with self.assertRaises(ValidationError) as caught:
            parse_node(json.dumps(value).encode())
        self.assertNotIn("SECRET-CANARY", str(caught.exception))

    def test_unsupported_shared_schema_is_rejected(self) -> None:
        value = json.loads(serialize_shared(make_pair().shared))
        value["schema_version"] = 2
        with self.assertRaises(ValidationError):
            parse_shared(json.dumps(value).encode())

    def test_unsupported_node_schema_is_rejected(self) -> None:
        value = json.loads(serialize_node(make_pair().node))
        value["schema_version"] = 2
        with self.assertRaises(ValidationError):
            parse_node(json.dumps(value).encode())

    def test_noncanonical_uuid_is_rejected(self) -> None:
        value = json.loads(serialize_shared(make_pair().shared))
        value["document_id"] = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
        with self.assertRaises(ValidationError):
            parse_shared(json.dumps(value).encode())

    def test_zero_and_boolean_revisions_are_rejected(self) -> None:
        for revision in (0, True):
            with self.subTest(revision=revision):
                value = json.loads(serialize_shared(make_pair().shared))
                value["revision"] = revision
                with self.assertRaises(ValidationError):
                    parse_shared(json.dumps(value).encode())

    def test_boolean_port_is_rejected(self) -> None:
        value = json.loads(serialize_shared(make_pair().shared))
        value["gateway"]["listen_port"] = True
        with self.assertRaises(ValidationError):
            parse_shared(json.dumps(value).encode())

    def test_non_utf8_and_invalid_json_are_rejected(self) -> None:
        for value in (b"\xff", b"{"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    parse_shared(value)

    def test_serialized_size_limits_are_enforced_before_decode(self) -> None:
        with self.assertRaisesRegex(ValidationError, "size limit"):
            parse_shared(b" " * (MAX_SHARED_BYTES + 1))
        with self.assertRaisesRegex(ValidationError, "size limit"):
            parse_node(b" " * (MAX_NODE_BYTES + 1))

    def test_exit_entity_limit_is_enforced(self) -> None:
        pair = make_pair()
        exits = tuple(
            ExitNode(
                id=f"exit-{index:03d}",
                display_name=f"Exit {index}",
                enabled=False,
                host=f"exit-{index:03d}.example.org",
                socks_port=20000 + index,
                target_port=30000 + index,
            )
            for index in range(257)
        )
        with self.assertRaisesRegex(ValidationError, "entity limit"):
            validate_shared(replace(pair.shared, exits=exits, routes=()))

    def test_route_entity_limit_is_enforced(self) -> None:
        pair = make_pair()
        routes = tuple(
            Route(
                id=f"route-{index:03d}",
                display_name=f"Route {index}",
                enabled=False,
                host="gateway.example.org",
                path=f"/route/{index:03d}",
                strategy=Strategy.ACTIVE_PASSIVE,
                exit_ids=("ee-primary",),
            )
            for index in range(257)
        )
        with self.assertRaisesRegex(ValidationError, "entity limit"):
            validate_shared(replace(pair.shared, routes=routes))

    def test_binding_entity_limit_is_enforced(self) -> None:
        pair = make_pair()
        bindings = tuple(
            Binding(
                exit_id=f"exit-{index:03d}",
                enabled=False,
                listen_address="127.0.0.1",
                listen_port=20000 + index,
                secret_ref="",
            )
            for index in range(257)
        )
        with self.assertRaisesRegex(ValidationError, "binding limit"):
            serialize_node(replace(pair.node, bindings=bindings))

    def test_document_ids_must_match(self) -> None:
        pair = make_pair()
        node = replace(pair.node, document_id="00000000-0000-4000-8000-000000000002")
        with self.assertRaisesRegex(ValidationError, "do not match"):
            validate_pair(type(pair)(pair.shared, node))

    def test_noncanonical_model_host_is_not_serialized(self) -> None:
        pair = make_pair()
        exit_node = replace(pair.shared.exits[0], host="Exit.Example.Org")
        with self.assertRaises(ValidationError):
            serialize_shared(replace(pair.shared, exits=(exit_node,)))

    def test_route_strategy_must_be_enum_in_model(self) -> None:
        pair = make_pair()
        route = replace(pair.shared.routes[0], strategy="active-passive")
        with self.assertRaises(ValidationError):
            validate_shared(replace(pair.shared, routes=(route,)))

    def test_strategy_enum_values_are_stable(self) -> None:
        self.assertEqual(Strategy.ACTIVE_PASSIVE.value, "active-passive")
        self.assertEqual(Strategy.ACTIVE_ACTIVE.value, "active-active")


if __name__ == "__main__":
    unittest.main()
