"""Strict, non-executable configuration for the monitoring subsystem."""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Mapping
from pathlib import Path

from monitoring.collector import CollectorConfig
from monitoring.scheduler import MAINTENANCE_INTERVAL_SECONDS
from monitoring.schema import DEFAULT_DB_PATH, DEFAULT_SAMPLE_INTERVAL_SECONDS


DEFAULT_CONFIG_PATH = "/etc/gost-manager/monitoring.env"
KEY_DB = "GOST_MONITOR_DB"
KEY_ENV_DIR = "GOST_ENV_DIR"
KEY_SAMPLE = "GOST_MONITOR_SAMPLE_INTERVAL"
KEY_TCP = "GOST_MONITOR_TCP_INTERVAL"
KEY_SLOW = "GOST_MONITOR_SLOW_INTERVAL"
KEY_MAINTENANCE = "GOST_MONITOR_MAINTENANCE_INTERVAL"
ALLOWED_KEYS = (
    KEY_DB,
    KEY_ENV_DIR,
    KEY_SAMPLE,
    KEY_TCP,
    KEY_SLOW,
    KEY_MAINTENANCE,
)
INTERVAL_BOUNDS = {
    KEY_SAMPLE: (5, 60),
    KEY_TCP: (10, 300),
    KEY_SLOW: (30, 900),
    KEY_MAINTENANCE: (300, 86400),
}
SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/+:-]+$")
UNSAFE_VALUE_RE = re.compile(r"[\s\x00\r\n'\"`$;&|<>\\(){}\[\]]")


class ConfigError(ValueError):
    """Configuration is malformed or outside the supported safety bounds."""


@dataclasses.dataclass(frozen=True)
class MonitoringConfig:
    db_path: str = DEFAULT_DB_PATH
    env_dir: str = "/etc/gost"
    sample_interval: int = int(DEFAULT_SAMPLE_INTERVAL_SECONDS)
    tcp_interval: int = 30
    slow_interval: int = 60
    maintenance_interval: int = int(MAINTENANCE_INTERVAL_SECONDS)

    def as_mapping(self) -> dict[str, str]:
        return {
            KEY_DB: self.db_path,
            KEY_ENV_DIR: self.env_dir,
            KEY_SAMPLE: str(self.sample_interval),
            KEY_TCP: str(self.tcp_interval),
            KEY_SLOW: str(self.slow_interval),
            KEY_MAINTENANCE: str(self.maintenance_interval),
        }

    def collector_config(self) -> CollectorConfig:
        return CollectorConfig(
            sample_interval=float(self.sample_interval),
            tcp_snapshot_interval=float(self.tcp_interval),
            slow_sample_interval=float(self.slow_interval),
            maintenance_interval=float(self.maintenance_interval),
        )


DEFAULT_CONFIG = MonitoringConfig()


def default_config_text() -> str:
    values = DEFAULT_CONFIG.as_mapping()
    return "".join(f"{key}={values[key]}\n" for key in ALLOWED_KEYS)


def _validate_path(value: str, label: str) -> str:
    if not value or UNSAFE_VALUE_RE.search(value) or not SAFE_PATH_RE.fullmatch(value):
        raise ConfigError(f"{label} must be a safe absolute path")
    normalized = os.path.normpath(value)
    if not os.path.isabs(value) or normalized != value:
        raise ConfigError(f"{label} must be normalized and absolute")
    return value


def _validate_interval(key: str, value: str) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise ConfigError(f"{key} must be an integer")
    parsed = int(value)
    minimum, maximum = INTERVAL_BOUNDS[key]
    if parsed < minimum or parsed > maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum} seconds")
    return parsed


def config_from_mapping(
    values: Mapping[str, object],
    *,
    require_all: bool = False,
) -> MonitoringConfig:
    unknown = sorted(set(values) - set(ALLOWED_KEYS))
    if unknown:
        raise ConfigError(f"unknown monitoring config key: {unknown[0]}")
    merged = DEFAULT_CONFIG.as_mapping()
    for key, raw in values.items():
        value = str(raw)
        if not value:
            raise ConfigError(f"{key} may not be empty")
        merged[key] = value
    if require_all:
        missing = [key for key in ALLOWED_KEYS if key not in values]
        if missing:
            raise ConfigError(f"missing monitoring config key: {missing[0]}")

    config = MonitoringConfig(
        db_path=_validate_path(merged[KEY_DB], KEY_DB),
        env_dir=_validate_path(merged[KEY_ENV_DIR], KEY_ENV_DIR),
        sample_interval=_validate_interval(KEY_SAMPLE, merged[KEY_SAMPLE]),
        tcp_interval=_validate_interval(KEY_TCP, merged[KEY_TCP]),
        slow_interval=_validate_interval(KEY_SLOW, merged[KEY_SLOW]),
        maintenance_interval=_validate_interval(
            KEY_MAINTENANCE, merged[KEY_MAINTENANCE]
        ),
    )
    if config.tcp_interval < config.sample_interval:
        raise ConfigError("GOST_MONITOR_TCP_INTERVAL may not be less than the sample interval")
    if config.slow_interval < config.sample_interval:
        raise ConfigError("GOST_MONITOR_SLOW_INTERVAL may not be less than the sample interval")
    if config.maintenance_interval < config.slow_interval:
        raise ConfigError(
            "GOST_MONITOR_MAINTENANCE_INTERVAL may not be less than the slow interval"
        )
    return config


def parse_config_text(text: str) -> MonitoringConfig:
    if "\x00" in text:
        raise ConfigError("monitoring config contains a NUL byte")
    values: dict[str, str] = {}
    lines = text.splitlines()
    if not lines:
        raise ConfigError("monitoring config is empty")
    for number, line in enumerate(lines, 1):
        if not line or line.count("=") != 1:
            raise ConfigError(f"malformed monitoring config line {number}")
        key, value = line.split("=", 1)
        if key not in ALLOWED_KEYS:
            raise ConfigError(f"unknown monitoring config key on line {number}: {key}")
        if key in values:
            raise ConfigError(f"duplicate monitoring config key on line {number}: {key}")
        if not value:
            raise ConfigError(f"empty monitoring config value on line {number}: {key}")
        if UNSAFE_VALUE_RE.search(value):
            raise ConfigError(f"unsafe monitoring config value on line {number}: {key}")
        values[key] = value
    return config_from_mapping(values, require_all=True)


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> MonitoringConfig:
    config_path = Path(path)
    if config_path.is_symlink():
        raise ConfigError("monitoring config path may not be a symlink")
    try:
        raw = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read monitoring config: {exc.__class__.__name__}") from exc
    return parse_config_text(raw)


def config_from_environment(environment: Mapping[str, str] | None = None) -> MonitoringConfig:
    source = os.environ if environment is None else environment
    values = {key: source[key] for key in ALLOWED_KEYS if key in source}
    return config_from_mapping(values)
