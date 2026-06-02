from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from pymongo.database import Database

from src.answer_synthesizer import (
    is_answer_synthesis_enabled,
    synthesize_general_answer,
    synthesize_rca_answer,
)
from src.config import Settings
from src.intent_parser import LLMParsedIntent, parse_question_with_llm
from src.telemetry.evidence import collect_evidence_bundle
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
    parse_reason: Optional[str] = None
    parse_source: str = "fallback"


class AgentExecution(BaseModel):
    answer: str
    rca_result: Optional[dict[str, object]] = None
    time_range: Optional[TimeRange] = None


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
    for pattern in range_patterns:
        match = re.search(pattern, text)
        if match:
            start_time = match.group(1).strip()
            end_time = match.group(2).strip()
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
        parse_reason="Parsed by deterministic fallback parser.",
        parse_source="fallback",
    )


def _llm_parsed_to_agent_intent(
    parsed: LLMParsedIntent,
    original_question: str,
) -> AgentIntent:
    return AgentIntent(
        intent=parsed.intent,
        original_question=original_question,
        service_name=parsed.service_name,
        metric=parsed.metric,
        start_time=parsed.start_time,
        end_time=parsed.end_time,
        window_minutes=parsed.window_minutes,
        time_parse_error=None,
        parse_reason=parsed.reason,
        parse_source="llm",
    )


def parse_question(
    question: str,
    settings: Optional[Settings] = None,
    history: list[dict] | None = None,
) -> AgentIntent:
    if settings is not None:
        parsed = parse_question_with_llm(settings, question, history=history)
        if parsed is not None:
            return _llm_parsed_to_agent_intent(parsed, question)

    return parse_question_fallback(question)


