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
from agent.phase5.candidate_store import (
    Phase5CandidateStore,
    Phase5CandidateValidationError,
)
from agent.phase5.worker_prompt import DayTask, build_day_suffix, build_shared_prefix
from llm.base import LLMProvider
from llm.types import ChunkType
from state.models import TravelPlanState
from tools.engine import ToolEngine

logger = logging.getLogger(__name__)

OnProgress = Callable[[int, str, dict], None] | None

_MAX_SAME_QUERY = 2
_MAX_POI_RECOVERY = 3
ERROR_NEEDS_PHASE3_REPLAN = "NEEDS_PHASE3_REPLAN"

_JSON_REPAIR_PROMPT = (
    "你刚才的回复没有触发 submit_day_plan_candidate，也未输出可解析的 DayPlan JSON。\n"
    "请基于上文中已收集的 POI 信息和路线，立即调用 submit_day_plan_candidate 提交。\n"
    "若提交工具返回 SUBMIT_UNAVAILABLE，则在文本里输出符合 schema 的 DayPlan JSON（用 ```json 代码块包裹），"
    "必须包含 day、date、activities 字段。"
)

_FORCED_EMIT_PROMPT = (
    "同一查询已达到重复上限（2次）或补救链已耗尽（3次）。"
    "请立即停止所有工具调用，基于已有信息提交 DayPlan。\n"
    "若信息确实不全：\n"
    "- 只保留已拿到坐标的 POI，缺少坐标的 POI 不纳入活动\n"
    "- 缺营业时间：在 notes 标注「请出行前确认营业时间」\n"
    "- 缺票价：cost 写 0，在 notes 标注「票价以现场为准」\n"
    "- 绝不在 location 中填入 0,0 假坐标\n"
    "不要再为了「再查一次」而调用任何工具。"
)

_LATE_EMIT_PROMPT = (
    "你已使用大部分工具调用预算。"
    "请在下一轮提交 DayPlan；如还需 1-2 个工具补齐核心信息可继续，但不要超过 2 个调用就必须提交。"
    "无法确认的事实写入 notes 字段。"
)

