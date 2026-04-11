# backend/tools/engine.py
from __future__ import annotations

import asyncio
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

    def get_tools_for_phase(
        self,
        phase: int,
        plan: Any | None = None,
    ) -> list[dict[str, Any]]:
        allowed_names = None
        if phase == 3 and plan is not None:
            allowed_names = self._phase3_tool_names(getattr(plan, "phase3_step", "brief"))
            known_phase3_names = self._phase3_builtin_tool_names()
        return [
            t.to_schema()
            for t in self._tools.values()
            if phase in t.phases
            and (
                allowed_names is None
                or t.name in allowed_names
                or t.name not in known_phase3_names
            )
        ]

    def _phase3_tool_names(self, step: str) -> set[str]:
        step_order = {
            "brief": {
                "update_plan_state",
                "web_search",
                "xiaohongshu_search",
            },
            "candidate": {
                "update_plan_state",
                "web_search",
                "xiaohongshu_search",
                "quick_travel_search",
                "get_poi_info",
            },
            "skeleton": {
                "update_plan_state",
                "web_search",
                "xiaohongshu_search",
                "quick_travel_search",
                "get_poi_info",
                "calculate_route",
                "assemble_day_plan",
                "check_availability",
            },
            "lock": {
                "update_plan_state",
                "web_search",
                "xiaohongshu_search",
                "quick_travel_search",
                "get_poi_info",
                "calculate_route",
                "assemble_day_plan",
                "check_availability",
                "search_flights",
                "search_trains",
                "search_accommodations",
            },
        }
        return step_order.get(step, step_order["brief"])

    def _phase3_builtin_tool_names(self) -> set[str]:
        return {
            "update_plan_state",
            "web_search",
            "xiaohongshu_search",
            "quick_travel_search",
            "get_poi_info",
            "calculate_route",
            "assemble_day_plan",
            "check_availability",
            "search_flights",
            "search_trains",
            "search_accommodations",
        }

    def get_tool(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def _internal_error_result(self, call: ToolCall, error: Exception | str) -> ToolResult:
        return ToolResult(
            tool_call_id=call.id,
            status="error",
            error=str(error),
            error_code="INTERNAL_ERROR",
            suggestion="An unexpected error occurred",
        )

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
                return self._internal_error_result(call, e)

    async def execute_batch(self, calls: list[ToolCall]) -> list[ToolResult]:
        if not calls:
            return []
        if len(calls) == 1:
            return [await self.execute(calls[0])]

        indexed_results: list[tuple[int, ToolResult]] = []

        read_calls: list[tuple[int, ToolCall]] = []
        write_calls: list[tuple[int, ToolCall]] = []
        for index, call in enumerate(calls):
            tool_def = self._tools.get(call.name)
            if tool_def and tool_def.side_effect == "write":
                write_calls.append((index, call))
            else:
                read_calls.append((index, call))

        read_results = await asyncio.gather(
            *(self.execute(call) for _, call in read_calls),
            return_exceptions=True,
        )
        for (index, call), result in zip(read_calls, read_results):
            if isinstance(result, Exception):
                result = self._internal_error_result(call, result)
            indexed_results.append((index, result))

        for index, call in write_calls:
            result = await self.execute(call)
            indexed_results.append((index, result))

        indexed_results.sort(key=lambda item: item[0])
        return [result for _, result in indexed_results]
