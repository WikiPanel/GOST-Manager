"""Bounded Ping execution and exact managed systemd actions."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

from gost_watchdog.models import MAX_PING_WORKERS, ManagedProfile
from gost_watchdog.profiles import validate_kharej_ip, validate_service_name


class CommandError(RuntimeError):
    """A bounded external command could not be executed safely."""


class SubprocessPingExecutor:
    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        ping_binary: str = "ping",
    ) -> None:
        self.runner = runner
        self.ping_binary = ping_binary

    def __call__(self, destination: str, timeout_seconds: int) -> bool:
        validate_kharej_ip(destination)
        argv = [
            self.ping_binary,
            "-n",
            "-c",
            "1",
            "-W",
            str(timeout_seconds),
            "--",
            destination,
        ]
        try:
            result = self.runner(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                shell=False,
                timeout=float(timeout_seconds) + 1.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0


def run_ping_checks(
    profiles: Iterable[ManagedProfile],
    executor: Callable[[str, int], bool],
    *,
    max_workers: int = MAX_PING_WORKERS,
) -> dict[str, bool]:
    active = [profile for profile in profiles if profile.config.mode != "disabled"]
    by_destination: dict[str, list[ManagedProfile]] = {}
    for profile in active:
        by_destination.setdefault(profile.kharej_ip, []).append(profile)
    if not by_destination:
        return {}
    workers = max(1, min(max_workers, MAX_PING_WORKERS, len(by_destination)))
    destination_results: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gost-watchdog-ping") as pool:
        futures = {
            pool.submit(
                executor,
                destination,
                min(profile.config.ping_timeout_seconds for profile in members),
            ): destination
            for destination, members in by_destination.items()
        }
        for future in as_completed(futures):
            destination = futures[future]
            try:
                destination_results[destination] = bool(future.result())
            except Exception:
                destination_results[destination] = False
    return {
        profile.profile_id: destination_results[profile.kharej_ip]
        for profile in active
    }


class SystemdController:
    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        systemctl_binary: str = "systemctl",
    ) -> None:
        self.runner = runner
        self.systemctl_binary = systemctl_binary

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(
                [self.systemctl_binary, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                timeout=15.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CommandError("systemd_command_unavailable") from exc

    def is_active(self, service_name: str) -> bool:
        validate_service_name(service_name)
        result = self._run("is-active", "--quiet", service_name)
        if result.returncode in (0, 3):
            return result.returncode == 0
        raise CommandError("service_state_unavailable")

    def stop(self, service_name: str) -> bool:
        validate_service_name(service_name)
        result = self._run("stop", service_name)
        return result.returncode == 0 and not self.is_active(service_name)

    def start(self, service_name: str) -> bool:
        validate_service_name(service_name)
        result = self._run("start", service_name)
        return result.returncode == 0 and self.is_active(service_name)
