from __future__ import annotations

from datetime import date, datetime
from typing import cast

from pydantic import BaseModel

from src.telemetry.time_utils import TimeRange


def serialize_for_llm(value: object) -> object:
    """Convert telemetry results into JSON-safe objects for LLM prompts.

    This does not mutate the original value. It recursively converts:
    - TimeRange objects to dictionaries
    - datetime/date values to ISO strings
    - Pydantic models to dictionaries
    - dictionaries, lists, tuples, and sets recursively
    """

    if isinstance(value, TimeRange):
        return {
            "start": value.start.isoformat(),
            "end": value.end.isoformat(),
            "label": value.label,
            "source": value.source,
        }

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, BaseModel):
        return serialize_for_llm(value.model_dump())

    if isinstance(value, dict):
        return {str(key): serialize_for_llm(item) for key, item in value.items()}

    if isinstance(value, set):
        return [serialize_for_llm(item) for item in sorted(str(i) for i in value)]
    if isinstance(value, (list, tuple)):
        return [serialize_for_llm(item) for item in value]

    return value


def serialize_rca_result_for_llm(
    result: dict[str, object],
) -> dict[str, object]:
    """Return a JSON-safe copy of an RCA result for future LLM summarization."""

    serialized = serialize_for_llm(result)

    if not isinstance(serialized, dict):
        raise TypeError("Serialized RCA result must be a dictionary.")

    return cast(dict[str, object], serialized)
