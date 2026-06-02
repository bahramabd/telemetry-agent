from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from pydantic import BaseModel
from pymongo.collection import Collection
from pymongo.database import Database


class TimeRange(BaseModel):
    start: datetime
    end: datetime
    label: str
    source: str


def get_collection_time_bounds(
    collection: Collection, time_field: str
) -> tuple[Optional[datetime], Optional[datetime]]:
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


def get_dataset_time_bounds(
    db: Database,
) -> tuple[Optional[datetime], Optional[datetime]]:
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


def get_dataset_reference_date(db: Database) -> date:
    _, dataset_end = get_dataset_time_bounds(db)
    if dataset_end is None:
        raise ValueError("Dataset has no timestamps.")
    return dataset_end.date()


def parse_time_on_reference_date(db: Database, time_str: str) -> datetime:
    ref_date = get_dataset_reference_date(db)
    raw = time_str.strip().lower()

    # Normalize spaces around am/pm
    raw = raw.replace(" ", "")

    formats = [
        "%H:%M",
        "%I%p",
        "%I:%M%p",
    ]

    for fmt in formats:
        try:
            t = datetime.strptime(raw, fmt).time()
            return datetime.combine(ref_date, t)
        except ValueError:
            continue

    raise ValueError(
        f"Unsupported time format '{time_str}'. Use formats like '14:00', '14:45', '2pm', or '2:45pm'."
    )


def resolve_time_range(
    db: Database,
    start_str: Optional[str] = None,
    end_str: Optional[str] = None,
    window_minutes: Optional[int] = None,
    default_minutes: int = 60,
) -> TimeRange:
    _, dataset_end = get_dataset_time_bounds(db)
    if dataset_end is None:
        raise ValueError("Dataset has no timestamps.")

    # Explicit range
    if start_str is not None and end_str is not None:
        start = parse_time_on_reference_date(db, start_str)
        end = parse_time_on_reference_date(db, end_str)
        if end <= start:
            raise ValueError(
                "End time must be after start time. Please provide an unambiguous time range."
            )
        label = f"{start_str}–{end_str}"
        return TimeRange(start=start, end=end, label=label, source="explicit_range")

    # Relative window
    if window_minutes is not None:
        if window_minutes <= 0:
            raise ValueError("window_minutes must be positive.")
        delta = timedelta(minutes=window_minutes)
        start = dataset_end - delta
        label = f"last {window_minutes} minutes"
        return TimeRange(
            start=start, end=dataset_end, label=label, source="relative_window"
        )

    # Default window
    delta = timedelta(minutes=default_minutes)
    start = dataset_end - delta
    label = f"last {default_minutes} minutes"
    return TimeRange(start=start, end=dataset_end, label=label, source="default_window")


_WINDOW_ALIASES: dict[str, int] = {
    "last_hour": 60,
    "last_15_minutes": 15,
    "last_30_minutes": 30,
    "last_2_hours": 120,
}

_DYNAMIC_WINDOW_PATTERN = re.compile(
    r"^last_(\d+)_(minute|minutes|min|mins|hour|hours|hr|hrs)$"
)


def _window_to_minutes(window: str) -> int:
    minutes = _WINDOW_ALIASES.get(window)
    if minutes is not None:
        return minutes

    match = _DYNAMIC_WINDOW_PATTERN.match(window)
    if not match:
        raise ValueError(f"Unsupported window: {window}")

    amount = int(match.group(1))
    unit = match.group(2)
    if amount <= 0:
        raise ValueError("Window duration must be positive.")

    if unit in ("hour", "hours", "hr", "hrs"):
        return amount * 60
    return amount


def resolve_time_window(
    db: Database, window: str = "last_hour"
) -> tuple[datetime, datetime]:
    minutes = _window_to_minutes(window)
    time_range = resolve_time_range(db, window_minutes=minutes)
    return (time_range.start, time_range.end)
