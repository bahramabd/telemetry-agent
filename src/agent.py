from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel
from pymongo.database import Database

from src.telemetry.health import (
    get_checkout_flow_health,
    get_highest_error_rate_service,
    get_overall_health,
    get_service_latency,
)
from src.telemetry.time_utils import TimeRange, resolve_time_range


_VALID_RELATIVE_UNITS = frozenset(
    {"minute", "minutes", "min", "mins", "hour", "hours", "hr", "hrs"}
)


class AgentIntent(BaseModel):
    intent: str
    original_question: str
    service_name: Optional[str] = None
    metric: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    window_minutes: Optional[int] = None
    time_parse_error: Optional[str] = None


def parse_question_fallback(question: str) -> AgentIntent:
    text = question.lower()

    intent = "unsupported"
    service: Optional[str] = None
    metric: Optional[str] = None

    if any(term in text for term in ("rca", "root cause", "incident")):
        intent = "rca_recent_incident"
    elif "overall health" in text or "system health" in text:
        intent = "overall_health"
    elif "highest error rate" in text:
        intent = "highest_error_rate"
    elif "checkout" in text and (
        "health" in text or "latency" in text or "bounds" in text
    ):
        intent = "checkout_health"
    elif "p99" in text:
        if "order" in text:
            intent = "service_latency"
            service = "order-service"
        elif "payment" in text:
            intent = "service_latency"
            service = "payments-service"
        elif "cart" in text:
            intent = "service_latency"
            service = "cart-service"
        elif "catalog" in text:
            intent = "service_latency"
            service = "catalog-service"
        metric = "p99"

    if intent != "service_latency":
        service = None
        metric = None

    window_minutes: Optional[int] = None
    time_parse_error: Optional[str] = None

    rel_match = re.search(
        r"(last|past|previous)\s+(\d+)\s*(minute|minutes|min|mins|hour|hours|hr|hrs)\b",
        text,
    )
    if rel_match:
        amount = int(rel_match.group(2))
        unit = rel_match.group(3)
        if unit in ("hour", "hours", "hr", "hrs"):
            window_minutes = amount * 60
        else:
            window_minutes = amount

    start_time: Optional[str] = None
    end_time: Optional[str] = None
    range_patterns = [
        r"between\s+([0-9:apm\s]+)\s+and\s+([0-9:apm\s]+)",
        r"from\s+([0-9:apm\s]+)\s+to\s+([0-9:apm\s]+)",
        r"\b([0-9:apm\s]+)-([0-9:apm\s]+)\b",
    ]
    for pat in range_patterns:
        m = re.search(pat, text)
        if m:
            start_time = m.group(1).strip()
            end_time = m.group(2).strip()
            break

    if rel_match is None and start_time is None and end_time is None:
        malformed_match = re.search(r"(last|past|previous)\s+(\d+)\s*([a-z]+)", text)
        if malformed_match:
            unit = malformed_match.group(3)
            if unit not in _VALID_RELATIVE_UNITS:
                time_parse_error = (
                    f"Unsupported time unit '{unit}'. "
                    "Use minutes, mins, hours, or hrs."
                )

    return AgentIntent(
        intent=str(intent),
        original_question=question,
        service_name=service,
        metric=metric,
        start_time=start_time,
        end_time=end_time,
        window_minutes=window_minutes,
        time_parse_error=time_parse_error,
    )


def _format_time_range(tr: TimeRange) -> str:
    return f"{tr.label} ({tr.start.isoformat()} -> {tr.end.isoformat()})"


