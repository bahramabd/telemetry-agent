from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from pymongo.database import Database

from src.telemetry.health import (
    get_checkout_flow_health,
    get_highest_error_rate_service,
    get_overall_health,
    get_service_latency,
)
from src.telemetry.rca import run_rca
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


def _user_specified_time(intent: AgentIntent) -> bool:
    has_explicit_range = intent.start_time is not None and intent.end_time is not None
    return has_explicit_range or intent.window_minutes is not None


def _format_timeline_time(value: object) -> str:
    if isinstance(value, datetime):
        return value.strftime("%H:%M:%S")
    return str(value)


def _baseline_row_is_significant(row: dict[str, object]) -> bool:
    incident_error = row.get("incident_error_rate_percent", 0)
    error_delta = row.get("error_rate_delta", 0)
    multiplier = row.get("latency_multiplier")

    if isinstance(incident_error, (int, float)) and incident_error > 0:
        return True
    if isinstance(error_delta, (int, float)) and error_delta > 0:
        return True
    if isinstance(multiplier, (int, float)) and multiplier >= 2:
        return True
    return False


def _format_log_evidence_line(entry: str) -> str:
    return f"  - {entry.strip()}"


def _timeline_shows_checkout_cascade(timeline: list[dict[str, object]]) -> bool:
    times: list[datetime] = []
    for service_name in ("mongodb", "payments-service", "order-service"):
        for item in timeline:
            if item.get("service_name") != service_name:
                continue
            first_problem_time = item.get("first_problem_time")
            if isinstance(first_problem_time, datetime):
                times.append(first_problem_time)
            break
    if len(times) < 2:
        return False
    return times == sorted(times)


