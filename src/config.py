from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import BaseModel


class Settings(BaseModel):
    mongo_uri: str = "mongodb://localhost:27017/telemetry"
    llm_provider: str = "openai"
    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-3-5-haiku-latest"
    debug_intent: bool = False


def get_settings() -> Settings:
    load_dotenv()

    return Settings(
        mongo_uri=os.getenv("MONGO_URI", "mongodb://localhost:27017/telemetry"),
        llm_provider=os.getenv("LLM_PROVIDER", "openai"),
        llm_api_key=os.getenv("LLM_API_KEY"),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        anthropic_model=os.getenv(
            "ANTHROPIC_MODEL",
            "claude-3-5-haiku-latest",
        ),
        debug_intent=os.getenv("DEBUG_INTENT", "false").lower() == "true",
    )
