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
import time
from dataclasses import dataclass
from typing import Any, Callable

from opentelemetry import trace

from agent.types import Message, Role, ToolCall, ToolResult
from agent.phase3.candidate_store import (
    Phase3CandidateStore,
    Phase3CandidateValidationError,
)
from agent.phase3.worker_prompt import DayTask, build_day_suffix, build_shared_prefix
from llm.base import LLMProvider
from llm.types import ChunkType
from state.models import TravelPlanState
from storage.trace_redaction import redact_for_trace, stable_content_hash
from telemetry.stats import estimate_llm_cost_usd, llm_cache_usage_metadata
from telemetry.trace_recorder import TraceContext, TraceRecorder
from tools.engine import ToolEngine

logger = logging.getLogger(__name__)

OnProgress = Callable[[int, str, dict], None] | None

_MAX_SAME_QUERY = 2
_MAX_POI_RECOVERY = 3
ERROR_NEEDS_PHASE3_REPLAN = "NEEDS_PHASE3_REPLAN"
_XIAOHONGSHU_TOOL_PREFIX = "xiaohongshu_"
_XIAOHONGSHU_DISABLE_ERROR_CODES = {
    "AUTH_REQUIRED",
    "CAPTCHA_REQUIRED",
    "FORBIDDEN",
    "LOGIN_REQUIRED",
    "NOT_AUTHENTICATED",
    "PERMISSION_DENIED",
    "VERIFICATION_REQUIRED",
}


def _truncate_preview(value: Any, max_len: int = 120) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text[:max_len] + "..." if len(text) > max_len else text


def _trace_context_for_worker(
    trace_context: TraceContext | None,
    *,
    task: DayTask,
    run_id: str | None,
    attempt: int,
    actor: str,
    parent_event_id: str | None,
    root_event_id: str | None,
    correlation_id: str | None = None,
) -> TraceContext | None:
    if trace_context is None:
        return None
    return TraceContext(
        run_id=trace_context.run_id,
        session_id=trace_context.session_id,
        trip_id=trace_context.trip_id,
        context_epoch=trace_context.context_epoch,
        phase=3,
        phase2_step=trace_context.phase2_step,
        parent_event_id=parent_event_id,
        root_event_id=root_event_id or parent_event_id,
        correlation_id=correlation_id
        or f"phase3_worker:{run_id or 'unknown'}:day:{task.day}:attempt:{attempt}",
        actor=actor,
        metadata=dict(trace_context.metadata),
    )


async def _attach_worker_artifact(
    *,
    trace_recorder: TraceRecorder | None,
    trace_context: TraceContext | None,
    task: DayTask,
    run_id: str | None,
    attempt: int,
    event_id: str | None,
    kind: str,
    content: Any,
    parent_event_id: str | None,
) -> Any | None:
    if trace_recorder is None:
        return None
    context = _trace_context_for_worker(
        trace_context,
        task=task,
        run_id=run_id,
        attempt=attempt,
        actor="phase3_worker",
        parent_event_id=parent_event_id,
        root_event_id=parent_event_id,
    )
    if context is None:
        return None
    return await trace_recorder.attach_artifact(
        context,
        event_id=event_id,
        kind=kind,
        content=content,
    )


async def _emit_worker_tool_call_trace(
    *,
    trace_recorder: TraceRecorder | None,
    trace_context: TraceContext | None,
    tool_engine: ToolEngine,
    task: DayTask,
    run_id: str | None,
    attempt: int,
    iteration: int,
    call: ToolCall,
    parent_event_id: str | None,
    root_event_id: str | None,
    worker_tools: list[dict[str, Any]],
) -> Any | None:
    if trace_recorder is None:
        return None
    context = _trace_context_for_worker(
        trace_context,
        task=task,
        run_id=run_id,
        attempt=attempt,
        actor="phase3_worker",
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
    )
    if context is None:
        return None
    redacted = redact_for_trace(call.arguments or {})
    tool_schema = next(
        (
            schema
            for schema in worker_tools
            if isinstance(schema, dict) and schema.get("name") == call.name
        ),
        None,
    )
    tool_def = tool_engine.get_tool(call.name)
    side_effect = (
        "phase3_candidate_submit"
        if call.name == "submit_day_plan_candidate"
        else getattr(tool_def, "side_effect", "read")
        if tool_def is not None
        else "read"
    )
    event = await trace_recorder.emit_event(
        context,
        event_type="tool_call",
        tool_name=call.name,
        status="started",
        iteration=iteration,
        actor="phase3_worker",
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
        payload={
            "tool_call_id": call.id,
            "tool_name": call.name,
            "scope": "phase3_worker",
            "day": task.day,
            "attempt": attempt,
            "worker_run_id": run_id,
            "arguments_hash": stable_content_hash(redacted.value),
            "arguments_preview": _truncate_preview(redacted.value, 500),
            "arguments_redaction_status": redacted.redaction_status,
            "tool_schema_hash": stable_content_hash(tool_schema)
            if tool_schema is not None
            else None,
            "side_effect": side_effect,
        },
    )
    if event is not None:
        await _attach_worker_artifact(
            trace_recorder=trace_recorder,
            trace_context=trace_context,
            task=task,
            run_id=run_id,
            attempt=attempt,
            event_id=event.event_id,
            kind="tool_arguments",
            content=call.arguments or {},
            parent_event_id=parent_event_id,
        )
    return event


