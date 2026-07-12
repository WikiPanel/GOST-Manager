"""Gateway state path validation and regular-file helpers."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from gateway.errors import OperationalError, StateError, ValidationError

DEFAULT_STATE_FILE = "/etc/gost-manager/state.json"
DEFAULT_NODE_FILE = "/etc/gost-manager/node.json"
DEFAULT_BACKUP_DIR = "/etc/gost-manager/backups/gateway"
DEFAULT_LOCK_FILE = "/run/gost-manager/gateway-state.lock"


def validated_path(value: str | Path, label: str) -> Path:
    raw = str(value)
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise ValidationError(f"{label} contains forbidden characters")
    if (
        not raw.startswith("/")
        or raw.startswith("//")
        or os.path.normpath(raw) != raw
    ):
        raise ValidationError(f"{label} must be an absolute normalized path")
    return Path(raw)


def reject_symlink_components(path: Path, include_final: bool = True) -> None:
    current = Path(path.anchor)
    parts = path.parts[1:] if include_final else path.parent.parts[1:]
    for part in parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValidationError("gateway state path may not traverse a symlink")


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def ensure_directory(path: Path) -> None:
    reject_symlink_components(path)
    try:
        try:
            existing_metadata = path.lstat()
            existed = True
        except FileNotFoundError:
            existing_metadata = None
            existed = False
        if existing_metadata is not None and not stat.S_ISDIR(
            existing_metadata.st_mode
        ):
            raise ValidationError("gateway state parent must be a directory")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        reject_symlink_components(path)
        descriptor = _open_directory(path)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValidationError("gateway state parent must be a directory")
            if not existed:
                os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)
    except ValidationError:
        raise
    except OSError as exc:
        raise OperationalError("gateway state directory is unavailable") from exc


def ensure_private_directory(path: Path) -> None:
    ensure_directory(path)
    try:
        descriptor = _open_directory(path)
        try:
            mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
            if mode != 0o700:
                raise ValidationError("gateway private directory must have mode 0700")
        finally:
            os.close(descriptor)
    except ValidationError:
        raise
    except OSError as exc:
        raise OperationalError("gateway private directory is unavailable") from exc


def read_regular_file(path: Path, maximum: int, label: str) -> bytes:
    reject_symlink_components(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise StateError(f"{label} is missing") from exc
    except OSError as exc:
        raise StateError(f"{label} is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise StateError(f"{label} is not a regular file")
        if metadata.st_size > maximum:
            raise StateError(f"{label} exceeds its size limit")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum:
            raise StateError(f"{label} exceeds its size limit")
        return data
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class StatePaths:
    state_file: Path
    node_file: Path
    backup_dir: Path
    lock_file: Path

    @classmethod
    def from_values(
        cls,
        state_file: str | Path = DEFAULT_STATE_FILE,
        node_file: str | Path = DEFAULT_NODE_FILE,
        backup_dir: str | Path = DEFAULT_BACKUP_DIR,
        lock_file: str | Path = DEFAULT_LOCK_FILE,
    ) -> "StatePaths":
        result = cls(
            state_file=validated_path(state_file, "state file"),
            node_file=validated_path(node_file, "node file"),
            backup_dir=validated_path(backup_dir, "backup directory"),
            lock_file=validated_path(lock_file, "lock file"),
        )
        values = {
            result.state_file,
            result.node_file,
            result.backup_dir,
            result.lock_file,
        }
        if len(values) != 4:
            raise ValidationError("gateway state paths must be different")
        return result
