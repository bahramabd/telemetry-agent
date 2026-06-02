from __future__ import annotations

import json
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.config import Settings
from src.llm import LLMError, call_llm, is_llm_configured
from src.prompts import INTENT_PARSER_SYSTEM_PROMPT, build_intent_parser_user_prompt


IntentName = Literal[
    "overall_health",
    "highest_error_rate",
    "service_latency",
    "checkout_health",
    "rca_recent_incident",
    "unsupported",
]

ServiceName = Literal[
    "catalog-service",
    "cart-service",
    "order-service",
    "payments-service",
    "mongodb",
]

MetricName = Literal[
    "p50",
    "p95",
    "p99",
    "avg",
    "max",
    "error_rate",
]


class LLMParsedIntent(BaseModel):
    """Structured intent returned by the LLM intent parser."""

    model_config = ConfigDict(extra="ignore")

    intent: IntentName
    service_name: Optional[ServiceName] = None
    metric: Optional[MetricName] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    window_minutes: Optional[int] = Field(default=None, ge=1)
    reason: Optional[str] = None


def _extract_json_object(text: str) -> str:
    """Extract a JSON object from an LLM response.

    The prompt asks for JSON only, but this helper is defensive in case the
    provider wraps the object in text or markdown fences.
    """

    stripped = text.strip()

    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)

    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped

    start = stripped.find("{")
    end = stripped.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise ValueError("LLM response did not contain a JSON object.")

    return stripped[start : end + 1]


def parse_llm_intent_text(text: str) -> LLMParsedIntent:
    """Parse and validate raw LLM JSON text into LLMParsedIntent."""

    json_text = _extract_json_object(text)

    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("LLM intent payload must be a JSON object.")

    try:
        return LLMParsedIntent.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"LLM intent payload failed validation: {exc}") from exc


def parse_question_with_llm(
    settings: Settings,
    question: str,
    history: list[dict] | None = None,  # ADD
) -> Optional[LLMParsedIntent]:
    """Parse a user question with the configured LLM.

    Returns None if the LLM is not configured, fails, returns invalid JSON,
    or produces an unsupported schema. The caller should then use the
    deterministic fallback parser.
    """

    if not is_llm_configured(settings):
        return None

    user_prompt = build_intent_parser_user_prompt(question, history=history)

    try:
        response = call_llm(
            settings=settings,
            system_prompt=INTENT_PARSER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.0,
            max_tokens=300,
        )
        return parse_llm_intent_text(response.text)
    except (LLMError, ValueError):
        return None
