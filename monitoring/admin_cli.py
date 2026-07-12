"""Administrative monitoring operations with stable, non-traceback failures."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import TextIO

from monitoring.config import (
    CONFIG_POLICIES,
    INSTALLED_POLICY,
    ConfigError,
    DEFAULT_CONFIG,
    DEFAULT_CONFIG_PATH,
    MonitoringConfig,
    apply_config_policy,
    load_config,
    rooted_path,
)
from monitoring.runtime_lock import DEFAULT_LOCK_PATH, RuntimeLock, RuntimeLockError
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
CONFIG_FIELDS = (
    "database_path",
    "env_directory",
    "sample_interval_seconds",
    "tcp_interval_seconds",
    "slow_interval_seconds",
    "maintenance_interval_seconds",
)
PURGE_FAILURE_PHASES = (
    "after_lock",
    "after_checkpoint",
    "after_replacement_create",
    "after_replacement_validate",
    "after_replacement_fsync",
    "after_backup_link",
    "after_atomic_replace",
    "after_replacement_validation",
    "after_wal_cleanup",
    "after_shm_cleanup",
    "after_first_directory_fsync",
    "after_backup_deletion",
    "after_final_directory_fsync",
)


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
    if must_exist and (not path.is_file() or path.is_symlink()):
        raise AdminDatabaseError("monitoring database does not exist")
    if path.exists() and not path.is_file():
        raise AdminUnsafeError("monitoring database path is not a regular file")
    return path


def _schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.DatabaseError as exc:
        raise AdminDatabaseError("monitoring database is corrupt or unsupported") from exc
    return int(row[0] or 0)


def _require_schema(conn: sqlite3.Connection) -> None:
    version = _schema_version(conn)
    if version != SCHEMA_VERSION:
        raise AdminDatabaseError(f"unsupported monitoring schema version {version}")


def database_status(db_path: str) -> dict[str, object]:
    path = _safe_database_path(db_path, must_exist=True)
    uri = f"file:{path}?mode=ro"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.execute("PRAGMA query_only=ON")
        _require_schema(conn)
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
        "schema_version": SCHEMA_VERSION,
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
    path = _safe_database_path(str(path), must_exist=False)
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


def _inject_failure(fail_phase: str | None, phase: str) -> None:
    aliases = {
        "after_create": "after_replacement_create",
        "after_backup": "after_backup_link",
        "after_replace": "after_atomic_replace",
    }
    selected = aliases.get(fail_phase or "", fail_phase)
    if selected == phase:
        raise OSError(f"injected purge failure at {phase}")


def _reserve_unique_path(parent: Path, prefix: str) -> Path:
    descriptor, raw = tempfile.mkstemp(prefix=prefix, dir=str(parent))
    os.close(descriptor)
    path = Path(raw)
    path.unlink()
    return path


def _hard_link(source: Path, parent: Path, prefix: str) -> Path:
    destination = _reserve_unique_path(parent, prefix)
    try:
        os.link(source, destination)
    except OSError as exc:
        raise AdminUnsafeError(
            "history purge requires same-filesystem hard-link backup support"
        ) from exc
    return destination


def _checkpoint_for_purge(path: Path) -> tuple[int, int, int]:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(
            f"file:{path}?mode=rw", uri=True, timeout=0.0, isolation_level=None
        )
        conn.execute("PRAGMA busy_timeout=0")
        _require_schema(conn)
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        result = tuple(int(value) for value in (row or (1, 0, 0)))
    except AdminError:
        raise
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise AdminUnsafeError("monitoring database checkpoint is busy") from exc
        raise AdminDatabaseError("monitoring database checkpoint failed") from exc
    except sqlite3.DatabaseError as exc:
        raise AdminDatabaseError("monitoring database checkpoint failed") from exc
    finally:
        if conn is not None:
            conn.close()
    if result[0] != 0:
        raise AdminUnsafeError("monitoring database checkpoint is busy")
    return result


def _validate_empty_database(path: Path) -> None:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro&immutable=1", uri=True, timeout=5.0
        )
        conn.execute("PRAGMA query_only=ON")
        _require_schema(conn)
        for table in ("events", "sample_cycles", "metric_points", "minute_rollups"):
            count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            if count != 0:
                raise AdminDatabaseError("replacement database is not empty")
    except AdminError:
        raise
    except sqlite3.DatabaseError as exc:
        raise AdminDatabaseError("replacement database validation failed") from exc
    finally:
        if conn is not None:
            conn.close()


def _create_replacement(path: Path, metadata: os.stat_result) -> Path:
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.purge.", dir=str(path.parent))
    os.close(descriptor)
    replacement = Path(raw)
    try:
        conn = migrate_database(str(replacement))
        _require_schema(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(replacement) + suffix)
            if sidecar.exists() and not sidecar.is_symlink():
                sidecar.unlink()
        os.chmod(replacement, metadata.st_mode & 0o777)
        try:
            os.chown(replacement, metadata.st_uid, metadata.st_gid)
        except PermissionError:
            if os.geteuid() == 0:
                raise
        return replacement
    except Exception:
        for candidate in (
            replacement,
            Path(str(replacement) + "-wal"),
            Path(str(replacement) + "-shm"),
        ):
            if candidate.exists() and not candidate.is_symlink():
                candidate.unlink()
        raise


def _unlink_regular(path: Path) -> None:
    if path.is_symlink():
        raise AdminUnsafeError(f"refusing symlink during history purge: {path}")
    if path.exists():
        path.unlink()


@contextlib.contextmanager
def _optional_lock(lock_path: str | Path | None) -> Iterator[None]:
    if lock_path is None:
        yield
        return
    try:
        with RuntimeLock(lock_path):
            yield
    except RuntimeLockError as exc:
        raise AdminUnsafeError(str(exc)) from exc


def purge_history(
    db_path: str,
    *,
    replace: Callable[[str, str], None] = os.replace,
    fail_phase: str | None = None,
    lock_path: str | Path | None = None,
) -> None:
    with _optional_lock(lock_path):
        _inject_failure(fail_phase, "after_lock")
        path = _safe_database_path(db_path, must_exist=True)
        parent = path.parent
        metadata = path.stat()
        _checkpoint_for_purge(path)
        _inject_failure(fail_phase, "after_checkpoint")
        _fsync_path(path)

        replacement: Path | None = None
        backup: Path | None = None
        recovery: Path | None = None
        sidecar_backups: dict[str, Path] = {}
        replaced = False
        rollback_verified = False
        try:
            replacement = _create_replacement(path, metadata)
            _inject_failure(fail_phase, "after_replacement_create")
            _validate_empty_database(replacement)
            _inject_failure(fail_phase, "after_replacement_validate")
            _fsync_path(replacement)
            _inject_failure(fail_phase, "after_replacement_fsync")

            recovery = _hard_link(path, parent, f".{path.name}.recovery.")
            backup = _hard_link(path, parent, f".{path.name}.backup.")
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(path) + suffix)
                if sidecar.exists():
                    if sidecar.is_symlink() or not sidecar.is_file():
                        raise AdminUnsafeError("monitoring sidecar path is unsafe")
                    sidecar_backups[suffix] = _hard_link(
                        sidecar, parent, f".{path.name}{suffix}.recovery."
                    )
            _inject_failure(fail_phase, "after_backup_link")

            replace(str(replacement), str(path))
            replaced = True
            replacement = None
            _inject_failure(fail_phase, "after_atomic_replace")
            _fsync_path(parent)
            _validate_empty_database(path)
            _inject_failure(fail_phase, "after_replacement_validation")

            _unlink_regular(Path(str(path) + "-wal"))
            _inject_failure(fail_phase, "after_wal_cleanup")
            _unlink_regular(Path(str(path) + "-shm"))
            _inject_failure(fail_phase, "after_shm_cleanup")
            _fsync_path(parent)
            _inject_failure(fail_phase, "after_first_directory_fsync")

            if backup is not None:
                backup.unlink()
                backup = None
            _inject_failure(fail_phase, "after_backup_deletion")
            _fsync_path(parent)
            _inject_failure(fail_phase, "after_final_directory_fsync")

            if recovery is not None:
                recovery.unlink()
                recovery = None
            for saved in sidecar_backups.values():
                if saved.exists():
                    saved.unlink()
            sidecar_backups.clear()
            try:
                _fsync_path(parent)
            except OSError:
                # Replacement durability was already fsynced while recovery existed.
                pass
            rollback_verified = True
        except Exception as original:
            if replaced and recovery is not None and recovery.exists():
                try:
                    replace(str(recovery), str(path))
                    recovery = None
                    for suffix in ("-wal", "-shm"):
                        current = Path(str(path) + suffix)
                        _unlink_regular(current)
                        saved = sidecar_backups.pop(suffix, None)
                        if saved is not None and saved.exists():
                            replace(str(saved), str(current))
                    _fsync_path(parent)
                    conn = sqlite3.connect(
                        f"file:{path}?mode=ro&immutable=1", uri=True
                    )
                    _require_schema(conn)
                    conn.close()
                    rollback_verified = True
                except Exception as rollback_error:
                    diagnostic = recovery or backup
                    raise AdminDatabaseError(
                        "history purge rollback could not be verified; "
                        f"preserve recovery file {diagnostic}: {rollback_error}"
                    ) from original
            else:
                rollback_verified = True
            raise
        finally:
            if replacement is not None and replacement.exists() and not replacement.is_symlink():
                replacement.unlink()
            if rollback_verified:
                for candidate in (backup, recovery, *sidecar_backups.values()):
                    if candidate is not None and candidate.exists() and not candidate.is_symlink():
                        candidate.unlink()


def _config_payload(config: MonitoringConfig) -> dict[str, object]:
    return {
        "database_path": config.db_path,
        "env_directory": config.env_dir,
        "sample_interval_seconds": config.sample_interval,
        "tcp_interval_seconds": config.tcp_interval,
        "slow_interval_seconds": config.slow_interval,
        "maintenance_interval_seconds": config.maintenance_interval,
    }


def _load_for_args(args: argparse.Namespace) -> MonitoringConfig:
    return load_config(args.config, policy=args.policy, root=args.path_root)


def _resolve_database(args: argparse.Namespace) -> str:
    if args.db:
        raw = args.db
        if args.policy == INSTALLED_POLICY:
            candidate = MonitoringConfig(
                db_path=raw,
                env_dir=DEFAULT_CONFIG.env_dir,
                sample_interval=DEFAULT_CONFIG.sample_interval,
                tcp_interval=DEFAULT_CONFIG.tcp_interval,
                slow_interval=DEFAULT_CONFIG.slow_interval,
                maintenance_interval=DEFAULT_CONFIG.maintenance_interval,
            )
            apply_config_policy(candidate, policy=INSTALLED_POLICY, root=args.path_root)
    else:
        raw = _load_for_args(args).db_path
    if args.policy == INSTALLED_POLICY:
        return str(rooted_path(raw, args.path_root))
    return raw


def _resolve_lock(args: argparse.Namespace) -> str:
    if args.path_root and args.lock_path == DEFAULT_LOCK_PATH:
        return str(rooted_path(DEFAULT_LOCK_PATH, args.path_root))
    return args.lock_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gost-monitor-admin")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--policy", choices=CONFIG_POLICIES, default=INSTALLED_POLICY)
    parser.add_argument("--path-root", help=argparse.SUPPRESS)
    parser.add_argument("--lock-path", default=DEFAULT_LOCK_PATH, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    config = subparsers.add_parser("config")
    config.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    config.add_argument("--format", choices=("json", "value"), default="json")
    config.add_argument("--field", choices=CONFIG_FIELDS)

    validate = subparsers.add_parser("validate-config")
    validate.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    for name in ("migrate", "status", "maintenance", "purge-history"):
        command = subparsers.add_parser(name)
        command.add_argument("--db")
        command.add_argument("--config", default=DEFAULT_CONFIG_PATH)
        if name == "purge-history":
            command.add_argument("--yes", action="store_true")
    return parser


def run_command(args: argparse.Namespace, *, stdout: TextIO = sys.stdout) -> int:
    if args.command == "config":
        payload = _config_payload(_load_for_args(args))
        if args.format == "value":
            if args.field is None:
                raise AdminInputError("--format value requires --field")
            print(payload[args.field], file=stdout)
        else:
            if args.field is not None:
                raise AdminInputError("--field is valid only with --format value")
            print(json.dumps(payload, sort_keys=True), file=stdout)
        return 0
    if args.command == "validate-config":
        config = _load_for_args(args)
        print(
            f"monitoring config valid: db={config.db_path} env_dir={config.env_dir}",
            file=stdout,
        )
        return 0
    db_path = _resolve_database(args)
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
        purge_history(
            db_path,
            fail_phase=os.environ.get("GOST_MONITOR_PURGE_FAIL_PHASE"),
            lock_path=_resolve_lock(args),
        )
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
