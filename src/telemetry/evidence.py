from __future__ import annotations

from pymongo.database import Database

from src.telemetry.health import get_service_health
from src.telemetry.logs import get_error_logs_summary, get_logs_in_window
from src.telemetry.metrics import get_metric_anomalies, get_metric_summary_by_service
from src.telemetry.time_utils import TimeRange
from src.telemetry.traces import (
    get_error_spans,
    get_slowest_spans,
    get_top_slow_operations,
)


def collect_evidence_bundle(
    db: Database,
    time_range: TimeRange,
) -> dict[str, object]:
    """Collect cross-signal telemetry evidence for LLM answer synthesis.

    This function computes facts only. It does not call an LLM and does not
    interpret root cause. It gathers spans, logs, and metrics for the same
    time range so the LLM can reason over a grounded evidence bundle.
    """

    start = time_range.start
    end = time_range.end

    return {
        "time_range": time_range,
        "service_health": get_service_health(db, start, end),
        "top_slow_operations": get_top_slow_operations(db, start, end, limit=10),
        "error_spans": get_error_spans(db, start, end, limit=10),
        "slowest_spans": get_slowest_spans(db, start, end, limit=10),
        "error_logs_summary": get_error_logs_summary(db, start, end, limit=10),
        "recent_error_logs": get_logs_in_window(
            db,
            start,
            end,
            severities=["ERROR", "WARN", "WARNING"],
            limit=20,
        ),
        "metric_summary": get_metric_summary_by_service(db, start, end),
        "metric_anomalies": get_metric_anomalies(db, start, end),
    }
