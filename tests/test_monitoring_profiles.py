"""Direct Mode multi-profile discovery and metadata regressions."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from monitoring.entities import (
    canonical_allowed_sources,
    discover_tunnels,
    parse_env_text,
    tunnel_from_env,
)
from monitoring.models import Tunnel
from monitoring.schema import SCHEMA_VERSION, init_db, upsert_tunnel


IRAN_ENV = """\
GOST_USER=iran-user
GOST_PASS=credential-canary-iran
KHAREJ_IP=203.0.113.20
TUNNEL_PORT=28420
MAPPINGS=2052:80,2053:443
"""

KHAREJ_ENV = """\
GOST_USER=kharej-user
GOST_PASS=credential-canary-kharej
TUNNEL_PORT=28421
ALLOWED_IRAN_SOURCES=198.51.100.11,198.51.100.0/24,198.51.100.11/32
FIREWALL_ENABLED=1
PROFILE_LABEL=kharej-edge
"""


class MonitoringProfileTests(unittest.TestCase):
    def test_strict_env_parser_rejects_duplicates_and_control_characters(self):
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            parse_env_text("GOST_USER=a\nGOST_USER=b\n")
        with self.assertRaisesRegex(ValueError, "control"):
            parse_env_text("GOST_USER=a\x00b\n")
        with self.assertRaisesRegex(ValueError, "invalid key"):
            parse_env_text(" GOST_USER=a\n")

    def test_profile_label_is_optional_safe_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unlabeled = root / "iran-1.env"
            labeled = root / "iran-2.env"
            unlabeled.write_text(IRAN_ENV, encoding="utf-8")
            labeled.write_text(IRAN_ENV + "PROFILE_LABEL=iran-edge_2\n", encoding="utf-8")
            self.assertIsNone(tunnel_from_env(unlabeled).profile_label)
            self.assertEqual("iran-edge_2", tunnel_from_env(labeled).profile_label)
            labeled.write_text(IRAN_ENV + "PROFILE_LABEL=$(id)\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "profile label"):
                tunnel_from_env(labeled)

    def test_modern_sources_are_canonical_deduplicated_and_sorted(self):
        values = parse_env_text(KHAREJ_ENV)
        self.assertEqual(
            ("198.51.100.0/24", "198.51.100.11/32"),
            canonical_allowed_sources(values),
        )

    def test_legacy_iran_ip_remains_supported_as_one_source(self):
        values = parse_env_text(
            KHAREJ_ENV.replace(
                "ALLOWED_IRAN_SOURCES=198.51.100.11,198.51.100.0/24,198.51.100.11/32",
                "IRAN_IP=198.51.100.10",
            )
        )
        self.assertEqual(("198.51.100.10/32",), canonical_allowed_sources(values))

    def test_unsafe_or_ambiguous_sources_are_rejected(self):
        for source in (
            "0.0.0.0/0",
            "192.0.0.0/7",
            "2001:db8::1",
            "example.invalid",
            "198.51.100.1, 198.51.100.2",
            "198.51.100.1,,198.51.100.2",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                canonical_allowed_sources({"ALLOWED_IRAN_SOURCES": source})

    def test_multiple_profiles_discover_in_stable_numeric_identity_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "iran-10.env").write_text(IRAN_ENV, encoding="utf-8")
            (root / "iran-2.env").write_text(IRAN_ENV, encoding="utf-8")
            (root / "kharej-1.env").write_text(KHAREJ_ENV, encoding="utf-8")
            tunnels, events = discover_tunnels(root)
            self.assertEqual([], events)
            self.assertEqual(
                ["iran-2", "iran-10", "kharej-1"],
                [tunnel.tunnel_id for tunnel in tunnels],
            )

    def test_malformed_profile_event_never_contains_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "iran-1.env").write_text(
                IRAN_ENV + "PROFILE_LABEL=bad label\n", encoding="utf-8"
            )
            tunnels, events = discover_tunnels(root)
            self.assertEqual([], tunnels)
            serialized = repr(events)
            self.assertIn("env_parse_error", serialized)
            self.assertNotIn("credential-canary-iran", serialized)

    def test_entity_metadata_has_safe_profile_fields_without_schema_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "monitoring.sqlite3"
            connection = init_db(str(database))
            tunnel = Tunnel(
                "kharej",
                3,
                "gost-kharej-3.service",
                "/etc/gost/kharej-3.env",
                (28423,),
                (),
                None,
                "kharej-edge",
                ("198.51.100.10/32", "198.51.100.11/32"),
            )
            upsert_tunnel(connection, tunnel, 100)
            row = connection.execute(
                "SELECT display_name,metadata_json FROM entities "
                "WHERE entity_type='tunnel' AND entity_id='kharej-3'"
            ).fetchone()
            metadata = json.loads(row[1])
            self.assertEqual("kharej-edge", row[0])
            self.assertEqual("kharej", metadata["side"])
            self.assertEqual(3, metadata["profile_number"])
            self.assertEqual(2, metadata["allowed_source_count"])
            self.assertEqual(
                ["198.51.100.10/32", "198.51.100.11/32"],
                metadata["allowed_sources"],
            )
            self.assertNotIn("credential", json.dumps(metadata).lower())
            self.assertEqual(4, SCHEMA_VERSION)
            connection.close()

    def test_label_update_keeps_one_stable_tunnel_and_existing_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "monitoring.sqlite3"
            connection = init_db(str(database))
            original = Tunnel(
                "iran", 1, "gost-iran-1.service", "/etc/gost/iran-1.env", (2052,), (80,)
            )
            labeled = Tunnel(
                "iran",
                1,
                "gost-iran-1.service",
                "/etc/gost/iran-1.env",
                (2052,),
                (80,),
                "203.0.113.20:28420",
                "iran-edge",
            )
            upsert_tunnel(connection, original, 100)
            entity_pk = connection.execute(
                "SELECT entity_pk FROM entities WHERE entity_id='iran-1'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO sample_cycles(collected_at,monotonic_started,monotonic_finished,duration_seconds,success,overrun) "
                "VALUES(100,1,2,1,1,0)"
            )
            cycle_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            connection.execute(
                "INSERT INTO metric_points(cycle_id,entity_pk,metric_name,ts,numeric_value,unit,quality) "
                "VALUES(?,?,?,?,?,?,?)",
                (cycle_id, entity_pk, "service_active", 100, 1, "boolean", "exact"),
            )
            upsert_tunnel(connection, labeled, 101)
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM entities WHERE entity_id='iran-1'"
                ).fetchone()[0],
            )
            self.assertEqual(
                1, connection.execute("SELECT COUNT(*) FROM metric_points").fetchone()[0]
            )
            connection.close()

    def test_representative_100_profile_monitoring_discovery_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for number in range(1, 51):
                (root / f"iran-{number}.env").write_text(
                    IRAN_ENV.replace("2052:80,2053:443", f"{10000 + number}:80,{11000 + number}:443"),
                    encoding="utf-8",
                )
                (root / f"kharej-{number}.env").write_text(
                    KHAREJ_ENV.replace("TUNNEL_PORT=28421", f"TUNNEL_PORT={20000 + number}"),
                    encoding="utf-8",
                )
            started = time.monotonic()
            tunnels, events = discover_tunnels(root)
            duration = time.monotonic() - started
            self.assertEqual(100, len(tunnels))
            self.assertEqual([], events)
            self.assertLess(duration, 3.0)


if __name__ == "__main__":
    unittest.main()
