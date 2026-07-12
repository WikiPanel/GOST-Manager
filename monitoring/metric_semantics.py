"""Central statistics policy for monitoring metric families."""

from __future__ import annotations

import dataclasses

GAUGE = "gauge"
RATE = "rate"
CUMULATIVE_COUNTER = "cumulative_counter"
CATEGORICAL = "categorical"
IDENTITY = "identity"
TIMESTAMP = "timestamp"
UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class MetricSemantics:
    category: str
    supports_range: bool = False
    supports_average: bool = False
    supports_p95: bool = False
    supports_transitions: bool = False


STATISTICAL = MetricSemantics(GAUGE, True, True, True)
RATE_STATISTICAL = MetricSemantics(RATE, True, True, True)
COUNTER = MetricSemantics(CUMULATIVE_COUNTER)
STATE = MetricSemantics(CATEGORICAL, supports_transitions=True)
IDENTIFIER = MetricSemantics(IDENTITY, supports_transitions=True)
TIME_VALUE = MetricSemantics(TIMESTAMP, supports_transitions=True)
CONSERVATIVE = MetricSemantics(UNKNOWN)

_CATEGORICAL_NAMES = {
    "checkpoint_success",
    "cycle_status",
    "env_source_valid",
    "listener_ownership_exact",
    "service_active",
}
_IDENTITY_NAMES = {
    "service_main_pid",
    "service_start_monotonic_us",
}
_CUMULATIVE_NAMES = {
    "cpu_jiffies_total",
    "overrun_count",
    "service_restart_count",
    "source_errors_total",
    "tcp_abort_on_data",
    "tcp_abort_on_memory",
    "tcp_active_opens",
    "tcp_attempt_failures",
    "tcp_established_resets",
    "tcp_listen_drops",
    "tcp_listen_overflows",
    "tcp_outbound_resets",
    "tcp_passive_opens",
    "tcp_retransmitted_segments",
    "tcp_timeouts",
}
_GAUGE_NAMES = {
    "conntrack_count",
    "conntrack_max",
    "cpu_logical_count",
    "events_written",
    "file_handles_allocated",
    "file_handles_max",
    "metrics_written",
    "missed_deadlines",
    "rows_write_attempted",
    "source_error_codes",
    "source_errors",
    "target_count",
    "tunnels_discovered",
}
_GAUGE_SUFFIXES = (
    "_current_bytes",
    "_duration_seconds",
    "_free_bytes",
    "_hard_limit",
    "_max",
    "_open_fds",
    "_peak_bytes",
    "_rss_bytes",
    "_rss_anon_bytes",
    "_rss_file_bytes",
    "_size_bytes",
    "_soft_limit",
    "_state_estab",
    "_state_syn_recv",
    "_state_syn_sent",
    "_state_close_wait",
    "_state_time_wait",
    "_tasks",
    "_threads",
    "_total_bytes",
    "_used_bytes",
)
_GAUGE_COUNT_SUFFIXES = (
    "_listener_count",
    "_listener_owned_count",
    "_process_count",
    "_socket_count",
    "_sockets_total",
)
_CUMULATIVE_SUFFIXES = (
    "_cpu_ticks",
    "_io_time_ms",
    "_jiffies",
    "_read_bytes",
    "_reads_completed",
    "_written_bytes",
    "_writes_completed",
)


def classify_metric(metric_name: str, unit: str) -> MetricSemantics:
    """Return conservative statistics eligibility for one metric series."""
    if unit in {"state", "text"}:
        return STATE
    if unit == "endpoint":
        return IDENTIFIER
    if unit == "boolean" or metric_name in _CATEGORICAL_NAMES:
        return STATE
    if unit == "pid" or metric_name in _IDENTITY_NAMES or metric_name.endswith("_pid"):
        return IDENTIFIER
    if unit == "unix_seconds" or metric_name.endswith("_timestamp"):
        return TIME_VALUE
    if metric_name.endswith("_per_second") or unit in {"B/s", "pps", "ops/s"}:
        return RATE_STATISTICAL
    if (
        metric_name in _CUMULATIVE_NAMES
        or metric_name in {"rx_bytes", "rx_packets", "rx_errors", "rx_drops",
                           "tx_bytes", "tx_packets", "tx_errors", "tx_drops"}
        or metric_name.startswith("cpu_") and metric_name.endswith("_jiffies")
        or metric_name.startswith("systemd_ip_") and metric_name.endswith("_bytes")
        or metric_name.endswith(_CUMULATIVE_SUFFIXES)
    ):
        return COUNTER
    if (
        metric_name in _GAUGE_NAMES
        or metric_name in {"load1", "load5", "load15"}
        or metric_name.endswith("_percent")
        or metric_name.endswith(_GAUGE_SUFFIXES)
        or metric_name.endswith(_GAUGE_COUNT_SUFFIXES)
        or metric_name.startswith("filesystem_")
        or metric_name.startswith("memory_")
        or metric_name.startswith("swap_")
        or metric_name.startswith("cgroup_memory_")
        or metric_name.startswith("tcp_state_")
        or metric_name in {"configured_listener_count", "observed_listener_count",
                           "listener_owned_count", "established_remote_sockets",
                           "process_count", "process_threads", "service_tasks"}
    ):
        return STATISTICAL
    return CONSERVATIVE
