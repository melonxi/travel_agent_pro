# backend/llm/types.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agent.types import ToolCall


class ChunkType(str, Enum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    DONE = "done"


@dataclass
class LLMChunk:
    type: ChunkType
    content: str | None = None
    tool_call: ToolCall | None = None
