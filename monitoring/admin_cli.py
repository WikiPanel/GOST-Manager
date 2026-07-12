"""Administrative monitoring operations with stable, non-traceback failures."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from monitoring.config import ConfigError, DEFAULT_CONFIG_PATH, load_config
from monitoring.schema import (
    EVENT_RETENTION_SECONDS,
    RAW_RETENTION_SECONDS,
    ROLLUP_RETENTION_SECONDS,
    SCHEMA_VERSION,
    checkpoint_wal,
    migrate_database,
    open_runtime_database,
    run_maintenance,
)


EXIT_INVALID = 2
EXIT_DATABASE = 3
EXIT_UNSAFE = 4


class AdminError(RuntimeError):
    exit_code = 1


class AdminInputError(AdminError):
    exit_code = EXIT_INVALID


class AdminDatabaseError(AdminError):
    exit_code = EXIT_DATABASE


class AdminUnsafeError(AdminError):
    exit_code = EXIT_UNSAFE


def _safe_database_path(raw: str, *, must_exist: bool) -> Path:
    path = Path(raw)
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
        raise AdminInputError("database path must be normalized and absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise AdminUnsafeError("database path may not traverse a symlink")
    if must_exist and not path.is_file():
        raise AdminDatabaseError("monitoring database does not exist")
    return path


def _schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.DatabaseError as exc:
        raise AdminDatabaseError("monitoring database is corrupt or unsupported") from exc
    return int(row[0] or 0)


def database_status(db_path: str) -> dict[str, object]:
    path = _safe_database_path(db_path, must_exist=True)
    uri = f"file:{path}?mode=ro"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.execute("PRAGMA query_only=ON")
        version = _schema_version(conn)
        if version != SCHEMA_VERSION:
            raise AdminDatabaseError(f"unsupported monitoring schema version {version}")
        row = conn.execute(
            "SELECT collected_at FROM sample_cycles WHERE success=1 "
            "ORDER BY collected_at DESC LIMIT 1"
        ).fetchone()
    except AdminError:
        raise
    except sqlite3.DatabaseError as exc:
        raise AdminDatabaseError("monitoring database is corrupt or unavailable") from exc
    finally:
        if conn is not None:
            conn.close()
    wal_path = Path(str(path) + "-wal")
    return {
        "schema_version": version,
        "database_path": str(path),
        "database_size_bytes": path.stat().st_size,
        "wal_size_bytes": wal_path.stat().st_size if wal_path.is_file() else 0,
        "latest_successful_cycle": int(row[0]) if row else None,
        "raw_retention_seconds": RAW_RETENTION_SECONDS,
        "rollup_retention_seconds": ROLLUP_RETENTION_SECONDS,
        "event_retention_seconds": EVENT_RETENTION_SECONDS,
    }


def migrate(db_path: str) -> int:
    path = _safe_database_path(db_path, must_exist=False)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        os.chmod(path, 0o600)
    try:
        conn = migrate_database(str(path))
        version = _schema_version(conn)
        conn.close()
    except (sqlite3.DatabaseError, RuntimeError, OSError) as exc:
        raise AdminDatabaseError(f"database migration failed: {exc}") from exc
    if version != SCHEMA_VERSION:
        raise AdminDatabaseError("database migration postcondition failed")
    os.chmod(path, 0o600)
    return version


def maintenance(db_path: str, now: int | None = None) -> tuple[int, int, int]:
    path = _safe_database_path(db_path, must_exist=True)
    conn: sqlite3.Connection | None = None
    try:
        conn = open_runtime_database(str(path))
        conn.execute("BEGIN IMMEDIATE")
        run_maintenance(conn, int(time.time()) if now is None else int(now))
        conn.commit()
        conn.close()
        conn = None
        return checkpoint_wal(str(path))
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise AdminUnsafeError("monitoring database is busy") from exc
        raise AdminDatabaseError("monitoring maintenance failed") from exc
    except (sqlite3.DatabaseError, RuntimeError, OSError) as exc:
        raise AdminDatabaseError(f"monitoring maintenance failed: {exc}") from exc
    finally:
        if conn is not None:
            try:
                conn.rollback()
            finally:
                conn.close()


def _fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def purge_history(
    db_path: str,
    *,
    replace: Callable[[str, str], None] = os.replace,
    fail_phase: str | None = None,
) -> None:
    path = _safe_database_path(db_path, must_exist=True)
    stat = path.stat()
    parent = path.parent
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=f".{path.name}.purge.", dir=str(parent)
    )
    os.close(descriptor)
    temporary = Path(temporary_raw)
    temporary.unlink()
    backup = parent / f".{path.name}.backup.{os.getpid()}"
    replaced_original = False
    installed_new = False
    try:
        conn = migrate_database(str(temporary))
        if _schema_version(conn) != SCHEMA_VERSION:
            raise AdminDatabaseError("replacement database schema validation failed")
        conn.close()
        os.chmod(temporary, stat.st_mode & 0o777)
        try:
            os.chown(temporary, stat.st_uid, stat.st_gid)
        except PermissionError:
            if os.geteuid() == 0:
                raise
        _fsync_path(temporary)
        if fail_phase == "after_create":
            raise OSError("injected purge failure after create")
        replace(str(path), str(backup))
        replaced_original = True
        if fail_phase == "after_backup":
            raise OSError("injected purge failure after backup")
        replace(str(temporary), str(path))
        installed_new = True
        if fail_phase == "after_replace":
            raise OSError("injected purge failure after replace")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() and not sidecar.is_symlink():
                sidecar.unlink()
        _fsync_path(parent)
        backup.unlink()
        replaced_original = False
    except Exception:
        if replaced_original:
            if installed_new and path.exists() and not path.is_symlink():
                path.unlink()
            if backup.exists():
                replace(str(backup), str(path))
        raise
    finally:
        for candidate in (temporary, Path(str(temporary) + "-wal"), Path(str(temporary) + "-shm")):
            if candidate.exists() and not candidate.is_symlink():
                candidate.unlink()
        if backup.exists() and not replaced_original:
            backup.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gost-monitor-admin")
    parser.add_argument("--debug", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-config")
    validate.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    for name in ("migrate", "status", "maintenance", "purge-history"):
        command = subparsers.add_parser(name)
        command.add_argument("--db")
        command.add_argument("--config", default=DEFAULT_CONFIG_PATH)
        if name == "purge-history":
            command.add_argument("--yes", action="store_true")
    return parser


def run_command(
    args: argparse.Namespace,
    *,
    stdout: TextIO = sys.stdout,
) -> int:
    if args.command == "validate-config":
        config = load_config(args.config)
        print(
            f"monitoring config valid: db={config.db_path} env_dir={config.env_dir}",
            file=stdout,
        )
        return 0
    db_path = args.db if args.db else load_config(args.config).db_path
    if args.command == "migrate":
        print(f"monitoring schema version: {migrate(db_path)}", file=stdout)
        return 0
    if args.command == "status":
        print(json.dumps(database_status(db_path), sort_keys=True), file=stdout)
        return 0
    if args.command == "maintenance":
        checkpoint = maintenance(db_path)
        print(
            json.dumps(
                {
                    "checkpoint": checkpoint,
                    "raw_retention_seconds": RAW_RETENTION_SECONDS,
                    "rollup_retention_seconds": ROLLUP_RETENTION_SECONDS,
                    "event_retention_seconds": EVENT_RETENTION_SECONDS,
                },
                sort_keys=True,
            ),
            file=stdout,
        )
        return 0
    if args.command == "purge-history":
        if not args.yes:
            raise AdminUnsafeError("purge-history requires explicit --yes")
        purge_history(db_path, fail_phase=os.environ.get("GOST_MONITOR_PURGE_FAIL_PHASE"))
        print("monitoring history purged; schema v4 is ready", file=stdout)
        return 0
    raise AdminInputError("unknown administrative command")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run_command(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except AdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:
        if args.debug:
            raise
        print(f"error: {exc.__class__.__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
