#!/usr/bin/env python3
"""Issue #6 monitoring installation and administrative integration tests."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from monitoring import admin_cli, gost_monitoring, query_cli
from monitoring.config import (
    ALLOWED_KEYS,
    ConfigError,
    DEFAULT_CONFIG,
    INTERVAL_BOUNDS,
    KEY_DB,
    KEY_ENV_DIR,
    KEY_MAINTENANCE,
    KEY_SAMPLE,
    KEY_SLOW,
    KEY_TCP,
    config_from_mapping,
    default_config_text,
    load_config,
    parse_config_text,
)
from monitoring.schema import EVENT_RETENTION_SECONDS, SCHEMA_VERSION, init_db


ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"


def config_text(**overrides: object) -> str:
    values = DEFAULT_CONFIG.as_mapping()
    values.update({key: str(value) for key, value in overrides.items()})
    return "".join(f"{key}={values[key]}\n" for key in ALLOWED_KEYS)


class ConfigParserTests(unittest.TestCase):
    def test_defaults_and_custom_values(self):
        self.assertEqual(DEFAULT_CONFIG, parse_config_text(default_config_text()))
        parsed = parse_config_text(
            config_text(
                **{
                    KEY_DB: "/srv/gost/metrics.sqlite3",
                    KEY_ENV_DIR: "/srv/gost/env",
                    KEY_SAMPLE: 10,
                    KEY_TCP: 20,
                    KEY_SLOW: 45,
                    KEY_MAINTENANCE: 600,
                }
            )
        )
        self.assertEqual("/srv/gost/metrics.sqlite3", parsed.db_path)
        self.assertEqual((10, 20, 45, 600), (
            parsed.sample_interval,
            parsed.tcp_interval,
            parsed.slow_interval,
            parsed.maintenance_interval,
        ))

    def test_unknown_duplicate_malformed_empty_and_unsafe_values(self):
        invalid = (
            default_config_text() + "UNKNOWN=value\n",
            default_config_text() + f"{KEY_DB}=/tmp/duplicate\n",
            default_config_text().replace(f"{KEY_DB}=", "MALFORMED", 1),
            default_config_text().replace(f"{KEY_DB}={DEFAULT_CONFIG.db_path}", f"{KEY_DB}="),
            default_config_text().replace(DEFAULT_CONFIG.db_path, "relative.sqlite3"),
            default_config_text().replace(DEFAULT_CONFIG.db_path, "/tmp/../escape.sqlite3"),
            default_config_text().replace(DEFAULT_CONFIG.db_path, "/tmp/$(id)"),
            default_config_text().replace(DEFAULT_CONFIG.db_path, "/tmp/`id`"),
            default_config_text().replace(DEFAULT_CONFIG.db_path, "/tmp/a b"),
            default_config_text().replace(DEFAULT_CONFIG.db_path, "/tmp/a\x00b"),
            default_config_text().replace(f"{KEY_SAMPLE}=5", f"{KEY_SAMPLE}=-5"),
            default_config_text().replace(f"{KEY_SAMPLE}=5", f"{KEY_SAMPLE}=five"),
        )
        for value in invalid:
            with self.subTest(value=value[-40:]), self.assertRaises(ConfigError):
                parse_config_text(value)

    def test_each_interval_boundary_and_cross_field_constraints(self):
        for key, (minimum, maximum) in INTERVAL_BOUNDS.items():
            for value in (minimum, maximum):
                values = DEFAULT_CONFIG.as_mapping()
                values[key] = str(value)
                if key == KEY_SAMPLE and value == maximum:
                    values[KEY_TCP] = str(maximum)
                    values[KEY_SLOW] = str(maximum)
                if key == KEY_SLOW and value == maximum:
                    values[KEY_MAINTENANCE] = str(maximum)
                config_from_mapping(values, require_all=True)
            for value in (minimum - 1, maximum + 1):
                values = DEFAULT_CONFIG.as_mapping()
                values[key] = str(value)
                with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                    config_from_mapping(values, require_all=True)
        for overrides in (
            {KEY_SAMPLE: "60", KEY_TCP: "30", KEY_SLOW: "60"},
            {KEY_SAMPLE: "60", KEY_TCP: "60", KEY_SLOW: "30"},
            {KEY_SLOW: "900", KEY_MAINTENANCE: "300"},
        ):
            values = DEFAULT_CONFIG.as_mapping()
            values.update(overrides)
            with self.assertRaises(ConfigError):
                config_from_mapping(values, require_all=True)

    def test_config_symlink_and_secret_canary_are_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / "target.env"
            target.write_text(default_config_text(), encoding="utf-8")
            link = root / "monitoring.env"
            link.symlink_to(target)
            with self.assertRaisesRegex(ConfigError, "symlink"):
                load_config(link)
        canary = "release-secret-canary"
        raw = default_config_text() + f"PASSWORD={canary}\n"
        with self.assertRaises(ConfigError) as caught:
            parse_config_text(raw)
        self.assertNotIn(canary, str(caught.exception))


class CollectorConfigurationTests(unittest.TestCase):
    def test_invalid_cadence_returns_two_without_creating_database(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp).resolve() / "must-not-exist.sqlite3"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = gost_monitoring.main(
                    ["--once", "--db", str(db), "--interval", "4"]
                )
            self.assertEqual(2, result)
            self.assertFalse(db.exists())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_valid_cli_overrides_reach_collector_config(self):
        observed = []

        def fake_collect(*_args, **kwargs):
            observed.append(kwargs["config"])
            return 1

        with mock.patch.object(gost_monitoring, "migrate_database"), mock.patch.object(
            gost_monitoring, "collect_once", side_effect=fake_collect
        ):
            result = gost_monitoring.main(
                [
                    "--once",
                    "--db", "/tmp/gost-monitor-test.sqlite3",
                    "--env-dir", "/tmp/gost-env",
                    "--interval", "10",
                    "--tcp-interval", "20",
                    "--slow-interval", "45",
                    "--maintenance-interval", "600",
                ]
            )
        self.assertEqual(0, result)
        self.assertEqual(1, len(observed))
        self.assertEqual((10, 20, 45, 600), (
            observed[0].sample_interval,
            observed[0].tcp_snapshot_interval,
            observed[0].slow_sample_interval,
            observed[0].maintenance_interval,
        ))

    def test_query_cli_uses_strict_config_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            db = root / "metrics.sqlite3"
            init_db(str(db)).close()
            config = root / "monitoring.env"
            config.write_text(config_text(**{KEY_DB: str(db)}), encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            result = query_cli.main(
                ["--config", str(config), "snapshot"], stdout=stdout, stderr=stderr
            )
            self.assertEqual(0, result)
            self.assertIn("OVERALL", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())


class AdminCliTests(unittest.TestCase):
    def test_validate_migrate_and_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            config = root / "monitoring.env"
            db = root / "metrics.sqlite3"
            config.write_text(config_text(**{KEY_DB: str(db)}), encoding="utf-8")
            before = config.read_bytes()
            self.assertEqual(0, admin_cli.main(["validate-config", "--config", str(config)]))
            self.assertEqual(before, config.read_bytes())
            self.assertEqual(0, admin_cli.main(["migrate", "--db", str(db)]))
            self.assertEqual(0o600, stat.S_IMODE(db.stat().st_mode))
            status = admin_cli.database_status(str(db))
            self.assertEqual(SCHEMA_VERSION, status["schema_version"])
            self.assertEqual(EVENT_RETENTION_SECONDS, status["event_retention_seconds"])

    def test_maintenance_retention_is_idempotent_and_checkpointed(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp).resolve() / "metrics.sqlite3"
            conn = init_db(str(db))
            now = 2_000_000_000
            conn.execute(
                "INSERT INTO events(ts,severity,code,message,details_json) VALUES(?,?,?,?,?)",
                (now - EVENT_RETENTION_SECONDS - 1, "info", "old", "old", "{}"),
            )
            conn.execute(
                "INSERT INTO events(ts,severity,code,message,details_json) VALUES(?,?,?,?,?)",
                (now - EVENT_RETENTION_SECONDS, "info", "keep", "keep", "{}"),
            )
            conn.close()
            first = admin_cli.maintenance(str(db), now)
            second = admin_cli.maintenance(str(db), now)
            self.assertEqual(3, len(first))
            self.assertEqual(3, len(second))
            reader = sqlite3.connect(db)
            self.assertEqual([("keep",)], reader.execute("SELECT code FROM events").fetchall())
            reader.close()

    def test_busy_maintenance_maps_to_unsafe_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp).resolve() / "metrics.sqlite3"
            init_db(str(db)).close()
            with mock.patch.object(
                admin_cli,
                "open_runtime_database",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                with self.assertRaises(admin_cli.AdminUnsafeError):
                    admin_cli.maintenance(str(db))

    def test_purge_requires_yes_and_replaces_with_private_empty_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            db = root / "metrics.sqlite3"
            config = root / "operator.env"
            config.write_text("keep-me", encoding="utf-8")
            conn = init_db(str(db))
            conn.execute(
                "INSERT INTO events(ts,severity,code,message,details_json) VALUES(1,'info','x','x','{}')"
            )
            conn.close()
            original_mode = stat.S_IMODE(db.stat().st_mode)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(4, admin_cli.main(["purge-history", "--db", str(db)]))
            self.assertEqual(1, sqlite3.connect(db).execute("SELECT COUNT(*) FROM events").fetchone()[0])
            admin_cli.purge_history(str(db))
            self.assertFalse(Path(str(db) + "-wal").exists())
            self.assertFalse(Path(str(db) + "-shm").exists())
            reader = sqlite3.connect(db)
            self.assertEqual(0, reader.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            self.assertEqual(4, reader.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])
            reader.close()
            self.assertEqual(original_mode, stat.S_IMODE(db.stat().st_mode))
            self.assertEqual("keep-me", config.read_text(encoding="utf-8"))

    def test_purge_rolls_back_after_each_injected_replacement_failure(self):
        for phase in ("after_create", "after_backup", "after_replace"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                db = Path(temp).resolve() / "metrics.sqlite3"
                conn = init_db(str(db))
                conn.execute(
                    "INSERT INTO events(ts,severity,code,message,details_json) VALUES(1,'info','keep','keep','{}')"
                )
                conn.close()
                with self.assertRaises(OSError):
                    admin_cli.purge_history(str(db), fail_phase=phase)
                reader = sqlite3.connect(db)
                self.assertEqual(1, reader.execute("SELECT COUNT(*) FROM events").fetchone()[0])
                reader.close()

    def test_database_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / "target.sqlite3"
            init_db(str(target)).close()
            link = root / "metrics.sqlite3"
            link.symlink_to(target)
            with self.assertRaises(admin_cli.AdminUnsafeError):
                admin_cli.database_status(str(link))

    def test_no_traceback_for_corrupt_database(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp).resolve() / "corrupt.sqlite3"
            db.write_bytes(b"not sqlite")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = admin_cli.main(["status", "--db", str(db)])
            self.assertEqual(3, result)
            self.assertNotIn("Traceback", stderr.getvalue())


class PackagingContractTests(unittest.TestCase):
    def test_systemd_unit_is_isolated_and_hardened(self):
        unit = (PACKAGING / "gost-monitor-collector.service").read_text(encoding="utf-8")
        for forbidden in (
            "Requires=", "PartOf=", "BindsTo=", "PrivateNetwork=",
            "ProtectProc=", "ProcSubset=", "InaccessiblePaths=/proc",
            "nginx.service", "gost-iran-", "gost-kharej-",
        ):
            self.assertNotIn(forbidden, unit)
        for required in (
            "After=local-fs.target", "Restart=on-failure", "StartLimitBurst=5",
            "Nice=10", "IOSchedulingClass=idle", "OOMScoreAdjust=500",
            "UMask=0077", "LimitNOFILE=65536", "StateDirectoryMode=0700",
            "ProtectSystem=strict", "ReadWritePaths=/var/lib/gost-manager",
        ):
            self.assertIn(required, unit)

    def test_launchers_preserve_arguments_and_set_fixed_pythonpath(self):
        modules = {
            "gost-monitor": "monitoring.query_cli",
            "gost-monitor-collector": "monitoring.gost_monitoring",
            "gost-monitor-admin": "monitoring.admin_cli",
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            log = root / "log"
            python = bin_dir / "python3"
            python.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'PYTHONPATH=%s\\n' \"${PYTHONPATH}\" > \"${LAUNCHER_LOG}\"\n"
                "printf '%s\\n' \"$@\" >> \"${LAUNCHER_LOG}\"\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            environment = dict(os.environ)
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["LAUNCHER_LOG"] = str(log)
            for launcher, module in modules.items():
                with self.subTest(launcher=launcher):
                    subprocess.run(
                        [str(PACKAGING / launcher), "argument with spaces", "--flag"],
                        check=True,
                        env=environment,
                    )
                    lines = log.read_text(encoding="utf-8").splitlines()
                    self.assertEqual("PYTHONPATH=/usr/local/lib/gost-manager", lines[0])
                    self.assertEqual(["-m", module], lines[1:3])
                    self.assertEqual(["argument with spaces", "--flag"], lines[-2:])
                    if launcher != "gost-monitor-admin":
                        self.assertIn("--config", lines)


if __name__ == "__main__":
    unittest.main()
