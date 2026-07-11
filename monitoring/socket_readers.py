"""Pure parsers and correlations for Linux ``ss`` snapshots."""

from __future__ import annotations

import re
from collections import Counter

from monitoring.models import SocketRecord

SS_PROCESS_RE = re.compile(r'"(?P<process>[^"]+)",pid=(?P<pid>\d+),fd=(?P<fd>\d+)')


def parse_socket_address(value: str) -> tuple[str, int] | None:
    value = value.strip()
    if value.startswith("["):
        host, separator, port = value.rpartition("]:")
        host = host[1:]
    else:
        host, separator, port = value.rpartition(":")
    if not separator:
        return None
    if port == "*":
        return host, 0
    if not port.isdigit():
        return None
    number = int(port)
    if number < 0 or number > 65535:
        return None
    return host, number


parse_listener_address = parse_socket_address


def parse_ss_sockets(text: str) -> list[SocketRecord]:
    records: list[SocketRecord] = []
    for raw in text.splitlines():
        parts = raw.strip().split()
        if len(parts) < 5:
            continue
        local = parse_socket_address(parts[3])
        peer = parse_socket_address(parts[4])
        if local is None or peer is None:
            continue
        process = SS_PROCESS_RE.search(raw)
        records.append(
            SocketRecord(
                state=parts[0].upper(),
                local_address=local[0],
                local_port=local[1],
                peer_address=peer[0],
                peer_port=peer[1],
                pid=int(process.group("pid")) if process else None,
                process=process.group("process") if process else None,
            )
        )
    return records


def parse_ss_listeners(text: str) -> list[dict[str, object]]:
    return [
        {
            "address": row.local_address,
            "port": row.local_port,
            "pid": row.pid,
            "process": row.process,
        }
        for row in parse_ss_sockets(text)
        if row.state == "LISTEN"
    ]


def tcp_state_counts(records: list[SocketRecord]) -> dict[str, int]:
    counts = Counter(row.state for row in records if row.state != "LISTEN")
    return dict(counts)


def owned_listener_ports(
    records: list[SocketRecord],
    ports: tuple[int, ...],
    pid: int,
) -> set[int]:
    expected = set(ports)
    return {
        row.local_port
        for row in records
        if row.state == "LISTEN" and row.local_port in expected and row.pid == pid
    }


def listener_ownership_exact(
    records: list[SocketRecord],
    ports: tuple[int, ...],
    pid: int,
) -> bool | None:
    expected = set(ports)
    relevant = [
        row for row in records if row.state == "LISTEN" and row.local_port in expected
    ]
    if any(row.pid is None for row in relevant):
        return None
    return all(any(row.local_port == port and row.pid == pid for row in relevant) for port in expected)


def established_socket_count(records: list[SocketRecord], pid: int) -> int:
    return sum(1 for row in records if row.state == "ESTAB" and row.pid == pid)


def process_tcp_states(records: list[SocketRecord], pid: int) -> dict[str, int]:
    return dict(Counter(row.state for row in records if row.pid == pid and row.state != "LISTEN"))
