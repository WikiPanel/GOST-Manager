"""Monotonic daemon scheduling and collection lifecycle events."""

from __future__ import annotations

import math
import signal
import sys
import time
from collections.abc import Callable, Sequence

from monitoring.collector import (
    CollectionCycleError,
    CollectorConfig,
    CollectorSources,
    collect_once,
)
from monitoring.event_state import EventState
from monitoring.models import Clock, Event, Metric, MetricSample
from monitoring.schema import (
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    _cycle,
    get_state,
    init_db,
    insert_event,
    insert_metric,
    insert_sample,
    migrate_database,
    open_runtime_database,
    set_state,
)

MAINTENANCE_INTERVAL_SECONDS = 15 * 60


def scheduler_ticks(start: float, interval: float, durations: Sequence[float]) -> list[float]:
    ticks: list[float] = []
    deadline = start
    for duration in durations:
        ticks.append(deadline)
        end = deadline + duration
        deadline += interval
        while deadline < end:
            deadline += interval
    return ticks


def record_cycle_overrun(
    db_path: str,
    ts: int,
    finished: float,
    deadline: float,
    interval: float,
) -> None:
    overrun_seconds = max(0.0, finished - (deadline + interval))
    conn = open_runtime_database(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = EventState(conn)
        if overrun_seconds <= 0:
            state.edge(
                "collection_overrun",
                False,
                ts,
                "collection_overrun",
                "Collection cycle exceeded its deadline",
                {},
            )
            conn.commit()
            return
        missed = math.ceil(overrun_seconds / interval) if interval > 0 else 0
        conn.execute(
            "UPDATE sample_cycles SET overrun=1,missed_deadlines=?,overrun_seconds=? "
            "WHERE collected_at=?",
            (missed, overrun_seconds, ts),
        )
        for event in state.edge(
            "collection_overrun",
            True,
            ts,
            "collection_overrun",
            "Collection cycle exceeded its deadline",
            {"missed_deadlines": missed, "overrun_seconds": overrun_seconds},
        ):
            insert_event(conn, event)
        row = conn.execute(
            "SELECT cycle_id FROM sample_cycles WHERE collected_at=?",
            (ts,),
        ).fetchone()
        if row:
            cycle_id = int(row[0])
            sample_id = insert_sample(
                conn,
                MetricSample(None, ts, 1, 1, 0, 0, 0, 0),
                cycle_id,
            )
            insert_metric(
                conn,
                sample_id,
                Metric("collector", "missed_deadlines", missed, "count", "exact", entity_type="collector", entity_id="local"),
                cycle_id,
                ts,
            )
            insert_metric(
                conn,
                sample_id,
                Metric("collector", "overrun_seconds", overrun_seconds, "seconds", "exact", entity_type="collector", entity_id="local"),
                cycle_id,
                ts,
            )
            raw = get_state(conn, "counter.overrun_count")
            count = int(raw) + 1 if raw else 1
            set_state(conn, "counter.overrun_count", str(count))
            insert_metric(
                conn,
                sample_id,
                Metric("collector", "overrun_count", count, "count", "exact", entity_type="collector", entity_id="local"),
                cycle_id,
                ts,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _lifecycle_event(db_path: str, event: Event) -> None:
    conn = open_runtime_database(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        insert_event(conn, event)
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def run_daemon(
    db_path: str,
    env_dir: str,
    interval: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    maintenance_interval: float = MAINTENANCE_INTERVAL_SECONDS,
    sources: CollectorSources | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    stop_requested: Callable[[], bool] | None = None,
    collect: Callable[..., int] = collect_once,
    record_overrun: Callable[[str, int, float, float, float], None] = record_cycle_overrun,
) -> int:
    active_sources = sources or CollectorSources()
    clock = active_sources.clock
    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    migrate_database(db_path)
    _lifecycle_event(
        db_path,
        Event(int(clock.wall()), "info", "collector_started", "Monitoring collector started"),
    )
    old_term = signal.signal(signal.SIGTERM, request_stop)
    old_int = signal.signal(signal.SIGINT, request_stop)
    try:
        deadline = clock.monotonic()
        next_maintenance = deadline
        while not stop and not (stop_requested and stop_requested()):
            current = clock.monotonic()
            if current < deadline:
                sleeper(deadline - current)
                continue
            maintenance = current >= next_maintenance
            if maintenance:
                next_maintenance = current + maintenance_interval
            try:
                cycle_ts = collect(
                    db_path,
                    env_dir,
                    sources=active_sources,
                    config=CollectorConfig(sample_interval=interval),
                    maintenance=maintenance,
                )
                record_overrun(db_path, cycle_ts, clock.monotonic(), deadline, interval)
            except CollectionCycleError as exc:
                try:
                    record_overrun(db_path, exc.ts, clock.monotonic(), deadline, interval)
                except Exception:
                    pass
                print(f"collection failed: {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"collection failed: {exc}", file=sys.stderr)
            deadline += interval
            current = clock.monotonic()
            while deadline < current:
                deadline += interval
        return 0
    finally:
        _lifecycle_event(
            db_path,
            Event(int(clock.wall()), "info", "collector_stopped", "Monitoring collector stopped"),
        )
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