_SUBMIT_DAY_PLAN_CANDIDATE_SCHEMA = {
    "name": "submit_day_plan_candidate",
    "description": (
        "提交你这一天的最终 DayPlan 候选给 Orchestrator。这是你完成本任务的唯一交付动作。\n"
        "\n"
        "【何时调用】\n"
        "- 当天活动序列已确定，所有 locked POI 已包含\n"
        "- 已用 get_poi_info 补齐你引用的 POI 信息（无法补齐的字段写 notes）\n"
        "- 时间表已留出交通/缓冲，活动数符合 pace 要求\n"
        "\n"
        "【何时不要调用】\n"
        "- 仍有 locked POI 未纳入活动\n"
        "- start_time/end_time 还未定（不要提交占位符）\n"
        "- 同一 POI 在你的活动列表中重复出现\n"
        "\n"
        "【提交后】\n"
        "- 此次提交是候选，Orchestrator 会做跨天校验，可能要求你修复重新提交\n"
        "- 提交成功后只输出一句确认（如：「已提交第 N 天」），不要粘贴整个 JSON\n"
        "- 提交失败时，根据 error_code 修正后最多再调一次；仍失败则在最终文本输出合法 JSON 兜底\n"
        "\n"
        "【错误码 → 动作】\n"
        "- INVALID_DAYPLAN（day 不匹配）→ 把 dayplan.day 改为当前任务天数\n"
        "- INVALID_DAYPLAN（字段缺失）→ 补齐 day/date/activities，每个 activity 含 name/location/start_time/end_time/category/cost\n"
        "- INVALID_DAYPLAN（location 非对象）→ location 必须是 {name, lat, lng}，不是字符串\n"
        "- SUBMIT_UNAVAILABLE → 此运行未注入 candidate_store，改为在最终文本输出合法 DayPlan JSON（用 ```json 代码块包裹）"
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "dayplan": {
                "type": "object",
                "description": "完整 DayPlan。day 必须等于你当前任务的天数；activities 至少 2 项；所有时间用 24 小时 HH:MM 格式。",
                "additionalProperties": False,
                "required": ["day", "date", "activities"],
                "properties": {
                    "day": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "天数（1-based），必须等于当前任务的 day。",
                    },
                    "date": {
                        "type": "string",
                        "pattern": r"^\d{4}-\d{2}-\d{2}$",
                        "description": "ISO 日期，YYYY-MM-DD。",
                    },
                    "notes": {
                        "type": "string",
                        "description": "当天补充说明（可选）。无法从工具确认的事实写在这里或活动 notes 里。",
                    },
                    "activities": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "name",
                                "location",
                                "start_time",
                                "end_time",
                                "category",
                                "cost",
                            ],
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "活动/POI 名称。",
                                },
                                "location": {
                                    "type": "object",
                                    "required": ["name", "lat", "lng"],
                                    "properties": {
                                        "name": {"type": "string"},
                                        "lat": {"type": "number", "minimum": -90, "maximum": 90},
                                        "lng": {"type": "number", "minimum": -180, "maximum": 180},
                                    },
                                    "description": "必须是对象 {name, lat, lng}，不能是字符串。lat/lng 来自 get_poi_info 返回值。",
                                },
                                "start_time": {
                                    "type": "string",
                                    "pattern": r"^\d{2}:\d{2}$",
                                    "description": "24 小时制 HH:MM。",
                                },
                                "end_time": {
                                    "type": "string",
                                    "pattern": r"^\d{2}:\d{2}$",
                                    "description": "晚于 start_time。",
                                },
                                "category": {
                                    "type": "string",
                                    "enum": [
                                        "shrine", "museum", "food", "transport",
                                        "activity", "shopping", "park",
                                        "viewpoint", "experience",
                                    ],
                                    "description": "活动类别枚举。餐饮使用 food。",
                                },
                                "cost": {
                                    "type": "number",
                                    "minimum": 0,
                                    "description": "人民币数字；免费写 0；估算时取保守上限。",
                                },
                                "transport_from_prev": {
                                    "type": "string",
                                    "description": "从上一活动到本活动的交通方式（步行/地铁/出租/巴士等）。",
                                },
                                "transport_duration_min": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "description": "上一活动到本活动的交通时长（分钟）。优先使用 calculate_route 返回值。",
                                },
                                "notes": {
                                    "type": "string",
                                    "description": "可选。无法确认的信息写在这里，例如「需提前预约（未确认链接）」。",
                                },
                            },
                        },
                    },
                },
            }
        },
        "required": ["dayplan"],
    },
}


def _should_force_emit(iteration: int, max_iterations: int) -> bool:
    return iteration + 1 >= max(3, int(max_iterations * 0.6))


def _tool_query_fingerprint(call: ToolCall) -> str | None:
    if call.name == "web_search":
        return f"web_search:{call.arguments.get('query', '')}"
    if call.name == "get_poi_info":
        q = call.arguments.get("query") or call.arguments.get("name") or ""
        return f"get_poi_info:{q}"
    return None


