"""Exact-file atomic storage and rollback support for generated runtime."""

from __future__ import annotations

import os
import json
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from gateway.errors import ConflictError, OperationalError, StateError, ValidationError
from gateway.paths import ensure_private_directory, reject_symlink_components
from gateway.runtime_paths import ENV_FILE_RE, SERVICE_RE, RuntimePaths


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    data: bytes | None
    mode: int | None


class RuntimeStore:
    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths

    def prepare(self) -> None:
        for path in (
            self.paths.generated_dir,
            self.paths.exits_dir,
            self.paths.runtime_backup_dir,
        ):
            ensure_private_directory(path)
        reject_symlink_components(self.paths.systemd_dir)
        if not self.paths.systemd_dir.is_dir():
            raise OperationalError("systemd unit directory is unavailable")

    def read_optional(self, path: Path, maximum: int) -> bytes | None:
        reject_symlink_components(path)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValidationError("managed runtime path is unsafe")
        if metadata.st_size > maximum:
            raise StateError("managed runtime file exceeds its size limit")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StateError("managed runtime file is unavailable") from exc

    def snapshot(self, paths: set[Path]) -> tuple[FileSnapshot, ...]:
        result: list[FileSnapshot] = []
        for path in sorted(paths, key=str):
            data = self.read_optional(path, 512 * 1024)
            mode = stat.S_IMODE(path.lstat().st_mode) if data is not None else None
            result.append(FileSnapshot(path, data, mode))
        return tuple(result)

    def write_atomic(self, path: Path, data: bytes, mode: int = 0o600) -> None:
        reject_symlink_components(path)
        ensure_private_directory(path.parent) if path.parent != self.paths.systemd_dir else None
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(descriptor, mode)
            offset = 0
            while offset < len(data):
                offset += os.write(descriptor, data[offset:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
            self.fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def remove_exact(self, path: Path) -> None:
        reject_symlink_components(path)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValidationError("managed runtime path is unsafe")
        path.unlink()
        self.fsync_directory(path.parent)

    def restore(self, snapshots: tuple[FileSnapshot, ...]) -> None:
        for item in snapshots:
            if item.data is None:
                self.remove_exact(item.path)
            else:
                self.write_atomic(item.path, item.data, item.mode or 0o600)

    def create_backup(self, snapshots: tuple[FileSnapshot, ...]) -> Path:
        ensure_private_directory(self.paths.runtime_backup_dir)
        transaction = Path(tempfile.mkdtemp(prefix="txn-", dir=self.paths.runtime_backup_dir))
        os.chmod(transaction, 0o700)
        records: list[dict[str, object]] = []
        for index, item in enumerate(snapshots):
            data_name = None
            if item.data is not None:
                data_name = f"{index:04d}.data"
                self.write_atomic(transaction / data_name, item.data, 0o600)
            records.append(
                {
                    "path": str(item.path), "present": item.data is not None,
                    "mode": item.mode, "data_file": data_name,
                }
            )
        metadata = (json.dumps({"schema_version": 1, "files": records}, sort_keys=True) + "\n").encode("utf-8")
        self.write_atomic(transaction / "snapshot.json", metadata, 0o600)
        self.fsync_directory(self.paths.runtime_backup_dir)
        return transaction

    def remove_backup_tree(self, transaction: Path) -> None:
        try:
            transaction.relative_to(self.paths.runtime_backup_dir)
        except ValueError as exc:
            raise ValidationError("runtime backup path escaped its directory") from exc
        reject_symlink_components(transaction)
        if transaction.exists():
            shutil.rmtree(transaction)

    def fsync_backup_parent(self) -> None:
        self.fsync_directory(self.paths.runtime_backup_dir)

    def remove_backup(self, transaction: Path) -> None:
        self.remove_backup_tree(transaction)
        self.fsync_backup_parent()

    def prune_backups(self, keep: int = 3, exclude: Path | None = None) -> None:
        ensure_private_directory(self.paths.runtime_backup_dir)
        candidates = []
        for path in self.paths.runtime_backup_dir.iterdir():
            if path == exclude:
                continue
            if not path.name.startswith("txn-") or path.is_symlink() or not path.is_dir():
                continue
            candidates.append(path)
        for path in sorted(candidates, key=lambda item: item.stat().st_mtime_ns)[:-keep]:
            self.remove_backup(path)

    def managed_file_ids(self) -> tuple[frozenset[str], frozenset[str]]:
        values: list[frozenset[str]] = []
        for directory, pattern in (
            (self.paths.exits_dir, ENV_FILE_RE),
            (self.paths.systemd_dir, SERVICE_RE),
        ):
            current: set[str] = set()
            try:
                reject_symlink_components(directory)
                entries = list(directory.iterdir())
            except FileNotFoundError:
                values.append(frozenset())
                continue
            for path in entries:
                match = pattern.fullmatch(path.name)
                if not match:
                    continue
                metadata = path.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                ):
                    raise ConflictError("managed runtime discovery found an unsafe path")
                current.add(match.group(1))
            values.append(frozenset(current))
        return values[0], values[1]

    def validate_dependency(self, path: Path, label: str) -> None:
        reject_symlink_components(path)
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise StateError(f"{label} is missing") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not metadata.st_mode & 0o111
        ):
            raise StateError(f"{label} is unsafe or not executable")

    @staticmethod
    def fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