async def _emit_worker_tool_result_trace(
    *,
    trace_recorder: TraceRecorder | None,
    trace_context: TraceContext | None,
    task: DayTask,
    run_id: str | None,
    attempt: int,
    iteration: int,
    call: ToolCall,
    result: ToolResult,
    call_event: Any | None,
) -> Any | None:
    if trace_recorder is None:
        return None
    parent_event_id = call_event.event_id if call_event is not None else None
    root_event_id = (
        call_event.root_event_id or call_event.event_id
        if call_event is not None
        else None
    )
    context = _trace_context_for_worker(
        trace_context,
        task=task,
        run_id=run_id,
        attempt=attempt,
        actor="phase3_worker",
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
    )
    if context is None:
        return None
    result_body = result.data if result.status == "success" else {
        "error": result.error,
        "error_code": result.error_code,
        "suggestion": result.suggestion,
    }
    redacted = redact_for_trace(result_body)
    event = await trace_recorder.emit_event(
        context,
        event_type="tool_result",
        tool_name=call.name,
        status=result.status,
        iteration=iteration,
        actor="phase3_worker",
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
        payload={
            "tool_call_id": call.id,
            "tool_name": call.name,
            "scope": "phase3_worker",
            "day": task.day,
            "attempt": attempt,
            "worker_run_id": run_id,
            "status": result.status,
            "error_code": result.error_code,
            "suggestion": result.suggestion,
            "retryable": result.status == "error",
            "result_hash": stable_content_hash(redacted.value),
            "result_preview": _truncate_preview(redacted.value, 500),
            "result_redaction_status": redacted.redaction_status,
            "quality_flags": {
                "usable": result.status == "success" and bool(result.data),
                "empty": result.status == "success" and not bool(result.data),
                "partial": bool(result.metadata and result.metadata.get("partial")),
                "low_confidence": bool(
                    result.metadata and result.metadata.get("low_confidence")
                ),
                "error": result.status == "error",
            },
            "metadata": dict(result.metadata or {}),
            "candidate_submission_path": (
                result.data.get("path")
                if isinstance(result.data, dict)
                and call.name == "submit_day_plan_candidate"
                else None
            ),
        },
    )
    if event is not None:
        await _attach_worker_artifact(
            trace_recorder=trace_recorder,
            trace_context=trace_context,
            task=task,
            run_id=run_id,
            attempt=attempt,
            event_id=event.event_id,
            kind="tool_result",
            content=result_body,
            parent_event_id=parent_event_id,
        )
    return event


def _worker_metadata(
    *,
    task: DayTask,
    run_id: str | None,
    attempt: int,
    iteration: int,
) -> dict[str, Any]:
    return {
        "scope": "phase3_worker",
        "day": task.day,
        "date": task.date,
        "attempt": attempt,
        "iteration": iteration,
        "worker_run_id": run_id,
    }


def _record_worker_llm_call(
    *,
    stats: Any | None,
    llm: LLMProvider,
    task: DayTask,
    run_id: str | None,
    attempt: int,
    iteration: int,
    input_tokens: int,
    output_tokens: int,
    duration_ms: float,
    usage_metadata: dict[str, Any] | None = None,
) -> None:
    if stats is None or not hasattr(stats, "record_llm_call"):
        return
    metadata = _worker_metadata(
        task=task,
        run_id=run_id,
        attempt=attempt,
        iteration=iteration,
    )
    metadata.update(usage_metadata or {})
    stats.record_llm_call(
        provider=getattr(llm, "provider_name", "unknown"),
        model=getattr(llm, "model", "unknown"),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
        phase=3,
        iteration=iteration,
        metadata=metadata,
    )


def _record_worker_tool_call(
    *,
    stats: Any | None,
    task: DayTask,
    run_id: str | None,
    attempt: int,
    iteration: int,
    tool_call: ToolCall,
    result: ToolResult,
) -> None:
    if stats is None or not hasattr(stats, "record_tool_call"):
        return
    result_metadata = result.metadata if isinstance(result.metadata, dict) else {}
    duration = result_metadata.get("duration_ms", 0.0)
    if not isinstance(duration, (int, float)):
        duration = 0.0
    stats.record_tool_call(
        tool_name=tool_call.name,
        duration_ms=float(duration),
        status=result.status,
        error_code=result.error_code,
        phase=3,
        arguments_preview=_truncate_preview(tool_call.arguments),
        result_preview=(
            _truncate_preview(f"ERROR: {result.error}")
            if result.error
            else _truncate_preview(result.data)
        ),
        suggestion=result.suggestion,
        metadata={
            **result_metadata,
            **_worker_metadata(
                task=task,
                run_id=run_id,
                attempt=attempt,
                iteration=iteration,
            ),
            "tool_call_id": tool_call.id,
        },
    )

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

