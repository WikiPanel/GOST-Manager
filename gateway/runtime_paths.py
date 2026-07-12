"""Validated production paths and exact managed runtime names."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from gateway.errors import ValidationError
from gateway.paths import validated_path
from gateway.validation import validate_secret_ref, validate_slug

DEFAULT_SECRET_DIR = "/etc/gost-manager/secrets"
DEFAULT_GENERATED_DIR = "/etc/gost-manager/generated/gateway"
DEFAULT_RUNTIME_BACKUP_DIR = "/etc/gost-manager/backups/gateway-runtime"
DEFAULT_RUNTIME_LOCK_FILE = "/run/gost-manager/gateway-runtime.lock"
DEFAULT_SYSTEMD_DIR = "/etc/systemd/system"
DEFAULT_RUNNER_PATH = "/usr/local/lib/gost-manager/gost-run-gateway-exit.sh"
DEFAULT_GOST_BIN = "/usr/local/bin/gost"

SERVICE_RE = re.compile(r"^gost-gateway-exit-([a-z][a-z0-9-]{0,62})\.service$")
SECRET_FILE_RE = re.compile(r"^([a-z][a-z0-9-]{0,63})\.env$")
ENV_FILE_RE = re.compile(r"^([a-z][a-z0-9-]{0,62})\.env$")


def service_name(exit_id: str) -> str:
    return f"gost-gateway-exit-{validate_slug(exit_id, 'exit ID')}.service"


def exit_id_from_service(value: str) -> str | None:
    match = SERVICE_RE.fullmatch(value)
    return match.group(1) if match else None


@dataclass(frozen=True)
class RuntimePaths:
    secret_dir: Path
    generated_dir: Path
    runtime_backup_dir: Path
    runtime_lock_file: Path
    systemd_dir: Path
    runner_path: Path
    gost_bin: Path

    @classmethod
    def from_values(
        cls,
        secret_dir: str | Path = DEFAULT_SECRET_DIR,
        generated_dir: str | Path = DEFAULT_GENERATED_DIR,
        runtime_backup_dir: str | Path = DEFAULT_RUNTIME_BACKUP_DIR,
        runtime_lock_file: str | Path = DEFAULT_RUNTIME_LOCK_FILE,
        systemd_dir: str | Path = DEFAULT_SYSTEMD_DIR,
        runner_path: str | Path = DEFAULT_RUNNER_PATH,
        gost_bin: str | Path = DEFAULT_GOST_BIN,
    ) -> "RuntimePaths":
        result = cls(
            secret_dir=validated_path(secret_dir, "secret directory"),
            generated_dir=validated_path(generated_dir, "generated directory"),
            runtime_backup_dir=validated_path(runtime_backup_dir, "runtime backup directory"),
            runtime_lock_file=validated_path(runtime_lock_file, "runtime lock file"),
            systemd_dir=validated_path(systemd_dir, "systemd directory"),
            runner_path=validated_path(runner_path, "runner path"),
            gost_bin=validated_path(gost_bin, "GOST binary"),
        )
        if len(set(result.__dict__.values())) != len(result.__dict__):
            raise ValidationError("gateway runtime paths must be different")
        return result

    @property
    def exits_dir(self) -> Path:
        return self.generated_dir / "exits"

    @property
    def manifest_file(self) -> Path:
        return self.generated_dir / "runtime.json"

    def secret_file(self, secret_ref: str) -> Path:
        validate_secret_ref(secret_ref, True)
        return self.secret_dir / f"{secret_ref}.env"

    def env_file(self, exit_id: str) -> Path:
        return self.exits_dir / f"{validate_slug(exit_id, 'exit ID')}.env"

    def unit_file(self, exit_id: str) -> Path:
        return self.systemd_dir / service_name(exit_id)
