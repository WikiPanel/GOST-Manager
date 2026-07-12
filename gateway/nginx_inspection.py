"""Injectable NGINX dependency, systemd, listener, cgroup, and status inspection."""

from __future__ import annotations

import http.client
import os
import re
import stat
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from gateway.errors import OperationalError, ValidationError
from gateway.nginx_models import NginxServiceState, StubStatus
from gateway.nginx_paths import NGINX_SERVICE_NAME
from gateway.runtime_inspection import CommandResult, parse_ss_listeners
from gateway.runtime_models import Listener


CGROUP_RE = re.compile(r"^/(?:[A-Za-z0-9_.@:-]+/)*[A-Za-z0-9_.@:-]+$")
STATUS_RE = re.compile(
    r"^Active connections:\s*([0-9]+)\s*\n"
    r"\s*server accepts handled requests\s*\n"
    r"\s*([0-9]+)\s+([0-9]+)\s+([0-9]+)\s*\n"
    r"Reading:\s*([0-9]+)\s+Writing:\s*([0-9]+)\s+Waiting:\s*([0-9]+)\s*$"
)


def run_command(argv: Sequence[str]) -> CommandResult:
    try:
        result = subprocess.run(
            list(argv), check=False, capture_output=True, text=True, shell=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperationalError("NGINX Gateway command is unavailable") from exc
    return CommandResult(result.returncode, result.stdout, result.stderr)


def parse_cgroup_pids(data: str) -> tuple[int, ...]:
    result: set[int] = set()
    for raw in data.splitlines():
        value = raw.strip()
        if not value or not value.isdigit() or int(value) <= 0:
            raise ValidationError("service cgroup PID list is malformed")
        result.add(int(value))
    if not result:
        raise ValidationError("service cgroup PID list is empty")
    return tuple(sorted(result))


def parse_stub_status(data: str) -> StubStatus:
    match = STATUS_RE.fullmatch(data.strip())
    if match is None:
        raise ValidationError("NGINX status response is malformed")
    values = tuple(int(value) for value in match.groups())
    return StubStatus(*values)


def _http_probe(port: int, timeout: float) -> str:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "/nginx_status", headers={"Host": "localhost"})
        response = connection.getresponse()
        data = response.read(4097)
        if response.status != 200 or len(data) > 4096:
            raise OperationalError("NGINX status probe failed")
        return data.decode("ascii")
    except (OSError, UnicodeDecodeError, http.client.HTTPException) as exc:
        raise OperationalError("NGINX status probe failed") from exc
    finally:
        connection.close()


class NginxInspector:
    def __init__(
        self,
        runner: Callable[[Sequence[str]], CommandResult] = run_command,
        *,
        cgroup_root: str | Path = "/sys/fs/cgroup",
        cgroup_reader: Callable[[Path], str] | None = None,
        status_reader: Callable[[int, float], str] = _http_probe,
    ) -> None:
        self.runner = runner
        self.cgroup_root = Path(cgroup_root)
        self.cgroup_reader = cgroup_reader or (
            lambda path: path.read_text(encoding="ascii")
        )
        self.status_reader = status_reader
        self.listener_calls = 0

    def command(self, *argv: str, allow_output: bool = False) -> CommandResult:
        result = self.runner(argv)
        if result.returncode != 0:
            raise OperationalError("NGINX Gateway command failed")
        if not allow_output and "[warn]" in result.stderr.lower():
            raise ValidationError("NGINX validation emitted a warning")
        return result

    def listeners(self) -> tuple[Listener, ...]:
        self.listener_calls += 1
        result = self.runner(("ss", "-H", "-lntp"))
        if result.returncode != 0:
            raise OperationalError("NGINX listener snapshot failed")
        return parse_ss_listeners(result.stdout)

    def _read_cgroup(self, value: str) -> tuple[tuple[int, ...], bool]:
        if not value or not CGROUP_RE.fullmatch(value) or ".." in value.split("/"):
            return (), False
        root = self.cgroup_root.resolve()
        path = (root / value.lstrip("/") / "cgroup.procs")
        try:
            parent = path.parent.resolve()
            parent.relative_to(root)
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                return (), False
            return parse_cgroup_pids(self.cgroup_reader(path)), True
        except (OSError, ValueError, ValidationError):
            return (), False

    def service_state(self, service_name: str = NGINX_SERVICE_NAME) -> NginxServiceState:
        result = self.runner(
            (
                "systemctl", "--no-pager", "show", service_name,
                "--property=LoadState,UnitFileState,ActiveState,SubState,MainPID,ControlGroup,FragmentPath",
            )
        )
        if result.returncode != 0:
            return NginxServiceState(False, False, False, "not-found", None, "", "", (), False)
        values: dict[str, str] = {}
        for raw in result.stdout.splitlines():
            if "=" in raw:
                key, value = raw.split("=", 1)
                values[key] = value
        try:
            pid = int(values.get("MainPID", "0"), 10)
        except ValueError:
            pid = 0
        pids, authoritative = self._read_cgroup(values.get("ControlGroup", ""))
        active = values.get("ActiveState") in {"active", "activating", "reloading"}
        return NginxServiceState(
            values.get("LoadState") == "loaded",
            values.get("UnitFileState") in {"enabled", "enabled-runtime", "linked"},
            active,
            values.get("SubState", "unknown"),
            pid if pid > 0 else None,
            values.get("ControlGroup", ""),
            values.get("FragmentPath", ""),
            pids,
            authoritative,
        )

    def systemctl(self, action: str) -> None:
        allowed = {"enable", "disable", "start", "stop", "reload", "restart"}
        if action not in allowed:
            raise ValidationError("unsupported NGINX service action")
        result = self.runner(("systemctl", action, NGINX_SERVICE_NAME))
        if result.returncode != 0:
            raise OperationalError("NGINX Gateway service operation failed")

    def nginx_test(self, binary: Path, config: Path) -> None:
        result = self.runner((str(binary), "-t", "-q", "-p", "/", "-c", str(config)))
        if result.returncode != 0 or "[warn]" in result.stderr.lower():
            raise ValidationError("NGINX candidate validation failed")

    def version(self, binary: Path) -> str:
        result = self.runner((str(binary), "-v"))
        if result.returncode != 0:
            raise OperationalError("NGINX version check failed")
        text = (result.stderr or result.stdout).strip()
        return text[:200]

    def status(self, port: int, timeout: float = 2.0) -> StubStatus:
        return parse_stub_status(self.status_reader(port, timeout))


def listener_owned_by_service(listener: Listener, state: NginxServiceState) -> bool:
    return bool(
        state.active
        and state.main_pid is not None
        and state.pids_authoritative
        and state.pids
        and listener.pids
        and set(listener.pids).issubset(set(state.pids))
    )
