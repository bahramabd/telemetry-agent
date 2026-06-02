from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Literal, Optional

from pymongo.database import Database

from src.telemetry.health import (
    calculate_percentiles,
    get_service_health,
    is_error_span,
)
from src.telemetry.logs import get_error_logs_summary, get_logs_for_trace_ids
from src.telemetry.metrics import get_metric_anomalies, get_metric_summary_by_service
from src.telemetry.time_utils import (
    TimeRange,
    get_collection_time_bounds,
    resolve_time_range,
)
from src.telemetry.traces import (
    build_trace_tree_summary,
    get_error_spans,
    get_slowest_spans,
    get_top_slow_operations,
    get_trace_spans,
)

_LOG_KEYWORDS = ("connection pool", "timeout", "database", "mongodb")
_POOL_EXHAUSTION_LOG_PHRASES = (
    "connection pool",
    "pool timeout",
    "db timeout",
    "database timeout",
    "mongodb",
)
_STATUS_MESSAGE_KEYWORDS = (
    "connection pool",
    "pool timeout",
    "timeout",
    "database",
    "mongodb",
    "downstream database",
    "payment step",
)
_CHECKOUT_CORE_SERVICES = frozenset({"order-service", "payments-service", "mongodb"})


def _floor_to_bucket(dt: datetime, origin: datetime, bucket_minutes: int) -> datetime:
    bucket_seconds = bucket_minutes * 60
    elapsed = (dt - origin).total_seconds()
    if elapsed < 0:
        return origin
    index = int(elapsed // bucket_seconds)
    return origin + timedelta(seconds=index * bucket_seconds)


def _service_is_stressed(service: dict[str, Any]) -> bool:
    error_rate = service.get("error_rate_percent", 0.0)
    p95 = service.get("p95_latency_ms") or 0.0
    p99 = service.get("p99_latency_ms") or 0.0
    if not isinstance(error_rate, (int, float)):
        error_rate = 0.0
    return bool(
        error_rate >= 1.0
        or p95 >= 500
        or p99 >= 1000
        or service.get("status") in ("critical", "degraded")
    )


def find_incident_windows(
    db: Database,
    bucket_minutes: int = 5,
    min_error_rate_percent: float = 5.0,
    min_p95_latency_ms: float = 1000.0,
) -> list[dict[str, object]]:
    dataset_start, dataset_end = get_collection_time_bounds(db["spans"], "start_time")
    if dataset_start is None or dataset_end is None:
        return []

    origin = _floor_to_bucket(dataset_start, dataset_start, bucket_minutes)

    span_projection = {
        "service_name": 1,
        "start_time": 1,
        "duration_ms": 1,
        "status_code": 1,
        "status_message": 1,
        "attributes": 1,
        "_id": 0,
    }
    cursor = db["spans"].find(
        {"start_time": {"$gte": dataset_start, "$lte": dataset_end}},
        projection=span_projection,
    )

    bucket_services: dict[datetime, dict[str, dict[str, Any]]] = defaultdict(
        lambda: defaultdict(
            lambda: {
                "latencies": [],
                "error_spans": 0,
                "total_spans": 0,
            }
        )
    )

    for span in cursor:
        start_time = span.get("start_time")
        if not isinstance(start_time, datetime):
            continue

        service_name = span.get("service_name")
        if not isinstance(service_name, str) or service_name == "":
            continue

        bucket_start = _floor_to_bucket(start_time, origin, bucket_minutes)
        group = bucket_services[bucket_start][service_name]
        group["total_spans"] += 1
        if is_error_span(span):
            group["error_spans"] += 1
        duration = span.get("duration_ms")
        if isinstance(duration, (int, float)):
            group["latencies"].append(float(duration))

    suspicious_buckets: list[dict[str, Any]] = []
    current = origin
    while current <= dataset_end:
        bucket_end = min(
            current + timedelta(minutes=bucket_minutes) - timedelta(microseconds=1),
            dataset_end,
        )
        if bucket_end < current:
            break

        affected_services: list[str] = []
        max_error_rate = 0.0
        max_p99 = 0.0
        is_suspicious = False

        for service_name, data in bucket_services.get(current, {}).items():
            total = data["total_spans"]
            if total <= 0:
                continue

            error_rate = round(data["error_spans"] / total * 100.0, 2)
            percentiles = calculate_percentiles(data["latencies"])
            p95 = float(percentiles["p95"] or 0.0)
            p99 = float(percentiles["p99"] or 0.0)

            if (
                error_rate >= min_error_rate_percent
                or p95 >= min_p95_latency_ms
                or p99 >= 3000
            ):
                is_suspicious = True
                affected_services.append(service_name)
                max_error_rate = max(max_error_rate, error_rate)
                max_p99 = max(max_p99, p99)

        if is_suspicious:
            duration_minutes = max(
                1,
                int((bucket_end - current).total_seconds() // 60) + 1,
            )
            severity = (
                "critical" if max_error_rate >= 5 or max_p99 >= 3000 else "degraded"
            )
            suspicious_buckets.append(
                {
                    "start": current,
                    "end": bucket_end,
                    "duration_minutes": duration_minutes,
                    "affected_services": sorted(affected_services),
                    "max_error_rate_percent": round(max_error_rate, 2),
                    "max_p99_latency_ms": round(max_p99, 2),
                    "severity": severity,
                }
            )

        current += timedelta(minutes=bucket_minutes)

    if not suspicious_buckets:
        return []

    merged: list[dict[str, Any]] = [dict(suspicious_buckets[0])]
    for bucket in suspicious_buckets[1:]:
        prev = merged[-1]
        gap_minutes = (bucket["start"] - prev["end"]).total_seconds() / 60.0
        if gap_minutes <= bucket_minutes:
            prev["end"] = bucket["end"]
            prev["affected_services"] = sorted(
                set(prev["affected_services"]) | set(bucket["affected_services"])
            )
            prev["max_error_rate_percent"] = max(
                float(prev["max_error_rate_percent"]),
                float(bucket["max_error_rate_percent"]),
            )
            prev["max_p99_latency_ms"] = max(
                float(prev["max_p99_latency_ms"]),
                float(bucket["max_p99_latency_ms"]),
            )
            prev["duration_minutes"] = max(
                1,
                int((prev["end"] - prev["start"]).total_seconds() // 60) + 1,
            )
            prev["severity"] = (
                "critical"
                if prev["max_error_rate_percent"] >= 5
                or prev["max_p99_latency_ms"] >= 3000
                else "degraded"
            )
        else:
            merged.append(dict(bucket))

    merged.sort(key=lambda inc: inc["start"], reverse=True)
    return merged


def get_baseline_comparison(
    db: Database,
    incident_start: datetime,
    incident_end: datetime,
) -> list[dict[str, object]]:
    dataset_start, _ = get_collection_time_bounds(db["spans"], "start_time")
    if dataset_start is None:
        return []

    incident_duration = incident_end - incident_start
    if incident_duration <= timedelta(0):
        return []

    baseline_end = incident_start
    baseline_start = incident_start - incident_duration
    if baseline_start < dataset_start:
        baseline_start = dataset_start
    if baseline_start >= baseline_end:
        return []

    baseline_health = {
        str(s["service_name"]): s
        for s in get_service_health(db, baseline_start, baseline_end)
        if isinstance(s.get("service_name"), str)
    }
    incident_health = {
        str(s["service_name"]): s
        for s in get_service_health(db, incident_start, incident_end)
        if isinstance(s.get("service_name"), str)
    }

    comparisons: list[dict[str, object]] = []
    for service_name, incident_svc in incident_health.items():
        baseline_svc = baseline_health.get(service_name)
        if baseline_svc is None:
            continue
        if int(baseline_svc.get("total_spans", 0)) <= 0:
            continue

        baseline_error_rate = round(
            float(baseline_svc.get("error_rate_percent", 0.0)), 2
        )
        incident_error_rate = round(
            float(incident_svc.get("error_rate_percent", 0.0)), 2
        )
        error_rate_delta = round(incident_error_rate - baseline_error_rate, 2)

        baseline_p99 = baseline_svc.get("p99_latency_ms")
        incident_p99 = incident_svc.get("p99_latency_ms")

        latency_multiplier: Optional[float] = None
        if (
            isinstance(baseline_p99, (int, float))
            and isinstance(incident_p99, (int, float))
            and baseline_p99 > 0
        ):
            latency_multiplier = round(float(incident_p99) / float(baseline_p99), 2)

        comparisons.append(
            {
                "service_name": service_name,
                "baseline_error_rate_percent": baseline_error_rate,
                "incident_error_rate_percent": incident_error_rate,
                "error_rate_delta": error_rate_delta,
                "baseline_p99_latency_ms": baseline_p99,
                "incident_p99_latency_ms": incident_p99,
                "latency_multiplier": latency_multiplier,
            }
        )

    comparisons.sort(
        key=lambda c: (
            -(
                c["latency_multiplier"]
                if isinstance(c["latency_multiplier"], (int, float))
                else 0.0
            ),
            -float(c["error_rate_delta"]),  # type: ignore[arg-type]
        )
    )
    return comparisons


def get_failure_timeline(
    db: Database,
    start: datetime,
    end: datetime,
) -> list[dict[str, object]]:
    cursor = db["spans"].find(
        {"start_time": {"$gte": start, "$lte": end}},
        projection={
            "service_name": 1,
            "name": 1,
            "start_time": 1,
            "duration_ms": 1,
            "status_code": 1,
            "status_message": 1,
            "attributes": 1,
            "_id": 0,
        },
    )

    earliest_error: dict[str, dict[str, object]] = {}
    earliest_slow: dict[str, dict[str, object]] = {}

    for span in cursor:
        service_name = span.get("service_name")
        start_time = span.get("start_time")
        if not isinstance(service_name, str) or service_name == "":
            continue
        if not isinstance(start_time, datetime):
            continue

        operation = span.get("name", "unknown")
        duration_ms = span.get("duration_ms")
        status_code = span.get("status_code", "")
        status_message = span.get("status_message") or ""

        entry = {
            "service_name": service_name,
            "first_problem_time": start_time,
            "operation": operation,
            "duration_ms": duration_ms,
            "status_code": status_code,
            "status_message": status_message,
        }

        if is_error_span(span):
            existing = earliest_error.get(service_name)
            if existing is None or start_time < existing["first_problem_time"]:  # type: ignore[operator]
                earliest_error[service_name] = entry
            continue

        if isinstance(duration_ms, (int, float)) and float(duration_ms) >= 1000:
            existing = earliest_slow.get(service_name)
            if existing is None or start_time < existing["first_problem_time"]:  # type: ignore[operator]
                earliest_slow[service_name] = entry

    timeline: list[dict[str, object]] = []
    all_services = set(earliest_error) | set(earliest_slow)
    for service_name in all_services:
        if service_name in earliest_error:
            item = dict(earliest_error[service_name])
            item["signal_type"] = "error"
        else:
            item = dict(earliest_slow[service_name])
            item["signal_type"] = "slow"
        timeline.append(item)

    timeline.sort(
        key=lambda item: item["first_problem_time"]  # type: ignore[return-value, arg-type]
    )
    return timeline


def _determine_affected_flow(affected_services: list[str]) -> str:
    affected = set(affected_services)
    checkout_core_hit = bool(affected & _CHECKOUT_CORE_SERVICES)
    catalog_hit = "catalog-service" in affected

    if checkout_core_hit and catalog_hit:
        return "mixed/unknown"
    if checkout_core_hit:
        return "checkout"
    if catalog_hit:
        return "listing"
    return "mixed/unknown"


def _build_blast_radius(affected_services: list[str], affected_flow: str) -> str:
    if not affected_services:
        return "No services showed significant degradation in the selected window."
    services = ", ".join(affected_services)
    return (
        f"The {affected_flow} flow was impacted, with elevated errors or latency "
        f"observed in: {services}."
    )


def _collect_log_keyword_evidence(
    error_logs_summary: list[dict[str, object]],
) -> list[str]:
    evidence: list[str] = []
    for entry in error_logs_summary:
        message = entry.get("message")
        if not isinstance(message, str):
            continue
        lower = message.lower()
        if any(keyword in lower for keyword in _LOG_KEYWORDS):
            service = entry.get("service_name", "unknown")
            severity = entry.get("severity", "")
            count = entry.get("count", 0)
            evidence.append(f"[{service}/{severity} x{count}] {message}")
    return evidence


def _logs_contain_pool_exhaustion_signals(
    error_logs_summary: list[dict[str, object]],
) -> bool:
    for entry in error_logs_summary:
        message = entry.get("message")
        if not isinstance(message, str):
            continue
        lower = message.lower()
        if any(phrase in lower for phrase in _POOL_EXHAUSTION_LOG_PHRASES):
            return True
    return False


def _text_contains_db_pool_signals(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in _POOL_EXHAUSTION_LOG_PHRASES) or any(
        keyword in lower for keyword in _STATUS_MESSAGE_KEYWORDS
    )


def _collect_span_status_evidence(
    error_spans: list[dict[str, object]],
    slowest_spans: list[dict[str, object]],
    limit: int = 5,
) -> list[str]:
    seen: set[tuple[str, str, str]] = set()
    evidence: list[str] = []

    for span in error_spans + slowest_spans:
        status_message = span.get("status_message")
        if not isinstance(status_message, str) or status_message == "":
            continue
        if not _text_contains_db_pool_signals(status_message):
            continue

        service_name = str(span.get("service_name", "unknown"))
        operation = str(span.get("name", "unknown"))
        key = (service_name, operation, status_message)
        if key in seen:
            continue
        seen.add(key)
        evidence.append(f"{service_name}/{operation}: {status_message}")
        if len(evidence) >= limit:
            break

    return evidence


def _infer_root_cause(
    service_health: list[dict[str, object]],
    error_logs_summary: list[dict[str, object]],
    metric_anomalies: list[dict[str, object]],
    top_slow_operations: list[dict[str, object]],
    span_status_evidence: list[str],
) -> tuple[str, Literal["low", "medium", "high"], list[str]]:
    health_by_name = {
        str(s["service_name"]): s
        for s in service_health
        if isinstance(s.get("service_name"), str)
    }

    span_signals: list[str] = []
    mongodb = health_by_name.get("mongodb")
    payments = health_by_name.get("payments-service")
    order_svc = health_by_name.get("order-service")

    mongodb_stressed = mongodb is not None and _service_is_stressed(mongodb)
    payments_stressed = payments is not None and _service_is_stressed(payments)
    order_stressed = order_svc is not None and _service_is_stressed(order_svc)

    if mongodb is not None and mongodb_stressed:
        span_signals.append(
            f"mongodb error_rate={mongodb['error_rate_percent']}%, "
            f"p99={mongodb.get('p99_latency_ms')} ms"
        )
    if payments is not None and payments_stressed:
        span_signals.append(
            f"payments-service error_rate={payments['error_rate_percent']}%, "
            f"p99={payments.get('p99_latency_ms')} ms"
        )
    if order_svc is not None and order_stressed:
        span_signals.append(
            f"order-service error_rate={order_svc['error_rate_percent']}%, "
            f"p99={order_svc.get('p99_latency_ms')} ms"
        )

    log_evidence = _collect_log_keyword_evidence(error_logs_summary)
    metric_messages = [
        str(a["message"]) for a in metric_anomalies if isinstance(a.get("message"), str)
    ]

    probable_root_cause = ""
    confidence: Literal["low", "medium", "high"] = "low"

    has_log_pool_signals = _logs_contain_pool_exhaustion_signals(error_logs_summary)
    has_status_pool_signals = bool(span_status_evidence)
    has_db_pool_evidence = has_log_pool_signals or has_status_pool_signals

    if mongodb_stressed and payments_stressed and has_db_pool_evidence:
        probable_root_cause = (
            "The most likely root cause is MongoDB connection pool exhaustion or "
            "database timeout behavior affecting payments-service. Payment charge "
            "operations became slow or failed, which propagated to order-service "
            "checkout failures."
        )
        if has_log_pool_signals and has_status_pool_signals:
            confidence = "high"
        elif has_log_pool_signals:
            confidence = "high"
        else:
            confidence = "medium"
    elif mongodb_stressed and payments_stressed:
        probable_root_cause = (
            "A database dependency issue affecting mongodb appears to be degrading "
            "payments-service (and likely the broader checkout flow)."
        )
        confidence = "medium"
        if log_evidence:
            confidence = "high"
    elif log_evidence and mongodb_stressed:
        probable_root_cause = (
            "Log messages reference database connectivity or timeouts alongside "
            "mongodb span degradation, indicating a likely database dependency failure."
        )
        confidence = "high" if payments_stressed else "medium"
    elif log_evidence:
        probable_root_cause = (
            "Error logs reference database connectivity or timeouts; spans suggest "
            "downstream services are failing while handling checkout-related work."
        )
        confidence = "medium"
    elif span_signals:
        top_op = top_slow_operations[0] if top_slow_operations else None
        if top_op:
            probable_root_cause = (
                f"Span evidence shows elevated errors/latency, with the slowest "
                f"operation {top_op['service_name']}/{top_op['operation']} "
                f"(error_rate={top_op['error_rate_percent']}%, "
                f"max_latency={top_op['max_latency_ms']} ms)."
            )
        else:
            probable_root_cause = (
                "Span evidence shows elevated errors or latency in: "
                + "; ".join(span_signals)
                + "."
            )
        confidence = "medium"
    else:
        probable_root_cause = "Root cause is uncertain; telemetry shows only weak or inconclusive symptoms."
        confidence = "low"

    if metric_messages and confidence != "high":
        if confidence == "low":
            confidence = "medium"
        probable_root_cause += (
            " Resource metrics also show elevated CPU or memory utilization as a "
            "possible contributing factor."
        )

    return probable_root_cause, confidence, log_evidence


def _recommended_next_steps(
    incident_detected: bool,
    affected_flow: str,
    metric_anomalies: list[dict[str, object]],
) -> list[str]:
    if not incident_detected:
        return [
            "Continue monitoring service health and error rates.",
            "Re-run RCA if new symptoms appear.",
        ]

    steps = [
        "Inspect mongodb and payments-service traces for timeout and connection errors.",
        "Review database connection pool settings and saturation during the incident window.",
        f"Validate {affected_flow} flow dependencies (order-service -> cart-service -> payments-service -> mongodb).",
    ]
    if metric_anomalies:
        steps.append(
            "Check process CPU and memory utilization on affected services for resource pressure."
        )
    steps.append(
        "Compare error log messages across affected services for shared failure patterns."
    )
    return steps


def run_rca(
    db: Database,
    time_range: Optional[TimeRange] = None,
) -> dict[str, object]:
    incident_from_discovery = False

    if time_range is None:
        incidents = find_incident_windows(db)
        if incidents:
            latest = incidents[0]
            time_range = TimeRange(
                start=latest["start"],  # type: ignore[arg-type]
                end=latest["end"],  # type: ignore[arg-type]
                label=f"discovered incident ({latest['start']} – {latest['end']})",
                source="incident_discovery",
            )
            incident_from_discovery = True
        else:
            time_range = resolve_time_range(db)
    else:
        incident_from_discovery = False

    start = time_range.start
    end = time_range.end

    service_health = get_service_health(db, start, end)
    top_slow_operations = get_top_slow_operations(db, start, end)
    error_spans = get_error_spans(db, start, end)
    slowest_spans = get_slowest_spans(db, start, end)
    error_logs_summary = get_error_logs_summary(db, start, end)
    metric_summary = get_metric_summary_by_service(db, start, end)
    metric_anomalies = get_metric_anomalies(db, start, end)
    baseline_comparison = get_baseline_comparison(db, start, end)
    failure_timeline = get_failure_timeline(db, start, end)
    span_status_evidence = _collect_span_status_evidence(error_spans, slowest_spans)

    affected_services = sorted(
        {
            str(s["service_name"])
            for s in service_health
            if _service_is_stressed(s) and isinstance(s.get("service_name"), str)
        }
    )

    incident_detected = incident_from_discovery or bool(affected_services)
    if not incident_detected:
        for svc in service_health:
            if svc.get("status") in ("critical", "degraded"):
                incident_detected = True
                break

    if incident_detected and not affected_services:
        affected_services = sorted(
            {
                str(s["service_name"])
                for s in service_health
                if s.get("status") in ("critical", "degraded")
                and isinstance(s.get("service_name"), str)
            }
        )

    critical_count = sum(1 for s in service_health if s.get("status") == "critical")
    degraded_count = sum(1 for s in service_health if s.get("status") == "degraded")
    if critical_count > 0:
        severity = "critical"
    elif degraded_count > 0 or incident_detected:
        severity = "degraded"
    else:
        severity = "healthy"

    affected_flow = _determine_affected_flow(affected_services)
    blast_radius = _build_blast_radius(affected_services, affected_flow)

    probable_root_cause, confidence, log_keyword_evidence = _infer_root_cause(
        service_health,
        error_logs_summary,
        metric_anomalies,
        top_slow_operations,
        span_status_evidence,
    )

    trace_ids: list[str] = []
    for span in error_spans + slowest_spans:
        trace_id = span.get("trace_id")
        if isinstance(trace_id, str) and trace_id not in trace_ids:
            trace_ids.append(trace_id)
        if len(trace_ids) >= 3:
            break

    representative_traces: list[dict[str, object]] = []
    logs_by_trace = get_logs_for_trace_ids(db, trace_ids)
    for trace_id in trace_ids:
        spans = get_trace_spans(db, trace_id)
        representative_traces.append(
            {
                "trace_id": trace_id,
                "span_summary": build_trace_tree_summary(spans),
                "logs": logs_by_trace.get(trace_id, []),
            }
        )

    return {
        "time_range": time_range,
        "incident_detected": incident_detected,
        "severity": severity,
        "probable_root_cause": probable_root_cause,
        "confidence": confidence,
        "affected_services": affected_services,
        "affected_flow": affected_flow,
        "blast_radius": blast_radius,
        "service_health": service_health,
        "top_slow_operations": top_slow_operations,
        "error_logs_summary": error_logs_summary,
        "metric_summary": metric_summary,
        "metric_anomalies": metric_anomalies,
        "error_spans": error_spans,
        "slowest_spans": slowest_spans,
        "log_keyword_evidence": log_keyword_evidence,
        "baseline_comparison": baseline_comparison,
        "failure_timeline": failure_timeline,
        "span_status_evidence": span_status_evidence,
        "representative_traces": representative_traces,
        "recommended_next_steps": _recommended_next_steps(
            incident_detected, affected_flow, metric_anomalies
        ),
    }
