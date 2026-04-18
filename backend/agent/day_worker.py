# backend/agent/day_worker.py
"""Day Worker: executes a single-day planning task in isolated context.

Each worker gets its own LLM conversation and tool execution scope.
It receives a shared prefix + day-specific suffix as system prompt,
runs a mini agent loop (LLM call → tool calls → LLM call → ... → final JSON),
and returns a DayWorkerResult with the parsed DayPlan.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace

from agent.types import Message, Role, ToolCall, ToolResult
from agent.worker_prompt import DayTask, build_shared_prefix, build_day_suffix
from llm.base import LLMProvider
from llm.types import ChunkType
from state.models import TravelPlanState
from tools.engine import ToolEngine


@dataclass
class DayWorkerResult:
    """Result from a single Day Worker execution."""

    day: int
    date: str
    success: bool
    dayplan: dict[str, Any] | None
    error: str | None = None
    iterations: int = 0


# JSON extraction patterns
_JSON_CODE_BLOCK = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def extract_dayplan_json(text: str) -> dict[str, Any] | None:
    """Extract DayPlan JSON from worker's final message.

    Tries in order:
    1. JSON code block (```json ... ```)
    2. Bare JSON object containing "day" and "activities"
    """
    # Try code block first
    match = _JSON_CODE_BLOCK.search(text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try bare JSON: find outermost { ... } containing "day" and "activities"
    brace_depth = 0
    start_idx = None
    for i, ch in enumerate(text):
        if ch == "{":
            if brace_depth == 0:
                start_idx = i
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
            if brace_depth == 0 and start_idx is not None:
                candidate = text[start_idx : i + 1]
                if '"day"' in candidate and '"activities"' in candidate:
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        pass
                start_idx = None
    return None


async def run_day_worker(
    *,
    llm: LLMProvider,
    tool_engine: ToolEngine,
    plan: TravelPlanState,
    task: DayTask,
    shared_prefix: str,
    max_iterations: int = 5,
    timeout_seconds: int = 60,
) -> DayWorkerResult:
    """Run a single Day Worker agent loop.

    The worker operates in its own isolated context:
    - system message = shared_prefix + day_suffix
    - user message = "请开始规划"
    - loops: LLM call → execute tools → LLM call → ... → extract JSON

    The worker does NOT have write tools. It only uses read tools
    (get_poi_info, optimize_day_route, calculate_route, etc.).
    """
    tracer = trace.get_tracer("day-worker")

    day_suffix = build_day_suffix(task)
    system_content = shared_prefix + day_suffix

    messages: list[Message] = [
        Message(role=Role.SYSTEM, content=system_content),
        Message(role=Role.USER, content="请开始规划这一天的行程。"),
    ]

    # Build tool list: only read tools for Phase 5
    worker_tools = _get_worker_tools(tool_engine)

    iterations = 0

    try:
        async with asyncio.timeout(timeout_seconds):
            with tracer.start_as_current_span(f"day_worker.run.day_{task.day}") as span:
                span.set_attribute("day", task.day)
                span.set_attribute("date", task.date)

                for iteration in range(max_iterations):
                    iterations = iteration + 1

                    # LLM call
                    tool_calls: list[ToolCall] = []
                    text_chunks: list[str] = []

                    async for chunk in llm.chat(
                        messages, tools=worker_tools, stream=True
                    ):
                        if chunk.type == ChunkType.TEXT_DELTA:
                            text_chunks.append(chunk.content or "")
                        elif (
                            chunk.type == ChunkType.TOOL_CALL_START and chunk.tool_call
                        ):
                            tool_calls.append(chunk.tool_call)

                    assistant_text = "".join(text_chunks)

                    # No tool calls → final response, extract JSON
                    if not tool_calls:
                        messages.append(
                            Message(role=Role.ASSISTANT, content=assistant_text)
                        )
                        dayplan = extract_dayplan_json(assistant_text)
                        if dayplan is not None:
                            return DayWorkerResult(
                                day=task.day,
                                date=task.date,
                                success=True,
                                dayplan=dayplan,
                                iterations=iterations,
                            )
                        # No JSON found, but no tools either — worker is stuck
                        return DayWorkerResult(
                            day=task.day,
                            date=task.date,
                            success=False,
                            dayplan=None,
                            error=f"Worker 未输出有效 DayPlan JSON (iteration {iterations})",
                            iterations=iterations,
                        )

                    # Has tool calls → execute them and continue
                    messages.append(
                        Message(
                            role=Role.ASSISTANT,
                            content=assistant_text or None,
                            tool_calls=tool_calls,
                        )
                    )

                    # Execute tools (all read, can be parallel)
                    results = await tool_engine.execute_batch(tool_calls)
                    for tc, result in zip(tool_calls, results):
                        messages.append(Message(role=Role.TOOL, tool_result=result))

                # Exhausted iterations
                last_text = ""
                for msg in reversed(messages):
                    if msg.role == Role.ASSISTANT and msg.content:
                        last_text = msg.content
                        break
                dayplan = extract_dayplan_json(last_text)
                if dayplan is not None:
                    return DayWorkerResult(
                        day=task.day,
                        date=task.date,
                        success=True,
                        dayplan=dayplan,
                        iterations=iterations,
                    )
                return DayWorkerResult(
                    day=task.day,
                    date=task.date,
                    success=False,
                    dayplan=None,
                    error=f"Worker 耗尽 {max_iterations} 轮迭代未输出 DayPlan",
                    iterations=iterations,
                )

    except TimeoutError:
        return DayWorkerResult(
            day=task.day,
            date=task.date,
            success=False,
            dayplan=None,
            error=f"Worker 超时 ({timeout_seconds}s)",
            iterations=iterations,
        )
    except Exception as e:
        return DayWorkerResult(
            day=task.day,
            date=task.date,
            success=False,
            dayplan=None,
            error=f"Worker 异常: {type(e).__name__}: {e}",
            iterations=iterations,
        )


def _get_worker_tools(tool_engine: ToolEngine) -> list[dict[str, Any]]:
    """Get read-only tools available to Day Workers."""
    _WORKER_TOOL_NAMES = {
        "get_poi_info",
        "optimize_day_route",
        "calculate_route",
        "check_availability",
        "check_weather",
        "xiaohongshu_search_notes",
        "xiaohongshu_read_note",
        "xiaohongshu_get_comments",
    }
    all_tools = []
    for name in _WORKER_TOOL_NAMES:
        tool_def = tool_engine.get_tool(name)
        if tool_def is not None:
            all_tools.append(tool_def.to_schema())
    return all_tools