_ROUTE_UNAVAILABLE_PROMPT = (
    "刚才的 calculate_route 返回 NO_ROUTE，表示该路线模式没有可用结构化结果。"
    "不要围绕同一组起终点和 mode 重复调用 calculate_route。"
    "允许用 web_search 兜底一次查询该路线的大致交通方式/时长；如果仍不可确认，"
    "请改用保守交通估算：同一区域步行/地铁 10-20 分钟，跨区地铁 25-45 分钟，"
    "并在该活动上设 transport_estimated=true（结构化标记），可另在 notes 补一句"
    "「路线工具未返回可用结果，交通时长为保守估算」。"
    "时间表必须满足：上一活动 end_time + transport_duration_min <= 下一活动 start_time。"
)

_WEB_SEARCH_DEGRADE_PROMPT = (
    "刚才 web_search 没有返回可用结果。不要再围绕同一查询追加兜底工具。"
    "如果该信息不是核心 POI 坐标或不可替代事实，请降级处理："
    "在 notes 标注「公开网页未确认，出行前复核」，然后继续提交 DayPlan。"
)

_XIAOHONGSHU_DISABLED_PROMPT = (
    "小红书工具当前不可用或需要登录验证，本 worker 后续不要再调用小红书工具。"
    "体验类信息可用 web_search 兜底一次；若仍不可确认，写入 notes，不要阻塞 DayPlan 提交。"
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
                "description": "完整 DayPlan。day 必须等于你当前任务的天数；activities 数量按 pace 要求（relaxed 2-3 / balanced 3-4 / intensive 4-5），到达/离开日最少 1 项；所有时间用 24 小时 HH:MM 格式。",
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
                                "transport_estimated": {
                                    "type": "boolean",
                                    "description": "可选。true 表示该交通时长是未经 calculate_route 验证的保守估算；实算时省略或 false。",
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


_REPORT_SKELETON_INFEASIBLE_SCHEMA = {
    "name": "report_skeleton_infeasible",
    "description": (
        "上报：当前骨架分配对这一天不可行，需要 Orchestrator 调整骨架后重新分派。\n"
        "【何时调用】仅在结构性不可行时：locked POI 当日闭馆/不存在、区域组合"
        "当天根本排不下（跨区通勤远超全天时间）、locked POI 与到达/离开时间冲突。\n"
        "【何时不要调用】信息缺失（写 notes 降级即可）、密度略高（减活动即可）、"
        "普通交通不便（改时间即可）。误报会浪费整轮重排。\n"
        "调用后本 worker 任务结束，不要再调用其他工具。"
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["reason"],
        "properties": {
            "reason": {
                "type": "string",
                "description": "结构性不可行的具体原因，含涉及的 POI/区域与证据。",
            },
            "suggestion": {
                "type": "string",
                "description": "可选。对骨架的修改建议，如「将 X 移到第 N 天」。",
            },
        },
    },
}


def _tool_query_fingerprint(call: ToolCall) -> str | None:
    if call.name == "web_search":
        return f"web_search:{call.arguments.get('query', '')}"
    if call.name == "get_poi_info":
        q = call.arguments.get("query") or call.arguments.get("name") or ""
        return f"get_poi_info:{q}"
    if call.name == "calculate_route":
        return _route_fingerprint(call)
    return None


def _tool_recovery_key(call: ToolCall) -> str | None:
    if call.name == "get_poi_info":
        return call.arguments.get("query") or call.arguments.get("name")
    if call.name == "web_search":
        return call.arguments.get("query")
    if call.name == "calculate_route":
        return _route_fingerprint(call)
    return None


def _route_fingerprint(call: ToolCall) -> str | None:
    required = ("origin_lat", "origin_lng", "dest_lat", "dest_lng")
    if any(key not in call.arguments for key in required):
        return None
    try:
        origin_lat = round(float(call.arguments["origin_lat"]), 5)
        origin_lng = round(float(call.arguments["origin_lng"]), 5)
        dest_lat = round(float(call.arguments["dest_lat"]), 5)
        dest_lng = round(float(call.arguments["dest_lng"]), 5)
    except (TypeError, ValueError):
        return None
    mode = call.arguments.get("mode") or "transit"
    return f"calculate_route:{mode}:{origin_lat},{origin_lng}->{dest_lat},{dest_lng}"


def _poi_fingerprint(call: ToolCall) -> str | None:
    if call.name != "get_poi_info":
        return None
    value = call.arguments.get("query") or call.arguments.get("name")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    location = call.arguments.get("location")
    if isinstance(location, str) and location.strip():
        return f"{value} in {location.strip()}"
    return value


def _is_xiaohongshu_tool(name: str) -> bool:
    return name.startswith(_XIAOHONGSHU_TOOL_PREFIX)


def _without_xiaohongshu_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        schema
        for schema in tools
        if not _is_xiaohongshu_tool(str(schema.get("name", "")))
    ]


def _is_xiaohongshu_disable_result(result: ToolResult) -> bool:
    return (
        result.status == "error"
        and result.error_code in _XIAOHONGSHU_DISABLE_ERROR_CODES
    )


def _poi_result_needs_fallback(result: ToolResult) -> bool:
    if result.status == "error":
        return True
    if result.status != "success":
        return False
    if not isinstance(result.data, dict):
        return False
    pois = result.data.get("pois")
    return isinstance(pois, list) and len(pois) == 0


def _annotate_result(result: ToolResult, **metadata: Any) -> None:
    existing = result.metadata if isinstance(result.metadata, dict) else {}
    result.metadata = {**existing, **metadata}


def _build_poi_fallback_prompt(poi_key: str) -> str:
    return (
        f"get_poi_info 未返回「{poi_key}」的可用结构化 POI。"
        "允许用 web_search 兜底一次查询官方/公开网页信息。"
        "小红书只可用于体验类参考，不可作为坐标、营业时间、票价的唯一依据。"
        "如果 web_search 也不可用：不要继续换词反复查，把该信息写入 notes，"
        "坐标未知的 POI 不要纳入正式活动。"
    )


def _time_to_minutes(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{2}):(\d{2})", value.strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _dayplan_time_conflicts(dayplan: dict[str, Any]) -> list[str]:
    activities = dayplan.get("activities")
    if not isinstance(activities, list):
        return []
    issues: list[str] = []
    for index in range(1, len(activities)):
        prev = activities[index - 1]
        curr = activities[index]
        if not isinstance(prev, dict) or not isinstance(curr, dict):
            continue
        prev_end = _time_to_minutes(prev.get("end_time"))
        curr_start = _time_to_minutes(curr.get("start_time"))
        travel = curr.get("transport_duration_min", 0) or 0
        try:
            travel_min = int(travel)
        except (TypeError, ValueError):
            travel_min = 0
        if prev_end is None or curr_start is None:
            continue
        effective_start = curr_start
        if prev_end - curr_start > 720:
            effective_start = curr_start + 1440
        if prev_end + travel_min > effective_start:
            issues.append(
                f"{prev.get('name', '上一活动')} {prev.get('end_time')} 结束 + "
                f"交通 {travel_min}min > {curr.get('name', '下一活动')} "
                f"{curr.get('start_time')} 开始"
            )
    return issues


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
    candidate_store: Phase3CandidateStore | None = None,
    run_id: str | None = None,
    attempt: int = 1,
    stats: Any | None = None,
    trace_recorder: TraceRecorder | None = None,
    trace_context: TraceContext | None = None,
    trace_parent_event_id: str | None = None,
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
        "优先补齐核心 POI 的坐标和票价；开放时间不确定写入 notes，无需为每个细节反复搜索。"
    )

    messages: list[Message] = [
        Message(role=Role.SYSTEM, content=shared_prefix),
        Message(role=Role.USER, content=day_suffix + iteration_note),
    ]

    # Build tool list: only read tools for Phase 3
    worker_tools = _get_worker_tools(tool_engine)
    if candidate_store is not None and run_id:
        worker_tools.append(_SUBMIT_DAY_PLAN_CANDIDATE_SCHEMA)
    # P1-6 最小版：给 worker 一个真实的骨架不可行上报通道，使
    # NEEDS_PHASE3_REPLAN 可达（此前生产代码从不产生该码，7b 分支死代码）。
    worker_tools.append(_REPORT_SKELETON_INFEASIBLE_SCHEMA)

    iterations = 0
    submitted_dayplan: dict[str, Any] | None = None
    emit_repair_attempted = False
    repair_round_pending = False
    forced_emit_mode = False
    forced_emit_reason: str | None = None
    late_emit_hinted = False
    repeated_query_counts: dict[str, int] = {}
    poi_recovery_counts: dict[str, int] = {}
    failed_route_fingerprints: set[str] = set()
    route_fallback_hinted: set[str] = set()
    poi_fallback_hinted: set[str] = set()
    web_search_failure_hinted: set[str] = set()
    xiaohongshu_disabled = False
    xiaohongshu_disabled_reason: str | None = None

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
                    provider_state: dict[str, object] = {}
                    llm_started_at = time.monotonic()
                    usage_recorded = False
                    usage_info: dict[str, Any] | None = None
                    worker_correlation_id = (
                        f"phase3_worker:{run_id or 'unknown'}:"
                        f"day:{task.day}:attempt:{attempt}:iter:{iterations}"
                    )
                    prompt_payload = [message.to_dict() for message in messages]
                    worker_tools_hash = stable_content_hash(worker_tools)
                    llm_call_event = None
                    llm_output_event = None
                    if trace_recorder is not None:
                        llm_context = _trace_context_for_worker(
                            trace_context,
                            task=task,
                            run_id=run_id,
                            attempt=attempt,
                            actor="phase3_worker",
                            parent_event_id=trace_parent_event_id,
                            root_event_id=trace_parent_event_id,
                            correlation_id=worker_correlation_id,
                        )
                        if llm_context is not None:
                            llm_call_event = await trace_recorder.emit_event(
                                llm_context,
                                event_type="llm_call",
                                iteration=iterations,
                                llm_provider=getattr(
                                    llm, "provider_name", "unknown"
                                ),
                                llm_model=getattr(llm, "model", "unknown"),
                                status="started",
                                actor="phase3_worker",
                                parent_event_id=trace_parent_event_id,
                                root_event_id=trace_parent_event_id,
                                correlation_id=worker_correlation_id,
                                payload={
                                    "scope": "phase3_worker",
                                    "day": task.day,
                                    "attempt": attempt,
                                    "worker_run_id": run_id,
                                    "provider": getattr(
                                        llm, "provider_name", "unknown"
                                    ),
                                    "model": getattr(llm, "model", "unknown"),
                                    "stream": True,
                                    "tool_names": [
                                        schema.get("name")
                                        for schema in worker_tools
                                        if isinstance(schema, dict)
                                    ],
                                    "tool_schema_hash": worker_tools_hash,
                                    "system_prompt_hash": stable_content_hash(
                                        shared_prefix
                                    ),
                                    "prompt_hash": stable_content_hash(
                                        prompt_payload
                                    ),
                                    "message_count": len(messages),
                                    "constraints_hash": stable_content_hash(
                                        task.__dict__
                                    ),
                                },
                            )
                            if llm_call_event is not None:
                                await _attach_worker_artifact(
                                    trace_recorder=trace_recorder,
                                    trace_context=trace_context,
                                    task=task,
                                    run_id=run_id,
                                    attempt=attempt,
                                    event_id=llm_call_event.event_id,
                                    kind="llm_prompt",
                                    content={
                                        "messages": prompt_payload,
                                        "tools": worker_tools,
                                    },
                                    parent_event_id=trace_parent_event_id,
                                )

                    async for chunk in llm.chat(
                        messages, tools=worker_tools, stream=True
                    ):
                        if (
                            chunk.type == ChunkType.PROVIDER_STATE_DELTA
                            and chunk.provider_state
                        ):
                            for key, value in chunk.provider_state.items():
                                if isinstance(value, str) and isinstance(
                                    provider_state.get(key), str
                                ):
                                    provider_state[key] = (
                                        provider_state[key] + value
                                    )
                                else:
                                    provider_state[key] = value
                        elif chunk.type == ChunkType.TEXT_DELTA:
                            text_chunks.append(chunk.content or "")
                        elif (
                            chunk.type == ChunkType.TOOL_CALL_START and chunk.tool_call
                        ):
                            tool_calls.append(chunk.tool_call)
                        elif chunk.type == ChunkType.USAGE and chunk.usage_info:
                            usage_info = dict(chunk.usage_info)
                            usage_recorded = True
                            _record_worker_llm_call(
                                stats=stats,
                                llm=llm,
                                task=task,
                                run_id=run_id,
                                attempt=attempt,
                                iteration=iterations,
                                input_tokens=int(
                                    chunk.usage_info.get("input_tokens", 0) or 0
                                ),
                                output_tokens=int(
                                    chunk.usage_info.get("output_tokens", 0) or 0
                                ),
                                duration_ms=max(
                                    0.0,
                                    (time.monotonic() - llm_started_at) * 1000,
                                ),
                                usage_metadata=llm_cache_usage_metadata(
                                    chunk.usage_info
                                ),
                            )
                    if not usage_recorded:
                        _record_worker_llm_call(
                            stats=stats,
                            llm=llm,
                            task=task,
                            run_id=run_id,
                            attempt=attempt,
                            iteration=iterations,
                            input_tokens=0,
                            output_tokens=0,
                            duration_ms=max(
                                0.0,
                                (time.monotonic() - llm_started_at) * 1000,
                            ),
                        )

                    assistant_text = "".join(text_chunks)
                    if trace_recorder is not None:
                        output_parent_event_id = (
                            llm_call_event.event_id
                            if llm_call_event is not None
                            else trace_parent_event_id
                        )
                        output_root_event_id = (
                            llm_call_event.root_event_id or llm_call_event.event_id
                            if llm_call_event is not None
                            else trace_parent_event_id
                        )
                        output_context = _trace_context_for_worker(
                            trace_context,
                            task=task,
                            run_id=run_id,
                            attempt=attempt,
                            actor="phase3_worker",
                            parent_event_id=output_parent_event_id,
                            root_event_id=output_root_event_id,
                            correlation_id=worker_correlation_id,
                        )
                        if output_context is not None:
                            input_tokens = int(
                                (usage_info or {}).get("input_tokens", 0)
                                or (usage_info or {}).get("prompt_tokens", 0)
                                or 0
                            )
                            output_tokens = int(
                                (usage_info or {}).get("output_tokens", 0)
                                or (usage_info or {}).get("completion_tokens", 0)
                                or 0
                            )
                            llm_output_event = await trace_recorder.emit_event(
                                output_context,
                                event_type="llm_output",
                                iteration=iterations,
                                llm_provider=getattr(
                                    llm, "provider_name", "unknown"
                                ),
                                llm_model=getattr(llm, "model", "unknown"),
                                status="success",
                                duration_ms=max(
                                    0.0,
                                    (time.monotonic() - llm_started_at) * 1000,
                                ),
                                cost_usd=round(
                                    estimate_llm_cost_usd(
                                        getattr(llm, "model", "unknown"),
                                        input_tokens,
                                        output_tokens,
                                        usage_info,
                                    ),
                                    6,
                                ),
                                actor="phase3_worker",
                                parent_event_id=output_parent_event_id,
                                root_event_id=output_root_event_id,
                                correlation_id=worker_correlation_id,
                                payload={
                                    "scope": "phase3_worker",
                                    "day": task.day,
                                    "attempt": attempt,
                                    "worker_run_id": run_id,
                                    "input_tokens": input_tokens,
                                    "output_tokens": output_tokens,
                                    "usage": usage_info or {},
                                    "output_hash": stable_content_hash(
                                        {
                                            "text": assistant_text,
                                            "tool_calls": [
                                                call.__dict__
                                                for call in tool_calls
                                            ],
                                        }
                                    ),
                                    "output_preview": _truncate_preview(
                                        assistant_text, 500
                                    ),
                                    "tool_call_ids": [
                                        call.id for call in tool_calls
                                    ],
                                    "tool_call_names": [
                                        call.name for call in tool_calls
                                    ],
                                },
                            )
                            if llm_output_event is not None:
                                await _attach_worker_artifact(
                                    trace_recorder=trace_recorder,
                                    trace_context=trace_context,
                                    task=task,
                                    run_id=run_id,
                                    attempt=attempt,
                                    event_id=llm_output_event.event_id,
                                    kind="llm_response",
                                    content={
                                        "text": assistant_text,
                                        "tool_calls": [
                                            call.__dict__ for call in tool_calls
                                        ],
                                        "usage": usage_info or {},
                                    },
                                    parent_event_id=output_parent_event_id,
                                )

                    # No tool calls → final response, extract JSON
                    if not tool_calls:
                        messages.append(
                            Message(
                                role=Role.ASSISTANT,
                                content=assistant_text,
                                provider_state=provider_state or None,
                            )
                        )
                        if submitted_dayplan is not None:
                            return DayWorkerResult(
                                day=task.day,
                                date=task.date,
                                success=True,
                                dayplan=submitted_dayplan,
                                iterations=iterations,
                            )
                        dayplan = _accept_text_fallback_dayplan(
                            extract_dayplan_json(assistant_text),
                            plan=plan,
                            task=task,
                            candidate_store=candidate_store,
                            run_id=run_id,
                            attempt=attempt,
                        )
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
                            provider_state=provider_state or None,
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

                    if forced_emit_mode and not any(
                        tc.name == "submit_day_plan_candidate" for tc in tool_calls
                    ):
                        skip_code = forced_emit_reason or "FORCED_EMIT"
                        for tc in tool_calls:
                            skipped = ToolResult(
                                tool_call_id=tc.id,
                                status="skipped",
                                error=(
                                    "Tool execution skipped because the worker "
                                    "entered forced emit mode."
                                ),
                                error_code=skip_code,
                                suggestion=(
                                    "Stop calling tools and submit a conservative "
                                    "DayPlan from existing evidence."
                                ),
                                metadata={
                                    "degraded": True,
                                    "fallback_reason": skip_code,
                                    "fallback_source": "existing_evidence",
                                },
                            )
                            _record_worker_tool_call(
                                stats=stats,
                                task=task,
                                run_id=run_id,
                                attempt=attempt,
                                iteration=iterations,
                                tool_call=tc,
                                result=skipped,
                            )
                            messages.append(Message(role=Role.TOOL, tool_result=skipped))
                        messages.append(
                            Message(role=Role.SYSTEM, content=_FORCED_EMIT_PROMPT)
                        )
                        continue

                    late_emit_prompt: str | None = None
                    if (
                        not late_emit_hinted
                        and _should_force_emit(iterations, max_iterations)
                        and tool_calls
                    ):
                        late_emit_hinted = True
                        late_emit_prompt = _LATE_EMIT_PROMPT

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

                    # P1-6：worker 上报骨架不可行 → 直接终止，产生
                    # NEEDS_PHASE3_REPLAN，由 orchestrator 修改骨架单天后重派。
                    replan_call = next(
                        (c for c in tool_calls if c.name == "report_skeleton_infeasible"),
                        None,
                    )
                    if replan_call is not None:
                        reason = str(replan_call.arguments.get("reason", "")).strip()
                        suggestion = str(
                            replan_call.arguments.get("suggestion", "")
                        ).strip()
                        detail = reason or "worker 判定当前骨架对该天不可行"
                        if suggestion:
                            detail = f"{detail}（建议：{suggestion}）"
                        return DayWorkerResult(
                            day=task.day,
                            date=task.date,
                            success=False,
                            dayplan=None,
                            error=detail,
                            error_code=ERROR_NEEDS_PHASE3_REPLAN,
                            iterations=iterations,
                        )

                    # Execute tools. The worker-only submit tool is handled here
                    # because it writes to the Phase 3 staging area, not the
                    # shared TravelPlanState tool registry.
                    results: list[ToolResult] = []
                    external_tool_calls: list[ToolCall] = []
                    external_positions: list[int] = []
                    tool_call_events: dict[str, Any | None] = {}
                    for pos, call in enumerate(tool_calls):
                        tool_call_events[call.id] = await _emit_worker_tool_call_trace(
                            trace_recorder=trace_recorder,
                            trace_context=trace_context,
                            tool_engine=tool_engine,
                            task=task,
                            run_id=run_id,
                            attempt=attempt,
                            iteration=iterations,
                            call=call,
                            parent_event_id=(
                                llm_output_event.event_id
                                if llm_output_event is not None
                                else llm_call_event.event_id
                                if llm_call_event is not None
                                else trace_parent_event_id
                            ),
                            root_event_id=trace_parent_event_id,
                            worker_tools=worker_tools,
                        )
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
                        elif (
                            call.name == "calculate_route"
                            and (route_fp := _route_fingerprint(call))
                            in failed_route_fingerprints
                        ):
                            results.append(
                                ToolResult(
                                    tool_call_id=call.id,
                                    status="error",
                                    error=(
                                        "Same route already returned NO_ROUTE in this "
                                        "worker run; skipped duplicate route lookup."
                                    ),
                                    error_code="NO_ROUTE",
                                    suggestion=(
                                        "Do not retry the same origin/destination/mode. "
                                        "Use a conservative transport estimate and set "
                                        "transport_estimated=true on the affected activity."
                                    ),
                                    metadata={
                                        "degraded": True,
                                        "fallback_reason": "duplicate_no_route",
                                        "fallback_source": "conservative_estimate",
                                    },
                                )
                            )
                        elif _is_xiaohongshu_tool(call.name) and xiaohongshu_disabled:
                            results.append(
                                ToolResult(
                                    tool_call_id=call.id,
                                    status="error",
                                    error=(
                                        "Xiaohongshu tools are disabled for this "
                                        "worker run after an auth/verification failure."
                                    ),
                                    error_code="XIAOHONGSHU_DISABLED",
                                    suggestion=(
                                        "Use web_search once for public evidence, "
                                        "or write the uncertainty into notes."
                                    ),
                                    metadata={
                                        "degraded": True,
                                        "fallback_reason": xiaohongshu_disabled_reason
                                        or "xiaohongshu_unavailable",
                                        "fallback_source": "web_search_or_notes",
                                    },
                                )
                            )
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

                    followup_prompts: list[str] = []
                    for tc, result in zip(tool_calls, results):
                        followup_prompt: str | None = None
                        if (
                            tc.name == "get_poi_info"
                            and _poi_result_needs_fallback(result)
                            and (poi_key := _poi_fingerprint(tc)) is not None
                            and poi_key not in poi_fallback_hinted
                        ):
                            poi_fallback_hinted.add(poi_key)
                            _annotate_result(
                                result,
                                degraded=True,
                                fallback_reason=result.error_code or "empty_poi_result",
                                fallback_source="web_search_once",
                                fallback_count=1,
                            )
                            followup_prompt = _build_poi_fallback_prompt(poi_key)
                        elif (
                            tc.name == "web_search"
                            and result.status == "error"
                            and (query := tc.arguments.get("query"))
                            and str(query) not in web_search_failure_hinted
                        ):
                            web_search_failure_hinted.add(str(query))
                            _annotate_result(
                                result,
                                degraded=True,
                                fallback_reason=result.error_code or "web_search_error",
                                fallback_source="notes",
                            )
                            followup_prompt = _WEB_SEARCH_DEGRADE_PROMPT
                        elif _is_xiaohongshu_tool(tc.name) and _is_xiaohongshu_disable_result(
                            result
                        ):
                            if not xiaohongshu_disabled:
                                xiaohongshu_disabled = True
                                xiaohongshu_disabled_reason = (
                                    result.error_code or "xiaohongshu_unavailable"
                                )
                                worker_tools = _without_xiaohongshu_tools(worker_tools)
                                _annotate_result(
                                    result,
                                    degraded=True,
                                    fallback_reason=xiaohongshu_disabled_reason,
                                    fallback_source="web_search_or_notes",
                                )
                                followup_prompt = _XIAOHONGSHU_DISABLED_PROMPT
                        elif (
                            tc.name == "calculate_route"
                            and result.error_code == "NO_ROUTE"
                            and (route_fp := _route_fingerprint(tc)) is not None
                        ):
                            failed_route_fingerprints.add(route_fp)
                            if route_fp not in route_fallback_hinted:
                                route_fallback_hinted.add(route_fp)
                                _annotate_result(
                                    result,
                                    degraded=True,
                                    fallback_reason="NO_ROUTE",
                                    fallback_source="web_search_once_then_estimate",
                                    fallback_count=1,
                                )
                                followup_prompt = _ROUTE_UNAVAILABLE_PROMPT

                        _record_worker_tool_call(
                            stats=stats,
                            task=task,
                            run_id=run_id,
                            attempt=attempt,
                            iteration=iterations,
                            tool_call=tc,
                            result=result,
                        )
                        await _emit_worker_tool_result_trace(
                            trace_recorder=trace_recorder,
                            trace_context=trace_context,
                            task=task,
                            run_id=run_id,
                            attempt=attempt,
                            iteration=iterations,
                            call=tc,
                            result=result,
                            call_event=tool_call_events.get(tc.id),
                        )
                        messages.append(Message(role=Role.TOOL, tool_result=result))
                        if followup_prompt:
                            followup_prompts.append(followup_prompt)

                    for prompt in followup_prompts:
                        messages.append(Message(role=Role.SYSTEM, content=prompt))
                    if late_emit_prompt:
                        messages.append(
                            Message(role=Role.SYSTEM, content=late_emit_prompt)
                        )

                # Exhausted iterations
                # P1-7：优先取已通过校验的 submit 提交，其次才是文本兜底
                #（且文本兜底同样要过 candidate_store 校验并写入 store）。
                if submitted_dayplan is not None:
                    return DayWorkerResult(
                        day=task.day,
                        date=task.date,
                        success=True,
                        dayplan=submitted_dayplan,
                        iterations=iterations,
                    )
                last_text = ""
                for msg in reversed(messages):
                    if msg.role == Role.ASSISTANT and msg.content:
                        last_text = msg.content
                        break
                dayplan = _accept_text_fallback_dayplan(
                    extract_dayplan_json(last_text),
                    plan=plan,
                    task=task,
                    candidate_store=candidate_store,
                    run_id=run_id,
                    attempt=attempt,
                )
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