def _format_rca_answer(result: dict[str, object]) -> str:
    time_range = result["time_range"]
    if not isinstance(time_range, TimeRange):
        return "RCA could not format results: missing time range."

    incident_detected = bool(result.get("incident_detected"))
    lines = [
        "RCA Summary",
        f"Incident detected: {'yes' if incident_detected else 'no'}",
        f"Time range: {_format_time_range(time_range)}",
        f"Severity: {result.get('severity', 'unknown')}",
        f"Affected flow: {result.get('affected_flow', 'unknown')}",
    ]

    affected_services = result.get("affected_services")
    if isinstance(affected_services, list) and affected_services:
        lines.append(
            f"Affected services: {', '.join(str(s) for s in affected_services)}"
        )
    else:
        lines.append("Affected services: none")

    lines.append("")
    lines.append("Probable root cause:")
    lines.append(str(result.get("probable_root_cause", "Unknown")))
    lines.append(f"Confidence: {result.get('confidence', 'low')}")

    baseline_comparison = result.get("baseline_comparison")
    significant_baseline_rows: list[dict[str, object]] = []
    if isinstance(baseline_comparison, list):
        for row in baseline_comparison:
            if isinstance(row, dict) and _baseline_row_is_significant(row):
                significant_baseline_rows.append(row)

    if significant_baseline_rows:
        lines.append("")
        lines.append("Baseline comparison:")
        for row in significant_baseline_rows[:5]:
            service_name = row.get("service_name")
            baseline_p99 = row.get("baseline_p99_latency_ms")
            incident_p99 = row.get("incident_p99_latency_ms")
            if not isinstance(service_name, str):
                continue
            if not isinstance(baseline_p99, (int, float)) or not isinstance(
                incident_p99, (int, float)
            ):
                continue
            multiplier = row.get("latency_multiplier")
            multiplier_text = (
                f" ({multiplier}x higher)"
                if isinstance(multiplier, (int, float)) and multiplier >= 2
                else ""
            )
            lines.append(
                f"- {service_name}: baseline p99={baseline_p99}ms -> "
                f"incident p99={incident_p99}ms{multiplier_text}, "
                f"error rate {row.get('baseline_error_rate_percent')}% -> "
                f"{row.get('incident_error_rate_percent')}%"
            )

    failure_timeline = result.get("failure_timeline")
    if isinstance(failure_timeline, list) and len(failure_timeline) >= 2:
        lines.append("")
        lines.append("Observed failure timeline:")
        typed_timeline = [item for item in failure_timeline if isinstance(item, dict)]
        for item in typed_timeline:
            time_label = _format_timeline_time(item.get("first_problem_time"))
            service_name = item.get("service_name", "unknown")
            operation = item.get("operation", "unknown")
            duration_ms = item.get("duration_ms", "n/a")
            status_code = item.get("status_code", "")
            signal_type = item.get("signal_type", "")
            lines.append(
                f"- {time_label} {service_name} first problem: {operation} "
                f"({duration_ms}ms, {status_code}, {signal_type})"
            )
        if not _timeline_shows_checkout_cascade(typed_timeline):
            lines.append(
                "- Note: Affected services degraded in the same incident window; "
                "observed ordering is shown above."
            )

    lines.append("")
    lines.append("Evidence:")

    error_spans = result.get("error_spans")
    if isinstance(error_spans, list) and error_spans:
        lines.append("- Span evidence:")
        for span in error_spans[:5]:
            if not isinstance(span, dict):
                continue
            lines.append(
                f"  - {span.get('service_name')} | {span.get('name')} | "
                f"{span.get('duration_ms')} ms | {span.get('status_code')} | "
                f"{span.get('status_message') or ''}"
            )
    else:
        lines.append("- Span evidence: none in window")

    top_slow = result.get("top_slow_operations")
    suspicious_ops: list[dict[str, object]] = []
    if isinstance(top_slow, list):
        for op in top_slow:
            if not isinstance(op, dict):
                continue
            error_rate = op.get("error_rate_percent", 0)
            max_latency = op.get("max_latency_ms") or 0
            if (isinstance(error_rate, (int, float)) and error_rate > 0) or (
                isinstance(max_latency, (int, float)) and max_latency >= 1000
            ):
                suspicious_ops.append(op)

    lines.append("- Slow/error-prone operations:")
    if suspicious_ops:
        for op in suspicious_ops[:5]:
            lines.append(
                f"  - {op.get('service_name')}/{op.get('operation')}: "
                f"error_rate={op.get('error_rate_percent')}%, "
                f"max_latency={op.get('max_latency_ms')} ms"
            )
    else:
        lines.append("  - none")

    log_keyword_evidence = result.get("log_keyword_evidence")
    error_logs_summary = result.get("error_logs_summary")
    lines.append("- Log evidence:")
    if isinstance(log_keyword_evidence, list) and log_keyword_evidence:
        for entry in log_keyword_evidence[:5]:
            if isinstance(entry, str):
                lines.append(_format_log_evidence_line(entry))
    elif isinstance(error_logs_summary, list) and error_logs_summary:
        for entry in error_logs_summary[:5]:
            if not isinstance(entry, dict):
                continue
            service = entry.get("service_name", "unknown")
            severity = entry.get("severity", "")
            count = entry.get("count", 0)
            message = entry.get("message", "")
            lines.append(f"  - [{service}/{severity} x{count}] {message}")
    else:
        lines.append("  - none")

    span_status_evidence = result.get("span_status_evidence")
    lines.append("- Span status-message evidence:")
    if isinstance(span_status_evidence, list) and span_status_evidence:
        for entry in span_status_evidence[:5]:
            if isinstance(entry, str):
                lines.append(f"  - {entry}")
    else:
        lines.append("  - none")

    metric_anomalies = result.get("metric_anomalies")
    lines.append("- Metrics evidence:")
    if isinstance(metric_anomalies, list) and metric_anomalies:
        for anomaly in metric_anomalies[:5]:
            if not isinstance(anomaly, dict):
                continue
            service_name = anomaly.get("service_name")
            metric_name = anomaly.get("metric_name")
            max_value = anomaly.get("max_value")
            if (
                isinstance(service_name, str)
                and isinstance(metric_name, str)
                and isinstance(max_value, (int, float))
            ):
                lines.append(
                    f"  - {service_name} {metric_name} peaked at {round(float(max_value), 2)}"
                )
            elif isinstance(anomaly.get("message"), str):
                message = str(anomaly["message"]).strip()
                lines.append(f"  - {message}")
    else:
        lines.append("  - none")

    lines.append("")
    lines.append("Blast radius:")
    lines.append(str(result.get("blast_radius", "")))

    if not incident_detected:
        lines.insert(
            1,
            "No major incident was detected in the selected window.",
        )
        service_health = result.get("service_health")
        if isinstance(service_health, list) and service_health:
            healthy = sum(1 for s in service_health if s.get("status") == "healthy")
            lines.insert(
                2,
                f"Brief health: {healthy}/{len(service_health)} services healthy in window.",
            )

    lines.append("")
    lines.append("Recommended next steps:")
    steps = result.get("recommended_next_steps")
    if isinstance(steps, list):
        for step in steps:
            lines.append(f"- {step}")
    else:
        lines.append("- Continue monitoring.")

    return "\n".join(lines)


def execute_intent(db: Database, intent: AgentIntent) -> str:
    if intent.time_parse_error:
        return (
            f"I couldn't understand that time range: {intent.time_parse_error} "
            "Try examples like 'last 12 minutes', 'between 14:00 and 14:45', "
            "or 'from 2pm to 2:45pm'."
        )

    if intent.intent == "rca_recent_incident":
        try:
            if _user_specified_time(intent):
                time_range = resolve_time_range(
                    db,
                    start_str=intent.start_time,
                    end_str=intent.end_time,
                    window_minutes=intent.window_minutes,
                )
                rca_result = run_rca(db, time_range)
            else:
                rca_result = run_rca(db, None)
        except ValueError as exc:
            return (
                f"I couldn't understand that time range: {exc} "
                "Try examples like 'last 12 minutes', 'between 14:00 and 14:45', "
                "or 'from 2pm to 2:45pm'."
            )
        return _format_rca_answer(rca_result)

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
        "- Run RCA on the most recent incident",
        "- What was the root cause between 14:00 and 14:45?",
    ]
    return (
        "I could not understand this question with the current fallback parser.\n"
        + "\n".join(examples)
    )


def answer_question(db: Database, question: str) -> str:
    intent = parse_question_fallback(question)
    return execute_intent(db, intent)
