"""Bounded Ping execution and exact managed systemd actions."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

from gost_watchdog.models import MAX_PING_WORKERS, ManagedProfile, ProbeResult
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

    def __call__(self, destination: str, timeout_seconds: int) -> ProbeResult:
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
        except FileNotFoundError:
            return ProbeResult("probe_error", "ping_binary_missing")
        except PermissionError:
            return ProbeResult("probe_error", "ping_permission_denied")
        except subprocess.TimeoutExpired:
            return ProbeResult("probe_error", "ping_execution_timeout")
        except (OSError, subprocess.SubprocessError):
            return ProbeResult("probe_error", "ping_execution_failed")
        if result.returncode == 0:
            return ProbeResult("success")
        if result.returncode == 1:
            return ProbeResult("unreachable")
        return ProbeResult("probe_error", "ping_execution_failed")


def _normalize_probe_result(result: object) -> ProbeResult:
    if isinstance(result, ProbeResult):
        return result
    if isinstance(result, bool):
        return ProbeResult("success" if result else "unreachable")
    return ProbeResult("probe_error", "ping_execution_failed")


def run_ping_checks(
    profiles: Iterable[ManagedProfile],
    executor: Callable[[str, int], ProbeResult | bool],
    *,
    max_workers: int = MAX_PING_WORKERS,
) -> dict[str, ProbeResult]:
    active = [profile for profile in profiles if profile.config.mode != "disabled"]
    by_probe: dict[tuple[str, int], list[ManagedProfile]] = {}
    for profile in active:
        key = (profile.kharej_ip, profile.config.ping_timeout_seconds)
        by_probe.setdefault(key, []).append(profile)
    if not by_probe:
        return {}
    workers = max(1, min(max_workers, MAX_PING_WORKERS, len(by_probe)))
    probe_results: dict[tuple[str, int], ProbeResult] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gost-watchdog-ping") as pool:
        futures = {
            pool.submit(executor, destination, timeout): (destination, timeout)
            for destination, timeout in by_probe
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                probe_results[key] = _normalize_probe_result(future.result())
            except Exception:
                probe_results[key] = ProbeResult(
                    "probe_error", "ping_execution_failed"
                )
    return {
        profile.profile_id: probe_results[
            (profile.kharej_ip, profile.config.ping_timeout_seconds)
        ]
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
