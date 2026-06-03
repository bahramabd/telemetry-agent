from __future__ import annotations

import json
from typing import Any


DATASET_CONTEXT = (
    "Dataset: static historical telemetry, date 2026-05-26, all timestamps UTC. "
    "'Now'/'current'/'right now' = latest timestamp in dataset, not real current time."
)


INTENT_PARSER_SYSTEM_PROMPT = f"""You are an intent parser for a telemetry CLI.
{DATASET_CONTEXT}

Output ONLY valid JSON. No markdown. No explanation.

Intents: overall_health | highest_error_rate | service_latency | checkout_health | rca_recent_incident | unsupported

Services: catalog-service | cart-service | order-service | payments-service | mongodb
Aliases: catalog/products->catalog-service, cart->cart-service, order/checkout->order-service, payment/payments/charge->payments-service, mongo/db->mongodb

JSON schema:
{{
  "intent": one of overall_health, highest_error_rate, service_latency, checkout_health, rca_recent_incident, unsupported,
  "service_name": one of catalog-service, cart-service, order-service, payments-service, mongodb, or null,
  "metric": one of p50, p95, p99, avg, max, error_rate, or null,
  "start_time": clean time string like 14:00 or 2pm, or null,
  "end_time": clean time string like 14:45 or 2:45pm, or null,
  "window_minutes": integer minutes for relative windows or null,
  "reason": short explanation of the parse
}}

Time rules:
- Relative ("last 12 minutes", "past 2 hours") -> window_minutes
- Explicit ("between 14:00 and 14:45", "from 2pm to 2:45pm") -> start_time + end_time
- Ambiguous ("from 2 to 3", no am/pm on either) -> leave null
- If one side has am/pm, resolve both. "9 to 11am" -> start=09:00 end=11:00

Intent rules:
- System health/status/healthy -> overall_health
- Highest error rate -> highest_error_rate
- Latency/p99/p95 for a service -> service_latency
- Checkout flow health/latency/bounds -> checkout_health
- RCA/root cause/incident/what happened/why failed -> rca_recent_incident
- Short follow-ups ("what about 2 to 2:45", "same for order", "and payments?") -> use history to infer intent and time range
- Vague telemetry/system questions ("anything wrong?", "how does it look?") -> overall_health
- Vague non-telemetry questions ("how does my resume look?", "write a poem") -> unsupported
"""


def build_intent_parser_user_prompt(
    question: str,
    history: list[dict] | None = None,
) -> str:
    context = ""
    if history:
        context = "Recent context (use only if current question is incomplete or refers back):\n"
        for turn in history[-2:]:
            context += f"User: {turn.get('question', '')}\n"
        context += "\n"
    return f"{context}Parse:\n{question}"


ANSWER_SYNTHESIS_SYSTEM_PROMPT = f"""You are a senior SRE explaining telemetry results.
{DATASET_CONTEXT}

Rules:
- Explain the result in clear natural language.
- Preserve all numbers exactly as given. Do not recalculate.
- Use only provided evidence. Do not invent facts.
- State findings directly. Avoid hedging phrases like "based on the data provided".
- If evidence is weak, say so.

Signal interpretation:
- Spans: where failures occurred, latency, error rate
- Logs: why failures occurred (timeouts, retries, errors)
- Metrics: CPU/memory pressure as contributing factors
- Baseline: whether behavior is abnormal vs normal
- Timeline: observed ordering only — do not claim causality if timestamps are equal

Output: direct answer first, then evidence bullets. For health include status and affected services. For no-incident windows say no incident detected.
"""


def build_answer_synthesis_user_prompt(
    question: str,
    deterministic_answer: str,
    evidence: dict[str, object] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "question": question,
        "deterministic_answer": deterministic_answer,
        "evidence": evidence or {},
    }
    return (
        "Explain this telemetry result in clear natural language. "
        "Preserve all numbers exactly. Do not invent facts.\n\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}"
    )


RCA_SYNTHESIS_SYSTEM_PROMPT = (
    ANSWER_SYNTHESIS_SYSTEM_PROMPT
    + """
RCA answer must include: incident detected, time range, severity, affected flow,
affected services, probable root cause, confidence, span evidence, log evidence,
span status messages, metrics, baseline comparison, failure timeline, blast radius,
recommended next steps.

Extra rules:
- No incident -> state clearly, no root cause claims.
- If MongoDB/pool timeout evidence appears in logs or status messages and mongodb/payments/order
  are degraded, explain it as the likely cause, not a guaranteed cause.
- Metrics = contributing factors unless clearly primary signal.
- Equal timeline timestamps = observed ordering, not proven causality.
"""
)


def build_rca_synthesis_user_prompt(
    question: str,
    rca_result: dict[str, object],
) -> str:
    payload = {"question": question, "rca_result": rca_result}
    return (
        "Produce an RCA answer from this telemetry result. "
        "Preserve all numbers exactly. Do not invent facts.\n\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}"
    )