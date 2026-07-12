"""Locked, revisioned, atomic storage for gateway state documents."""

from __future__ import annotations

import datetime as dt
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from gateway.errors import (
    ConflictError,
    OperationalError,
    StateError,
    ValidationError,
)
from gateway.locking import GatewayStateLock
from gateway.models import (
    MAX_NODE_BYTES,
    MAX_SHARED_BYTES,
    NODE_SCHEMA_VERSION,
    SHARED_SCHEMA_VERSION,
    Gateway,
    NodeState,
    SharedState,
    StatePair,
)
from gateway.paths import (
    StatePaths,
    ensure_directory,
    ensure_private_directory,
    read_regular_file,
    reject_symlink_components,
)
from gateway.serialization import (
    parse_node,
    parse_shared,
    serialize_node,
    serialize_shared,
)
from gateway.validation import (
    canonical_ipv4,
    canonical_server_names,
    validate_pair,
    validate_slug,
    validate_timestamp,
    validate_uuid,
)

FailureHook = Callable[[str], None]
SharedMutator = Callable[[SharedState], SharedState]
NodeMutator = Callable[[NodeState], NodeState]

BACKUP_LIMIT = 10
BACKUP_RE = re.compile(r"^(shared|node)-r([1-9][0-9]*)-([a-f0-9]{16,64})\.json$")
FAILURE_PHASES = (
    "after_lock_acquisition",
    "after_current_state_read",
    "after_candidate_serialization",
    "after_temporary_creation",
    "after_temporary_write",
    "after_file_fsync",
    "after_backup_creation",
    "after_atomic_replacement",
    "after_parent_fsync",
    "after_post_replace_validation",
    "after_backup_pruning",
    "after_first_document_init",
    "after_second_document_init",
)


def _default_clock() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass(frozen=True)
class FileOperations:
    mkstemp: Callable[..., tuple[int, str]] = tempfile.mkstemp
    fchmod: Callable[[int, int], None] = os.fchmod
    write: Callable[[int, bytes], int] = os.write
    fsync: Callable[[int], None] = os.fsync
    close: Callable[[int], None] = os.close
    replace: Callable[[str, str], None] = os.replace
    unlink: Callable[[str], None] = os.unlink
    open: Callable[..., int] = os.open
    scandir: Callable[[str], object] = os.scandir
    read_file: Callable[[Path, int, str], bytes] = read_regular_file
    ensure_directory: Callable[[Path], None] = ensure_directory
    ensure_private_directory: Callable[[Path], None] = ensure_private_directory
    suffix: Callable[[], str] = lambda: uuid.uuid4().hex


@dataclass(frozen=True)
class MutationResult:
    pair: StatePair
    changed: bool


