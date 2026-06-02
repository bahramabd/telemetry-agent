from __future__ import annotations

from pymongo.database import Database

from src.telemetry.time_utils import get_dataset_time_bounds


def get_collection_counts(db: Database) -> dict[str, int]:
    return {
        "spans": db["spans"].count_documents({}),
        "logs": db["logs"].count_documents({}),
        "metrics": db["metrics"].count_documents({}),
    }


def get_distinct_services(db: Database) -> list[str]:
    values: set[str] = set()
    for collection_name in ("spans", "logs", "metrics"):
        services = db[collection_name].distinct("service_name")
        for service in services:
            if isinstance(service, str):
                values.add(service)
    return sorted(values)


def get_distinct_metric_names(db: Database) -> list[str]:
    metric_names = db["metrics"].distinct("metric_name")
    return sorted(name for name in metric_names if isinstance(name, str))


def get_top_span_names(db: Database, limit: int = 10) -> list[dict[str, object]]:
    pipeline = [
        {"$match": {"name": {"$exists": True, "$ne": None}}},
        {"$group": {"_id": "$name", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": limit},
    ]
    results = db["spans"].aggregate(pipeline)
    return [{"name": row["_id"], "count": row["count"]} for row in results]


def get_status_code_counts(db: Database) -> list[dict[str, object]]:
    pipeline = [
        {"$match": {"status_code": {"$exists": True, "$ne": None}}},
        {"$group": {"_id": "$status_code", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    results = db["spans"].aggregate(pipeline)
    return [{"status_code": row["_id"], "count": row["count"]} for row in results]


def get_dataset_profile(db: Database) -> dict[str, object]:
    start, end = get_dataset_time_bounds(db)
    return {
        "counts": get_collection_counts(db),
        "time_bounds": {"start": start, "end": end},
        "services": get_distinct_services(db),
        "metric_names": get_distinct_metric_names(db),
        "top_span_names": get_top_span_names(db, limit=10),
        "status_code_counts": get_status_code_counts(db),
    }
