"""Observational monitoring health evaluation."""

from __future__ import annotations

import dataclasses
from collections import defaultdict

from monitoring.models import QUALITY_RANK
from monitoring.query_models import HealthResult


@dataclasses.dataclass(frozen=True)
class HealthPolicy:
    stale_after_seconds: int = 20
    cpu_degraded_percent: float = 80.0
    memory_degraded_percent: float = 85.0
    filesystem_degraded_percent: float = 85.0
    filesystem_critical_percent: float = 95.0
    conntrack_degraded_percent: float = 85.0
    conntrack_critical_percent: float = 95.0
    file_handles_degraded_percent: float = 85.0
    file_handles_critical_percent: float = 95.0
    recent_event_seconds: int = 3600


def _required_services(snapshot: dict[str, object]) -> set[str]:
    required: set[str] = set()
    for entity in snapshot.get("entities", []):
        if not isinstance(entity, dict):
            continue
        metadata = entity.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if entity.get("entity_type") == "tunnel" and metadata.get("service"):
            required.add(str(metadata["service"]))
        if (
            entity.get("entity_type") == "service"
            and metadata.get("gateway_required") is True
        ):
            required.add(str(entity.get("entity_id", "")))
    return required


def _unavailable(metric: dict[str, object] | None) -> bool:
    return (
        metric is None
        or metric.get("quality") == "unavailable"
        or bool(metric.get("stale"))
    )


def _required_event_codes(
    snapshot: dict[str, object],
    now: int,
    policy: HealthPolicy,
) -> set[str]:
    required_services = _required_services(snapshot)
    current_tunnels = {
        str(entity.get("entity_id", "")): int(entity.get("updated_at", 0))
        for entity in snapshot.get("entities", [])
        if isinstance(entity, dict) and entity.get("entity_type") == "tunnel"
    }
    metrics = _metric_map(snapshot)
    codes: set[str] = set()
    resolved: set[tuple[str, str]] = set()
    paired = {
        "listener_disappeared": ("listener", True),
        "listener_returned": ("listener", False),
        "metric_source_unavailable": ("source", True),
        "metric_source_available": ("source", False),
        "collection_failed": ("collection", True),
        "collection_recovered": ("collection", False),
        "wal_checkpoint_failed": ("checkpoint", True),
        "wal_checkpoint_recovered": ("checkpoint", False),
        "database_retention_failed": ("retention", True),
        "database_retention_recovered": ("retention", False),
        "env_parse_error": ("managed_env", True),
        "env_parse_recovered": ("managed_env", False),
    }
    events = sorted(
        (
            event for event in snapshot.get("health_events", snapshot.get("events", []))
            if isinstance(event, dict)
        ),
        key=lambda event: int(event.get("ts", 0)),
        reverse=True,
    )
    for event in events:
        if not isinstance(event, dict) or now - int(event.get("ts", 0)) > policy.recent_event_seconds:
            continue
        details = event.get("details")
        details = details if isinstance(details, dict) else {}
        service = str(details.get("service", ""))
        source = str(details.get("source", ""))
        tunnel = str(details.get("tunnel_id", ""))
        code = str(event.get("code", ""))
        event_ts = int(event.get("ts", 0))
        if tunnel and tunnel not in current_tunnels:
            continue
        if tunnel and event_ts < current_tunnels[tunnel]:
            continue
        if service and service not in required_services:
            continue
        if "nginx.service" in source and "nginx.service" not in required_services:
            continue
        if code in {"metric_source_unavailable", "metric_source_available"} and source:
            required_host_sources = {"proc_stat", "proc_meminfo", "filesystem:/"}
            required_service_source = any(
                required in source for required in required_services
            )
            if source not in required_host_sources and not required_service_source:
                continue
        if code == "service_state_changed":
            identity = ("service", service or "unknown")
            if identity in resolved:
                continue
            resolved.add(identity)
            current = str(details.get("current", "")).lower()
            active = (
                str(event.get("severity", "")).lower()
                in {"warning", "error", "critical"}
                and current in {"failed", "inactive"}
            )
            metric = metrics.get(("service", service, "service_active"))
            if (
                active
                and metric is not None
                and metric.get("quality") == "exact"
                and metric.get("numeric_value") == 1
                and int(metric.get("ts", 0)) >= event_ts
            ):
                active = False
            if active:
                codes.add(code)
            continue
        if code in paired:
            family, active = paired[code]
            identity_value = tunnel or service or source or str(details.get("path", "global"))
            identity = (family, identity_value)
            if identity in resolved:
                continue
            resolved.add(identity)
            if family == "listener" and tunnel:
                ownership = metrics.get(("tunnel", tunnel, "listener_ownership_exact"))
                if (
                    active
                    and ownership is not None
                    and ownership.get("quality") == "exact"
                    and ownership.get("numeric_value") == 1
                    and int(ownership.get("ts", 0)) >= event_ts
                ):
                    active = False
            if family == "managed_env":
                active = active and bool(snapshot.get("invalid_managed_env_sources"))
            if active:
                codes.add("managed_env_invalid" if family == "managed_env" else code)
            continue
        severity = str(event.get("severity", "")).lower()
        if code == "pid_replaced" or severity in {"warning", "error", "critical"}:
            codes.add(code)
    if snapshot.get("invalid_managed_env_sources"):
        codes.add("managed_env_invalid")
    return codes


