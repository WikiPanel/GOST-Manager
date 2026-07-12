"""Private bounded advisory lock for gateway state operations."""

from __future__ import annotations

import fcntl
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path

from gateway.errors import ConflictError, OperationalError, ValidationError
from gateway.paths import ensure_private_directory, reject_symlink_components, validated_path


class GatewayStateLock:
    def __init__(
        self,
        path: str | Path,
        timeout: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        label: str = "gateway state",
        marker: bytes = b"gateway-state\n",
    ) -> None:
        if timeout < 0 or timeout > 60:
            raise ValidationError("lock timeout must be from 0 through 60 seconds")
        self.path = validated_path(path, "lock file")
        self.timeout = timeout
        self.monotonic = monotonic
        self.sleep = sleep
        self.label = label
        self.marker = marker
        self._descriptor: int | None = None

    def acquire(self) -> "GatewayStateLock":
        if self._descriptor is not None:
            return self
        ensure_private_directory(self.path.parent)
        reject_symlink_components(self.path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise OperationalError(f"{self.label} lock file is unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValidationError(f"{self.label} lock must be a regular file")
            os.fchmod(descriptor, 0o600)
            deadline = self.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if self.monotonic() >= deadline:
                        raise ConflictError(f"{self.label} lock is busy")
                    self.sleep(min(0.05, max(0.0, deadline - self.monotonic())))
            os.ftruncate(descriptor, 0)
            os.write(descriptor, self.marker)
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def release(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "GatewayStateLock":
        return self.acquire()

    def __exit__(self, *_args: object) -> None:
        self.release()


class GatewayRuntimeLock(GatewayStateLock):
    """Separate lock for generated runtime and private-secret mutations."""

    def __init__(self, path: str | Path, timeout: float = 5.0, **kwargs: object) -> None:
        super().__init__(
            path,
            timeout=timeout,
            label="gateway runtime",
            marker=b"gateway-runtime\n",
            **kwargs,
        )
