from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from pymongo.database import Database

from src.telemetry.health import is_error_span

_SPAN_FIELDS = {
    "trace_id": 1,
    "span_id": 1,
    "parent_span_id": 1,
    "service_name": 1,
    "name": 1,
    "start_time": 1,
    "duration_ms": 1,
    "status_code": 1,
    "status_message": 1,
    "attributes": 1,
    "_id": 0,
}


def _span_to_dict(
    doc: dict[str, Any], include_attributes: bool = True
) -> dict[str, object]:
    result: dict[str, object] = {
        "trace_id": doc.get("trace_id"),
        "span_id": doc.get("span_id"),
        "parent_span_id": doc.get("parent_span_id"),
        "service_name": doc.get("service_name"),
        "name": doc.get("name"),
        "start_time": doc.get("start_time"),
        "duration_ms": doc.get("duration_ms"),
        "status_code": doc.get("status_code"),
        "status_message": doc.get("status_message"),
    }
    if include_attributes:
        result["attributes"] = doc.get("attributes")
    return result


def get_error_spans(
    db: Database,
    start: datetime,
    end: datetime,
    limit: int = 50,
) -> list[dict[str, object]]:
    cursor = db["spans"].find(
        {"start_time": {"$gte": start, "$lte": end}},
        projection=_SPAN_FIELDS,
    )

    errors: list[dict[str, object]] = []
    for doc in cursor:
        if is_error_span(doc):
            errors.append(_span_to_dict(doc))

    errors.sort(
        key=lambda s: (
            float(s["duration_ms"])
            if isinstance(s["duration_ms"], (int, float))
            else 0.0
        ),
        reverse=True,
    )
    return errors[:limit]


def get_slowest_spans(
    db: Database,
    start: datetime,
    end: datetime,
    limit: int = 10,
) -> list[dict[str, object]]:
    cursor = db["spans"].find(
        {"start_time": {"$gte": start, "$lte": end}},
        projection=_SPAN_FIELDS,
    )

    spans = [
        _span_to_dict(doc)
        for doc in cursor
        if isinstance(doc.get("duration_ms"), (int, float))
    ]
    spans.sort(
        key=lambda s: (
            float(s["duration_ms"])
            if isinstance(s["duration_ms"], (int, float))
            else 0.0
        ),
        reverse=True,
    )
    return spans[:limit]


def get_top_slow_operations(
    db: Database,
    start: datetime,
    end: datetime,
    limit: int = 10,
) -> list[dict[str, object]]:
    cursor = db["spans"].find(
        {"start_time": {"$gte": start, "$lte": end}},
        projection=_SPAN_FIELDS,
    )

    groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "latencies": [],
            "error_count": 0,
            "count": 0,
        }
    )

    for doc in cursor:
        service_name = doc.get("service_name")
        name = doc.get("name")
        if not isinstance(service_name, str) or not isinstance(name, str):
            continue

        key = (service_name, name)
        groups[key]["count"] += 1
        duration = doc.get("duration_ms")
        if isinstance(duration, (int, float)):
            groups[key]["latencies"].append(float(duration))
        if is_error_span(doc):
            groups[key]["error_count"] += 1

    results: list[dict[str, object]] = []
    for (service_name, operation), data in groups.items():
        count = data["count"]
        if count == 0:
            continue
        latencies = data["latencies"]
        avg_latency_ms = (
            round(sum(latencies) / len(latencies), 2) if latencies else None
        )
        max_latency_ms = round(max(latencies), 2) if latencies else None
        error_count = data["error_count"]
        error_rate_percent = round(error_count / count * 100.0, 2)

        results.append(
            {
                "service_name": service_name,
                "operation": operation,
                "count": count,
                "avg_latency_ms": avg_latency_ms,
                "max_latency_ms": max_latency_ms,
                "error_count": error_count,
                "error_rate_percent": error_rate_percent,
            }
        )

    results.sort(
        key=lambda r: (-r["error_rate_percent"], -(r["max_latency_ms"] or 0.0)),
    )
    return results[:limit]


def get_trace_spans(db: Database, trace_id: str) -> list[dict[str, object]]:
    cursor = (
        db["spans"]
        .find({"trace_id": trace_id}, projection=_SPAN_FIELDS)
        .sort("start_time", 1)
    )
    return [_span_to_dict(doc, include_attributes=False) for doc in cursor]


def build_trace_tree_summary(spans: list[dict[str, object]]) -> list[str]:
    if not spans:
        return []

    by_id: dict[str, dict[str, object]] = {}
    children: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []

    for span in spans:
        span_id = span.get("span_id")
        if not isinstance(span_id, str):
            continue
        by_id[span_id] = span

    for span_id, span in by_id.items():
        parent_id = span.get("parent_span_id")
        if isinstance(parent_id, str) and parent_id in by_id:
            children[parent_id].append(span_id)
        else:
            roots.append(span_id)

    lines: list[str] = []

    def format_line(span: dict[str, object], indent: int) -> str:
        service = span.get("service_name", "unknown")
        name = span.get("name", "unknown")
        duration = span.get("duration_ms", "n/a")
        status_code = span.get("status_code", "")
        status_message = span.get("status_message") or ""
        prefix = "  " * indent
        return f"{prefix}{service} | {name} | {duration} ms | {status_code} | {status_message}"

    def walk(span_id: str, indent: int) -> None:
        span = by_id.get(span_id)
        if span is None:
            return
        lines.append(format_line(span, indent))
        for child_id in sorted(children.get(span_id, [])):
            walk(child_id, indent + 1)

    if not roots:
        for span in spans:
            lines.append(format_line(span, 0))
        return lines

    for root_id in sorted(roots):
        walk(root_id, 0)

    return lines