class GatewayStateStore:
    def __init__(
        self,
        paths: StatePaths,
        *,
        clock: Callable[[], dt.datetime] = _default_clock,
        uuid_factory: Callable[[], uuid.UUID | str] = uuid.uuid4,
        file_operations: FileOperations = FileOperations(),
        lock_timeout: float = 5.0,
        lock_factory: Callable[[Path, float], GatewayStateLock] | None = None,
        failure_hook: FailureHook | None = None,
    ) -> None:
        self.paths = paths
        self.clock = clock
        self.uuid_factory = uuid_factory
        self.ops = file_operations
        self.lock_timeout = lock_timeout
        self.lock_factory = lock_factory or (
            lambda path, timeout: GatewayStateLock(path, timeout=timeout)
        )
        self.failure_hook = failure_hook

    def _fail(self, phase: str) -> None:
        if self.failure_hook is not None:
            self.failure_hook(phase)

    def _timestamp(self) -> str:
        current = self.clock()
        if not isinstance(current, dt.datetime):
            raise OperationalError("gateway clock returned an invalid value")
        if current.tzinfo is None or current.utcoffset() != dt.timedelta(0):
            raise OperationalError("gateway clock must return UTC")
        value = current.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
        return validate_timestamp(value.replace("+00:00", "Z"))

    def _document_id(self) -> str:
        generated = self.uuid_factory()
        value = str(generated)
        return validate_uuid(value)

    def _lock(self) -> GatewayStateLock:
        return self.lock_factory(self.paths.lock_file, self.lock_timeout)

    def _read_pair_unlocked(
        self, runtime_ready: bool = False
    ) -> tuple[StatePair, bytes, bytes]:
        try:
            shared_data = self.ops.read_file(
                self.paths.state_file, MAX_SHARED_BYTES, "shared state"
            )
            node_data = self.ops.read_file(
                self.paths.node_file, MAX_NODE_BYTES, "node state"
            )
            pair = StatePair(parse_shared(shared_data), parse_node(node_data))
            validate_pair(pair, runtime_ready=runtime_ready)
            return pair, shared_data, node_data
        except StateError:
            raise
        except ValidationError as exc:
            raise StateError("gateway state is corrupt or unsupported") from exc
        except OSError as exc:
            raise StateError("gateway state is unavailable") from exc

    def _load_pair_unlocked(self, runtime_ready: bool = False) -> StatePair:
        pair, _shared_data, _node_data = self._read_pair_unlocked(
            runtime_ready=runtime_ready
        )
        return pair

    def load_pair(self, runtime_ready: bool = False) -> StatePair:
        with self._lock():
            self._fail("after_lock_acquisition")
            pair = self._load_pair_unlocked(runtime_ready=runtime_ready)
            self._fail("after_current_state_read")
            return pair

    def initialize(
        self,
        *,
        gateway_id: str,
        node_id: str,
        listen_address: str,
        listen_port: int,
        server_names: list[str] | tuple[str, ...],
        status_port: int = 18000,
    ) -> StatePair:
        timestamp = self._timestamp()
        document_id = self._document_id()
        pair = StatePair(
            shared=SharedState(
                schema_version=SHARED_SCHEMA_VERSION,
                document_id=document_id,
                revision=1,
                updated_at=timestamp,
                gateway=Gateway(
                    id=validate_slug(gateway_id, "gateway ID"),
                    enabled=False,
                    listen_address=canonical_ipv4(
                        listen_address, "gateway listen address"
                    ),
                    listen_port=listen_port,
                    server_names=canonical_server_names(list(server_names)),
                    status_port=status_port,
                ),
                exits=(),
                routes=(),
            ),
            node=NodeState(
                schema_version=NODE_SCHEMA_VERSION,
                document_id=document_id,
                node_id=validate_slug(node_id, "node ID"),
                revision=1,
                updated_at=timestamp,
                bindings=(),
            ),
        )
        validate_pair(pair)
        shared_data = serialize_shared(pair.shared)
        node_data = serialize_node(pair.node)
        self._fail("after_candidate_serialization")

        with self._lock():
            self._fail("after_lock_acquisition")
            for path in (self.paths.state_file, self.paths.node_file):
                reject_symlink_components(path)
                try:
                    path.lstat()
                except FileNotFoundError:
                    continue
                raise ConflictError("gateway state is already initialized")
            self._prepare_directories()

            created: list[Path] = []
            try:
                self._install_document(
                    "shared",
                    self.paths.state_file,
                    shared_data,
                    previous_data=None,
                    previous_revision=None,
                    validate_installed=lambda: parse_shared(
                        self.ops.read_file(
                            self.paths.state_file, MAX_SHARED_BYTES, "shared state"
                        )
                    ),
                )
                created.append(self.paths.state_file)
                self._fail("after_first_document_init")
                self._install_document(
                    "node",
                    self.paths.node_file,
                    node_data,
                    previous_data=None,
                    previous_revision=None,
                    validate_installed=lambda: parse_node(
                        self.ops.read_file(
                            self.paths.node_file, MAX_NODE_BYTES, "node state"
                        )
                    ),
                )
                created.append(self.paths.node_file)
                self._fail("after_second_document_init")
                installed = self._load_pair_unlocked()
                self._fail("after_post_replace_validation")
                return installed
            except Exception:
                for path in reversed(created):
                    self._unlink_regular(path)
                    self._fsync_directory(path.parent)
                raise

    def mutate_shared(
        self,
        mutator: SharedMutator,
        *,
        expected_revision: int | None = None,
    ) -> MutationResult:
        preview = self.load_pair()
        validate_pair(StatePair(mutator(preview.shared), preview.node))
        with self._lock():
            self._fail("after_lock_acquisition")
            current, previous_data, _node_data = self._read_pair_unlocked()
            self._fail("after_current_state_read")
            self._check_revision(
                "shared", current.shared.revision, expected_revision
            )
            candidate = mutator(current.shared)
            if candidate == current.shared:
                return MutationResult(current, False)
            if candidate.gateway.id != current.shared.gateway.id:
                raise ConflictError("gateway ID is immutable")
            candidate = replace(
                candidate,
                revision=current.shared.revision + 1,
                updated_at=self._timestamp(),
            )
            pair = StatePair(candidate, current.node)
            validate_pair(pair)
            data = serialize_shared(candidate)
            self._fail("after_candidate_serialization")
            self._install_document(
                "shared",
                self.paths.state_file,
                data,
                previous_data=previous_data,
                previous_revision=current.shared.revision,
                validate_installed=lambda: validate_pair(
                    StatePair(
                        parse_shared(
                            self.ops.read_file(
                                self.paths.state_file,
                                MAX_SHARED_BYTES,
                                "shared state",
                            )
                        ),
                        current.node,
                    )
                ),
            )
            installed = self._load_pair_unlocked()
            return MutationResult(installed, True)

    def mutate_node(
        self,
        mutator: NodeMutator,
        *,
        expected_revision: int | None = None,
    ) -> MutationResult:
        preview = self.load_pair()
        validate_pair(StatePair(preview.shared, mutator(preview.node)))
        with self._lock():
            self._fail("after_lock_acquisition")
            current, _shared_data, previous_data = self._read_pair_unlocked()
            self._fail("after_current_state_read")
            self._check_revision("node", current.node.revision, expected_revision)
            candidate = mutator(current.node)
            if candidate == current.node:
                return MutationResult(current, False)
            if candidate.node_id != current.node.node_id:
                raise ConflictError("node ID is immutable")
            candidate = replace(
                candidate,
                revision=current.node.revision + 1,
                updated_at=self._timestamp(),
            )
            pair = StatePair(current.shared, candidate)
            validate_pair(pair)
            data = serialize_node(candidate)
            self._fail("after_candidate_serialization")
            self._install_document(
                "node",
                self.paths.node_file,
                data,
                previous_data=previous_data,
                previous_revision=current.node.revision,
                validate_installed=lambda: validate_pair(
                    StatePair(
                        current.shared,
                        parse_node(
                            self.ops.read_file(
                                self.paths.node_file, MAX_NODE_BYTES, "node state"
                            )
                        ),
                    )
                ),
            )
            installed = self._load_pair_unlocked()
            return MutationResult(installed, True)

    @staticmethod
    def _check_revision(
        document: str, current: int, expected: int | None
    ) -> None:
        if expected is None:
            return
        if type(expected) is not int or expected < 1:
            raise ValidationError("expected revision must be a positive integer")
        if current != expected:
            raise ConflictError(
                f"{document} revision conflict: expected {expected}, current {current}"
            )

    def _prepare_directories(self) -> None:
        self.ops.ensure_directory(self.paths.state_file.parent)
        self.ops.ensure_directory(self.paths.node_file.parent)
        self.ops.ensure_private_directory(self.paths.backup_dir)

    def _write_all(self, descriptor: int, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = self.ops.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError("short gateway state write")
            offset += written

    def _temporary_file(
        self,
        directory: Path,
        prefix: str,
        data: bytes,
        *,
        emit_hooks: bool,
    ) -> Path:
        descriptor: int | None = None
        path: Path | None = None
        try:
            descriptor, raw_path = self.ops.mkstemp(prefix=prefix, dir=str(directory))
            path = Path(raw_path)
            self.ops.fchmod(descriptor, 0o600)
            if emit_hooks:
                self._fail("after_temporary_creation")
            self._write_all(descriptor, data)
            if emit_hooks:
                self._fail("after_temporary_write")
            self.ops.fsync(descriptor)
            if emit_hooks:
                self._fail("after_file_fsync")
            self.ops.close(descriptor)
            descriptor = None
            return path
        except Exception:
            if descriptor is not None:
                self.ops.close(descriptor)
            if path is not None:
                self._safe_unlink(path)
            raise

    def _create_backup(
        self, kind: str, revision: int, data: bytes
    ) -> Path:
        self.ops.ensure_private_directory(self.paths.backup_dir)
        suffix = self.ops.suffix().lower()
        if not re.fullmatch(r"[a-f0-9]{16,64}", suffix):
            raise OperationalError("backup suffix generator returned an invalid value")
        destination = self.paths.backup_dir / f"{kind}-r{revision}-{suffix}.json"
        temporary = self._temporary_file(
            self.paths.backup_dir,
            f".{kind}-backup.",
            data,
            emit_hooks=False,
        )
        try:
            self.ops.replace(str(temporary), str(destination))
            self._fsync_directory(self.paths.backup_dir)
        except Exception:
            self._safe_unlink(temporary)
            self._safe_unlink(destination)
            raise
        self._fail("after_backup_creation")
        return destination

    def _install_document(
        self,
        kind: str,
        path: Path,
        data: bytes,
        *,
        previous_data: bytes | None,
        previous_revision: int | None,
        validate_installed: Callable[[], object],
    ) -> None:
        self._prepare_directories()
        reject_symlink_components(path)
        temporary: Path | None = self._temporary_file(
            path.parent, f".{path.name}.new.", data, emit_hooks=True
        )
        backup: Path | None = None
        replaced = False
        try:
            if previous_data is not None and previous_revision is not None:
                backup = self._create_backup(
                    kind, previous_revision, previous_data
                )
            self.ops.replace(str(temporary), str(path))
            temporary = None
            replaced = True
            self._fail("after_atomic_replacement")
            self._fsync_directory(path.parent)
            self._fail("after_parent_fsync")
            validate_installed()
            self._fail("after_post_replace_validation")
            self._prune_backups(kind)
            self._fail("after_backup_pruning")
        except Exception as original:
            if replaced:
                try:
                    if previous_data is None:
                        self._unlink_regular(path)
                        self._fsync_directory(path.parent)
                    else:
                        self._restore_document(path, previous_data)
                        validate_installed()
                except Exception as rollback_error:
                    raise OperationalError(
                        "gateway state rollback could not be verified"
                    ) from rollback_error
            raise original
        finally:
            if temporary is not None:
                self._safe_unlink(temporary)
            # A verified backup remains as bounded operator history.
            _ = backup

    def _restore_document(self, path: Path, data: bytes) -> None:
        temporary = self._temporary_file(
            path.parent, f".{path.name}.restore.", data, emit_hooks=False
        )
        try:
            self.ops.replace(str(temporary), str(path))
            self._fsync_directory(path.parent)
        finally:
            self._safe_unlink(temporary)

    def _prune_backups(self, kind: str) -> None:
        entries: list[tuple[int, str, Path]] = []
        try:
            with self.ops.scandir(str(self.paths.backup_dir)) as iterator:
                for entry in iterator:
                    match = BACKUP_RE.fullmatch(entry.name)
                    if match is None or match.group(1) != kind:
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    metadata = entry.stat(follow_symlinks=False)
                    entries.append(
                        (metadata.st_mtime_ns, entry.name, Path(entry.path))
                    )
            entries.sort()
            for _mtime, _name, path in entries[:-BACKUP_LIMIT]:
                self.ops.unlink(str(path))
            if len(entries) > BACKUP_LIMIT:
                self._fsync_directory(self.paths.backup_dir)
        except OSError as exc:
            raise OperationalError("gateway backup pruning failed") from exc

    def _fsync_directory(self, path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = self.ops.open(str(path), flags)
        try:
            self.ops.fsync(descriptor)
        finally:
            self.ops.close(descriptor)

    def _unlink_regular(self, path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValidationError("refusing to remove an unsafe gateway state path")
        self.ops.unlink(str(path))

    def _safe_unlink(self, path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISREG(metadata.st_mode):
            try:
                self.ops.unlink(str(path))
            except FileNotFoundError:
                pass
