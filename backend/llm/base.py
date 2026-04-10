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
        tool_choice: dict | str | None = None,
    ) -> AsyncIterator[LLMChunk]: ...

    async def count_tokens(self, messages: list[Message]) -> int: ...

    async def get_context_window(self) -> int | None: ...
