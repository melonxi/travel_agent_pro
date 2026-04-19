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
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from opentelemetry import trace

from agent.types import Message, Role, ToolCall, ToolResult
from agent.worker_prompt import DayTask, build_shared_prefix, build_day_suffix
from llm.base import LLMProvider
from llm.types import ChunkType
from state.models import TravelPlanState
from tools.engine import ToolEngine

logger = logging.getLogger(__name__)

OnProgress = Callable[[int, str, dict], None] | None

_MAX_SAME_QUERY = 2
_MAX_POI_RECOVERY = 3

_JSON_REPAIR_PROMPT = (
    "只输出合法 DayPlan JSON，必须包含 `day`、`date`、`activities`。"
)

_FORCED_EMIT_PROMPT = (
    "你已陷入重复查询/补救循环，请立即停止调用工具，直接输出 DayPlan JSON。"
    "必须包含 `day`、`date`、`activities`。"
)

_LATE_EMIT_PROMPT = (
    "你已进入收口阶段。不要再为细节重复搜索；"
    "请基于已知信息立即输出 DayPlan JSON，无法确认的事实写入 notes。"
)


def _should_force_emit(iteration: int, max_iterations: int) -> bool:
    return iteration + 1 >= max(3, int(max_iterations * 0.6))


def _tool_query_fingerprint(call: ToolCall) -> str | None:
    if call.name == "web_search":
        return f"web_search:{call.arguments.get('query', '')}"
    if call.name == "get_poi_info":
        q = call.arguments.get("query") or call.arguments.get("name") or ""
        return f"get_poi_info:{q}"
    if call.name == "check_availability":
        p = call.arguments.get("placeName", "")
        d = call.arguments.get("date", "")
        return f"check_availability:{p}:{d}"
    return None


def _tool_recovery_key(call: ToolCall) -> str | None:
    if call.name == "get_poi_info":
        return call.arguments.get("query") or call.arguments.get("name")
    if call.name == "check_availability":
        return call.arguments.get("placeName")
    if call.name == "web_search":
        return call.arguments.get("query")
    return None


@dataclass
class DayWorkerResult:
    """Result from a single Day Worker execution."""

    day: int
    date: str
    success: bool
    dayplan: dict[str, Any] | None
    error: str | None = None
    error_code: str | None = None
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
    on_progress: OnProgress = None,
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
    emit_repair_attempted = False
    repair_round_pending = False
    forced_emit_mode = False
    forced_emit_reason: str | None = None
    late_emit_hinted = False
    repeated_query_counts: dict[str, int] = {}
    poi_recovery_counts: dict[str, int] = {}

    try:
        async with asyncio.timeout(timeout_seconds):
            with tracer.start_as_current_span(f"day_worker.run.day_{task.day}") as span:
                span.set_attribute("day", task.day)
                span.set_attribute("date", task.date)

                while iterations < max_iterations or repair_round_pending:
                    repair_round_pending = False
                    iterations += 1

                    def _safe_emit(kind: str, payload: dict) -> None:
                        if on_progress is None:
                            return
                        try:
                            on_progress(task.day, kind, payload)
                        except Exception as exc:
                            logger.warning(
                                "day_worker on_progress callback failed: %s", exc
                            )

                    _safe_emit(
                        "iter_start",
                        {"iteration": iterations, "max": max_iterations},
                    )

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
                        if not emit_repair_attempted:
                            emit_repair_attempted = True
                            repair_round_pending = True
                            messages.append(
                                Message(
                                    role=Role.SYSTEM,
                                    content=_JSON_REPAIR_PROMPT,
                                )
                            )
                            continue
                        return DayWorkerResult(
                            day=task.day,
                            date=task.date,
                            success=False,
                            dayplan=None,
                            error=f"Worker 未输出有效 DayPlan JSON (iteration {iterations})",
                            error_code="JSON_EMIT_FAILED",
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

                    # Convergence guards: check for repeated queries & recovery chains
                    for tc in tool_calls:
                        fp = _tool_query_fingerprint(tc)
                        if fp is not None:
                            repeated_query_counts[fp] = repeated_query_counts.get(fp, 0) + 1
                            if repeated_query_counts[fp] > _MAX_SAME_QUERY:
                                forced_emit_mode = True
                                forced_emit_reason = "REPEATED_QUERY_LOOP"
                                break
                        rk = _tool_recovery_key(tc)
                        if rk is not None:
                            poi_recovery_counts[rk] = poi_recovery_counts.get(rk, 0) + 1
                            if poi_recovery_counts[rk] > _MAX_POI_RECOVERY:
                                forced_emit_mode = True
                                forced_emit_reason = "RECOVERY_CHAIN_EXHAUSTED"
                                break

                    if forced_emit_mode:
                        messages.append(
                            Message(role=Role.SYSTEM, content=_FORCED_EMIT_PROMPT)
                        )
                        continue

                    if (
                        not late_emit_hinted
                        and _should_force_emit(iterations, max_iterations)
                        and tool_calls
                    ):
                        late_emit_hinted = True
                        messages.append(
                            Message(role=Role.SYSTEM, content=_LATE_EMIT_PROMPT)
                        )

                    if tool_calls:
                        first = tool_calls[0]
                        tool_def = tool_engine.get_tool(first.name)
                        _safe_emit(
                            "tool_start",
                            {
                                "tool": first.name,
                                "human_label": (
                                    tool_def.human_label
                                    if tool_def is not None
                                    and getattr(tool_def, "human_label", None)
                                    else first.name
                                ),
                            },
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
                    error_code=forced_emit_reason if forced_emit_mode else None,
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
