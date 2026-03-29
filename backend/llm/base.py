# backend/llm/base.py
from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from agent.types import Message
from llm.types import LLMChunk


@runtime_checkable
class LLMProvider(Protocol):
    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[LLMChunk]: ...

    async def count_tokens(self, messages: list[Message]) -> int: ...