def _accept_text_fallback_dayplan(
    dayplan: dict[str, Any] | None,
    *,
    plan: TravelPlanState,
    task: DayTask,
    candidate_store: Phase3CandidateStore | None,
    run_id: str | None,
    attempt: int,
) -> dict[str, Any] | None:
    """P1-7：文本 JSON 兜底必须过 candidate_store 同一套校验并写入 store。

    否则未校验的文本结果会与 artifact 候选混用，orchestrator 按 store 汇总时
    直接丢掉这些天，触发整批缺天失败。校验不过时返回 None（视为该天失败）。
    """
    if dayplan is None:
        return None
    if not isinstance(dayplan, dict):
        return None
    if _dayplan_time_conflicts(dayplan):
        return None
    if candidate_store is None or not run_id:
        # 无 store 可写时只做结构校验（day 匹配 + 基本字段）
        if dayplan.get("day") != task.day:
            return None
        if not isinstance(dayplan.get("activities"), list):
            return None
        return dayplan
    try:
        candidate_store.submit_candidate(
            session_id=plan.session_id,
            run_id=run_id,
            worker_id=f"day_{task.day}_attempt_{attempt}_textfallback",
            expected_day=task.day,
            attempt=attempt,
            dayplan=dayplan,
        )
    except Phase3CandidateValidationError:
        return None
    return dayplan


def _submit_day_plan_candidate(
    *,
    call: ToolCall,
    plan: TravelPlanState,
    task: DayTask,
    candidate_store: Phase3CandidateStore | None,
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

    time_conflicts = _dayplan_time_conflicts(dayplan)
    if time_conflicts:
        preview = "；".join(time_conflicts[:3])
        return ToolResult(
            tool_call_id=call.id,
            status="error",
            error=f"DayPlan has time conflicts: {preview}",
            error_code="INVALID_DAYPLAN_TIME_CONFLICT",
            suggestion=(
                "修正时间表后再提交：每个活动必须满足上一活动 end_time + "
                "transport_duration_min <= 下一活动 start_time。可以减少活动数，"
                "或把下一活动 start_time 后移。"
            ),
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
    except Phase3CandidateValidationError as exc:
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
