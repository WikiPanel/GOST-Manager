"""Bounded health probes and exact managed systemd actions."""

from __future__ import annotations

import errno
import socket
import subprocess
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

from gost_watchdog.models import MAX_PROBE_WORKERS, ManagedProfile, ProbeResult
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


TCP_UNREACHABLE_ERRNOS = frozenset(
    (
        errno.ECONNREFUSED,
        errno.ETIMEDOUT,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.ECONNRESET,
    )
)
TCP_LOCAL_RESOURCE_ERRNOS = frozenset(
    (
        errno.EMFILE,
        errno.ENFILE,
        errno.ENOMEM,
        errno.EACCES,
        errno.EPERM,
        errno.ENOBUFS,
    )
)


class TcpConnectExecutor:
    def __init__(
        self,
        connector: Callable[..., socket.socket] = socket.create_connection,
    ) -> None:
        self.connector = connector

    def __call__(self, destination: str, port: int, timeout_seconds: int) -> ProbeResult:
        validate_kharej_ip(destination)
        if port < 1 or port > 65535:
            return ProbeResult("probe_error", "invalid_tcp_target")
        try:
            connection = self.connector(
                (destination, port),
                timeout=float(timeout_seconds),
            )
            connection.close()
            return ProbeResult("success")
        except (TimeoutError, socket.timeout):
            return ProbeResult("unreachable")
        except socket.gaierror:
            return ProbeResult("probe_error", "tcp_name_resolution_failed")
        except OSError as exc:
            if exc.errno in TCP_UNREACHABLE_ERRNOS:
                return ProbeResult("unreachable")
            if exc.errno in TCP_LOCAL_RESOURCE_ERRNOS:
                return ProbeResult("probe_error", "tcp_local_resource_error")
            return ProbeResult("probe_error", "tcp_socket_error")


def _normalize_probe_result(result: object, mode: str) -> ProbeResult:
    if isinstance(result, ProbeResult):
        return result
    if isinstance(result, bool):
        return ProbeResult("success" if result else "unreachable")
    return ProbeResult("probe_error", f"{mode}_execution_failed")


def run_checks(
    profiles: Iterable[ManagedProfile],
    ping_executor: Callable[[str, int], ProbeResult | bool],
    tcp_executor: Callable[[str, int, int], ProbeResult | bool],
    *,
    max_workers: int = MAX_PROBE_WORKERS,
) -> dict[str, ProbeResult]:
    active = [profile for profile in profiles if profile.config.mode != "disabled"]
    by_probe: dict[tuple[str, str, int | None, int], list[ManagedProfile]] = {}
    results: dict[str, ProbeResult] = {}
    for profile in active:
        timeout = profile.config.ping_timeout_seconds
        if profile.config.check_mode == "tcp" and (
            profile.target_error is not None or profile.tunnel_port is None
        ):
            results[profile.profile_id] = ProbeResult(
                "probe_error", "invalid_tcp_target"
            )
            continue
        port = profile.tunnel_port if profile.config.check_mode == "tcp" else None
        key = (profile.config.check_mode, profile.kharej_ip, port, timeout)
        by_probe.setdefault(key, []).append(profile)
    if not by_probe:
        return results
    workers = max(1, min(max_workers, MAX_PROBE_WORKERS, len(by_probe)))
    probe_results: dict[tuple[str, str, int | None, int], ProbeResult] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gost-watchdog-probe") as pool:
        futures = {}
        for key in by_probe:
            mode, destination, port, timeout = key
            if mode == "tcp" and port is not None:
                future = pool.submit(tcp_executor, destination, port, timeout)
            else:
                future = pool.submit(ping_executor, destination, timeout)
            futures[future] = key
        for future in as_completed(futures):
            key = futures[future]
            try:
                probe_results[key] = _normalize_probe_result(future.result(), key[0])
            except Exception:
                probe_results[key] = ProbeResult(
                    "probe_error", f"{key[0]}_execution_failed"
                )
    for key, matching_profiles in by_probe.items():
        for profile in matching_profiles:
            results[profile.profile_id] = probe_results[key]
    return results


def run_ping_checks(
    profiles: Iterable[ManagedProfile],
    executor: Callable[[str, int], ProbeResult | bool],
    *,
    max_workers: int = MAX_PROBE_WORKERS,
) -> dict[str, ProbeResult]:
    return run_checks(
        profiles,
        executor,
        TcpConnectExecutor(),
        max_workers=max_workers,
    )


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
