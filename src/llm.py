from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.config import Settings


LLMProvider = Literal["openai", "anthropic"]


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str


class LLMError(RuntimeError):
    """Raised when an LLM call fails or is misconfigured."""


def _normalize_provider(provider: str) -> LLMProvider:
    normalized = provider.strip().lower()
    if normalized in {"openai", "anthropic"}:
        return normalized  # type: ignore[return-value]
    raise LLMError(
        f"Unsupported LLM_PROVIDER '{provider}'. Use 'openai' or 'anthropic'."
    )


def _get_openai_api_key(settings: Settings) -> str:
    if not settings.llm_api_key:
        raise LLMError("LLM_PROVIDER is 'openai' but LLM_API_KEY is not set.")
    return settings.llm_api_key


def _get_anthropic_api_key(settings: Settings) -> str:
    if not settings.anthropic_api_key:
        raise LLMError("LLM_PROVIDER is 'anthropic' but ANTHROPIC_API_KEY is not set.")
    return settings.anthropic_api_key


def _call_openai(
    settings: Settings,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
) -> LLMResponse:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise LLMError("openai package is not installed.") from exc

    api_key = _get_openai_api_key(settings)
    client = OpenAI(api_key=api_key, timeout=10.0)

    try:
        response = client.chat.completions.create(
            model=settings.llm_model,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
    except Exception as exc:
        raise LLMError(f"OpenAI call failed: {exc}") from exc

    content = response.choices[0].message.content
    if not content:
        raise LLMError("OpenAI returned an empty response.")

    return LLMResponse(
        text=content.strip(),
        provider="openai",
        model=settings.llm_model,
    )


def _call_anthropic(
    settings: Settings,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
) -> LLMResponse:
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise LLMError("anthropic package is not installed.") from exc

    api_key = _get_anthropic_api_key(settings)
    client = Anthropic(api_key=api_key, timeout=10.0)

    try:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_prompt},
            ],
        )
    except Exception as exc:
        raise LLMError(f"Anthropic call failed: {exc}") from exc

    text_parts: list[str] = []
    for block in response.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            text_parts.append(text)

    text = "\n".join(text_parts).strip()
    if not text:
        raise LLMError("Anthropic returned an empty response.")

    return LLMResponse(
        text=text,
        provider="anthropic",
        model=settings.anthropic_model,
    )


def call_llm(
    settings: Settings,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.0,
    max_tokens: int = 1500,
) -> LLMResponse:
    """Call the configured LLM provider.

    This function accepts prepared prompts only.
    It does not query MongoDB, calculate telemetry, or mutate application state.
    """

    provider = _normalize_provider(settings.llm_provider)

    if provider == "openai":
        return _call_openai(
            settings=settings,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    if provider == "anthropic":
        return _call_anthropic(
            settings=settings,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    raise LLMError(f"Unsupported provider: {provider}")


def is_llm_configured(settings: Settings) -> bool:
    """Return whether the currently selected LLM provider has the needed API key."""

    try:
        provider = _normalize_provider(settings.llm_provider)
    except LLMError:
        return False

    if provider == "openai":
        return bool(settings.llm_api_key)

    if provider == "anthropic":
        return bool(settings.anthropic_api_key)

    return False
