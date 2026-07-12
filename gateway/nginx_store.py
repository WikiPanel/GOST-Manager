"""Atomic exact-file storage and bounded recovery backups for NGINX runtime."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from gateway.errors import ConflictError, OperationalError, StateError, ValidationError
from gateway.nginx_manifest import parse_manifest
from gateway.nginx_models import MAX_NGINX_CONFIG_BYTES, MAX_NGINX_MANIFEST_BYTES
from gateway.nginx_paths import NginxPaths
from gateway.paths import ensure_private_directory, reject_symlink_components
from gateway.runtime_render import sha256


BACKUP_RE = re.compile(r"^txn-[a-z0-9_-]+$")


@dataclass(frozen=True)
class NginxFileSnapshot:
    path: Path
    data: bytes | None
    mode: int | None


class NginxStore:
    def __init__(self, paths: NginxPaths) -> None:
        self.paths = paths

    def prepare(self) -> None:
        ensure_private_directory(self.paths.generated_dir)
        ensure_private_directory(self.paths.backup_dir)

    def read_optional(self, path: Path, maximum: int) -> bytes | None:
        reject_symlink_components(path)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > maximum
        ):
            raise ConflictError("managed NGINX runtime path is unsafe")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StateError("managed NGINX runtime file is unavailable") from exc

    def inspect_owned(self) -> tuple[bytes | None, bytes | None]:
        config = self.read_optional(self.paths.config_file, MAX_NGINX_CONFIG_BYTES)
        manifest_data = self.read_optional(self.paths.manifest_file, MAX_NGINX_MANIFEST_BYTES)
        if config is None and manifest_data is None:
            return None, None
        if manifest_data is None:
            raise ConflictError("NGINX config exists without a managed manifest")
        manifest = parse_manifest(manifest_data, self.paths)
        if config is not None and sha256(config) != manifest.config_sha256:
            raise ConflictError("managed NGINX config hash does not match its manifest")
        for path, data in (
            (self.paths.config_file, config),
            (self.paths.manifest_file, manifest_data),
        ):
            if data is None:
                continue
            metadata = path.lstat()
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise ConflictError("managed NGINX runtime file mode is not 0600")
            if os.geteuid() == 0 and (metadata.st_uid != 0 or metadata.st_gid != 0):
                raise ConflictError("managed NGINX runtime owner is not root:root")
        return config, manifest_data

    def snapshot(self) -> tuple[NginxFileSnapshot, ...]:
        result = []
        for path, maximum in (
            (self.paths.config_file, MAX_NGINX_CONFIG_BYTES),
            (self.paths.manifest_file, MAX_NGINX_MANIFEST_BYTES),
        ):
            data = self.read_optional(path, maximum)
            mode = stat.S_IMODE(path.lstat().st_mode) if data is not None else None
            result.append(NginxFileSnapshot(path, data, mode))
        return tuple(result)

    def write_atomic(self, path: Path, data: bytes, mode: int = 0o600) -> None:
        reject_symlink_components(path)
        ensure_private_directory(path.parent)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(descriptor, mode)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, data[offset:])
                if written <= 0:
                    raise OSError("short NGINX runtime write")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
            self.fsync_directory(path.parent)
            installed = self.read_optional(path, max(len(data), 1))
            if installed != data or stat.S_IMODE(path.lstat().st_mode) != mode:
                raise OperationalError("NGINX runtime replacement verification failed")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def remove_exact(self, path: Path) -> None:
        data = self.read_optional(
            path,
            MAX_NGINX_CONFIG_BYTES if path == self.paths.config_file else MAX_NGINX_MANIFEST_BYTES,
        )
        if data is None:
            return
        path.unlink()
        self.fsync_directory(path.parent)

    def restore(self, snapshots: tuple[NginxFileSnapshot, ...]) -> None:
        for item in snapshots:
            if item.data is None:
                self.remove_exact(item.path)
            else:
                self.write_atomic(item.path, item.data, item.mode or 0o600)

    def create_backup(self, snapshots: tuple[NginxFileSnapshot, ...]) -> Path:
        self.prepare()
        path = Path(tempfile.mkdtemp(prefix="txn-", dir=self.paths.backup_dir))
        os.chmod(path, 0o700)
        records: list[dict[str, object]] = []
        for index, item in enumerate(snapshots):
            filename = ""
            if item.data is not None:
                filename = f"{index:02d}.data"
                self.write_atomic(path / filename, item.data)
            records.append(
                {"path": str(item.path), "present": item.data is not None, "mode": item.mode, "data": filename}
            )
        self.write_atomic(
            path / "snapshot.json",
            (json.dumps({"schema_version": 1, "files": records}, sort_keys=True) + "\n").encode("utf-8"),
        )
        self.fsync_directory(self.paths.backup_dir)
        return path

    def remove_backup(self, path: Path) -> None:
        try:
            path.relative_to(self.paths.backup_dir)
        except ValueError as exc:
            raise ValidationError("NGINX backup path escaped its directory") from exc
        if not BACKUP_RE.fullmatch(path.name) or path.is_symlink():
            raise ValidationError("NGINX backup path is unmanaged")
        if path.exists():
            shutil.rmtree(path)
            self.fsync_directory(self.paths.backup_dir)

    def prune_backups(self, keep: int = 10, exclude: Path | None = None) -> None:
        self.prepare()
        candidates = [
            item for item in self.paths.backup_dir.iterdir()
            if item != exclude and BACKUP_RE.fullmatch(item.name)
            and item.is_dir() and not item.is_symlink()
        ]
        candidates.sort(key=lambda item: (item.stat().st_mtime_ns, item.name))
        for item in candidates[:-keep]:
            self.remove_backup(item)

    @staticmethod
    def fsync_directory(path: Path) -> None:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
