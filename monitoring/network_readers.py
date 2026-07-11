"""Network-interface and TCP/IP counter readers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from monitoring.models import CounterDelta, InterfaceCounters, Metric

INTERFACE_FIELDS = (
    "rx_bytes",
    "rx_packets",
    "rx_errors",
    "rx_drops",
    "tx_bytes",
    "tx_packets",
    "tx_errors",
    "tx_drops",
)


def counter_delta(
    previous: int | None,
    current: int | None,
    elapsed: float,
    max_gap: float | None = None,
) -> CounterDelta:
    if previous is None or current is None or elapsed <= 0:
        return CounterDelta(None, None, "unavailable", False, False)
    gap = bool(max_gap is not None and elapsed > max_gap)
    if current < previous:
        return CounterDelta(None, None, "unavailable", True, gap)
    delta = current - previous
    return CounterDelta(delta, delta / elapsed, "derived", False, gap)


def parse_net_dev(text: str) -> dict[str, InterfaceCounters]:
    interfaces: dict[str, InterfaceCounters] = {}
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        name_raw, values_raw = raw.split(":", 1)
        name = name_raw.strip()
        values = values_raw.split()
        if not name or len(values) < 16:
            continue
        try:
            numbers = [int(value) for value in values[:16]]
        except ValueError:
            continue
        interfaces[name] = InterfaceCounters(
            name=name,
            rx_bytes=numbers[0],
            rx_packets=numbers[1],
            rx_errors=numbers[2],
            rx_drops=numbers[3],
            tx_bytes=numbers[8],
            tx_packets=numbers[9],
            tx_errors=numbers[10],
            tx_drops=numbers[11],
        )
    return interfaces


def aggregate_external(
    interfaces: dict[str, InterfaceCounters],
) -> InterfaceCounters:
    external = [value for name, value in interfaces.items() if name != "lo"]
    sums = {
        field: sum(int(getattr(item, field)) for item in external)
        for field in INTERFACE_FIELDS
    }
    return InterfaceCounters(name="external-total", **sums)


def read_interface_link(
    name: str,
    sys_root: Path = Path("/sys"),
    read_text: Callable[[Path], str] | None = None,
) -> dict[str, int | str | None]:
    reader = read_text or (lambda path: path.read_text(encoding="utf-8"))
    root = sys_root / "class/net" / name

    def optional(relative: str) -> str | None:
        try:
            return reader(root / relative).strip()
        except (OSError, ValueError):
            return None

    state = optional("operstate")
    mtu_raw = optional("mtu")
    speed_raw = optional("speed")
    mtu = int(mtu_raw) if mtu_raw and mtu_raw.isdigit() else None
    speed = int(speed_raw) if speed_raw and speed_raw.lstrip("-").isdigit() else None
    if speed is not None and speed < 0:
        speed = None
    return {
        "state": state,
        "link_up": None if state is None else int(state == "up"),
        "mtu": mtu,
        "speed_mbps": speed,
    }


def interface_metrics(
    current: InterfaceCounters,
    previous: InterfaceCounters | None,
    elapsed: float | None,
    link: dict[str, int | str | None] | None = None,
    max_gap: float | None = None,
) -> list[Metric]:
    loopback = current.name == "lo"
    aggregate = current.name == "external-total"
    scope = "net.loopback" if loopback else "net.external"
    entity_id = "interface:external-total" if aggregate else f"interface:{current.name}"
    labels = {"interface": current.name}
    metrics: list[Metric] = []
    for field in INTERFACE_FIELDS:
        unit = "bytes" if "bytes" in field else "packets"
        value = int(getattr(current, field))
        metrics.append(
            Metric(
                scope,
                field,
                value,
                unit,
                "exact",
                labels,
                "interface",
                entity_id,
            )
        )
        previous_value = int(getattr(previous, field)) if previous is not None else None
        delta = counter_delta(previous_value, value, elapsed or 0.0, max_gap)
        rate_name = f"{field}_per_second"
        rate_unit = "B/s" if "bytes" in field else "pps"
        metrics.append(
            Metric(
                scope,
                rate_name,
                delta.rate,
                rate_unit,
                delta.quality,
                labels,
                "interface",
                entity_id,
                delta.reset,
                delta.gap,
            )
        )
    if link is not None and not aggregate:
        for name, unit in (
            ("link_up", "boolean"),
            ("link_state", "state"),
            ("mtu", "bytes"),
            ("speed_mbps", "Mbit/s"),
        ):
            key = "state" if name == "link_state" else name
            value = link.get(key)
            metrics.append(
                Metric(
                    scope,
                    name,
                    value,
                    unit,
                    "exact" if value is not None else "unavailable",
                    labels,
                    "interface",
                    entity_id,
                )
            )
    return metrics

def parse_protocol_table(text: str) -> dict[str, dict[str, int]]:
    """Parse paired-header tables used by /proc/net/snmp and netstat."""
    result: dict[str, dict[str, int]] = {}
    lines = [line.split() for line in text.splitlines() if line.strip()]
    index = 0
    while index + 1 < len(lines):
        header = lines[index]
        values = lines[index + 1]
        index += 2
        if not header or not values or header[0] != values[0]:
            continue
        protocol = header[0].rstrip(":")
        names = header[1:]
        raw_values = values[1:]
        if len(names) != len(raw_values):
            continue
        parsed: dict[str, int] = {}
        try:
            parsed = {name: int(value) for name, value in zip(names, raw_values)}
        except ValueError:
            continue
        result[protocol] = parsed
    return result


TCP_COUNTERS = {
    "ActiveOpens": "tcp_active_opens",
    "PassiveOpens": "tcp_passive_opens",
    "AttemptFails": "tcp_attempt_failures",
    "EstabResets": "tcp_established_resets",
    "CurrEstab": "tcp_current_established",
    "RetransSegs": "tcp_retransmitted_segments",
    "OutRsts": "tcp_outbound_resets",
}

TCPEXT_COUNTERS = {
    "ListenOverflows": "tcp_listen_overflows",
    "ListenDrops": "tcp_listen_drops",
    "TCPAbortOnData": "tcp_abort_on_data",
    "TCPAbortOnMemory": "tcp_abort_on_memory",
    "TCPTimeouts": "tcp_timeouts",
}


def selected_tcp_counters(snmp_text: str, netstat_text: str) -> dict[str, int]:
    snmp = parse_protocol_table(snmp_text).get("Tcp", {})
    netstat = parse_protocol_table(netstat_text).get("TcpExt", {})
    selected = {
        output: snmp[source]
        for source, output in TCP_COUNTERS.items()
        if source in snmp
    }
    selected.update(
        {
            output: netstat[source]
            for source, output in TCPEXT_COUNTERS.items()
            if source in netstat
        }
    )
    return selected


def tcp_counter_metrics(
    current: dict[str, int],
    previous: dict[str, int] | None,
    elapsed: float | None,
    max_gap: float | None = None,
) -> list[Metric]:
    metrics: list[Metric] = []
    gauges = {"tcp_current_established"}
    for name, value in sorted(current.items()):
        metrics.append(
            Metric("tcp", name, value, "count", "exact", entity_type="host", entity_id="local")
        )
        if name in gauges:
            continue
        delta = counter_delta(
            previous.get(name) if previous else None,
            value,
            elapsed or 0.0,
            max_gap,
        )
        metrics.append(
            Metric(
                "tcp",
                f"{name}_per_second",
                delta.rate,
                "events/s",
                delta.quality,
                entity_type="host",
                entity_id="local",
                reset=delta.reset,
                gap=delta.gap,
            )
        )
    return metrics
