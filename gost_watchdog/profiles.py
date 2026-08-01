"""Safe discovery of numbered managed Iran profiles."""

from __future__ import annotations

import re
import stat
from collections.abc import Iterable
from pathlib import Path

from gost_watchdog.config import ConfigError, load_profile_config, profile_config_from_mapping
from gost_watchdog.models import GlobalConfig, ManagedProfile


PROFILE_RE = re.compile(r"^iran-([1-9][0-9]*)$")
ENV_RE = re.compile(r"^iran-([1-9][0-9]*)\.env$")
UNIT_RE = re.compile(r"^gost-iran-([1-9][0-9]*)\.service$")
SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
SAFE_TARGET_KEYS = frozenset(("KHAREJ_IP", "TUNNEL_PORT"))


class ProfileError(ValueError):
    """A managed profile is malformed or unsafe."""


def validate_profile_id(profile_id: str) -> str:
    if not PROFILE_RE.fullmatch(profile_id):
        raise ProfileError("profile ID must match iran-[1-9][0-9]*")
    return profile_id


def validate_service_name(service_name: str) -> str:
    if not UNIT_RE.fullmatch(service_name):
        raise ProfileError("service name is not a managed Iran unit")
    return service_name


def validate_kharej_ip(value: str) -> str:
    if not value or len(value) > 253 or not SAFE_HOST_RE.fullmatch(value):
        raise ProfileError("KHAREJ_IP is not a safe IPv4 address or DNS name")
    return value


def validate_tunnel_port(value: str) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise ProfileError("TUNNEL_PORT must be an integer between 1 and 65535")
    port = int(value)
    if port < 1 or port > 65535:
        raise ProfileError("TUNNEL_PORT must be an integer between 1 and 65535")
    return port


def _parse_safe_target_lines(lines: Iterable[str]) -> tuple[str, int | None, str | None]:
    """Extract allowlisted target fields without retaining unrelated values."""
    seen: set[str] = set()
    kharej_ip: str | None = None
    tunnel_port: int | None = None
    target_error: str | None = None
    for number, raw_line in enumerate(lines, 1):
        line = raw_line.removesuffix("\n")
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in SAFE_TARGET_KEYS:
            continue
        if "\x00" in value or "\r" in value:
            raise ProfileError("profile target contains an invalid control character")
        if key in seen:
            raise ProfileError(f"profile line {number} duplicates {key}")
        seen.add(key)
        if key == "KHAREJ_IP":
            kharej_ip = validate_kharej_ip(value)
        elif key == "TUNNEL_PORT":
            try:
                tunnel_port = validate_tunnel_port(value)
            except ProfileError:
                target_error = "invalid_tunnel_port"
    if kharej_ip is None:
        raise ProfileError("profile is missing KHAREJ_IP")
    if "TUNNEL_PORT" not in seen:
        target_error = "missing_tunnel_port"
    return kharej_ip, tunnel_port, target_error


def parse_safe_target_text(text: str) -> tuple[str, int | None, str | None]:
    """Extract only the non-secret Watchdog target fields from env text."""
    return _parse_safe_target_lines(text.splitlines())


def read_safe_target_file(path: Path) -> tuple[str, int | None, str | None]:
    """Stream an env file while retaining only KHAREJ_IP and TUNNEL_PORT."""
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            return _parse_safe_target_lines(stream)
    except (OSError, UnicodeError) as exc:
        raise ProfileError("cannot read managed profile identity") from exc


def parse_kharej_ip_text(text: str) -> str:
    return parse_safe_target_text(text)[0]


def _validate_managed_file(path: Path, *, expected_uid: int | None) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProfileError(f"cannot inspect managed file: {exc.__class__.__name__}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ProfileError("managed file must be regular and non-symlink")
    if metadata.st_mode & 0o022:
        raise ProfileError("managed file may not be group/world writable")
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise ProfileError("managed file must be owned by the trusted user")


def load_managed_profile_identity(
    profile_id: str,
    env_dir: str | Path,
    unit_dir: str | Path,
    config_dir: str | Path,
    global_config: GlobalConfig,
    *,
    expected_uid: int | None = None,
) -> ManagedProfile:
    validate_profile_id(profile_id)
    number = profile_id.removeprefix("iran-")
    service_name = validate_service_name(f"gost-iran-{number}.service")
    env_root = Path(env_dir)
    unit_root = Path(unit_dir)
    config_root = Path(config_dir)
    for directory in (env_root, unit_root, config_root):
        if directory.is_symlink() or not directory.is_dir():
            raise ProfileError("managed directory must be real and non-symlink")
    env_path = env_root / f"{profile_id}.env"
    unit_path = unit_root / service_name
    _validate_managed_file(env_path, expected_uid=expected_uid)
    _validate_managed_file(unit_path, expected_uid=expected_uid)
    kharej_ip, tunnel_port, target_error = read_safe_target_file(env_path)
    return ManagedProfile(
        profile_id=profile_id,
        service_name=service_name,
        kharej_ip=kharej_ip,
        env_path=str(env_path),
        unit_path=str(unit_path),
        config_path=str(config_root / f"{profile_id}.conf"),
        config=profile_config_from_mapping({"MODE": "disabled"}, global_config),
        tunnel_port=tunnel_port,
        target_error=target_error,
    )


def discover_profiles(
    env_dir: str | Path,
    unit_dir: str | Path,
    config_dir: str | Path,
    global_config: GlobalConfig,
    *,
    expected_uid: int | None = None,
    boundary: Path | None = None,
) -> tuple[list[ManagedProfile], list[tuple[str | None, str]]]:
    env_root = Path(env_dir)
    unit_root = Path(unit_dir)
    config_root = Path(config_dir)
    if env_root.is_symlink() or unit_root.is_symlink() or config_root.is_symlink():
        return [], [(None, "unsafe_directory")]
    if not env_root.is_dir() or not unit_root.is_dir() or not config_root.is_dir():
        return [], []
    profiles: list[ManagedProfile] = []
    errors: list[tuple[str | None, str]] = []
    candidates = sorted(
        env_root.glob("iran-*.env"),
        key=lambda item: int(ENV_RE.fullmatch(item.name).group(1))
        if ENV_RE.fullmatch(item.name)
        else 0,
    )
    for env_path in candidates:
        match = ENV_RE.fullmatch(env_path.name)
        if not match:
            continue
        number = match.group(1)
        profile_id = f"iran-{number}"
        service_name = f"gost-iran-{number}.service"
        unit_path = unit_root / service_name
        config_path = config_root / f"{profile_id}.conf"
        try:
            _validate_managed_file(env_path, expected_uid=expected_uid)
            _validate_managed_file(unit_path, expected_uid=expected_uid)
            kharej_ip, tunnel_port, target_error = read_safe_target_file(env_path)
            config = load_profile_config(
                config_path,
                global_config,
                expected_uid=expected_uid,
                boundary=boundary,
            )
        except (OSError, UnicodeError, ConfigError, ProfileError):
            errors.append((profile_id, "invalid_profile"))
            continue
        if config.check_mode == "tcp" and target_error is not None:
            errors.append((profile_id, target_error))
        profiles.append(
            ManagedProfile(
                profile_id=profile_id,
                service_name=service_name,
                kharej_ip=kharej_ip,
                env_path=str(env_path),
                unit_path=str(unit_path),
                config_path=str(config_path),
                config=config,
                tunnel_port=tunnel_port,
                target_error=target_error,
            )
        )
    return profiles, errors
