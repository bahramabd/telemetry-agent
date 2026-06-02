from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Optional

from pymongo.database import Database

_LOG_FIELDS = {
    "timestamp": 1,
    "service_name": 1,
    "severity": 1,
    "message": 1,
    "trace_id": 1,
    "span_id": 1,
    "_id": 0,
}


def _log_to_dict(doc: dict[str, Any]) -> dict[str, object]:
    return {
        "timestamp": doc.get("timestamp"),
        "service_name": doc.get("service_name"),
        "severity": doc.get("severity"),
        "message": doc.get("message"),
        "trace_id": doc.get("trace_id"),
        "span_id": doc.get("span_id"),
    }


def get_logs_in_window(
    db: Database,
    start: datetime,
    end: datetime,
    service_name: Optional[str] = None,
    severities: Optional[list[str]] = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    query: dict[str, Any] = {"timestamp": {"$gte": start, "$lte": end}}
    if service_name is not None:
        query["service_name"] = service_name
    if severities is not None:
        query["severity"] = {"$in": severities}

    cursor = (
        db["logs"].find(query, projection=_LOG_FIELDS).sort("timestamp", 1).limit(limit)
    )
    return [_log_to_dict(doc) for doc in cursor]


def _normalize_log_message(message: str, max_length: int = 80) -> str:
    if len(message) > max_length:
        return message[:max_length] + "..."
    return message


def get_error_logs_summary(
    db: Database,
    start: datetime,
    end: datetime,
    limit: int = 10,
) -> list[dict[str, object]]:
    cursor = db["logs"].find(
        {
            "timestamp": {"$gte": start, "$lte": end},
            "severity": {"$in": ["ERROR", "WARN", "WARNING"]},
        },
        projection={
            "service_name": 1,
            "severity": 1,
            "message": 1,
            "_id": 0,
        },
    )

    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for doc in cursor:
        service = doc.get("service_name")
        severity = doc.get("severity")
        message = doc.get("message")
        if not isinstance(service, str) or not isinstance(severity, str):
            continue
        if not isinstance(message, str):
            message = str(message) if message is not None else ""
        message = _normalize_log_message(message)
        counts[(service, severity, message)] += 1

    results = [
        {
            "service_name": key[0],
            "severity": key[1],
            "message": key[2],
            "count": count,
        }
        for key, count in counts.items()
    ]
    results.sort(key=lambda r: -r["count"])
    return results[:limit]


def get_logs_for_trace_ids(
    db: Database,
    trace_ids: list[str],
    limit_per_trace: int = 20,
) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    for trace_id in trace_ids:
        cursor = (
            db["logs"]
            .find({"trace_id": trace_id}, projection=_LOG_FIELDS)
            .sort("timestamp", 1)
            .limit(limit_per_trace)
        )
        result[trace_id] = [_log_to_dict(doc) for doc in cursor]
    return result