def _format_time_range(time_range: TimeRange) -> str:
    return (
        f"{time_range.label} "
        f"({time_range.start.isoformat()} -> {time_range.end.isoformat()})"
    )


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
            f"Affected services: {', '.join(str(service) for service in affected_services)}"
        )
    else:
        lines.append("Affected services: none")

    lines.append("")
    lines.append("Probable root cause:")
    lines.append(str(result.get("probable_root_cause", "Unknown")))
    confidence = result.get("confidence", "high")
    if incident_detected and confidence == "high":
        lines.append("Confidence: high")

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
        for operation in top_slow:
            if not isinstance(operation, dict):
                continue
            error_rate = operation.get("error_rate_percent", 0)
            max_latency = operation.get("max_latency_ms") or 0
            if (isinstance(error_rate, (int, float)) and error_rate > 0) or (
                isinstance(max_latency, (int, float)) and max_latency >= 1000
            ):
                suspicious_ops.append(operation)

    lines.append("- Slow/error-prone operations:")
    if suspicious_ops:
        for operation in suspicious_ops[:5]:
            lines.append(
                f"  - {operation.get('service_name')}/{operation.get('operation')}: "
                f"error_rate={operation.get('error_rate_percent')}%, "
                f"max_latency={operation.get('max_latency_ms')} ms"
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
            message = str(entry.get("message", "")).strip()
            lines.append(f"  - [{service}/{severity} x{count}] {message}")
    else:
        lines.append("  - none")

    span_status_evidence = result.get("span_status_evidence")
    lines.append("- Span status-message evidence:")
    if isinstance(span_status_evidence, list) and span_status_evidence:
        for entry in span_status_evidence[:5]:
            if isinstance(entry, str):
                lines.append(f"  - {entry.strip()}")
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
        lines.insert(1, "No major incident was detected in the selected window.")
        service_health = result.get("service_health")
        if isinstance(service_health, list) and service_health:
            healthy = sum(
                1 for service in service_health if service.get("status") == "healthy"
            )
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


def execute_intent_with_result(db: Database, intent: AgentIntent) -> AgentExecution:
    time_range: Optional[TimeRange] = None
    if intent.time_parse_error:
        return AgentExecution(
            answer=(
                f"I couldn't understand that time range: {intent.time_parse_error} "
                "Try examples like 'last 12 minutes', 'between 14:00 and 14:45', "
                "or 'from 2pm to 2:45pm'."
            )
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
                raw_time_range = rca_result.get("time_range")
                time_range = (
                    raw_time_range if isinstance(raw_time_range, TimeRange) else None
                )
        except ValueError as exc:
            return AgentExecution(
                answer=(
                    f"I couldn't understand that time range: {exc} "
                    "Try examples like 'last 12 minutes', 'between 14:00 and 14:45', "
                    "or 'from 2pm to 2:45pm'."
                )
            )

        return AgentExecution(
            answer=_format_rca_answer(rca_result),
            rca_result=rca_result,
            time_range=time_range,
        )

    try:
        time_range = resolve_time_range(
            db,
            start_str=intent.start_time,
            end_str=intent.end_time,
            window_minutes=intent.window_minutes,
        )
    except ValueError as exc:
        return AgentExecution(
            answer=(
                f"I couldn't understand that time range: {exc} "
                "Try examples like 'last 12 minutes', 'between 14:00 and 14:45', "
                "or 'from 2pm to 2:45pm'."
            )
        )

    if intent.intent == "overall_health":
        result = get_overall_health(db, time_range)
        summary = result["summary"]
        services = result["services"]
        highest_error_service = (
            max(services, key=lambda service: service["error_rate_percent"])
            if services
            else None
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
        return AgentExecution(answer="\n".join(lines), time_range=time_range)

    if intent.intent == "highest_error_rate":
        highest = get_highest_error_rate_service(db, time_range.start, time_range.end)
        if not highest:
            return AgentExecution(
                answer="No services found in the selected time range.",
                time_range=time_range,
            )

        if highest["error_rate_percent"] == 0:
            return AgentExecution(
                answer=(
                    "No errors were observed in the selected time range.\n"
                    f"Time range: {_format_time_range(time_range)}"
                ),
                time_range=time_range,
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
        return AgentExecution(answer="\n".join(lines), time_range=time_range)

    if intent.intent == "service_latency":
        if not intent.service_name:
            return AgentExecution(
                answer=(
                    "Could not determine which service you are asking about for latency. "
                    "Try mentioning order, payments, cart, or catalog explicitly."
                ),
                time_range=time_range,
            )

        stats = get_service_latency(
            db,
            intent.service_name,
            time_range.start,
            time_range.end,
        )
        if not stats:
            return AgentExecution(
                answer=(
                    f"No spans found for {intent.service_name} in the selected time range."
                ),
                time_range=time_range,
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
        return AgentExecution(answer="\n".join(lines), time_range=time_range)

    if intent.intent == "checkout_health":
        result = get_checkout_flow_health(db, time_range)
        lines = [
            f"Checkout flow health: {result['checkout_status']}",
            f"Time range: {_format_time_range(time_range)}",
            str(result["interpretation"]),
        ]

        for service in result["services"]:
            lines.append(
                f"- {service['service_name']}: status={service['status']}, "
                f"error_rate={service['error_rate_percent']}%, "
                f"p95={service['p95_latency_ms']} ms, "
                f"p99={service['p99_latency_ms']} ms"
            )

        return AgentExecution(answer="\n".join(lines), time_range=time_range)

    examples = [
        "Examples of supported questions:",
        "- What is the overall health in the last 12 minutes?",
        "- Which service has the highest error rate between 14:00 and 14:45?",
        "- What is the p99 latency for the Order Service from 2pm to 2:45pm?",
        "- Is the checkout flow operating within normal latency bounds between 14:00 and 14:45?",
        "- Run RCA on the most recent incident",
        "- What was the root cause between 14:00 and 14:45?",
    ]
    return AgentExecution(
        answer="I could not understand this question with the current parser.\n"
        + "\n".join(examples),
        time_range=time_range,
    )


def execute_intent(db: Database, intent: AgentIntent) -> str:
    return execute_intent_with_result(db, intent).answer


def _debug_intent_block(intent: AgentIntent) -> str:
    debug_lines = [
        "[debug]",
        f"- parser: {intent.parse_source}",
        f"- intent: {intent.intent}",
        f"- service_name: {intent.service_name}",
        f"- metric: {intent.metric}",
        f"- start_time: {intent.start_time}",
        f"- end_time: {intent.end_time}",
        f"- window_minutes: {intent.window_minutes}",
        f"- reason: {intent.parse_reason}",
        "",
    ]
    return "\n".join(debug_lines)


def _maybe_synthesize_answer(
    db: Database,
    question: str,
    settings: Optional[Settings],
    intent: AgentIntent,
    execution: AgentExecution,
) -> str:
    """Optionally synthesize a final LLM answer from deterministic evidence.

    If synthesis is disabled or fails, return the deterministic answer.
    """

    if settings is None or not is_answer_synthesis_enabled(settings):
        return execution.answer

    if intent.intent == "unsupported" or intent.time_parse_error:
        return execution.answer

    try:
        if intent.intent == "rca_recent_incident":
            if execution.rca_result is None:
                return execution.answer

            synthesized = synthesize_rca_answer(
                settings=settings,
                question=question,
                rca_result=execution.rca_result,
            )
            return synthesized or execution.answer

        if execution.time_range is None:
            return execution.answer

        evidence = collect_evidence_bundle(db, execution.time_range)

        synthesized = synthesize_general_answer(
            settings=settings,
            question=question,
            deterministic_answer=execution.answer,
            evidence=evidence,
        )
        return synthesized or execution.answer

    except Exception:
        return execution.answer


def answer_question(
    db: Database,
    question: str,
    settings: Optional[Settings] = None,
    history: list[dict] | None = None,
) -> str:
    intent = parse_question(question, settings, history=history)
    execution = execute_intent_with_result(db, intent)

    if settings is not None and settings.debug_intent:
        return _debug_intent_block(intent) + execution.answer

    return _maybe_synthesize_answer(
        db=db,
        question=question,
        settings=settings,
        intent=intent,
        execution=execution,
    )
