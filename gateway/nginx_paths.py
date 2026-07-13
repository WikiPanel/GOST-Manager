"""Validated paths for the dedicated NGINX Gateway runtime."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from gateway.paths import validated_path
from gateway.errors import ValidationError

DEFAULT_NGINX_BIN = "/usr/sbin/nginx"
DEFAULT_GENERATED_DIR = "/etc/gost-manager/generated/gateway/nginx"
DEFAULT_BACKUP_DIR = "/etc/gost-manager/backups/gateway-nginx"
DEFAULT_LOCK_FILE = "/run/gost-manager/gateway-nginx.lock"
DEFAULT_SYSTEMD_DIR = "/etc/systemd/system"
SERVICE_NAME = "gost-nginx-gateway.service"

@dataclass(frozen=True)
class NginxPaths:
    nginx_bin: Path
    generated_dir: Path
    backup_dir: Path
    lock_file: Path
    systemd_dir: Path
    @classmethod
    def from_values(cls, nginx_bin: str|Path=DEFAULT_NGINX_BIN, generated_dir: str|Path=DEFAULT_GENERATED_DIR, backup_dir: str|Path=DEFAULT_BACKUP_DIR, lock_file: str|Path=DEFAULT_LOCK_FILE, systemd_dir: str|Path=DEFAULT_SYSTEMD_DIR) -> "NginxPaths":
        obj=cls(validated_path(nginx_bin,"NGINX binary"), validated_path(generated_dir,"NGINX generated directory"), validated_path(backup_dir,"NGINX backup directory"), validated_path(lock_file,"NGINX lock file"), validated_path(systemd_dir,"systemd directory"))
        if obj.generated_dir == Path('/etc/nginx') or str(obj.generated_dir).startswith('/etc/nginx/'):
            raise ValidationError("dedicated NGINX gateway may not write under /etc/nginx")
        return obj
    @property
    def config_file(self)->Path: return self.generated_dir / "nginx.conf"
    @property
    def manifest_file(self)->Path: return self.generated_dir / "runtime.json"
    @property
    def pid_file(self)->Path: return self.generated_dir / "nginx.pid"
    @property
    def error_log(self)->Path: return self.generated_dir / "error.log"
    @property
    def access_log(self)->Path: return self.generated_dir / "access.log"
    @property
    def unit_file(self)->Path: return self.systemd_dir / SERVICE_NAME
