#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path

from monitoring.gost_monitoring import (
    MetricSample, Tunnel, apply_retention, collect_sample, discover_tunnels,
    init_db, insert_sample, parse_mappings, tunnel_from_env, upsert_tunnel,
)

class MonitoringTests(unittest.TestCase):
    def test_parses_legacy_env_without_mutating_it(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / "iran-1.env"
            env.write_text("PORT_MAPPINGS=80:8080,2052:2052\nGOST_USER=a\n", encoding="utf-8")
            tunnel = tunnel_from_env(env)
            self.assertEqual(tunnel.tunnel_id, "iran-1")
            self.assertEqual(tunnel.listen_ports, (80, 2052))
            self.assertEqual(tunnel.target_ports, (8080, 2052))
            self.assertIn("PORT_MAPPINGS=80:8080", env.read_text(encoding="utf-8"))

    def test_discovers_only_numbered_gost_env_files(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "iran-1.env").write_text("PORT_MAPPINGS=443:443\n", encoding="utf-8")
            Path(td, "notes.env").write_text("PORT=1\n", encoding="utf-8")
            self.assertEqual([t.tunnel_id for t in discover_tunnels(td)], ["iran-1"])

    def test_sqlite_schema_samples_retention_and_rollups(self):
        with tempfile.TemporaryDirectory() as td:
            conn = init_db(str(Path(td) / "m.sqlite3"))
            env = Path(td) / "kharej-2.env"
            env.write_text("SOCKS_PORT=28420\n", encoding="utf-8")
            tunnel = tunnel_from_env(env)
            upsert_tunnel(conn, tunnel, 1_000_000)
            insert_sample(conn, MetricSample("kharej-2", 1_000_000 - 8 * 24 * 3600, 1, 1, 2, 1, 1, 0))
            insert_sample(conn, MetricSample("kharej-2", 1_000_000, 1, 1, 3, 1, 0, 0))
            apply_retention(conn, 1_000_000)
            conn.commit()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0], 1)
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM metric_rollups").fetchone()[0], 0)

    def test_collect_sample_is_deterministic_with_runner(self):
        tunnel = Tunnel("iran", 3, "gost-iran-3.service", "/tmp/iran-3.env", (80, 2052), (80, 2052))
        def runner(cmd):
            if cmd[0] == "systemctl":
                return "ActiveState=active\nSubState=running\nNRestarts=4\n"
            return "LISTEN 0 4096 0.0.0.0:80 0.0.0.0:*\n"
        sample = collect_sample(tunnel, now=123, runner=runner)
        self.assertEqual(sample.restart_count, 4)
        self.assertEqual(sample.listen_ports_up, 1)
        self.assertEqual(sample.configured_mappings_total, 2)

    def test_parse_mappings_ignores_invalid_fragments(self):
        self.assertEqual(parse_mappings("bad,80:443,0:1,65536:1"), ((80, 443),))

if __name__ == "__main__":
    unittest.main()
