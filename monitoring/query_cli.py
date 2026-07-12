#!/usr/bin/env python3
"""Read-only operator CLI for local GOST Manager monitoring data."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import traceback
from collections.abc import Callable, Sequence
from typing import TextIO

from monitoring.exporters import export_data
from monitoring.config import (
    CONFIG_POLICIES,
    INSTALLED_POLICY,
    ConfigError,
    DEFAULT_CONFIG,
    MonitoringConfig,
    apply_config_policy,
    config_from_environment,
    load_config,
    rooted_path,
)
from monitoring.query_db import ReadOnlyDatabase
from monitoring.query_engine import QueryEngine
from monitoring.query_models import QueryError, QueryInputError
from monitoring.query_window import resolve_window
from monitoring.renderers import render_events, render_snapshot_plain, render_summary, run_live

NETWORK_HOST_METRICS = (
    "tcp_state_estab",
    "tcp_state_syn_sent",
    "tcp_state_syn_recv",
    "tcp_state_close_wait",
    "tcp_state_time_wait",
    "tcp_retransmitted_segments_per_second",
    "tcp_listen_drops",
    "tcp_listen_overflows",
)

class QueryArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise QueryInputError(message)


def _add_database(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", help="monitoring SQLite database")


def _add_window(parser: argparse.ArgumentParser, default: str = "10m") -> None:
    parser.add_argument("--window", default=None, help=f"duration such as {default}, 90s, 2h, or 2d")
    parser.add_argument("--start", help="timezone-aware ISO-8601 start")
    parser.add_argument("--end", help="timezone-aware ISO-8601 end")
    parser.set_defaults(default_window=default)


def _add_metrics(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--metric",
        action="append",
        dest="metrics",
        help="exact metric name; repeat to select more than one",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = QueryArgumentParser(prog="python3 -m monitoring.query_cli")
    parser.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--policy", choices=CONFIG_POLICIES, default=INSTALLED_POLICY)
    parser.add_argument("--path-root", help=argparse.SUPPRESS)
    parser.add_argument("--config", help="strict monitoring config file")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="print the current monitoring snapshot")
    _add_database(snapshot)

    live = subparsers.add_parser("live", help="refresh the monitoring snapshot")
    _add_database(live)
    live.add_argument("--refresh", type=float, default=2.0)
    live.add_argument("--iterations", type=int)
    live.add_argument("--no-color", action="store_true")

    for name in ("summary", "host", "network", "services", "tunnels", "collector"):
        command = subparsers.add_parser(name)
        _add_database(command)
        _add_window(command)
        _add_metrics(command)

    service = subparsers.add_parser("service")
    service.add_argument("service_name")
    _add_database(service)
    _add_window(service, "30m")
    _add_metrics(service)

    tunnel = subparsers.add_parser("tunnel")
    tunnel.add_argument("tunnel_id")
    _add_database(tunnel)
    _add_window(tunnel, "1h")
    _add_metrics(tunnel)

    events = subparsers.add_parser("events")
    _add_database(events)
    _add_window(events, "1h")
    events.add_argument("--severity", help="comma-separated severity names")

    export = subparsers.add_parser("export")
    _add_database(export)
    _add_window(export, "1h")
    export.add_argument("--format", required=True, choices=("json", "csv"))
    export.add_argument(
        "--granularity",
        default="auto",
        choices=("summary", "raw", "minute", "auto"),
    )
    export.add_argument("--output", required=True)
    export.add_argument("--entity-type")
    export.add_argument("--entity-id")
    _add_metrics(export)
    return parser


def _window(args: argparse.Namespace, now: int):
    duration = args.window
    if duration is None and args.start is None and args.end is None:
        duration = args.default_window
    return resolve_window(now, duration, args.start, args.end)


def _engine(args: argparse.Namespace, clock: Callable[[], float]) -> QueryEngine:
    return QueryEngine(ReadOnlyDatabase(args.db), clock=clock)


def _write_json(stream: TextIO, value: object) -> None:
    json.dump(value, stream, sort_keys=True, separators=(",", ":"))
    stream.write("\n")


def _render_detail(
    engine: QueryEngine,
    window,
    stdout: TextIO,
    entity_type: str,
    entity_id: str | None,
    metrics: Sequence[str] | None,
    require_match: bool = False,
) -> None:
    result = engine.summary(
        window,
        entity_type=entity_type,
        entity_id=entity_id,
        metric_names=metrics,
        require_match=require_match,
    )
    stdout.write(render_summary(result))


def run_command(
    args: argparse.Namespace,
    stdout: TextIO,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> int:
    engine = _engine(args, clock)
    if args.command == "snapshot":
        stdout.write(render_snapshot_plain(engine.snapshot()))
        return 0
    if args.command == "live":
        if args.iterations is not None and args.iterations <= 0:
            raise QueryInputError("--iterations must be greater than zero")
        if args.refresh < 0.2 or args.refresh > 60:
            raise QueryInputError("--refresh must be between 0.2 and 60 seconds")
        return run_live(
            engine.snapshot,
            stdout,
            refresh=args.refresh,
            iterations=args.iterations,
            no_color=args.no_color,
            sleeper=sleeper,
        )

    now = int(clock())
    window = _window(args, now)
    metrics = args.metrics if hasattr(args, "metrics") else None
    if args.command == "summary":
        stdout.write(render_summary(engine.summary(window, metric_names=metrics)))
    elif args.command == "host":
        _render_detail(engine, window, stdout, "host", None, metrics)
        _render_detail(engine, window, stdout, "filesystem", None, metrics)
    elif args.command == "network":
        _render_detail(engine, window, stdout, "interface", None, metrics)
        _render_detail(
            engine,
            window,
            stdout,
            "host",
            None,
            metrics or NETWORK_HOST_METRICS,
        )
    elif args.command == "services":
        _render_detail(engine, window, stdout, "service", None, metrics)
    elif args.command == "service":
        _render_detail(
            engine, window, stdout, "service", args.service_name, metrics, True
        )
    elif args.command == "tunnels":
        _render_detail(engine, window, stdout, "tunnel", None, metrics)
    elif args.command == "tunnel":
        _render_detail(engine, window, stdout, "tunnel", args.tunnel_id, metrics, True)
    elif args.command == "collector":
        _render_detail(engine, window, stdout, "collector", None, metrics)
    elif args.command == "events":
        severities = None
        if args.severity:
            severities = [value.strip() for value in args.severity.split(",") if value.strip()]
            if not severities:
                raise QueryInputError("--severity must contain at least one value")
        stdout.write(render_events(engine.events(window, severities)))
    elif args.command == "export":
        filters = {
            "entity_type": args.entity_type,
            "entity_id": args.entity_id,
            "metric_names": args.metrics,
        }
        filters = {key: value for key, value in filters.items() if value is not None}
        metadata = export_data(
            engine,
            window,
            args.output,
            args.format,
            args.granularity,
            filters,
            stdout=stdout,
        )
        if args.output != "-":
            _write_json(stdout, {"output": args.output, "metadata": metadata})
    else:
        raise QueryInputError("unsupported command")
    return 0


def main(
    argv: Sequence[str] | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.db is None:
            try:
                config = (
                    load_config(args.config, policy=args.policy, root=args.path_root)
                    if args.config
                    else config_from_environment()
                )
                config = apply_config_policy(
                    config, policy=args.policy, root=args.path_root
                )
            except ConfigError as exc:
                raise QueryInputError(str(exc)) from exc
            args.db = config.db_path
        elif args.policy == INSTALLED_POLICY:
            try:
                apply_config_policy(
                    MonitoringConfig(
                        db_path=args.db,
                        env_dir=DEFAULT_CONFIG.env_dir,
                        sample_interval=DEFAULT_CONFIG.sample_interval,
                        tcp_interval=DEFAULT_CONFIG.tcp_interval,
                        slow_interval=DEFAULT_CONFIG.slow_interval,
                        maintenance_interval=DEFAULT_CONFIG.maintenance_interval,
                    ),
                    policy=args.policy,
                    root=args.path_root,
                )
            except ConfigError as exc:
                raise QueryInputError(str(exc)) from exc
        if args.policy == INSTALLED_POLICY:
            args.db = str(rooted_path(args.db, args.path_root))
        return run_command(args, stdout, clock, sleeper)
    except QueryError as exc:
        stderr.write(f"error: {exc}\n")
        if "args" in locals() and args.debug:
            traceback.print_exc(file=stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        try:
            stdout.close()
        except OSError:
            pass
        return 0
    except Exception as exc:
        stderr.write(f"error: query failed: {exc}\n")
        if "args" in locals() and args.debug:
            traceback.print_exc(file=stderr)
        return 3


def _sigterm_as_interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    previous = signal.signal(signal.SIGTERM, _sigterm_as_interrupt)
    try:
        raise SystemExit(main())
    finally:
        signal.signal(signal.SIGTERM, previous)
