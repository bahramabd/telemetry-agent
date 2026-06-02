from __future__ import annotations

from typing import Optional

from src.config import Settings
from src.llm import LLMError, call_llm, is_llm_configured
from src.prompts import (
    ANSWER_SYNTHESIS_SYSTEM_PROMPT,
    RCA_SYNTHESIS_SYSTEM_PROMPT,
    build_answer_synthesis_user_prompt,
    build_rca_synthesis_user_prompt,
)
from src.telemetry.serialization import (
    serialize_for_llm,
    serialize_rca_result_for_llm,
)


def is_answer_synthesis_enabled(settings: Settings) -> bool:
    """Return whether LLM final-answer synthesis should be attempted."""

    return bool(settings.enable_llm_synthesis and is_llm_configured(settings))


def synthesize_general_answer(
    settings: Settings,
    question: str,
    deterministic_answer: str,
    evidence: dict[str, object],
) -> Optional[str]:
    """Use the LLM to explain a deterministic non-RCA telemetry result.

    Returns None if synthesis fails so the caller can fall back to the
    deterministic answer.
    """

    safe_evidence = serialize_for_llm(evidence)
    if not isinstance(safe_evidence, dict):
        return None

    user_prompt = build_answer_synthesis_user_prompt(
        question=question,
        deterministic_answer=deterministic_answer,
        evidence=safe_evidence,
    )

    try:
        response = call_llm(
            settings=settings,
            system_prompt=ANSWER_SYNTHESIS_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.2,
            max_tokens=2000,
        )
    except LLMError:
        return None

    text = response.text.strip()
    return text or None


def synthesize_rca_answer(
    settings: Settings,
    question: str,
    rca_result: dict[str, object],
) -> Optional[str]:
    """Use the LLM to explain a deterministic RCA result.

    Returns None if synthesis fails so the caller can fall back to the
    deterministic RCA answer.
    """

    safe_rca_result = serialize_rca_result_for_llm(rca_result)

    user_prompt = build_rca_synthesis_user_prompt(
        question=question,
        rca_result=safe_rca_result,
    )

    try:
        response = call_llm(
            settings=settings,
            system_prompt=RCA_SYNTHESIS_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.2,
            max_tokens=2200,
        )
    except LLMError:
        return None

    text = response.text.strip()
    return text or None