def _metric_map(snapshot: dict[str, object]) -> dict[tuple[str, str, str], dict[str, object]]:
    result: dict[tuple[str, str, str], dict[str, object]] = {}
    for raw in snapshot.get("metrics", []):
        if not isinstance(raw, dict):
            continue
        key = (
            str(raw.get("entity_type", "")),
            str(raw.get("entity_id", "")),
            str(raw.get("metric_name", "")),
        )
        result[key] = raw
    return result


def _quality(metrics: list[dict[str, object]]) -> str:
    if not metrics:
        return "unavailable"
    return max(
        (str(item.get("quality", "unavailable")) for item in metrics),
        key=lambda item: QUALITY_RANK.get(item, 3),
    )


def _result(
    status: str,
    reasons: list[tuple[str, str]],
    now: int,
    age: int | None,
    quality: str,
    entity_type: str,
    entity_id: str,
) -> HealthResult:
    return HealthResult(
        status,
        tuple(code for code, _message in reasons),
        tuple(message for _code, message in reasons),
        now,
        age,
        quality,
        entity_type,
        entity_id,
    )


def _threshold_status(
    metric: dict[str, object] | None,
    degraded: float,
    critical: float,
    code: str,
) -> tuple[str | None, tuple[str, str] | None]:
    if not metric or metric.get("numeric_value") is None:
        return None, None
    value = float(metric["numeric_value"])
    quality = str(metric.get("quality", "unavailable"))
    if value >= critical:
        if quality in {"exact", "derived"}:
            return "critical", (f"{code}_critical", f"{code} is critically high ({value:.1f}%)")
        return "degraded", (f"{code}_estimated_high", f"{code} estimate is high ({value:.1f}%)")
    if value >= degraded:
        return "degraded", (f"{code}_degraded", f"{code} is high ({value:.1f}%)")
    return None, None


