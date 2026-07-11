"""Legacy Direct Mode tunnel discovery without secret exposure."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Iterable
from pathlib import Path

from monitoring.models import Clock, Event, Tunnel

DEFAULT_ENV_DIR = "/etc/gost"
ENV_RE = re.compile(r"^(iran|kharej)-([1-9][0-9]*)\.env$")
SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")


def parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"line {lineno}: missing '='")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"line {lineno}: invalid key")
        try:
            parts = shlex.split(raw_value, posix=True)
        except ValueError as exc:
            raise ValueError(f"line {lineno}: malformed value") from exc
        values[key] = parts[0] if parts else ""
    return values


def parse_env_file(
    path: str | Path,
    read_text: Callable[[Path], str] | None = None,
) -> dict[str, str]:
    reader = read_text or (lambda item: item.read_text(encoding="utf-8"))
    return parse_env_text(reader(Path(path)))


def parse_port(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,4}", value):
        raise ValueError("invalid port")
    port = int(value)
    if port > 65535:
        raise ValueError("invalid port")
    return port


_port = parse_port


def parse_mappings(value: str) -> tuple[tuple[int, int], ...]:
    if not value or value.startswith(",") or value.endswith(",") or ",," in value:
        raise ValueError("MAPPINGS must use listen:target")
    mappings: list[tuple[int, int]] = []
    seen: set[int] = set()
    for raw in value.split(","):
        item = raw.strip()
        if not re.fullmatch(r"[0-9]+:[0-9]+", item):
            raise ValueError("invalid mapping")
        listen_raw, target_raw = item.split(":", 1)
        listen = parse_port(listen_raw)
        target = parse_port(target_raw)
        if listen in seen:
            raise ValueError(f"duplicate listen port: {listen}")
        seen.add(listen)
        mappings.append((listen, target))
    return tuple(mappings)


def safe_remote_endpoint(values: dict[str, str]) -> str | None:
    host = values.get("KHAREJ_IP", "")
    port = values.get("TUNNEL_PORT", "")
    if not host or not port:
        return None
    if not SAFE_HOST_RE.fullmatch(host):
        raise ValueError("invalid remote host")
    return f"{host}:{parse_port(port)}"


def tunnel_from_env(
    path: str | Path,
    read_text: Callable[[Path], str] | None = None,
) -> Tunnel:
    env_path = Path(path)
    match = ENV_RE.match(env_path.name)
    if not match:
        raise ValueError(f"unsupported env name: {env_path.name}")
    side = match.group(1)
    number = int(match.group(2))
    values = parse_env_file(env_path, read_text)
    service = f"gost-{side}-{number}.service"
    if side == "iran":
        mappings = parse_mappings(values.get("MAPPINGS", ""))
        return Tunnel(
            side,
            number,
            service,
            str(env_path),
            tuple(listen for listen, _target in mappings),
            tuple(target for _listen, target in mappings),
            safe_remote_endpoint(values),
        )
    return Tunnel(
        side,
        number,
        service,
        str(env_path),
        (parse_port(values.get("TUNNEL_PORT", "")),),
        (),
    )


def discover_tunnels(
    env_dir: str | Path = DEFAULT_ENV_DIR,
    clock: Clock = Clock(),
    paths: Iterable[Path] | None = None,
    read_text: Callable[[Path], str] | None = None,
) -> tuple[list[Tunnel], list[Event]]:
    root = Path(env_dir)
    candidates = paths
    if candidates is None:
        if not root.exists():
            return [], []
        candidates = root.glob("*.env")
    tunnels: list[Tunnel] = []
    events: list[Event] = []
    now = int(clock.wall())
    for path in sorted(candidates):
        if not ENV_RE.match(path.name):
            continue
        try:
            tunnels.append(tunnel_from_env(path, read_text))
        except Exception as exc:
            events.append(
                Event(
                    now,
                    "warning",
                    "env_parse_error",
                    f"Skipping malformed env file {path.name}",
                    {"path": str(path), "reason": exc.__class__.__name__},
                )
            )
    return tunnels, events