def _tool_recovery_key(call: ToolCall) -> str | None:
    if call.name == "get_poi_info":
        return call.arguments.get("query") or call.arguments.get("name")
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
    max_iterations: int = 10,
    timeout_seconds: int = 120,
    on_progress: OnProgress = None,
    candidate_store: Phase5CandidateStore | None = None,
    run_id: str | None = None,
    attempt: int = 1,
) -> DayWorkerResult:
    """Run a single Day Worker agent loop.

    The worker operates in its own isolated context:
    - system message = shared_prefix
    - user message = day_suffix
    - loops: LLM call → execute tools → LLM call → ... → extract JSON

    The worker does NOT have write tools. It only uses read tools
    (get_poi_info, optimize_day_route, calculate_route, etc.).
    """
    tracer = trace.get_tracer("day-worker")

    day_suffix = build_day_suffix(task)
    iteration_note = (
        f"\n\n你的工具调用预算：同一查询最多 {_MAX_SAME_QUERY} 次，"
        f"同一 POI 信息最多 {_MAX_POI_RECOVERY} 次，"
        f"总迭代上限 {max_iterations} 轮。"
        "优先补齐核心 POI 的坐标与开放时间，无需为每个细节反复搜索。"
    )

    messages: list[Message] = [
        Message(role=Role.SYSTEM, content=shared_prefix),
        Message(role=Role.USER, content=day_suffix + iteration_note),
    ]

    # Build tool list: only read tools for Phase 5
    worker_tools = _get_worker_tools(tool_engine)
    if candidate_store is not None and run_id:
        worker_tools.append(_SUBMIT_DAY_PLAN_CANDIDATE_SCHEMA)

    iterations = 0
    submitted_dayplan: dict[str, Any] | None = None
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
                        if submitted_dayplan is not None:
                            return DayWorkerResult(
                                day=task.day,
                                date=task.date,
                                success=True,
                                dayplan=submitted_dayplan,
                                iterations=iterations,
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

                    # Execute tools. The worker-only submit tool is handled here
                    # because it writes to the Phase 5 staging area, not the
                    # shared TravelPlanState tool registry.
                    results: list[ToolResult] = []
                    external_tool_calls: list[ToolCall] = []
                    external_positions: list[int] = []
                    for pos, call in enumerate(tool_calls):
                        if call.name == "submit_day_plan_candidate":
                            result = _submit_day_plan_candidate(
                                call=call,
                                plan=plan,
                                task=task,
                                candidate_store=candidate_store,
                                run_id=run_id,
                                attempt=attempt,
                            )
                            if result.status == "success":
                                submitted_dayplan = result.data["dayplan"]
                            results.append(result)
                        else:
                            results.append(
                                ToolResult(tool_call_id=call.id, status="skipped")
                            )
                            external_tool_calls.append(call)
                            external_positions.append(pos)

                    if external_tool_calls:
                        external_results = await tool_engine.execute_batch(
                            external_tool_calls
                        )
                        for pos, result in zip(external_positions, external_results):
                            results[pos] = result

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
                if submitted_dayplan is not None:
                    return DayWorkerResult(
                        day=task.day,
                        date=task.date,
                        success=True,
                        dayplan=submitted_dayplan,
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
        "check_weather",
        "web_search",
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


def _submit_day_plan_candidate(
    *,
    call: ToolCall,
    plan: TravelPlanState,
    task: DayTask,
    candidate_store: Phase5CandidateStore | None,
    run_id: str | None,
    attempt: int,
) -> ToolResult:
    if candidate_store is None or not run_id:
        return ToolResult(
            tool_call_id=call.id,
            status="error",
            error="submit_day_plan_candidate is unavailable in this worker",
            error_code="SUBMIT_UNAVAILABLE",
            suggestion="Output DayPlan JSON in the final response instead.",
        )

    dayplan = call.arguments.get("dayplan")
    if not isinstance(dayplan, dict):
        return ToolResult(
            tool_call_id=call.id,
            status="error",
            error="dayplan must be an object",
            error_code="INVALID_DAYPLAN",
            suggestion="Call submit_day_plan_candidate with a complete dayplan object.",
        )

    try:
        result = candidate_store.submit_candidate(
            session_id=plan.session_id,
            run_id=run_id,
            worker_id=f"day_{task.day}_attempt_{attempt}",
            expected_day=task.day,
            attempt=attempt,
            dayplan=dayplan,
        )
    except Phase5CandidateValidationError as exc:
        return ToolResult(
            tool_call_id=call.id,
            status="error",
            error=str(exc),
            error_code="INVALID_DAYPLAN",
            suggestion=f"Submit a DayPlan whose day is {task.day}.",
        )

    return ToolResult(
        tool_call_id=call.id,
        status="success",
        data={**result, "dayplan": dayplan},
    )
