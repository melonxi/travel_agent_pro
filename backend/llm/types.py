# backend/llm/types.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agent.types import ToolCall, ToolResult


class ChunkType(str, Enum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_RESULT = "tool_result"
    CONTEXT_COMPRESSION = "context_compression"
    KEEPALIVE = (
        "keepalive"  # SSE ping to prevent proxy/client timeout during tool execution
    )
    USAGE = "usage"
    DONE = "done"
    PHASE_TRANSITION = "phase_transition"
    AGENT_STATUS = "agent_status"


@dataclass
class LLMChunk:
    type: ChunkType
    content: str | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    compression_info: dict | None = None
    usage_info: dict | None = None  # {"input_tokens": N, "output_tokens": N}
    phase_info: dict | None = None
    agent_status: dict | None = None
