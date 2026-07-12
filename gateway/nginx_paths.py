"""Fixed and validated paths for the dedicated NGINX Gateway."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gateway.errors import ValidationError
from gateway.paths import validated_path


DEFAULT_NGINX_DIR = "/etc/gost-manager/generated/gateway/nginx"
DEFAULT_NGINX_BACKUP_DIR = "/etc/gost-manager/backups/nginx-gateway"
DEFAULT_NGINX_LOCK_FILE = "/run/gost-manager/nginx-gateway.lock"
DEFAULT_NGINX_RUNTIME_DIR = "/run/gost-manager-nginx"
DEFAULT_NGINX_UNIT = "/etc/systemd/system/gost-nginx-gateway.service"
DEFAULT_NGINX_RUNNER = "/usr/local/lib/gost-manager/gost-run-nginx-gateway.sh"
DEFAULT_NGINX_BIN = "/usr/sbin/nginx"
DEFAULT_NGINX_LAUNCHER = "/usr/local/sbin/gost-gateway-nginx"
NGINX_SERVICE_NAME = "gost-nginx-gateway.service"


@dataclass(frozen=True)
class NginxPaths:
    generated_dir: Path
    backup_dir: Path
    lock_file: Path
    runtime_dir: Path
    unit_file: Path
    runner_path: Path
    nginx_bin: Path
    launcher_path: Path

    @classmethod
    def from_values(
        cls,
        generated_dir: str | Path = DEFAULT_NGINX_DIR,
        backup_dir: str | Path = DEFAULT_NGINX_BACKUP_DIR,
        lock_file: str | Path = DEFAULT_NGINX_LOCK_FILE,
        runtime_dir: str | Path = DEFAULT_NGINX_RUNTIME_DIR,
        unit_file: str | Path = DEFAULT_NGINX_UNIT,
        runner_path: str | Path = DEFAULT_NGINX_RUNNER,
        nginx_bin: str | Path = DEFAULT_NGINX_BIN,
        launcher_path: str | Path = DEFAULT_NGINX_LAUNCHER,
    ) -> "NginxPaths":
        result = cls(
            validated_path(generated_dir, "NGINX generated directory"),
            validated_path(backup_dir, "NGINX backup directory"),
            validated_path(lock_file, "NGINX lock file"),
            validated_path(runtime_dir, "NGINX runtime directory"),
            validated_path(unit_file, "NGINX unit file"),
            validated_path(runner_path, "NGINX runner path"),
            validated_path(nginx_bin, "NGINX binary path"),
            validated_path(launcher_path, "NGINX launcher path"),
        )
        if len(set(result.__dict__.values())) != len(result.__dict__):
            raise ValidationError("NGINX Gateway paths must be different")
        return result

    @property
    def config_file(self) -> Path:
        return self.generated_dir / "nginx.conf"

    @property
    def manifest_file(self) -> Path:
        return self.generated_dir / "runtime.json"

    @property
    def pid_file(self) -> Path:
        return self.runtime_dir / "nginx.pid"
