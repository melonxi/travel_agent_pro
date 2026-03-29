# backend/llm/factory.py
from __future__ import annotations

from config import LLMConfig
from llm.base import LLMProvider
from llm.anthropic_provider import AnthropicProvider
from llm.openai_provider import OpenAIProvider


def create_llm_provider(config: LLMConfig) -> LLMProvider:
    if config.provider == "openai":
        return OpenAIProvider(
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    elif config.provider == "anthropic":
        return AnthropicProvider(
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    else:
        raise ValueError(f"Unknown LLM provider: {config.provider}")
