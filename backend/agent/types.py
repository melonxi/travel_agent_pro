# backend/agent/types.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    human_label: str | None = None


@dataclass
class ToolResult:
    tool_call_id: str
    status: str  # "success" | "error" | "skipped"
    data: Any = None
    metadata: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None
    suggestion: str | None = None


@dataclass
class Message:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_result: ToolResult | None = None
    name: str | None = None
    provider_state: dict[str, Any] | None = None
    incomplete: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role.value}
        if self.content is not None:
            d["content"] = self.content
        if self.name is not None:
            d["name"] = self.name
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "human_label": tc.human_label,
                }
                for tc in self.tool_calls
            ]
        if self.tool_result:
            tr = self.tool_result
            tr_dict: dict[str, Any] = {
                "tool_call_id": tr.tool_call_id,
                "status": tr.status,
            }
            if tr.data is not None:
                tr_dict["data"] = tr.data
            if tr.metadata is not None:
                tr_dict["metadata"] = tr.metadata
            if tr.error is not None:
                tr_dict["error"] = tr.error
            if tr.error_code is not None:
                tr_dict["error_code"] = tr.error_code
            if tr.suggestion is not None:
                tr_dict["suggestion"] = tr.suggestion
            d["tool_result"] = tr_dict
        if self.incomplete:
            d["incomplete"] = True
        return d