def evaluate_node(
    snapshot: dict[str, object],
    now: int,
    policy: HealthPolicy = HealthPolicy(),
) -> HealthResult:
    cycle = snapshot.get("cycle")
    if not isinstance(cycle, dict):
        return _result("unknown", [("no_data", "No collector cycle is available")], now, None, "unavailable", "node", "local")
    collected_at = int(cycle.get("collected_at", 0))
    age = max(0, now - collected_at)
    if age > policy.stale_after_seconds:
        return _result("unknown", [("stale_data", f"Latest sample is {age}s old")], now, age, "unavailable", "node", "local")
    if not bool(cycle.get("success")):
        return _result("critical", [("collector_cycle_failed", "Latest collector cycle failed")], now, age, "exact", "node", "local")

    metrics = _metric_map(snapshot)
    host = [value for (kind, entity, _name), value in metrics.items() if kind in {"host", "filesystem"} and entity in {"local", "fs:/", "fs:/var/lib/gost-manager"}]
    reasons: list[tuple[str, str]] = []
    status = "healthy"
    checks = (
        (metrics.get(("host", "local", "cpu_utilization_percent")), policy.cpu_degraded_percent, 101.0, "cpu"),
        (metrics.get(("host", "local", "memory_used_percent")), policy.memory_degraded_percent, 101.0, "memory"),
        (metrics.get(("filesystem", "fs:/", "filesystem_used_percent")), policy.filesystem_degraded_percent, policy.filesystem_critical_percent, "filesystem"),
        (metrics.get(("host", "local", "conntrack_utilization_percent")), policy.conntrack_degraded_percent, policy.conntrack_critical_percent, "conntrack"),
        (metrics.get(("host", "local", "file_handles_utilization_percent")), policy.file_handles_degraded_percent, policy.file_handles_critical_percent, "file_handles"),
    )
    rank = {"healthy": 0, "degraded": 1, "unknown": 2, "critical": 3}
    for metric, degraded, critical, code in checks:
        candidate, reason = _threshold_status(metric, degraded, critical, code)
        if candidate and rank[candidate] > rank[status]:
            status = candidate
        if reason:
            reasons.append(reason)
    required = (
        metrics.get(("host", "local", "cpu_utilization_percent")),
        metrics.get(("host", "local", "memory_used_percent")),
        metrics.get(("filesystem", "fs:/", "filesystem_used_percent")),
    )
    if any(_unavailable(metric) for metric in required):
        status = "unknown" if status != "critical" else status
        reasons.append(("required_data_unavailable", "Required host utilization data is unavailable"))
    recent_codes = _required_event_codes(snapshot, now, policy)
    if recent_codes & {
        "metric_source_unavailable", "service_state_changed", "pid_replaced",
        "collection_failed", "database_retention_failed", "wal_checkpoint_failed",
        "listener_disappeared", "sampling_gap",
    }:
        if status == "healthy":
            status = "degraded"
        reasons.append(("recent_monitoring_event", "A recent service or source transition was recorded"))
    if "managed_env_invalid" in recent_codes:
        if status == "healthy":
            status = "degraded"
        reasons.append(
            ("managed_env_invalid", "A managed tunnel environment source is malformed")
        )
    if bool(snapshot.get("health_events_truncated")):
        if status == "healthy":
            status = "degraded"
        reasons.append(
            (
                "health_event_overflow",
                "Health event volume exceeded the bounded evaluation window",
            )
        )
    if not reasons:
        reasons.append(("observations_current", "Current observations are within configured thresholds"))
    return _result(status, reasons, now, age, _quality(host), "node", "local")


def evaluate_services(
    snapshot: dict[str, object],
    now: int,
    policy: HealthPolicy = HealthPolicy(),
) -> dict[str, HealthResult]:
    metrics = _metric_map(snapshot)
    required_services = _required_services(snapshot)
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for (kind, entity_id, _name), value in metrics.items():
        if kind == "service":
            grouped[entity_id].append(value)
    results: dict[str, HealthResult] = {}
    for entity_id, values in grouped.items():
        by_name = {str(item["metric_name"]): item for item in values}
        latest_ts = max(int(item.get("ts", 0)) for item in values)
        age = max(0, now - latest_ts)
        reasons: list[tuple[str, str]] = []
        active = by_name.get("service_active")
        listener = by_name.get("listener_owned_count")
        process = by_name.get("process_rss_bytes")
        if _unavailable(active):
            status = "unknown"
            reasons.append(("service_state_unavailable", "Service state is unavailable"))
        elif active.get("quality") != "exact":
            status = "unknown"
            reasons.append(("service_state_not_authoritative", "Service state is not authoritative"))
        elif float(active.get("numeric_value") or 0) == 0:
            status = "down"
            reasons.append(("service_inactive", "Service is inactive"))
        elif _unavailable(process):
            status = "unknown"
            reasons.append(("process_snapshot_unavailable", "Process snapshot is unavailable"))
        elif _unavailable(listener) or listener.get("quality") != "exact":
            status = "unknown"
            reasons.append(("listener_ownership_unavailable", "Listener ownership is unavailable"))
        elif float(listener.get("numeric_value") or 0) == 0:
            status = "down"
            reasons.append(("required_listener_missing", "No listener is owned by the active service"))
        else:
            status = "healthy"
            reasons.append(("service_observed", "Service is active and observations are current"))
        results[entity_id] = _result(status, reasons, now, age, _quality(values), "service", entity_id)
    if bool(snapshot.get("current_membership_authoritative")):
        for entity_id in required_services - set(results):
            results[entity_id] = _result(
                "unknown",
                [("service_data_unavailable", "Required service observations are unavailable")],
                now,
                None,
                "unavailable",
                "service",
                entity_id,
            )
    return results


