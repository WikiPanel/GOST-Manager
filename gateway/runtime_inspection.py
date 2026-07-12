"""Injectable systemd and socket inspection without shell execution."""

from __future__ import annotations

import re
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from gateway.errors import OperationalError, ValidationError
from gateway.runtime_models import Listener, ServiceState
from gateway.runtime_paths import service_name

PID_RE = re.compile(r"pid=([1-9][0-9]*)")
NAME_RE = re.compile(r'\(\("([^"\\]+)"')


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def run_command(argv: Sequence[str]) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv), check=False, capture_output=True, text=True, shell=False
        )
    except OSError as exc:
        raise OperationalError("gateway runtime command is unavailable") from exc
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _split_endpoint(value: str) -> tuple[str, int]:
    text = value.strip()
    if text.startswith("["):
        closing = text.rfind("]:")
        if closing < 0:
            raise ValidationError("listener snapshot is malformed")
        address, port_text = text[1:closing], text[closing + 2 :]
    else:
        if ":" not in text:
            raise ValidationError("listener snapshot is malformed")
        address, port_text = text.rsplit(":", 1)
    try:
        port = int(port_text, 10)
    except ValueError as exc:
        raise ValidationError("listener snapshot is malformed") from exc
    if not 1 <= port <= 65535:
        raise ValidationError("listener snapshot is malformed")
    return address or "*", port


def parse_ss_listeners(output: str) -> tuple[Listener, ...]:
    listeners: list[Listener] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        fields = line.split(None, 5)
        if len(fields) < 4:
            raise ValidationError("listener snapshot is malformed")
        # `ss -H -lntp` may include or omit the LISTEN state column by version.
        local_index = 3 if fields[0] == "LISTEN" else 2
        if len(fields) <= local_index:
            raise ValidationError("listener snapshot is malformed")
        address, port = _split_endpoint(fields[local_index])
        pids = tuple(sorted({int(value) for value in PID_RE.findall(line)}))
        names = tuple(sorted(set(NAME_RE.findall(line))))
        listeners.append(Listener(address, port, pids, names))
    return tuple(listeners)


def verify_proc_listener(address: str, port: int, pid: int) -> bool:
    """Prove an IPv4 LISTEN inode is referenced by the exact process."""

    if address != "127.0.0.1" or pid <= 0 or not 1 <= port <= 65535:
        return False
    expected_local = f"0100007F:{port:04X}"
    inodes: set[str] = set()
    try:
        with open("/proc/net/tcp", "r", encoding="ascii") as handle:
            for raw in handle:
                fields = raw.split()
                if len(fields) >= 10 and fields[1] == expected_local and fields[3] == "0A":
                    inodes.add(fields[9])
    except (OSError, UnicodeError):
        return False
    if not inodes:
        return False
    try:
        with os.scandir(f"/proc/{pid}/fd") as entries:
            for entry in entries:
                try:
                    target = os.readlink(entry.path)
                except OSError:
                    continue
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    return True
    except OSError:
        return False
    return False


class RuntimeInspector:
    def __init__(
        self,
        runner: Callable[[Sequence[str]], CommandResult] = run_command,
        listener_verifier: Callable[[str, int, int], bool] = verify_proc_listener,
    ) -> None:
        self.runner = runner
        self.listener_verifier = listener_verifier
        self.listener_calls = 0

    def listeners(self) -> tuple[Listener, ...]:
        self.listener_calls += 1
        result = self.runner(("ss", "-H", "-lntp"))
        if result.returncode != 0:
            raise OperationalError("listener ownership snapshot failed")
        return parse_ss_listeners(result.stdout)

    def service_state(self, exit_id: str) -> ServiceState:
        name = service_name(exit_id)
        result = self.runner(
            (
                "systemctl", "--no-pager", "show", name,
                "--property=LoadState,UnitFileState,ActiveState,SubState,MainPID",
            )
        )
        if result.returncode != 0:
            return ServiceState(name, False, False, False, None)
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        try:
            pid = int(values.get("MainPID", "0"), 10)
        except ValueError:
            pid = 0
        return ServiceState(
            name,
            values.get("LoadState") == "loaded",
            values.get("UnitFileState") in {"enabled", "enabled-runtime", "linked"},
            values.get("ActiveState") in {"active", "activating", "reloading"},
            pid if pid > 0 else None,
        )

    def systemctl(self, *arguments: str) -> None:
        result = self.runner(("systemctl", *arguments))
        if result.returncode != 0:
            raise OperationalError("gateway service operation failed")

    def verify_unit(self, path: str) -> None:
        try:
            result = self.runner(("systemd-analyze", "verify", path))
        except OperationalError:
            return
        if result.returncode != 0:
            raise ValidationError("generated gateway unit failed systemd verification")

    def verify_service_listener(self, address: str, port: int, pid: int) -> None:
        if not self.listener_verifier(address, port, pid):
            raise OperationalError("gateway service listener ownership verification failed")
