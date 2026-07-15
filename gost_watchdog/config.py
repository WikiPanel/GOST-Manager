"""Strict non-executable configuration and durable atomic writes."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

from gost_watchdog.models import GlobalConfig, MODES, ProfileConfig


DEFAULT_GLOBAL_CONFIG_PATH = "/etc/gost-manager/watchdog.conf"
DEFAULT_PROFILE_CONFIG_DIR = "/etc/gost-manager/watchdog.d"
GLOBAL_KEYS = (
    "CHECK_MODE",
    "CHECK_INTERVAL_SECONDS",
    "PING_TIMEOUT_SECONDS",
    "FAILURE_THRESHOLD",
    "SUCCESS_THRESHOLD",
    "RECOVERY_HOLD_SECONDS",
    "RECOVERY_JITTER_MAX_SECONDS",
)
PROFILE_KEYS = ("MODE",) + GLOBAL_KEYS[1:]
BOUNDS = {
    "CHECK_INTERVAL_SECONDS": (1, 300),
    "PING_TIMEOUT_SECONDS": (1, 60),
    "FAILURE_THRESHOLD": (1, 1000),
    "SUCCESS_THRESHOLD": (1, 1000),
    "RECOVERY_HOLD_SECONDS": (0, 3600),
    "RECOVERY_JITTER_MAX_SECONDS": (0, 300),
}


class ConfigError(ValueError):
    """A Watchdog configuration is malformed or unsafe."""


def rooted_path(path: str | Path, root: str | Path | None = None) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ConfigError("managed path must be absolute")
    if root is None or str(root) == "":
        return candidate
    boundary = Path(root)
    if not boundary.is_absolute() or boundary.is_symlink():
        raise ConfigError("path root must be a real absolute directory")
    return boundary.joinpath(*candidate.parts[1:])


def _reject_symlink_components(path: Path, boundary: Path | None = None) -> None:
    if boundary is None:
        current = Path(path.anchor)
        parts = path.parts[1:]
    else:
        current = boundary
        try:
            parts = path.relative_to(boundary).parts
        except ValueError as exc:
            raise ConfigError("managed path escaped its root") from exc
    if current.is_symlink():
        raise ConfigError(f"managed path traverses a symlink: {current}")
    for part in parts:
        current /= part
        if current.is_symlink():
            raise ConfigError(f"managed path traverses a symlink: {current}")


def validate_file_security(
    path: Path,
    *,
    expected_uid: int | None,
    boundary: Path | None = None,
) -> None:
    _reject_symlink_components(path, boundary)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConfigError(f"cannot inspect config: {exc.__class__.__name__}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ConfigError("config must be a regular non-symlink file")
    if metadata.st_mode & 0o077:
        raise ConfigError("config permissions must be 0600 or stricter")
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise ConfigError("config must be owned by the trusted user")


def _parse_text(text: str, allowed: tuple[str, ...], *, require_all: bool) -> dict[str, str]:
    if not text or "\x00" in text or "\r" in text:
        raise ConfigError("config is empty or contains an invalid control character")
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line or line.count("=") != 1:
            raise ConfigError(f"malformed config line {number}")
        key, value = line.split("=", 1)
        if key not in allowed:
            raise ConfigError(f"unknown config key on line {number}: {key}")
        if key in values:
            raise ConfigError(f"duplicate config key on line {number}: {key}")
        if not value or not value.isascii() or any(char.isspace() for char in value):
            raise ConfigError(f"unsafe config value on line {number}: {key}")
        values[key] = value
    if require_all:
        missing = [key for key in allowed if key not in values]
        if missing:
            raise ConfigError(f"missing config key: {missing[0]}")
    return values


def _integer(values: Mapping[str, str], key: str, default: int) -> int:
    raw = values.get(key, str(default))
    if not raw.isascii() or not raw.isdigit():
        raise ConfigError(f"{key} must be an integer")
    parsed = int(raw)
    minimum, maximum = BOUNDS[key]
    if parsed < minimum or parsed > maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    return parsed


def _validate_timing(interval: int, timeout: int) -> None:
    if timeout > interval:
        raise ConfigError("PING_TIMEOUT_SECONDS must not exceed CHECK_INTERVAL_SECONDS")


def global_config_from_mapping(values: Mapping[str, str]) -> GlobalConfig:
    unknown = set(values) - set(GLOBAL_KEYS)
    if unknown:
        raise ConfigError(f"unknown global config key: {sorted(unknown)[0]}")
    mode = values.get("CHECK_MODE", "ping")
    if mode != "ping":
        raise ConfigError("CHECK_MODE must be ping in Watchdog v1")
    defaults = GlobalConfig()
    interval = _integer(values, "CHECK_INTERVAL_SECONDS", defaults.check_interval_seconds)
    timeout = _integer(values, "PING_TIMEOUT_SECONDS", defaults.ping_timeout_seconds)
    _validate_timing(interval, timeout)
    return GlobalConfig(
        check_mode=mode,
        check_interval_seconds=interval,
        ping_timeout_seconds=timeout,
        failure_threshold=_integer(values, "FAILURE_THRESHOLD", defaults.failure_threshold),
        success_threshold=_integer(values, "SUCCESS_THRESHOLD", defaults.success_threshold),
        recovery_hold_seconds=_integer(values, "RECOVERY_HOLD_SECONDS", defaults.recovery_hold_seconds),
        recovery_jitter_max_seconds=_integer(
            values,
            "RECOVERY_JITTER_MAX_SECONDS",
            defaults.recovery_jitter_max_seconds,
        ),
    )


def parse_global_config(text: str) -> GlobalConfig:
    return global_config_from_mapping(_parse_text(text, GLOBAL_KEYS, require_all=True))


def profile_config_from_mapping(
    values: Mapping[str, str],
    global_config: GlobalConfig,
) -> ProfileConfig:
    unknown = set(values) - set(PROFILE_KEYS)
    if unknown:
        raise ConfigError(f"unknown profile config key: {sorted(unknown)[0]}")
    mode = values.get("MODE", "disabled")
    if mode not in MODES:
        raise ConfigError("MODE must be disabled, monitor, or auto")
    interval = _integer(values, "CHECK_INTERVAL_SECONDS", global_config.check_interval_seconds)
    timeout = _integer(values, "PING_TIMEOUT_SECONDS", global_config.ping_timeout_seconds)
    _validate_timing(interval, timeout)
    return ProfileConfig(
        mode=mode,
        check_interval_seconds=interval,
        ping_timeout_seconds=timeout,
        failure_threshold=_integer(values, "FAILURE_THRESHOLD", global_config.failure_threshold),
        success_threshold=_integer(values, "SUCCESS_THRESHOLD", global_config.success_threshold),
        recovery_hold_seconds=_integer(
            values,
            "RECOVERY_HOLD_SECONDS",
            global_config.recovery_hold_seconds,
        ),
        recovery_jitter_max_seconds=_integer(
            values,
            "RECOVERY_JITTER_MAX_SECONDS",
            global_config.recovery_jitter_max_seconds,
        ),
    )


def parse_profile_config(text: str, global_config: GlobalConfig) -> ProfileConfig:
    return profile_config_from_mapping(parse_profile_values(text), global_config)


def parse_profile_values(text: str) -> dict[str, str]:
    return _parse_text(text, PROFILE_KEYS, require_all=False)


def load_global_config(
    path: str | Path = DEFAULT_GLOBAL_CONFIG_PATH,
    *,
    expected_uid: int | None = None,
    boundary: Path | None = None,
) -> GlobalConfig:
    config_path = Path(path)
    validate_file_security(config_path, expected_uid=expected_uid, boundary=boundary)
    try:
        text = config_path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read global config: {exc.__class__.__name__}") from exc
    return parse_global_config(text)


def load_profile_config(
    path: str | Path,
    global_config: GlobalConfig,
    *,
    expected_uid: int | None = None,
    boundary: Path | None = None,
) -> ProfileConfig:
    config_path = Path(path)
    if not config_path.exists() and not config_path.is_symlink():
        return profile_config_from_mapping({}, global_config)
    validate_file_security(config_path, expected_uid=expected_uid, boundary=boundary)
    try:
        text = config_path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read profile config: {exc.__class__.__name__}") from exc
    return parse_profile_config(text, global_config)


def render_global_config(config: GlobalConfig) -> str:
    values = {
        "CHECK_MODE": config.check_mode,
        "CHECK_INTERVAL_SECONDS": str(config.check_interval_seconds),
        "PING_TIMEOUT_SECONDS": str(config.ping_timeout_seconds),
        "FAILURE_THRESHOLD": str(config.failure_threshold),
        "SUCCESS_THRESHOLD": str(config.success_threshold),
        "RECOVERY_HOLD_SECONDS": str(config.recovery_hold_seconds),
        "RECOVERY_JITTER_MAX_SECONDS": str(config.recovery_jitter_max_seconds),
    }
    return "".join(f"{key}={values[key]}\n" for key in GLOBAL_KEYS)


def render_profile_values(values: Mapping[str, str]) -> str:
    profile_config_from_mapping(values, GlobalConfig())
    return "".join(f"{key}={values[key]}\n" for key in PROFILE_KEYS if key in values)


def atomic_write_config(
    path: str | Path,
    text: str,
    *,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
    boundary: Path | None = None,
) -> None:
    destination = Path(path)
    parent = destination.parent
    _reject_symlink_components(parent, boundary)
    if not parent.is_dir() or parent.is_symlink():
        raise ConfigError("config parent must be a real directory")
    if destination.exists() or destination.is_symlink():
        validate_file_security(destination, expected_uid=owner_uid, boundary=boundary)
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw = tempfile.mkstemp(prefix=f".{destination.name}.", dir=parent)
        temporary = Path(raw)
        os.fchmod(descriptor, 0o600)
        if owner_uid is not None:
            os.fchown(descriptor, owner_uid, owner_gid if owner_gid is not None else 0)
        payload = text.encode("ascii")
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"atomic config write failed: {exc.__class__.__name__}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def default_global_config_text() -> str:
    return render_global_config(GlobalConfig())
