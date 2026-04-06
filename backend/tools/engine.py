# backend/tools/engine.py
from __future__ import annotations

import json
from typing import Any

from opentelemetry import trace

from agent.types import ToolCall, ToolResult
from telemetry.attributes import TOOL_NAME, TOOL_STATUS, TOOL_ERROR_CODE, EVENT_TOOL_INPUT, EVENT_TOOL_OUTPUT, truncate
from tools.base import ToolDef, ToolError


class ToolEngine:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool_def: ToolDef) -> None:
        self._tools[tool_def.name] = tool_def

    def get_tools_for_phase(self, phase: int) -> list[dict[str, Any]]:
        return [t.to_schema() for t in self._tools.values() if phase in t.phases]

    def get_tool(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    async def execute(self, call: ToolCall) -> ToolResult:
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("tool.execute") as span:
            span.add_event(EVENT_TOOL_INPUT, {
                "arguments": truncate(json.dumps(call.arguments, ensure_ascii=False)),
            })

            tool_def = self._tools.get(call.name)
            if not tool_def:
                span.set_attribute(TOOL_NAME, call.name)
                span.set_attribute(TOOL_STATUS, "error")
                span.set_attribute(TOOL_ERROR_CODE, "UNKNOWN_TOOL")
                span.add_event(EVENT_TOOL_OUTPUT, {
                    "error": f"Unknown tool: {call.name}",
                    "error_code": "UNKNOWN_TOOL",
                })
                return ToolResult(
                    tool_call_id=call.id,
                    status="error",
                    error=f"Unknown tool: {call.name}",
                    error_code="UNKNOWN_TOOL",
                    suggestion=f"Available tools: {', '.join(self._tools.keys())}",
                )

            try:
                data = await tool_def(**call.arguments)
                metadata = None
                if isinstance(data, dict) and "_metadata" in data:
                    payload = dict(data)
                    metadata = payload.pop("_metadata")
                    data = payload
                span.set_attribute(TOOL_NAME, call.name)
                span.set_attribute(TOOL_STATUS, "success")
                span.add_event(EVENT_TOOL_OUTPUT, {
                    "data": truncate(json.dumps(data, ensure_ascii=False)),
                })
                return ToolResult(
                    tool_call_id=call.id,
                    status="success",
                    data=data,
                    metadata=metadata,
                )
            except ToolError as e:
                span.set_attribute(TOOL_NAME, call.name)
                span.set_attribute(TOOL_STATUS, "error")
                span.set_attribute(TOOL_ERROR_CODE, e.error_code)
                span.add_event(EVENT_TOOL_OUTPUT, {
                    "error": str(e),
                    "error_code": e.error_code,
                })
                return ToolResult(
                    tool_call_id=call.id,
                    status="error",
                    error=str(e),
                    error_code=e.error_code,
                    suggestion=e.suggestion,
                )
            except Exception as e:
                span.set_attribute(TOOL_NAME, call.name)
                span.set_attribute(TOOL_STATUS, "error")
                span.set_attribute(TOOL_ERROR_CODE, "INTERNAL_ERROR")
                span.record_exception(e)
                span.add_event(EVENT_TOOL_OUTPUT, {
                    "error": truncate(str(e)),
                    "error_code": "INTERNAL_ERROR",
                })
                return ToolResult(
                    tool_call_id=call.id,
                    status="error",
                    error=str(e),
                    error_code="INTERNAL_ERROR",
                    suggestion="An unexpected error occurred",
                )
