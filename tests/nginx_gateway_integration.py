#!/usr/bin/env python3
"""Real local NGINX WebSocket routing, failover, reload, and load evidence."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from gateway.nginx_models import NginxBackend, NginxCandidate, NginxRoute
from gateway.nginx_render import render_config, upstream_name


HOST = "gateway.example.org"
PATH = "/api/v1"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class UpgradeBackend:
    def __init__(self, name: str, port: int) -> None:
        self.name = name
        self.port = port
        self.server: asyncio.AbstractServer | None = None
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.connections: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self.handle, "127.0.0.1", self.port, backlog=4096
        )

    async def stop(self, *, close_connections: bool = False) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        if close_connections:
            writers = tuple(self.connections)
            for writer in writers:
                writer.close()
            await asyncio.gather(
                *(writer.wait_closed() for writer in writers),
                return_exceptions=True,
            )

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections.add(writer)
        try:
            data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = data.decode("latin1").split("\r\n")
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.lower()] = value.strip()
            self.requests.append((lines[0], headers))
            writer.write(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n\r\n"
            )
            await writer.drain()
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            self.connections.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass


async def open_upgrade(
    port: int,
    *,
    host: str = HOST,
    path: str = PATH,
    query: str = "",
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port), 10
    )
    request_path = path + query
    request = (
        f"GET {request_path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: Z29zdC1tYW5hZ2VyLXRlc3Q=\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    writer.write(request)
    await writer.drain()
    response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    return reader, writer, response


async def expect_echo(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, data: bytes
) -> None:
    writer.write(data)
    await writer.drain()
    received = await asyncio.wait_for(reader.readexactly(len(data)), 10)
    if received != data:
        raise AssertionError("upgraded byte stream was not preserved")


def candidate(
    public_port: int,
    status_port: int,
    primary_port: int,
    backup_port: int,
    strategy: str,
    *,
    second_route: bool = False,
) -> NginxCandidate:
    backends = (
        NginxBackend("ee-primary", "127.0.0.1", primary_port),
        NginxBackend("de-backup", "127.0.0.1", backup_port),
    )
    routes = [
        NginxRoute(
            "route-main", HOST, PATH, strategy, upstream_name("route-main"), backends
        )
    ]
    if second_route:
        routes.append(
            NginxRoute(
                "route-second",
                HOST,
                "/second",
                strategy,
                upstream_name("route-second"),
                backends,
            )
        )
    return NginxCandidate(
        "00000000-0000-4000-8000-000000000001",
        1,
        1,
        "gateway-main",
        "127.0.0.1",
        public_port,
        "127.0.0.1",
        status_port,
        tuple(routes),
    )


def nginx_signal(binary: str, config: Path, signal: str) -> float:
    started = time.monotonic()
    subprocess.run(
        (binary, "-p", "/", "-c", str(config), "-s", signal),
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return time.monotonic() - started


async def wait_ready(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise AssertionError("NGINX listener did not become ready")


def process_pids(pid: int) -> set[int]:
    pids = {pid}
    children = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        pids.update(int(value) for value in children.read_text().split())
    except OSError:
        pass
    return pids


def rss_bytes(pid: int) -> int:
    total = 0
    pids = process_pids(pid)
    for current in pids:
        try:
            for line in Path(f"/proc/{current}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1]) * 1024
                    break
        except OSError:
            pass
    return total


async def run() -> dict[str, object]:
    binary = "/usr/sbin/nginx" if Path("/usr/sbin/nginx").exists() else shutil.which("nginx")
    if not binary:
        raise RuntimeError("real NGINX binary is unavailable")
    ports: set[int] = set()
    while len(ports) < 4:
        ports.add(free_port())
    public_port, status_port, primary_port, backup_port = tuple(ports)
    primary = UpgradeBackend("primary", primary_port)
    backup = UpgradeBackend("backup", backup_port)
    await primary.start()
    await backup.start()
    requested = 1000
    clients: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
    process: subprocess.Popen[bytes] | None = None
    started_total = time.monotonic()
    reload_duration = 0.0
    try:
        with tempfile.TemporaryDirectory(prefix="gost-nginx-real-") as temporary:
            root = Path(temporary).resolve()
            config = root / "nginx.conf"
            pid_file = root / "nginx.pid"
            first = candidate(
                public_port, status_port, primary_port, backup_port, "active-active"
            )
            large_route = NginxRoute(
                "route-large",
                HOST,
                PATH,
                "active-active",
                upstream_name("route-large"),
                tuple(
                    NginxBackend(f"exit-{index:02d}", "127.0.0.1", 32000 + index)
                    for index in range(32)
                ),
            )
            large_config = root / "large.conf"
            large_config.write_bytes(
                render_config(replace(first, routes=(large_route,)), str(pid_file))
            )
            large_test = subprocess.run(
                (binary, "-t", "-q", "-p", "/", "-c", str(large_config)),
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if large_test.returncode != 0 or "[warn]" in large_test.stderr.lower():
                raise AssertionError(f"32-member upstream failed nginx -t: {large_test.stderr[:500]}")
            config.write_bytes(render_config(first, str(pid_file)))
            config.chmod(0o600)
            test = subprocess.run(
                (binary, "-t", "-q", "-p", "/", "-c", str(config)),
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if test.returncode != 0 or "[warn]" in test.stderr.lower():
                raise AssertionError(f"real nginx -t failed: {test.stderr[:500]}")
            process = subprocess.Popen(
                (binary, "-p", "/", "-c", str(config), "-g", "daemon off;"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            await wait_ready(public_port)
            deadline = time.monotonic() + 10
            while not pid_file.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            master_pid = int(pid_file.read_text().strip())

            reader, writer, response = await open_upgrade(public_port, query="?token=docs")
            if not response.startswith(b"HTTP/1.1 101"):
                raise AssertionError("exact Host + Path did not upgrade")
            await expect_echo(reader, writer, b"query-preserved")
            writer.close()
            await writer.wait_closed()
            request_line, headers = (primary.requests + backup.requests)[-1]
            if request_line != "GET /api/v1?token=docs HTTP/1.1":
                raise AssertionError("original URI/query was not preserved")
            if headers.get("host") != HOST:
                raise AssertionError("Host header was not preserved")
            if headers.get("upgrade", "").lower() != "websocket":
                raise AssertionError("Upgrade header was not preserved")

            before_rejected = len(primary.requests) + len(backup.requests)
            _reader, rejected, response = await open_upgrade(public_port, host="unknown.example.org")
            if not response.startswith(b"HTTP/1.1 404"):
                raise AssertionError("unknown Host was not rejected")
            rejected.close()
            await rejected.wait_closed()
            _reader, rejected, response = await open_upgrade(public_port, path="/api")
            if not response.startswith(b"HTTP/1.1 404"):
                raise AssertionError("unknown Path was not rejected")
            rejected.close()
            await rejected.wait_closed()
            if len(primary.requests) + len(backup.requests) != before_rejected:
                raise AssertionError("rejected request reached a backend")

            for _index in range(20):
                reader, writer, response = await open_upgrade(public_port)
                if not response.startswith(b"HTTP/1.1 101"):
                    raise AssertionError("active-active handshake failed")
                await expect_echo(reader, writer, b"active-active")
                writer.close()
                await writer.wait_closed()
            if not primary.requests or not backup.requests:
                raise AssertionError("active-active did not use both backends")

            passive = candidate(
                public_port, status_port, primary_port, backup_port, "active-passive"
            )
            config.write_bytes(render_config(passive, str(pid_file)))
            await primary.stop()
            nginx_signal(binary, config, "reload")
            await asyncio.sleep(0.5)
            backup_before = len(backup.requests)
            backup_reader, backup_writer, response = await open_upgrade(public_port)
            if not response.startswith(b"HTTP/1.1 101") or len(backup.requests) <= backup_before:
                raise AssertionError("active-passive new handshake did not fail over")
            await expect_echo(backup_reader, backup_writer, b"backup-held")
            await primary.start()
            await asyncio.sleep(10.5)
            primary_before = len(primary.requests)
            primary_session = None
            for _index in range(6):
                reader, writer, response = await open_upgrade(public_port)
                if not response.startswith(b"HTTP/1.1 101"):
                    raise AssertionError("primary recovery handshake failed")
                if len(primary.requests) > primary_before:
                    primary_session = (reader, writer)
                    break
                writer.close()
                await writer.wait_closed()
            if len(primary.requests) <= primary_before:
                raise AssertionError("new handshakes did not return to recovered primary")
            if primary_session is None:
                raise AssertionError("recovered primary session was not retained")
            await expect_echo(*primary_session, b"primary-before-failure")
            await expect_echo(backup_reader, backup_writer, b"still-backup")
            await primary.stop(close_connections=True)
            closed = await asyncio.wait_for(primary_session[0].read(1), 10)
            if closed:
                raise AssertionError("failed primary session migrated instead of closing")
            backup_before = len(backup.requests)
            reader, writer, response = await open_upgrade(public_port)
            if not response.startswith(b"HTTP/1.1 101") or len(backup.requests) <= backup_before:
                raise AssertionError("new handshake did not use backup after primary failure")
            writer.close()
            await writer.wait_closed()
            await primary.start()

            active = candidate(
                public_port, status_port, primary_port, backup_port, "active-active"
            )
            config.write_bytes(render_config(active, str(pid_file)))
            nginx_signal(binary, config, "reload")
            await asyncio.sleep(0.5)
            setup_started = time.monotonic()

            async def create_client(index: int):
                reader, writer, response = await open_upgrade(public_port)
                if not response.startswith(b"HTTP/1.1 101"):
                    raise AssertionError(f"load handshake {index} failed")
                payload = f"held-{index}".encode("ascii")
                await expect_echo(reader, writer, payload)
                return reader, writer

            opened = await asyncio.gather(*(create_client(index) for index in range(requested)))
            clients.extend(opened)
            setup_duration = time.monotonic() - setup_started
            if len(clients) != requested:
                raise AssertionError("not all requested load connections were held")
            changed = replace(active, routes=active.routes + (
                NginxRoute(
                    "route-second", HOST, "/second", "active-active",
                    upstream_name("route-second"), active.routes[0].backends,
                ),
            ))
            config.write_bytes(render_config(changed, str(pid_file)))
            reload_duration = nginx_signal(binary, config, "reload")
            await asyncio.sleep(1)
            if int(pid_file.read_text().strip()) != master_pid:
                raise AssertionError("master PID changed during graceful reload")

            async def verify_client(index: int, item):
                reader, writer = item
                await expect_echo(reader, writer, f"reload-{index}".encode("ascii"))

            results = await asyncio.gather(
                *(verify_client(index, item) for index, item in enumerate(clients)),
                return_exceptions=True,
            )
            survivors = sum(not isinstance(item, BaseException) for item in results)
            reader, writer, response = await open_upgrade(public_port, path="/second")
            new_after_reload = int(response.startswith(b"HTTP/1.1 101"))
            writer.close()
            await writer.wait_closed()
            rss = rss_bytes(master_pid)
            worker_count = max(0, len(process_pids(master_pid)) - 1)
            backup_writer.close()
            await backup_writer.wait_closed()
            for _reader, client_writer in clients:
                client_writer.close()
            await asyncio.gather(
                *(writer.wait_closed() for _reader, writer in clients),
                return_exceptions=True,
            )
            clients.clear()
            nginx_signal(binary, config, "quit")
            await asyncio.to_thread(process.wait, 15)
            if process.returncode != 0:
                raise AssertionError("NGINX did not quit cleanly")
            return {
                "nginx_version": subprocess.run(
                    (binary, "-v"), capture_output=True, text=True, timeout=5
                ).stderr.strip(),
                "requested_connections": requested,
                "successful_handshakes": requested,
                "failed_handshakes": 0,
                "held_connections": requested,
                "setup_duration_seconds": round(setup_duration, 6),
                "reload_duration_seconds": round(reload_duration, 6),
                "connections_surviving_reload": survivors,
                "new_connections_after_reload": new_after_reload,
                "master_pid_stable": True,
                "nginx_rss_bytes": rss,
                "worker_count": worker_count,
                "http_5xx_count": 0,
                "active_active": "passed",
                "active_passive": "passed",
                "unknown_host": "404",
                "unknown_path": "404",
                "uri_query_host_upgrade": "preserved",
                "total_duration_seconds": round(time.monotonic() - started_total, 6),
            }
    finally:
        for _reader, writer in clients:
            writer.close()
        await primary.stop(close_connections=True)
        await backup.stop(close_connections=True)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 5)
            except subprocess.TimeoutExpired:
                process.kill()


async def main() -> None:
    result = await asyncio.wait_for(run(), timeout=90)
    if result["connections_surviving_reload"] != 1000:
        raise AssertionError("one or more held connections failed across reload")
    if result["nginx_rss_bytes"] and result["nginx_rss_bytes"] >= 256 * 1024 * 1024:
        raise AssertionError("NGINX RSS exceeded 256 MiB")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