def evaluate_tunnels(
    snapshot: dict[str, object],
    now: int,
    policy: HealthPolicy = HealthPolicy(),
) -> dict[str, HealthResult]:
    metrics = _metric_map(snapshot)
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for (kind, entity_id, _name), value in metrics.items():
        if kind == "tunnel":
            grouped[entity_id].append(value)
    results: dict[str, HealthResult] = {}
    for entity_id, values in grouped.items():
        by_name = {str(item["metric_name"]): item for item in values}
        latest_ts = max(int(item.get("ts", 0)) for item in values)
        age = max(0, now - latest_ts)
        ownership = by_name.get("listener_ownership_exact")
        active = by_name.get("service_active")
        reasons: list[tuple[str, str]] = []
        if _unavailable(active):
            status = "unknown"
            reasons.append(("service_state_unavailable", "Tunnel service state is unavailable"))
        elif active.get("quality") == "exact" and float(active.get("numeric_value") or 0) == 0:
            status = "down"
            reasons.append(("service_inactive", "Tunnel service is inactive"))
        elif active and active.get("quality") != "exact":
            status = "unknown"
            reasons.append(("service_state_not_authoritative", "Tunnel service state is not authoritative"))
        elif _unavailable(ownership) or ownership.get("quality") != "exact":
            status = "unknown"
            reasons.append(("listener_ownership_unavailable", "Listener ownership is unavailable"))
        elif float(ownership.get("numeric_value") or 0) == 0 and ownership.get("quality") == "exact":
            status = "down"
            reasons.append(("listener_missing", "Configured tunnel listener is missing"))
        else:
            status = "healthy"
            reasons.append(("tunnel_observed", "Tunnel listener and service are observed"))
        results[entity_id] = _result(status, reasons, now, age, _quality(values), "tunnel", entity_id)
    return results


def evaluate_snapshot(
    snapshot: dict[str, object],
    policy: HealthPolicy = HealthPolicy(),
) -> dict[str, object]:
    now = int(snapshot.get("generated_at", 0))
    node = evaluate_node(snapshot, now, policy)
    services = evaluate_services(snapshot, now, policy)
    tunnels = evaluate_tunnels(snapshot, now, policy)
    required_services = _required_services(snapshot)
    status = node.status
    reason_codes = list(node.reason_codes)
    reasons = list(node.reasons)
    rank = {"healthy": 0, "degraded": 1, "unknown": 2, "critical": 3}
    for entity_type, values in (("service", services), ("tunnel", tunnels)):
        for entity_id, result in values.items():
            if entity_type == "service" and entity_id not in required_services:
                continue
            candidate = "critical" if result.status == "down" else result.status
            if rank.get(candidate, 0) > rank.get(status, 0):
                status = candidate
            if result.status != "healthy":
                reason_codes.append(f"{entity_type}_{result.status}")
                reasons.append(
                    f"{entity_type.capitalize()} {entity_id} is {result.status}"
                )
    relevant_results = [
        result for key, result in services.items() if key in required_services
    ] + list(tunnels.values())
    if any(result.status != "healthy" for result in relevant_results):
        paired = [
            (code, reason)
            for code, reason in zip(reason_codes, reasons)
            if code != "observations_current"
        ]
        reason_codes = [code for code, _reason in paired]
        reasons = [reason for _code, reason in paired]
    overall = dataclasses.replace(
        node,
        status=status,
        reason_codes=tuple(reason_codes),
        reasons=tuple(reasons),
    )
    service_values = {}
    for key, value in services.items():
        rendered = value.to_dict()
        rendered["required"] = key in required_services
        service_values[key] = rendered
    return {
        "overall": overall.to_dict(),
        "services": service_values,
        "tunnels": {key: value.to_dict() for key, value in tunnels.items()},
    }
