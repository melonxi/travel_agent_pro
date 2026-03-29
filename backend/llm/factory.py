# backend/llm/factory.py
from __future__ import annotations

from config import LLMConfig
from llm.anthropic_provider import AnthropicProvider
from llm.openai_provider import OpenAIProvider


def create_llm_provider(config: LLMConfig) -> OpenAIProvider | AnthropicProvider:
    if config.provider == "anthropic":
        return AnthropicProvider(
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    return OpenAIProvider(
        model=config.model,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
