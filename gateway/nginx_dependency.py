"""Explicit opt-in installation and read-only status for the NGINX package."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Sequence

from gateway.errors import ConflictError, OperationalError
from gateway.nginx_inspection import NginxInspector
from gateway.nginx_models import DependencyStatus
from gateway.nginx_paths import NGINX_SERVICE_NAME, NginxPaths
from gateway.paths import reject_symlink_components
from gateway.runtime_inspection import CommandResult


class NginxDependencyManager:
    def __init__(
        self,
        paths: NginxPaths,
        inspector: NginxInspector,
        runner: Callable[[Sequence[str]], CommandResult] | None = None,
    ) -> None:
        self.paths = paths
        self.inspector = inspector
        self.runner = runner or inspector.runner

    def _binary_flags(self) -> tuple[bool, bool, bool, bool, bool]:
        path = self.paths.nginx_bin
        reject_symlink_components(path.parent)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return False, False, False, False, False
        symlink = stat.S_ISLNK(metadata.st_mode)
        regular = stat.S_ISREG(metadata.st_mode)
        executable = bool(metadata.st_mode & 0o111)
        return True, regular, symlink, executable, metadata.st_nlink == 1

    def _distro_state(self) -> tuple[bool, bool, bool]:
        state = self.inspector.service_state("nginx.service")
        return state.loaded, state.enabled, state.active

    def status(self) -> DependencyStatus:
        present, regular, symlink, executable, links = self._binary_flags()
        version = self.inspector.version(self.paths.nginx_bin) if present and regular and not symlink and executable and links else ""
        distro = self._distro_state()
        gateway = self.inspector.service_state(NGINX_SERVICE_NAME)
        return DependencyStatus(
            str(self.paths.nginx_bin), present, regular, symlink, executable, links,
            version, *distro, gateway.loaded, gateway.enabled, gateway.active,
        )

    def install(self, *, yes: bool, is_root: bool | None = None) -> str:
        if not yes:
            raise ConflictError("NGINX package installation requires explicit confirmation")
        if not (os.geteuid() == 0 if is_root is None else is_root):
            raise ConflictError("NGINX package installation requires root")
        before = self.status()
        if before.present:
            if not (before.regular and not before.symlink and before.executable and before.link_count_safe):
                raise ConflictError("existing NGINX binary path is unsafe")
            return "no-op"
        if before.distro_loaded:
            raise ConflictError("pre-existing nginx.service blocks package installation")
        for command in (
            ("apt-get", "update"),
            ("apt-get", "install", "-y", "--no-install-recommends", "nginx"),
        ):
            result = self.runner(command)
            if result.returncode != 0:
                raise OperationalError("NGINX package installation failed; package-manager changes may remain")
        after = self.status()
        if not (
            after.present and after.regular and not after.symlink
            and after.executable and after.link_count_safe
        ):
            raise OperationalError("NGINX package completed but the fixed binary is unavailable")
        if after.distro_active:
            result = self.runner(("systemctl", "stop", "nginx.service"))
            if result.returncode != 0:
                raise ConflictError("new nginx.service could not be stopped")
        if after.distro_enabled:
            result = self.runner(("systemctl", "disable", "nginx.service"))
            if result.returncode != 0:
                raise ConflictError("new nginx.service could not be disabled")
        final = self.status()
        if final.distro_active or final.distro_enabled:
            raise ConflictError("new nginx.service remains active or enabled")
        return "installed"
