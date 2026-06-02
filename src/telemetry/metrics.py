from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Optional

from pymongo.database import Database


def get_metric_summary_by_service(
    db: Database,
    start: datetime,
    end: datetime,
) -> list[dict[str, object]]:
    cursor = db["metrics"].find(
        {"timestamp": {"$gte": start, "$lte": end}},
        projection={
            "service_name": 1,
            "metric_name": 1,
            "value": 1,
            "unit": 1,
            "_id": 0,
        },
    )

    groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "values": [],
            "unit": None,
        }
    )

    for doc in cursor:
        service_name = doc.get("service_name")
        metric_name = doc.get("metric_name")
        value = doc.get("value")
        if not isinstance(service_name, str) or not isinstance(metric_name, str):
            continue
        if not isinstance(value, (int, float)):
            continue

        key = (service_name, metric_name)
        groups[key]["values"].append(float(value))
        unit = doc.get("unit")
        if isinstance(unit, str):
            groups[key]["unit"] = unit

    results: list[dict[str, object]] = []
    for (service_name, metric_name), data in sorted(groups.items()):
        values = data["values"]
        if not values:
            continue
        results.append(
            {
                "service_name": service_name,
                "metric_name": metric_name,
                "count": len(values),
                "avg_value": round(sum(values) / len(values), 2),
                "max_value": round(max(values), 2),
                "min_value": round(min(values), 2),
                "unit": data["unit"],
            }
        )

    return results


def get_metric_anomalies(
    db: Database,
    start: datetime,
    end: datetime,
) -> list[dict[str, object]]:
    summary = get_metric_summary_by_service(db, start, end)
    anomalies: list[dict[str, object]] = []

    for row in summary:
        metric_name = row["metric_name"]
        max_value = row["max_value"]
        if not isinstance(metric_name, str) or not isinstance(max_value, (int, float)):
            continue

        message: Optional[str] = None
        if metric_name == "process.cpu.utilization" and max_value >= 80:
            message = f"CPU utilization peaked at {max_value}% (threshold 80%)."
        elif metric_name == "process.memory.usage" and max_value >= 80:
            message = f"Memory usage peaked at {max_value}% (threshold 80%)."

        if message is not None:
            anomalies.append(
                {
                    "service_name": row["service_name"],
                    "metric_name": metric_name,
                    "max_value": max_value,
                    "message": message,
                }
            )

    return anomalies
