from __future__ import annotations

import unittest

from gateway.crud import GatewayCRUD
from gateway.errors import ConflictError, ValidationError
from gateway.models import Strategy
from gateway.store import GatewayStateStore
from test_gateway_support import TemporaryStore


class GatewayCRUDTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryStore()
        self.store = self.temporary.initialize()
        self.crud = GatewayCRUD(self.store)

    def tearDown(self) -> None:
        self.temporary.close()

    def add_exit(self, exit_id: str = "ee-primary") -> None:
        self.crud.add_exit(
            exit_id=exit_id,
            display_name="Estonia primary",
            host="192.0.2.10",
            socks_port=28420,
            target_port=18081,
        )

    def add_binding(self, exit_id: str = "ee-primary", port: int = 18081) -> None:
        self.crud.set_binding(
            exit_id=exit_id,
            listen_port=port,
            secret_ref=f"secret-{exit_id}",
            enabled=True,
        )

    def add_route(self, *, enabled: bool = False) -> None:
        self.crud.add_route(
            route_id="route-estonia",
            display_name="Estonia",
            host="Gateway.Example.Org",
            path="/ee1/api/v1",
            strategy="active-passive",
            exit_ids=["ee-primary"],
            enabled=enabled,
        )

    def test_init_canonicalizes_host_and_starts_disabled(self) -> None:
        pair = self.crud.show()
        self.assertEqual(pair.shared.gateway.server_names, ("gateway.example.org",))
        self.assertFalse(pair.shared.gateway.enabled)
        self.assertEqual(pair.shared.revision, 1)
        self.assertEqual(pair.node.revision, 1)

    def test_init_refuses_to_overwrite_either_document(self) -> None:
        with self.assertRaises(ConflictError):
            self.store.initialize(
                gateway_id="other-gateway",
                node_id="other-node",
                listen_address="0.0.0.0",
                listen_port=8080,
                server_names=["other.example.org"],
            )

    def test_gateway_set_and_noop_revision(self) -> None:
        changed = self.crud.set_gateway(enabled=True, listen_address="127.0.0.1")
        self.assertTrue(changed.changed)
        self.assertEqual(changed.pair.shared.revision, 2)
        noop = self.crud.set_gateway(enabled=True, listen_address="127.0.0.1")
        self.assertFalse(noop.changed)
        self.assertEqual(noop.pair.shared.revision, 2)

    def test_gateway_set_requires_a_field_option(self) -> None:
        with self.assertRaises(ValidationError):
            self.crud.set_gateway()

    def test_add_edit_list_and_delete_exit(self) -> None:
        self.add_exit()
        edited = self.crud.edit_exit(
            exit_id="ee-primary",
            display_name="Estonia updated",
            host="Exit.Example.Org",
            socks_port=28430,
            target_port=18091,
            enabled=False,
        )
        item = edited.pair.shared.exits[0]
        self.assertEqual(item.id, "ee-primary")
        self.assertEqual(item.host, "exit.example.org")
        self.assertEqual(item.display_name, "Estonia updated")
        deleted = self.crud.delete_exit(exit_id="ee-primary")
        self.assertEqual(deleted.pair.shared.exits, ())

    def test_exit_id_is_immutable_and_duplicates_conflict(self) -> None:
        self.add_exit()
        with self.assertRaises(ConflictError):
            self.add_exit()
        self.assertNotIn("new_id", GatewayCRUD.edit_exit.__annotations__)

    def test_exit_edit_requires_a_field_option(self) -> None:
        self.add_exit()
        with self.assertRaises(ValidationError):
            self.crud.edit_exit(exit_id="ee-primary")

    def test_shared_revision_increments_once_per_mutation(self) -> None:
        self.add_exit()
        self.assertEqual(self.crud.show().shared.revision, 2)
        self.crud.edit_exit(exit_id="ee-primary", display_name="Updated")
        self.assertEqual(self.crud.show().shared.revision, 3)
        self.assertEqual(self.crud.show().node.revision, 1)

    def test_shared_expected_revision_conflict_preserves_state(self) -> None:
        self.add_exit()
        before = self.temporary.paths.state_file.read_bytes()
        with self.assertRaisesRegex(ConflictError, "expected 1, current 2"):
            self.crud.edit_exit(
                exit_id="ee-primary",
                display_name="Not applied",
                expected_revision=1,
            )
        self.assertEqual(self.temporary.paths.state_file.read_bytes(), before)

    def test_binding_set_edits_existing_and_node_revision_only(self) -> None:
        self.add_exit()
        self.add_binding()
        result = self.crud.set_binding(
            exit_id="ee-primary",
            listen_port=18091,
            secret_ref="secret-ee-updated",
            enabled=False,
        )
        binding = result.pair.node.bindings[0]
        self.assertFalse(binding.enabled)
        self.assertEqual(binding.listen_port, 18091)
        self.assertEqual(result.pair.node.revision, 3)
        self.assertEqual(result.pair.shared.revision, 2)

    def test_binding_noop_does_not_increment_revision(self) -> None:
        self.add_exit()
        self.add_binding()
        result = self.crud.set_binding(
            exit_id="ee-primary",
            listen_port=18081,
            secret_ref="secret-ee-primary",
            enabled=True,
        )
        self.assertFalse(result.changed)
        self.assertEqual(result.pair.node.revision, 2)

    def test_node_expected_revision_conflict(self) -> None:
        self.add_exit()
        self.add_binding()
        with self.assertRaisesRegex(ConflictError, "expected 1, current 2"):
            self.crud.set_binding(
                exit_id="ee-primary",
                listen_port=18082,
                secret_ref="secret-ee-primary",
                enabled=True,
                expected_revision=1,
            )

    def test_binding_port_conflicts_are_refused(self) -> None:
        self.add_exit()
        self.crud.set_gateway(listen_port=18081)
        for port in (18081, 18000):
            with self.subTest(port=port), self.assertRaises(ConflictError):
                self.crud.set_binding(
                    exit_id="ee-primary",
                    listen_port=port,
                    secret_ref="secret-ee-primary",
                    enabled=True,
                )

    def test_duplicate_binding_port_is_refused(self) -> None:
        self.add_exit()
        self.add_exit("de-backup")
        self.add_binding()
        with self.assertRaises(ConflictError):
            self.add_binding("de-backup", 18081)

    def test_binding_remove(self) -> None:
        self.add_exit()
        self.add_binding()
        result = self.crud.remove_binding(exit_id="ee-primary")
        self.assertEqual(result.pair.node.bindings, ())

    def test_route_add_edit_order_and_delete(self) -> None:
        self.add_exit()
        self.add_exit("de-backup")
        self.add_route()
        result = self.crud.edit_route(
            route_id="route-estonia",
            display_name="Estonia route",
            strategy=Strategy.ACTIVE_ACTIVE,
            exit_ids=["de-backup", "ee-primary"],
        )
        route = result.pair.shared.routes[0]
        self.assertEqual(route.strategy, Strategy.ACTIVE_ACTIVE)
        self.assertEqual(route.exit_ids, ("de-backup", "ee-primary"))
        deleted = self.crud.delete_route(route_id="route-estonia")
        self.assertEqual(deleted.pair.shared.routes, ())

    def test_route_edit_requires_a_field_option(self) -> None:
        self.add_exit()
        self.add_route()
        with self.assertRaises(ValidationError):
            self.crud.edit_route(route_id="route-estonia")

    def test_enabled_route_requires_usable_backend(self) -> None:
        self.add_exit()
        with self.assertRaises(ValidationError):
            self.add_route(enabled=True)
        self.assertEqual(self.crud.show().shared.routes, ())

    def test_complete_route_can_be_enabled_and_runtime_ready(self) -> None:
        self.add_exit()
        self.add_binding()
        self.add_route()
        self.crud.set_gateway(enabled=True)
        self.crud.edit_route(route_id="route-estonia", enabled=True)
        pair = self.crud.show(runtime_ready=True)
        self.assertTrue(pair.shared.routes[0].enabled)

    def test_enabled_route_host_path_collision_is_refused(self) -> None:
        self.add_exit()
        self.add_binding()
        self.add_route(enabled=True)
        with self.assertRaises(ConflictError) as caught:
            self.crud.add_route(
                route_id="route-copy",
                display_name="Copy",
                host="gateway.example.org",
                path="/ee1/api/v1",
                strategy="active-passive",
                exit_ids=["ee-primary"],
                enabled=True,
            )
        self.assertIn("route-estonia", str(caught.exception))
        self.assertIn("route-copy", str(caught.exception))

    def test_disabled_collision_can_be_prepared_but_not_enabled(self) -> None:
        self.add_exit()
        self.add_binding()
        self.add_route(enabled=True)
        self.crud.add_route(
            route_id="route-copy",
            display_name="Copy",
            host="GATEWAY.EXAMPLE.ORG",
            path="/ee1/api/v1",
            strategy="active-passive",
            exit_ids=["ee-primary"],
            enabled=False,
        )
        with self.assertRaises(ConflictError):
            self.crud.edit_route(route_id="route-copy", enabled=True)

    def test_trailing_slash_route_does_not_conflict(self) -> None:
        self.add_exit()
        self.add_binding()
        self.add_route(enabled=True)
        self.crud.add_route(
            route_id="route-slash",
            display_name="Slash",
            host="gateway.example.org",
            path="/ee1/api/v1/",
            strategy="active-passive",
            exit_ids=["ee-primary"],
            enabled=True,
        )
        self.assertEqual(len(self.crud.show().shared.routes), 2)

    def test_referenced_exit_cannot_be_deleted(self) -> None:
        self.add_exit()
        self.add_route()
        with self.assertRaisesRegex(ConflictError, "route-estonia"):
            self.crud.delete_exit(exit_id="ee-primary")

    def test_final_usable_exit_cannot_be_disabled(self) -> None:
        self.add_exit()
        self.add_binding()
        self.add_route(enabled=True)
        with self.assertRaisesRegex(ConflictError, "usable backend"):
            self.crud.edit_exit(exit_id="ee-primary", enabled=False)

    def test_final_usable_binding_cannot_be_disabled_or_removed(self) -> None:
        self.add_exit()
        self.add_binding()
        self.add_route(enabled=True)
        with self.assertRaises(ConflictError):
            self.crud.set_binding(
                exit_id="ee-primary",
                listen_port=18081,
                secret_ref="",
                enabled=False,
            )
        with self.assertRaises(ConflictError):
            self.crud.remove_binding(exit_id="ee-primary")

    def test_backup_allows_primary_to_be_disabled(self) -> None:
        self.add_exit()
        self.add_binding()
        self.crud.add_exit(
            exit_id="de-backup",
            display_name="Germany backup",
            host="198.51.100.20",
            socks_port=28421,
            target_port=18082,
        )
        self.add_binding("de-backup", 18082)
        self.crud.add_route(
            route_id="route-estonia",
            display_name="Estonia",
            host="gateway.example.org",
            path="/ee1/api/v1",
            strategy="active-passive",
            exit_ids=["ee-primary", "de-backup"],
            enabled=True,
        )
        result = self.crud.edit_exit(exit_id="ee-primary", enabled=False)
        self.assertFalse(next(item for item in result.pair.shared.exits if item.id == "ee-primary").enabled)

    def test_gateway_server_name_dependency_is_protected(self) -> None:
        self.add_exit()
        self.add_route()
        with self.assertRaises(ConflictError):
            self.crud.set_gateway(server_names=["other.example.org"])

    def test_gateway_listener_change_cannot_conflict_with_binding(self) -> None:
        self.add_exit()
        self.add_binding()
        with self.assertRaises(ConflictError):
            self.crud.set_gateway(status_port=18081)

    def test_missing_entities_are_mutation_conflicts(self) -> None:
        operations = (
            lambda: self.crud.edit_exit(exit_id="missing", enabled=False),
            lambda: self.crud.delete_route(route_id="missing"),
            lambda: self.crud.remove_binding(exit_id="missing"),
        )
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(ConflictError):
                operation()


if __name__ == "__main__":
    unittest.main()
