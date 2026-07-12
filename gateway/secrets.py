"""Strict private credential parsing and atomic secret storage."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from gateway.errors import ConflictError, OperationalError, StateError, ValidationError
from gateway.locking import GatewayRuntimeLock
from gateway.paths import ensure_private_directory, reject_symlink_components
from gateway.runtime_models import Credentials
from gateway.runtime_paths import SECRET_FILE_RE, RuntimePaths
from gateway.validation import require_exact_keys, validate_secret_ref

MAX_SECRET_BYTES = 1024
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


def _validate_token(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValidationError(f"{label} is invalid")
    if not TOKEN_RE.fullmatch(value):
        raise ValidationError(f"{label} is invalid")
    return value


def validate_credentials(username: object, password: object) -> Credentials:
    return Credentials(
        _validate_token(username, "username", 128),
        _validate_token(password, "password", 256),
    )


def render_secret(credentials: Credentials) -> bytes:
    validated = validate_credentials(credentials.username, credentials.password)
    return (
        f"GOST_USER={validated.username}\nGOST_PASS={validated.password}\n"
    ).encode("utf-8")


def parse_secret(data: bytes) -> Credentials:
    if not data or len(data) > MAX_SECRET_BYTES or not data.endswith(b"\n"):
        raise ValidationError("secret file format is invalid")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("secret file format is invalid") from exc
    if "\x00" in text or text.count("\n") != 2:
        raise ValidationError("secret file format is invalid")
    lines = text[:-1].split("\n")
    if len(lines) != 2 or any("=" not in line for line in lines):
        raise ValidationError("secret file format is invalid")
    if not lines[0].startswith("GOST_USER=") or not lines[1].startswith("GOST_PASS="):
        raise ValidationError("secret file format is invalid")
    values: dict[str, str] = {}
    for line in lines:
        key, value = line.split("=", 1)
        if key in values or key not in {"GOST_USER", "GOST_PASS"}:
            raise ValidationError("secret file format is invalid")
        values[key] = value
    if set(values) != {"GOST_USER", "GOST_PASS"}:
        raise ValidationError("secret file format is invalid")
    return validate_credentials(values["GOST_USER"], values["GOST_PASS"])


def parse_secret_json(data: bytes) -> Credentials:
    if not data or len(data) > MAX_SECRET_BYTES:
        raise ValidationError("secret input is invalid")
    try:
        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in items:
                if key in result:
                    raise ValidationError("secret input is invalid")
                result[key] = value
            return result

        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("secret input is invalid") from exc
    if not isinstance(value, dict):
        raise ValidationError("secret input is invalid")
    require_exact_keys(value, frozenset({"username", "password"}), "secret input")
    return validate_credentials(value["username"], value["password"])


@dataclass(frozen=True)
class SecretStatus:
    secret_ref: str
    valid: bool
    referenced_exit_ids: tuple[str, ...] = ()

    @property
    def referenced(self) -> bool:
        return bool(self.referenced_exit_ids)


class SecretStore:
    def __init__(
        self,
        paths: RuntimePaths,
        *,
        lock_timeout: float = 5.0,
        lock_factory: Callable[[Path, float], GatewayRuntimeLock] | None = None,
        replace: Callable[[str, str], None] = os.replace,
        fsync: Callable[[int], None] = os.fsync,
        failure_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.paths = paths
        self.lock_timeout = lock_timeout
        self.lock_factory = lock_factory or (
            lambda path, timeout: GatewayRuntimeLock(path, timeout=timeout)
        )
        self.replace = replace
        self.fsync = fsync
        self.failure_hook = failure_hook

    def lock(self) -> GatewayRuntimeLock:
        return self.lock_factory(self.paths.runtime_lock_file, self.lock_timeout)

    def _fail(self, phase: str) -> None:
        if self.failure_hook is not None:
            self.failure_hook(phase)

    def _safe_metadata(self, path: Path, *, missing_ok: bool = False) -> os.stat_result | None:
        reject_symlink_components(path)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if missing_ok:
                return None
            raise StateError("secret is missing")
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValidationError("secret path is unsafe")
        return metadata

    def _read_unlocked(self, secret_ref: str) -> tuple[Credentials, int]:
        credentials, mtime_ns, _data = self._read_data_unlocked(secret_ref)
        return credentials, mtime_ns

    def _read_data_unlocked(self, secret_ref: str) -> tuple[Credentials, int, bytes]:
        path = self.paths.secret_file(secret_ref)
        reject_symlink_components(path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as exc:
            raise StateError("secret is missing") from exc
        except OSError as exc:
            raise StateError("secret is unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > MAX_SECRET_BYTES
            ):
                raise ValidationError("secret file permissions, type, or size are invalid")
            chunks: list[bytes] = []
            remaining = MAX_SECRET_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 4096))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > MAX_SECRET_BYTES:
                raise ValidationError("secret file permissions, type, or size are invalid")
            return parse_secret(data), metadata.st_mtime_ns, data
        finally:
            os.close(descriptor)

    def read(self, secret_ref: str) -> tuple[Credentials, int]:
        validate_secret_ref(secret_ref, True)
        return self._read_unlocked(secret_ref)

    def set(self, secret_ref: str, credentials: Credentials) -> str:
        validate_secret_ref(secret_ref, True)
        data = render_secret(credentials)
        with self.lock():
            return self._set_unlocked(secret_ref, data)

    def _set_unlocked(self, secret_ref: str, data: bytes) -> str:
        ensure_private_directory(self.paths.secret_dir)
        path = self.paths.secret_file(secret_ref)
        old_metadata = self._safe_metadata(path, missing_ok=True)
        previous = self._read_data_unlocked(secret_ref)[2] if old_metadata is not None else None
        descriptor, temporary = tempfile.mkstemp(prefix=f".{secret_ref}.", dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(data):
                offset += os.write(descriptor, data[offset:])
            self.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            self._fail("after_secret_staging")
            self.replace(temporary, str(path))
            self._fail("after_secret_replacement")
            self._fsync_directory(path.parent)
            self._read_unlocked(secret_ref)
            return "updated" if previous is not None else "created"
        except Exception:
            if previous is not None:
                self._restore(path, previous)
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _restore(self, path: Path, data: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".restore.", dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, data)
            self.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            self.replace(temporary, str(path))
            self._fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def delete_unlocked(self, secret_ref: str, referencing_exit_ids: Iterable[str]) -> None:
        validate_secret_ref(secret_ref, True)
        references = tuple(sorted(set(referencing_exit_ids)))
        if references:
            raise ConflictError("secret is still referenced by gateway bindings")
        path = self.paths.secret_file(secret_ref)
        self._safe_metadata(path)
        try:
            path.unlink()
            self._fsync_directory(path.parent)
        except OSError as exc:
            raise OperationalError("secret could not be deleted") from exc

    def list(self, references: dict[str, tuple[str, ...]] | None = None) -> tuple[SecretStatus, ...]:
        references = references or {}
        try:
            reject_symlink_components(self.paths.secret_dir)
            entries = list(self.paths.secret_dir.iterdir())
        except FileNotFoundError:
            entries = []
        result: list[SecretStatus] = []
        for path in sorted(entries, key=lambda item: item.name):
            match = SECRET_FILE_RE.fullmatch(path.name)
            if not match:
                continue
            secret_ref = match.group(1)
            try:
                self._read_unlocked(secret_ref)
                valid = True
            except (StateError, ValidationError):
                valid = False
            result.append(SecretStatus(secret_ref, valid, tuple(sorted(references.get(secret_ref, ())))))
        return tuple(result)

    def validate(self, secret_ref: str | None = None) -> tuple[SecretStatus, ...]:
        if secret_ref is not None:
            self._read_unlocked(secret_ref)
            return (SecretStatus(secret_ref, True),)
        statuses = self.list()
        if any(not item.valid for item in statuses):
            raise ValidationError("one or more managed secrets are invalid")
        return statuses

    def _fsync_directory(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            self.fsync(descriptor)
        finally:
            os.close(descriptor)
