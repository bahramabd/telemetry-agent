from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
from pymongo.database import Database

from src.telemetry.time_utils import TimeRange


def _http_status_is_server_error(http_status: Any) -> bool:
    if isinstance(http_status, (int, float)):
        return http_status >= 500
    if isinstance(http_status, str):
        try:
            return float(http_status) >= 500
        except ValueError:
            return False
    return False


def is_error_span(span: Dict[str, Any]) -> bool:
    status_code = span.get("status_code")
    if isinstance(status_code, str) and status_code != "OK":
        return True

    attributes = span.get("attributes")
    if not isinstance(attributes, dict):
        return False

    http_status = attributes.get("http.status_code")
    if http_status is not None and _http_status_is_server_error(http_status):
        return True

    error_type = attributes.get("error.type")
    if isinstance(error_type, str) and error_type != "":
        return True

    return False


def calculate_percentiles(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"p50": None, "p95": None, "p99": None}

    arr = np.array(values, dtype=float)
    p50, p95, p99 = np.percentile(arr, [50, 95, 99])
    return {
        "p50": round(float(p50), 2),
        "p95": round(float(p95), 2),
        "p99": round(float(p99), 2),
    }


def _status_severity(status: str) -> int:
    if status == "critical":
        return 2
    if status == "degraded":
        return 1
    return 0


def get_service_health(
    db: Database, start: datetime, end: datetime
) -> List[Dict[str, Any]]:
    cursor = db["spans"].find(
        {
            "start_time": {"$gte": start, "$lte": end},
            "service_name": {"$exists": True, "$ne": None},
        }
    )

    groups: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "service_name": "",
            "latencies": [],
            "total_spans": 0,
            "error_spans": 0,
        }
    )

    for span in cursor:
        service_name = span.get("service_name")
        if not isinstance(service_name, str) or service_name == "":
            continue

        duration = span.get("duration_ms")
        if isinstance(duration, (int, float)):
            groups[service_name]["latencies"].append(float(duration))

        groups[service_name]["service_name"] = service_name
        groups[service_name]["total_spans"] += 1

        if is_error_span(span):
            groups[service_name]["error_spans"] += 1

    results: List[Dict[str, Any]] = []
    for service_data in groups.values():
        total = service_data["total_spans"]
        errors = service_data["error_spans"]
        latencies = service_data["latencies"]

        error_rate_percent = (errors / total * 100.0) if total > 0 else 0.0
        percentiles = calculate_percentiles(latencies)
        avg_latency_ms: Optional[float] = (
            round(float(np.mean(latencies)), 2) if latencies else None
        )
        max_latency_ms: Optional[float] = (
            round(max(latencies), 2) if latencies else None
        )

        status = "healthy"
        p95 = percentiles["p95"] or 0.0
        p99 = percentiles["p99"] or 0.0

        if error_rate_percent >= 5 or p99 >= 3000:
            status = "critical"
        elif error_rate_percent >= 1 or p95 >= 1000:
            status = "degraded"

        results.append(
            {
                "service_name": service_data["service_name"],
                "total_spans": total,
                "error_spans": errors,
                "error_rate_percent": round(error_rate_percent, 2),
                "avg_latency_ms": avg_latency_ms,
                "p50_latency_ms": percentiles["p50"],
                "p95_latency_ms": percentiles["p95"],
                "p99_latency_ms": percentiles["p99"],
                "max_latency_ms": max_latency_ms,
                "status": status,
            }
        )

    results.sort(
        key=lambda r: (
            -_status_severity(r["status"]),
            -r["error_rate_percent"],
            -(r["p99_latency_ms"] or 0.0),
        )
    )
    return results


def get_highest_error_rate_service(
    db: Database, start: datetime, end: datetime
) -> Optional[Dict[str, Any]]:
    services = get_service_health(db, start, end)
    if not services:
        return None
    return max(services, key=lambda s: s["error_rate_percent"])


def get_service_latency(
    db: Database, service_name: str, start: datetime, end: datetime
) -> Optional[Dict[str, Any]]:
    cursor = db["spans"].find(
        {
            "service_name": service_name,
            "start_time": {"$gte": start, "$lte": end},
        }
    )

    latencies: List[float] = []
    total_spans = 0
    for span in cursor:
        total_spans += 1
        duration = span.get("duration_ms")
        if isinstance(duration, (int, float)):
            latencies.append(float(duration))

    if total_spans == 0:
        return None

    percentiles = calculate_percentiles(latencies)
    avg_latency_ms: Optional[float] = (
        round(float(np.mean(latencies)), 2) if latencies else None
    )
    max_latency_ms: Optional[float] = round(max(latencies), 2) if latencies else None

    return {
        "service_name": service_name,
        "total_spans": total_spans,
        "avg_latency_ms": avg_latency_ms,
        "p50_latency_ms": percentiles["p50"],
        "p95_latency_ms": percentiles["p95"],
        "p99_latency_ms": percentiles["p99"],
        "max_latency_ms": max_latency_ms,
    }


def get_overall_health(db: Database, time_range: TimeRange) -> Dict[str, Any]:
    services = get_service_health(db, time_range.start, time_range.end)

    critical_services = [s for s in services if s["status"] == "critical"]
    degraded_services = [s for s in services if s["status"] == "degraded"]
    healthy_services = [s for s in services if s["status"] == "healthy"]

    if critical_services:
        overall_status = "critical"
    elif degraded_services:
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    highest_error = (
        max(services, key=lambda s: s["error_rate_percent"]) if services else None
    )
    slowest_by_p99 = (
        max(services, key=lambda s: s["p99_latency_ms"] or 0.0) if services else None
    )

    summary = {
        "total_services": len(services),
        "critical_services": len(critical_services),
        "degraded_services": len(degraded_services),
        "healthy_services": len(healthy_services),
        "highest_error_rate_service": (
            highest_error["service_name"] if highest_error else None
        ),
        "slowest_service_by_p99": (
            slowest_by_p99["service_name"] if slowest_by_p99 else None
        ),
    }

    return {
        "time_range": time_range,
        "overall_status": overall_status,
        "services": services,
        "summary": summary,
    }


def get_checkout_flow_health(db: Database, time_range: TimeRange) -> Dict[str, Any]:
    services = get_service_health(db, time_range.start, time_range.end)
    checkout_names = {"order-service", "cart-service", "payments-service", "mongodb"}
    checkout_services = [s for s in services if s["service_name"] in checkout_names]

    status_by_name = {s["service_name"]: s["status"] for s in checkout_services}

    checkout_status = "healthy"
    if (
        status_by_name.get("order-service") == "critical"
        or status_by_name.get("payments-service") == "critical"
    ):
        checkout_status = "critical"
    elif any(status in {"critical", "degraded"} for status in status_by_name.values()):
        checkout_status = "degraded"

    interpretation = (
        "Checkout flow is operating within normal latency and error thresholds."
        if checkout_status == "healthy"
        else "Checkout flow shows elevated latency or error rates in one or more services."
    )

    return {
        "time_range": time_range,
        "checkout_status": checkout_status,
        "services": checkout_services,
        "interpretation": interpretation,
    }
