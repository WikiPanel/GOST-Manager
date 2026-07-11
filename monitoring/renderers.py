"""Plain and ANSI terminal renderers for monitoring queries."""

from __future__ import annotations

import datetime as dt
import os
import shutil
import textwrap
import time
from collections.abc import Callable, Mapping
from typing import TextIO

from monitoring.health import HealthPolicy, evaluate_snapshot
from monitoring.query_models import EventRecord, QueryResult, SeriesSummary

HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
CLEAR_SCREEN = "\x1b[H\x1b[2J"
COLOR = {
    "healthy": "\x1b[32m",
    "degraded": "\x1b[33m",
    "critical": "\x1b[31m",
    "down": "\x1b[31m",
    "unknown": "\x1b[36m",
}
RESET = "\x1b[0m"


def _value(point: dict[str, object] | None) -> str:
    if not point:
        return "unavailable"
    value = point.get("numeric_value")
    if value is None:
        value = point.get("text_value")
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _display(value: object | None) -> object:
    return "unavailable" if value is None else value


def _utc(timestamp: object | None) -> str:
    if timestamp is None:
        return "unavailable"
    return dt.datetime.fromtimestamp(int(timestamp), dt.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _age(point: dict[str, object] | None) -> str:
    if not point:
        return "age=unavailable"
    age = point.get("data_age_seconds")
    if age is None:
        return "age=unavailable"
    stale = " stale" if point.get("stale") else ""
    return f"at={_utc(point.get('ts'))} age={age}s{stale}"


def _metric_index(snapshot: dict[str, object]) -> dict[tuple[str, str, str], dict[str, object]]:
    return {
        (
            str(item.get("entity_type", "")),
            str(item.get("entity_id", "")),
            str(item.get("metric_name", "")),
        ): item
        for item in snapshot.get("metrics", [])
        if isinstance(item, dict)
    }


def _line(text: str, width: int) -> str:
    return textwrap.fill(
        text,
        width=max(8, width),
        subsequent_indent="  ",
        break_long_words=True,
        break_on_hyphens=False,
    )


def render_snapshot_plain(
    snapshot: dict[str, object],
    width: int = 100,
    policy: HealthPolicy = HealthPolicy(),
) -> str:
    health = evaluate_snapshot(snapshot, policy)
    metrics = _metric_index(snapshot)
    overall = health["overall"]
    cycle = snapshot.get("cycle") if isinstance(snapshot.get("cycle"), dict) else {}
    lines: list[str] = ["OVERALL"]
    lines.append(f"status: {overall['status']}")
    lines.append("reasons: " + ", ".join(overall["reason_codes"]))
    last_success = metrics.get(("collector", "local", "last_successful_cycle_timestamp"))
    last_success_ts = (
        last_success.get("numeric_value") if last_success else cycle.get("collected_at")
    )
    lines.append(f"last successful sample: {_utc(last_success_ts)}")
    lines.append(f"sample age: {overall.get('data_age_seconds', 'unavailable')}s")
    lines.append("history coverage: not applicable (latest-per-series snapshot)")

    lines.extend(["", "HOST"])
    for label, name in (
        ("CPU", "cpu_utilization_percent"),
        ("RAM", "memory_used_percent"),
        ("load 1", "load1"),
        ("load 5", "load5"),
        ("load 15", "load15"),
        ("conntrack", "conntrack_utilization_percent"),
        ("file handles", "file_handles_utilization_percent"),
    ):
        point = metrics.get(("host", "local", name))
        lines.append(f"{label}: {_value(point)} [{point.get('quality', 'unavailable') if point else 'unavailable'}; {_age(point)}]")
    root = metrics.get(("filesystem", "fs:/", "filesystem_used_percent"))
    monitor_fs = metrics.get(("filesystem", "fs:/var/lib/gost-manager", "filesystem_used_percent"))
    lines.append(f"root filesystem: {_value(root)} [{_age(root)}]")
    lines.append(f"monitoring filesystem: {_value(monitor_fs)} [{_age(monitor_fs)}]")

    lines.extend(["", "NETWORK"])
    for label, entity, name in (
        ("external RX", "interface:external-total", "rx_bytes_per_second"),
        ("external TX", "interface:external-total", "tx_bytes_per_second"),
        ("external RX packets", "interface:external-total", "rx_packets_per_second"),
        ("external TX packets", "interface:external-total", "tx_packets_per_second"),
        ("external RX errors", "interface:external-total", "rx_errors"),
        ("external TX errors", "interface:external-total", "tx_errors"),
        ("external RX drops", "interface:external-total", "rx_drops"),
        ("external TX drops", "interface:external-total", "tx_drops"),
        ("loopback RX", "interface:lo", "rx_bytes_per_second"),
        ("loopback TX", "interface:lo", "tx_bytes_per_second"),
    ):
        point = metrics.get(("interface", entity, name))
        lines.append(f"{label}: {_value(point)} [{_age(point)}]")

    lines.extend(["", "TCP"])
    for label, name in (
        ("established", "tcp_state_estab"),
        ("SYN-SENT", "tcp_state_syn_sent"),
        ("SYN-RECV", "tcp_state_syn_recv"),
        ("CLOSE-WAIT", "tcp_state_close_wait"),
        ("TIME-WAIT", "tcp_state_time_wait"),
        ("retransmit rate", "tcp_retransmitted_segments_per_second"),
        ("listen drops", "tcp_listen_drops"),
        ("listen overflows", "tcp_listen_overflows"),
    ):
        point = metrics.get(("host", "local", name))
        lines.append(f"{label}: {_value(point)} [{_age(point)}]")

    lines.extend(["", "NGINX / SERVICES"])
    services = health["services"]
    if not services:
        lines.append("no managed service data")
    for service, result in sorted(services.items()):
        prefix = ("service", service)
        lines.append(
            f"{service}: {result['status']} required={str(result.get('required', False)).lower()} "
            f"state={_value(metrics.get(prefix + ('service_active_state',)))} "
            f"cpu={_value(metrics.get(prefix + ('process_cpu_percent',)))} "
            f"rss={_value(metrics.get(prefix + ('process_rss_bytes',)))} "
            f"processes={_value(metrics.get(prefix + ('process_count',)))} "
            f"fds={_value(metrics.get(prefix + ('process_open_fds',)))} "
            f"listeners={_value(metrics.get(prefix + ('listener_owned_count',)))} "
            f"sockets={_value(metrics.get(prefix + ('established_sockets_total',)))} "
            f"restarts={_value(metrics.get(prefix + ('service_restart_count',)))} "
            f"quality={result['source_quality']} age={result['data_age_seconds']}s"
            f" observed={_utc(metrics.get(prefix + ('service_active',), {}).get('ts'))}"
        )

    lines.extend(["", "TUNNELS"])
    tunnels = health["tunnels"]
    if not tunnels:
        lines.append("no managed tunnel data")
    entity_meta = {
        str(item.get("entity_id")): item.get("metadata", {})
        for item in snapshot.get("entities", [])
        if isinstance(item, dict) and item.get("entity_type") == "tunnel"
    }
    for tunnel, result in sorted(tunnels.items()):
        prefix = ("tunnel", tunnel)
        metadata = entity_meta.get(tunnel, {})
        service = metadata.get("service", "unavailable") if isinstance(metadata, dict) else "unavailable"
        lines.append(
            f"{tunnel}: {result['status']} service={service} "
            f"listeners={_value(metrics.get(prefix + ('observed_listener_count',)))}/"
            f"{_value(metrics.get(prefix + ('configured_listener_count',)))} "
            f"ownership={_value(metrics.get(prefix + ('listener_ownership_exact',)))} "
            f"remote={_value(metrics.get(prefix + ('remote_endpoint',)))} "
            f"remote_sockets={_value(metrics.get(prefix + ('established_remote_sockets',)))} "
            f"cpu={_value(metrics.get(prefix + ('process_cpu_percent',)))} "
            f"rss={_value(metrics.get(prefix + ('process_rss_bytes',)))} "
            f"fds={_value(metrics.get(prefix + ('process_open_fds',)))} "
            f"quality={result['source_quality']} age={result['data_age_seconds']}s"
            f" observed={_utc(metrics.get(prefix + ('service_active',), {}).get('ts'))}"
        )

    lines.extend(["", "COLLECTOR / DATABASE"])
    for label, name in (
        ("cycle", "cycle_status"),
        ("duration", "duration_seconds"),
        ("missed deadlines", "missed_deadlines"),
        ("source errors", "source_errors_total"),
        ("database size", "database_size_bytes"),
        ("WAL size", "database_wal_size_bytes"),
        ("checkpoint", "checkpoint_success"),
    ):
        point = metrics.get(("collector", "local", name))
        lines.append(f"{label}: {_value(point)} [{_age(point)}]")

    lines.extend(["", "RECENT EVENTS"])
    events = snapshot.get("events", [])
    if not events:
        lines.append("no recent events")
    for event in list(events)[:10]:
        if isinstance(event, dict):
            details = event.get("details", {})
            entity = ""
            if isinstance(details, dict):
                for key in ("tunnel_id", "service", "entity_id", "source"):
                    if details.get(key):
                        entity = f" entity={details[key]}"
                        break
            lines.append(
                f"{_utc(event.get('ts'))} {event.get('severity')} {event.get('code')}{entity}: {event.get('message')}"
            )
    return "\n".join(_line(item, width) if item else "" for item in lines) + "\n"


def render_summary(result: QueryResult, width: int = 120) -> str:
    lines = [
        f"SUMMARY source={result.source_mode} truncated={str(result.window.truncated).lower()}",
        f"requested={_utc(result.window.requested_start)}..{_utc(result.window.requested_end)}",
        f"effective={_utc(result.window.effective_start)}..{_utc(result.window.effective_end)} "
        f"materialized_rows={result.materialized_rows} rows_scanned={result.rows_scanned} "
        f"max_buffered={result.maximum_rows_buffered}",
        "ENTITY | METRIC | SEMANTICS | LATEST@TIME | MIN | AVG | MAX | P95 | FIRST..LAST | TRANSITIONS | SAMPLES/EXPECTED | COVERAGE | UNAVAILABLE | RESET | GAP | AGE | QUALITY | UNIT | SOURCE",
    ]
    for item in result.series:
        values = (
            f"{item.entity_type}:{item.entity_id} | {item.metric_name} | {item.metric_semantics} | "
            f"{_display(item.latest)}@{_utc(item.latest_timestamp)} | "
            f"{_display(item.minimum)} | {_display(item.average)} | {_display(item.maximum)} | "
            f"{_display(item.p95)} | "
            f"{_utc(item.first_timestamp)}..{_utc(item.last_timestamp)} | "
            f"{_display(item.transition_count)} | "
            f"{item.sample_count}/{item.expected_sample_count} | {item.coverage:.2f} | "
            f"{item.unavailable_count} | {item.reset_count} | {item.gap_count} | "
            f"{_display(item.data_age_seconds)} | {item.quality} | {item.unit} | {item.source_mode}"
        )
        lines.append(_line(values, width))
    if not result.series:
        lines.append("no matching monitoring data")
    return "\n".join(lines) + "\n"


def render_events(events: list[EventRecord], width: int = 120) -> str:
    lines = ["EVENTS", "TIME (UTC) | SEVERITY | CODE | ENTITY | MESSAGE"]
    for event in events:
        entity = "unavailable"
        for key in ("tunnel_id", "service", "entity_id", "source"):
            if event.details.get(key):
                entity = str(event.details[key])
                break
        lines.append(
            _line(
                f"{_utc(event.ts)} | {event.severity} | {event.code} | {entity} | {event.message}",
                width,
            )
        )
    if not events:
        lines.append("no matching events")
    return "\n".join(lines) + "\n"


def render_ansi_snapshot(snapshot: dict[str, object], width: int = 100) -> str:
    plain = render_snapshot_plain(snapshot, width)
    health = evaluate_snapshot(snapshot)["overall"]["status"]
    colored = f"{COLOR.get(str(health), '')}{str(health).upper()}{RESET}"
    return plain.replace(f"status: {health}", f"status: {colored}", 1)


def ansi_enabled(
    isatty: Callable[[], bool],
    environ: Mapping[str, str],
    no_color: bool,
) -> bool:
    return (
        isatty()
        and environ.get("TERM", "") != "dumb"
        and "NO_COLOR" not in environ
        and not no_color
    )


def run_live(
    snapshot_provider: Callable[[], dict[str, object]],
    stdout: TextIO,
    refresh: float = 2.0,
    iterations: int | None = None,
    no_color: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
    terminal_size: Callable[[], os.terminal_size] = shutil.get_terminal_size,
    isatty: Callable[[], bool] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    if refresh < 0.2 or refresh > 60:
        raise ValueError("refresh must be between 0.2 and 60 seconds")
    active_isatty = isatty or stdout.isatty
    active_environ = environ if environ is not None else os.environ
    use_ansi = ansi_enabled(active_isatty, active_environ, no_color)
    count = 0
    if use_ansi:
        stdout.write(HIDE_CURSOR)
        stdout.flush()
    try:
        while iterations is None or count < iterations:
            snapshot = snapshot_provider()
            width = max(20, terminal_size().columns)
            rendered = (
                render_ansi_snapshot(snapshot, width)
                if use_ansi
                else render_snapshot_plain(snapshot, width)
            )
            if use_ansi:
                stdout.write(CLEAR_SCREEN)
            stdout.write(rendered)
            stdout.flush()
            count += 1
            if iterations is None or count < iterations:
                sleeper(refresh)
    except KeyboardInterrupt:
        return 130
    finally:
        if use_ansi:
            stdout.write(SHOW_CURSOR)
            stdout.flush()
    return 0
