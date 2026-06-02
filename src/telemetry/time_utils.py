from __future__ import annotations

from datetime import datetime, timedelta

from pymongo.collection import Collection
from pymongo.database import Database


def get_collection_time_bounds(
    collection: Collection, time_field: str
) -> tuple[datetime | None, datetime | None]:
    query = {time_field: {"$exists": True, "$ne": None}}
    earliest_doc = collection.find_one(
        query,
        sort=[(time_field, 1)],
        projection={time_field: 1, "_id": 0},
    )
    latest_doc = collection.find_one(
        query,
        sort=[(time_field, -1)],
        projection={time_field: 1, "_id": 0},
    )

    if earliest_doc is None or latest_doc is None:
        return (None, None)

    earliest_value = earliest_doc.get(time_field)
    latest_value = latest_doc.get(time_field)

    if not isinstance(earliest_value, datetime) or not isinstance(
        latest_value, datetime
    ):
        return (None, None)

    return (earliest_value, latest_value)


def get_dataset_time_bounds(db: Database) -> tuple[datetime | None, datetime | None]:
    collection_fields: list[tuple[str, str]] = [
        ("spans", "start_time"),
        ("logs", "timestamp"),
        ("metrics", "timestamp"),
    ]

    starts: list[datetime] = []
    ends: list[datetime] = []

    for collection_name, time_field in collection_fields:
        start, end = get_collection_time_bounds(db[collection_name], time_field)
        if start is not None:
            starts.append(start)
        if end is not None:
            ends.append(end)

    if not starts or not ends:
        return (None, None)

    return (min(starts), max(ends))


def resolve_time_window(
    db: Database, window: str = "last_hour"
) -> tuple[datetime, datetime]:
    _, dataset_end = get_dataset_time_bounds(db)
    if dataset_end is None:
        raise ValueError("Dataset has no timestamps.")

    window_map: dict[str, timedelta] = {
        "last_15_minutes": timedelta(minutes=15),
        "last_30_minutes": timedelta(minutes=30),
        "last_hour": timedelta(hours=1),
        "last_2_hours": timedelta(hours=2),
    }

    delta = window_map.get(window)
    if delta is None:
        raise ValueError(f"Unsupported window: {window}")

    return (dataset_end - delta, dataset_end)
