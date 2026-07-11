"""Pure parsers and correlations for Linux ``ss`` snapshots."""

from __future__ import annotations

import ipaddress
import re
from collections import Counter
from collections.abc import Iterable

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
    if not text.strip():
        return records
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parts = raw.strip().split()
        if len(parts) < 5:
            raise ValueError("malformed ss row")
        local = parse_socket_address(parts[3])
        peer = parse_socket_address(parts[4])
        if local is None or peer is None:
            raise ValueError("malformed ss socket address")
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


def _pid_set(pids: int | Iterable[int]) -> set[int]:
    return {pids} if isinstance(pids, int) else {int(pid) for pid in pids}


def owned_listener_ports(
    records: list[SocketRecord],
    ports: tuple[int, ...],
    pids: int | Iterable[int],
) -> set[int]:
    expected = set(ports)
    owners = _pid_set(pids)
    return {
        row.local_port
        for row in records
        if row.state == "LISTEN" and row.local_port in expected and row.pid in owners
    }


def listener_ownership_exact(
    records: list[SocketRecord],
    ports: tuple[int, ...],
    pids: int | Iterable[int],
) -> bool | None:
    expected = set(ports)
    owners = _pid_set(pids)
    relevant = [
        row for row in records if row.state == "LISTEN" and row.local_port in expected
    ]
    if any(row.pid is None for row in relevant):
        return None
    return all(any(row.local_port == port and row.pid in owners for row in relevant) for port in expected)


def established_socket_count(records: list[SocketRecord], pids: int | Iterable[int]) -> int:
    owners = _pid_set(pids)
    return sum(1 for row in records if row.state == "ESTAB" and row.pid in owners)


def process_tcp_states(records: list[SocketRecord], pids: int | Iterable[int]) -> dict[str, int]:
    owners = _pid_set(pids)
    return dict(Counter(row.state for row in records if row.pid in owners and row.state != "LISTEN"))


def established_remote_socket_count(
    records: list[SocketRecord],
    pids: int | Iterable[int],
    remote_host: str,
    remote_port: int,
) -> int | None:
    owners = _pid_set(pids)

    def normalized(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            address = ipaddress.ip_address(value.split("%", 1)[0])
        except ValueError:
            return None
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            return address.ipv4_mapped
        return address

    expected = normalized(remote_host)
    if expected is None:
        return None
    count = 0
    for row in records:
        if row.state != "ESTAB" or row.peer_port != remote_port:
            continue
        peer = normalized(row.peer_address)
        if peer is None:
            if row.pid is None or row.pid in owners:
                return None
            continue
        if peer != expected:
            continue
        if row.pid is None:
            return None
        if row.pid in owners:
            count += 1
    return count