def execute_intent(db: Database, intent: AgentIntent) -> str:
    if intent.intent == "rca_recent_incident":
        return (
            "RCA is not implemented yet. Health analysis is available now; "
            "RCA will be added in the next implementation step."
        )

    if intent.time_parse_error:
        return (
            f"I couldn't understand that time range: {intent.time_parse_error} "
            "Try examples like 'last 12 minutes', 'between 14:00 and 14:45', "
            "or 'from 2pm to 2:45pm'."
        )

    try:
        time_range = resolve_time_range(
            db,
            start_str=intent.start_time,
            end_str=intent.end_time,
            window_minutes=intent.window_minutes,
        )
    except ValueError as exc:
        return (
            f"I couldn't understand that time range: {exc} "
            "Try examples like 'last 12 minutes', 'between 14:00 and 14:45', "
            "or 'from 2pm to 2:45pm'."
        )

    if intent.intent == "overall_health":
        result = get_overall_health(db, time_range)
        summary = result["summary"]
        services = result["services"]
        highest_error_service = (
            max(services, key=lambda s: s["error_rate_percent"]) if services else None
        )
        if (
            highest_error_service is None
            or highest_error_service["error_rate_percent"] == 0
        ):
            highest_error_line = (
                "Highest error rate service: none; no errors observed in this window"
            )
        else:
            highest_error_line = (
                f"Highest error rate service: {highest_error_service['service_name']}"
            )
        lines = [
            f"Overall health: {result['overall_status']}",
            f"Time range: {_format_time_range(time_range)}",
            f"Services: total={summary['total_services']}, critical={summary['critical_services']}, degraded={summary['degraded_services']}, healthy={summary['healthy_services']}",
            highest_error_line,
            f"Slowest service by p99: {summary['slowest_service_by_p99']}",
        ]
        return "\n".join(lines)

    if intent.intent == "highest_error_rate":
        highest = get_highest_error_rate_service(db, time_range.start, time_range.end)
        if not highest:
            return "No services found in the selected time range."
        if highest["error_rate_percent"] == 0:
            return (
                "No errors were observed in the selected time range.\n"
                f"Time range: {_format_time_range(time_range)}"
            )
        lines = [
            "Service with highest error rate:",
            f"Time range: {_format_time_range(time_range)}",
            f"- Service: {highest['service_name']}",
            f"- Error rate: {highest['error_rate_percent']}%",
            f"- Total spans: {highest['total_spans']}",
            f"- Error spans: {highest['error_spans']}",
            f"- p95 latency: {highest['p95_latency_ms']} ms",
            f"- p99 latency: {highest['p99_latency_ms']} ms",
        ]
        return "\n".join(lines)

    if intent.intent == "service_latency":
        if not intent.service_name:
            return (
                "Could not determine which service you are asking about for latency. "
                "Try mentioning order, payments, cart, or catalog explicitly."
            )
        stats = get_service_latency(
            db, intent.service_name, time_range.start, time_range.end
        )
        if not stats:
            return (
                f"No spans found for {intent.service_name} in the selected time range."
            )
        lines = [
            f"Latency for {intent.service_name}:",
            f"Time range: {_format_time_range(time_range)}",
            f"- Total spans: {stats['total_spans']}",
            f"- Average latency: {stats['avg_latency_ms']} ms",
            f"- p50 latency: {stats['p50_latency_ms']} ms",
            f"- p95 latency: {stats['p95_latency_ms']} ms",
            f"- p99 latency: {stats['p99_latency_ms']} ms",
            f"- Max latency: {stats['max_latency_ms']} ms",
        ]
        return "\n".join(lines)

    if intent.intent == "checkout_health":
        result = get_checkout_flow_health(db, time_range)
        lines = [
            f"Checkout flow health: {result['checkout_status']}",
            f"Time range: {_format_time_range(time_range)}",
            result["interpretation"],
        ]
        for svc in result["services"]:
            lines.append(
                f"- {svc['service_name']}: status={svc['status']}, error_rate={svc['error_rate_percent']}%, p95={svc['p95_latency_ms']} ms, p99={svc['p99_latency_ms']} ms"
            )
        return "\n".join(lines)

    examples = [
        "Examples of supported questions:",
        "- What is the overall health in the last 12 minutes?",
        "- Which service has the highest error rate between 14:00 and 14:45?",
        "- What is the p99 latency for the Order Service from 2pm to 2:45pm?",
        "- Is the checkout flow operating within normal latency bounds between 14:00 and 14:45?",
    ]
    return (
        "I could not understand this question with the current fallback parser.\n"
        + "\n".join(examples)
    )


def answer_question(db: Database, question: str) -> str:
    intent = parse_question_fallback(question)
    return execute_intent(db, intent)
