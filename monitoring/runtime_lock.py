"""Private advisory lock shared by collectors and destructive admin work."""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path


DEFAULT_LOCK_PATH = "/run/gost-manager/collector.lock"


class RuntimeLockError(RuntimeError):
    """The runtime lock is busy or cannot be used safely."""


class RuntimeLock:
    def __init__(self, path: str | Path = DEFAULT_LOCK_PATH) -> None:
        self.path = Path(path)
        self._descriptor: int | None = None

    def _validate_parent(self) -> None:
        if not self.path.is_absolute():
            raise RuntimeLockError("runtime lock path must be absolute")
        current = Path(self.path.anchor)
        for part in self.path.parent.parts[1:]:
            current /= part
            if current.is_symlink():
                raise RuntimeLockError("runtime lock path may not traverse a symlink")
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeLockError("runtime lock directory is unavailable") from exc
        if self.path.parent.is_symlink() or not self.path.parent.is_dir():
            raise RuntimeLockError("runtime lock directory is unsafe")
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError as exc:
            raise RuntimeLockError("runtime lock directory cannot be made private") from exc

    def acquire(self) -> "RuntimeLock":
        if self._descriptor is not None:
            return self
        self._validate_parent()
        if self.path.is_symlink():
            raise RuntimeLockError("runtime lock file may not be a symlink")
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise RuntimeLockError("runtime lock file is unsafe or unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeLockError("runtime lock must be a regular file")
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeLockError("monitoring collector or admin operation is busy") from exc
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "RuntimeLock":
        return self.acquire()

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()
