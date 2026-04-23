# backend/main.py
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from agent.compaction import (
    compact_messages_for_prompt,
    compute_prompt_budget,
    estimate_messages_tokens,
)
from agent.hooks import GateResult, HookManager
from agent.internal_tasks import InternalTask
from agent.loop import AgentLoop
from agent.reflection import ReflectionInjector
from agent.tool_choice import ToolChoiceDecider
from agent.types import Message, Role, ToolCall, ToolResult
from config import load_config
from telemetry import setup_telemetry
from telemetry.stats import SessionStats
from context.manager import ContextManager
from harness.guardrail import ToolGuardrail
from harness.judge import (
    build_judge_prompt,
    build_judge_tool,
    judge_tool_name,
    parse_judge_response,
    parse_judge_tool_arguments,
)
from harness.validator import (
    validate_hard_constraints,
    validate_incremental,
    validate_lock_budget,
)
from llm.errors import LLMError, LLMErrorCode
from llm.factory import create_llm_provider
from llm.types import ChunkType
from memory.async_jobs import (
    MemoryJobScheduler,
    MemoryJobSnapshot,
    build_extraction_user_window,
    build_gate_user_window,
)
from memory.extraction import (
    V3ExtractionResult,
    build_v3_extraction_gate_prompt,
    build_v3_extraction_gate_tool,
    build_v3_extraction_tool,
    build_v3_extraction_prompt,
    build_v3_profile_extraction_prompt,
    build_v3_profile_extraction_tool,
    build_v3_working_memory_extraction_prompt,
    build_v3_working_memory_extraction_tool,
    parse_v3_extraction_gate_tool_arguments,
    parse_v3_extraction_tool_arguments,
    parse_v3_profile_extraction_tool_arguments,
    parse_v3_working_memory_extraction_tool_arguments,
)
from memory.archival import build_archived_trip_episode
from memory.episode_slices import build_episode_slices
from memory.formatter import MemoryRecallTelemetry
from memory.manager import MemoryManager
from memory.policy import MemoryPolicy
from memory.profile_normalization import (
    merge_profile_item_with_existing,
    normalize_profile_item,
)
from memory.recall_query import (
    ALLOWED_PROFILE_BUCKETS,
    ALLOWED_RECALL_DOMAINS,
    RecallRetrievalPlan,
    parse_recall_query_tool_arguments,
)
from memory.recall_gate import (
    apply_recall_short_circuit,
    build_recall_gate_tool,
    parse_recall_gate_tool_arguments,
)
from memory.symbolic_recall import heuristic_retrieval_plan_from_message
from memory.v3_models import MemoryAuditEvent, generate_profile_item_id
from phase.router import PhaseRouter
from storage.archive_store import ArchiveStore
from storage.database import Database
from storage.message_store import MessageStore
from storage.session_store import SessionStore
from state.intake import extract_trip_facts
from state.models import TravelPlanState
from state.manager import StateManager
from tools.engine import ToolEngine
from tools.assemble_day_plan import make_assemble_day_plan_tool
from tools.optimize_day_route import make_optimize_day_route_tool
from tools.calculate_route import make_calculate_route_tool
from tools.check_availability import make_check_availability_tool
from tools.check_weather import make_check_weather_tool
from tools.generate_summary import make_generate_summary_tool
from tools.get_poi_info import make_get_poi_info_tool
from tools.search_accommodations import make_search_accommodations_tool
from tools.search_flights import make_search_flights_tool
from tools.search_trains import make_search_trains_tool
from tools.ai_travel_search import make_ai_travel_search_tool
from tools.quick_travel_search import make_quick_travel_search_tool
from tools.search_travel_services import make_search_travel_services_tool
from tools.web_search import make_web_search_tool
from tools.xiaohongshu_search import (
    make_xiaohongshu_get_comments_tool,
    make_xiaohongshu_read_note_tool,
    make_xiaohongshu_search_notes_tool,
)
from tools.plan_tools import PLAN_WRITER_TOOL_NAMES, make_all_plan_tools

logger = logging.getLogger(__name__)

KEEPALIVE_INTERVAL_S = 8


@dataclass
class MemoryExtractionOutcome:
    status: str
    message: str
    item_ids: list[str]
    saved_profile_count: int = 0
    saved_working_count: int = 0
    reason: str | None = None
    error: str | None = None

    @property
    def saved_total(self) -> int:
        return self.saved_profile_count + self.saved_working_count

    def to_result(self) -> dict[str, Any]:
        result = {
            "item_ids": list(self.item_ids),
            "count": len(self.item_ids),
            "saved_profile_count": self.saved_profile_count,
            "saved_working_count": self.saved_working_count,
            "saved_total": self.saved_total,
        }
        if self.reason:
            result["reason"] = self.reason
        return result


@dataclass
class MemoryExtractionProgress:
    saved_profile_count: int = 0
    saved_working_count: int = 0
    pending_ids: list[str] = field(default_factory=list)

    @property
    def saved_total(self) -> int:
        return self.saved_profile_count + self.saved_working_count


@dataclass
class MemoryRouteSaveProgress:
    saved_count: int = 0
    pending_ids: list[str] = field(default_factory=list)


@dataclass
class MemoryExtractionGateDecision:
    should_extract: bool
    reason: str
    message: str
    routes: dict[str, bool] = field(default_factory=dict)
    error: str | None = None

    @property
    def status(self) -> str:
        if self.reason in {"timeout", "error", "no_tool_result"}:
            return "warning"
        if self.should_extract:
            return "success"
        return "skipped"

    def to_result(self) -> dict[str, Any]:
        result = {
            "should_extract": self.should_extract,
            "reason": self.reason,
            "routes": dict(self.routes),
        }
        if self.error:
            result["error"] = self.error
        return result


@dataclass
class MemorySchedulerRuntime:
    scheduler: MemoryJobScheduler
    last_consumed_user_count: int = 0


@dataclass
class MemoryRecallDecision:
    needs_recall: bool
    stage0_decision: str
    stage0_reason: str
    stage0_matched_rule: str = ""
    stage0_signals: dict[str, list[str]] = field(default_factory=dict)
    intent_type: str = ""
    reason: str = ""
    confidence: float | None = None
    fallback_used: str = "none"
    recall_skip_source: str = ""
    gate_user_window: list[str] = field(default_factory=list)
    memory_summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecallQueryPlanResult:
    plan: RecallRetrievalPlan | None
    query_plan_source: str
    query_plan_fallback: str = "none"


_GATE_HEURISTIC_RECALL_FALLBACKS = {
    "gate_timeout_heuristic_recall",
    "gate_error_heuristic_recall",
    "invalid_tool_payload_heuristic_recall",
}


def _final_recall_decision_from_gate(needs_recall: bool) -> str:
    return "query_recall_enabled" if needs_recall else "no_recall_applied"


def _stage0_signals_to_dict(
    signals: tuple[tuple[str, tuple[str, ...]], ...],
) -> dict[str, list[str]]:
    return {name: list(hits) for name, hits in signals}


def _gate_failure_recall_decision_from_heuristic(
    *,
    user_message: str,
    stage0: Any,
    stage0_signals: dict[str, list[str]],
    reason: str,
    fallback_used: str,
) -> MemoryRecallDecision:
    plan = heuristic_retrieval_plan_from_message(
        user_message,
        stage0_decision=stage0.decision,
        stage0_signals=stage0_signals,
    )
    if plan.fallback_used == "none":
        return MemoryRecallDecision(
            needs_recall=True,
            stage0_decision=stage0.decision,
            stage0_reason=stage0.reason,
            stage0_matched_rule=stage0.matched_rule,
            stage0_signals=stage0_signals,
            intent_type="gate_decision_unavailable",
            reason=f"{reason}_heuristic_recall",
            confidence=0.0,
            fallback_used=f"{fallback_used}_heuristic_recall",
        )
    return MemoryRecallDecision(
        needs_recall=False,
        stage0_decision=stage0.decision,
        stage0_reason=stage0.reason,
        stage0_matched_rule=stage0.matched_rule,
        stage0_signals=stage0_signals,
        intent_type="gate_decision_unavailable",
        reason=reason,
        confidence=0.0,
        fallback_used=fallback_used,
        recall_skip_source="gate_failure_no_heuristic",
    )


def _build_recall_query_tool() -> dict[str, Any]:
    common_properties = {
        "domains": {
            "type": "array",
            "items": {"type": "string", "enum": list(ALLOWED_RECALL_DOMAINS)},
            "description": (
                "Exact system domain labels used by symbolic matching. "
                "Do not invent synonyms or localized labels."
            ),
        },
        "destination": {
            "type": "string",
            "description": (
                "Exact destination name for episode-slice lookup. "
                "Use an empty string when no destination can be inferred."
            ),
        },
        "keywords": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Short exact keywords that are likely to appear in stored "
                "profile items or slice summaries."
            ),
        },
        "top_k": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "description": "Per-source candidate budget. It applies independently to profile and episode-slice retrieval.",
        },
        "reason": {
            "type": "string",
            "description": (
                "A short telemetry explanation describing why this plan "
                "was chosen. Keep it under 160 characters."
            ),
        },
    }
    bucket_property = {
        "buckets": {
            "type": "array",
            "items": {"type": "string", "enum": list(ALLOWED_PROFILE_BUCKETS)},
            "description": (
                "Profile buckets to search. Required for 'profile' and "
                "'hybrid_history' plans."
            ),
        }
    }
    return {
        "type": "function",
        "name": "build_recall_retrieval_plan",
        "description": (
            "Generate a retrieval plan for symbolic travel-memory lookup after the "
            "recall gate has already decided that recall is needed. Choose the "
            "smallest plan that can drive exact domain/keyword/destination matching."
        ),
        "parameters": {
            "type": "object",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "source": {"const": "profile"},
                        **bucket_property,
                        **common_properties,
                    },
                    "required": [
                        "source",
                        "buckets",
                        "domains",
                        "destination",
                        "keywords",
                        "top_k",
                        "reason",
                    ],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "source": {"const": "episode_slice"},
                        **common_properties,
                    },
                    "required": [
                        "source",
                        "domains",
                        "destination",
                        "keywords",
                        "top_k",
                        "reason",
                    ],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "source": {"const": "hybrid_history"},
                        **bucket_property,
                        **common_properties,
                    },
                    "required": [
                        "source",
                        "buckets",
                        "domains",
                        "destination",
                        "keywords",
                        "top_k",
                        "reason",
                    ],
                    "additionalProperties": False,
                },
            ],
        },
    }


def _build_recall_query_prompt(
    *,
    user_message: str,
    recent_user_window: list[str],
    gate_intent_type: str,
    gate_reason: str,
    gate_confidence: float | None,
    stage0_signals: dict[str, list[str]],
    plan_facts: dict[str, Any],
    memory_summary: dict[str, Any],
) -> str:
    return "\n".join(
        [
            "你是旅行记忆召回规划器。",
            "Recall gate 已经确认需要召回。你不要重新判断要不要 recall，只输出检索计划。",
            "你的计划会驱动符号检索，不是语义向量检索；domains、destination、keywords 必须精确、可被下游字符串匹配消费。",
            "source 决策规则：profile_preference_recall -> profile；profile_constraint_recall -> profile；past_trip_experience_recall -> episode_slice 或 hybrid_history；mixed_or_ambiguous -> hybrid_history。",
            "buckets 仅在 source=profile 或 hybrid_history 时填写。profile_constraint_recall 优先 constraints/rejections；profile_preference_recall 优先 constraints/rejections/stable_preferences；mixed_or_ambiguous 可加 preference_hypotheses。",
            f"合法 domains 只有：{json.dumps(list(ALLOWED_RECALL_DOMAINS), ensure_ascii=False)}",
            "destination 只允许填写目的地名称；无法可靠推断时填空字符串。",
            "top_k 表示每个 source 的候选预算，不是总候选数。",
            "reason 只写一行简短遥测说明，不要展开推理过程。",
            f"user_message={json.dumps(user_message, ensure_ascii=False)}",
            f"recent_user_window={json.dumps(recent_user_window, ensure_ascii=False)}",
            f"gate_intent_type={json.dumps(gate_intent_type, ensure_ascii=False)}",
            f"gate_reason={json.dumps(gate_reason, ensure_ascii=False)}",
            f"gate_confidence={json.dumps(gate_confidence, ensure_ascii=False)}",
            f"stage0_signals={json.dumps(stage0_signals, ensure_ascii=False)}",
            f"plan_facts={json.dumps(plan_facts, ensure_ascii=False)}",
            f"memory_summary={json.dumps(memory_summary, ensure_ascii=False)}",
            '示例1：{"source":"episode_slice","domains":["hotel"],"destination":"大阪","keywords":["住宿","酒店"],"top_k":3,"reason":"past_trip_experience_recall -> Osaka hotel slices"}',
            '示例2：{"source":"profile","buckets":["constraints","rejections","stable_preferences"],"domains":["hotel"],"destination":"","keywords":["住宿","偏好"],"top_k":3,"reason":"profile_preference_recall -> hotel preference profile"}',
            "必须调用 build_recall_retrieval_plan 工具输出结果。",
        ]
    )


def _query_plan_summary(plan: RecallRetrievalPlan) -> dict[str, Any]:
    return {
        "buckets": list(plan.buckets),
        "domains": list(plan.domains),
        "destination": plan.destination,
        "top_k": plan.top_k,
    }


def _truncate_for_log(value: str, limit: int = 600) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated>"


async def _collect_forced_tool_call_arguments(
    llm,
    *,
    messages: list[Message],
    tool_def: dict[str, Any],
) -> dict[str, Any] | None:
    tool_name = tool_def["name"]
    async for chunk in llm.chat(
        messages,
        tools=[tool_def],
        stream=True,
        tool_choice={"type": "function", "function": {"name": tool_name}},
    ):
        if chunk.type == ChunkType.TOOL_CALL_START and chunk.tool_call:
            if chunk.tool_call.name == tool_name:
                return chunk.tool_call.arguments
    return None


def push_pending_system_note(session: dict, content: str) -> None:
    """Buffer a system note to be flushed into messages before next LLM call.

    Writing to session["messages"] during tool execution risks inserting
    a system message between an assistant.tool_calls and its tool responses,
    which breaks OpenAI protocol. Use this helper instead; flush at on_before_llm.
    """
    session.setdefault("_pending_system_notes", []).append(content)


def flush_pending_system_notes(session: dict, msgs: list) -> int:
    """Flush buffered notes into msgs as SYSTEM messages. Returns count flushed."""
    from agent.types import Message, Role

    pending = session.get("_pending_system_notes") or []
    if not pending:
        return 0
    for content in pending:
        msgs.append(Message(role=Role.SYSTEM, content=content))
    session["_pending_system_notes"] = []
    return len(pending)


class ChatRequest(BaseModel):
    message: str
    user_id: str = "default_user"


class BacktrackRequest(BaseModel):
    to_phase: int
    reason: str = ""


def _should_replace_dates_with_message_dates(
    current_dates,
    message_dates,
    *,
    today: date,
) -> bool:
    if message_dates is None:
        return False
    if current_dates is None:
        return True

    try:
        current_start = date.fromisoformat(current_dates.start)
        message_start = date.fromisoformat(message_dates.start)
    except ValueError:
        return False

    return current_start < today <= message_start


async def _apply_message_fallbacks(
    plan: TravelPlanState,
    message: str,
    phase_router: PhaseRouter,
    *,
    today: date | None = None,
) -> None:
    today = today or date.today()
    facts = extract_trip_facts(message, today=today)
    changed = False

    destination = facts.get("destination")
    if destination and not plan.destination:
        plan.destination = destination
        changed = True

    budget = facts.get("budget")
    if budget and not plan.budget:
        plan.budget = budget
        changed = True

    travelers = facts.get("travelers")
    if travelers and not plan.travelers:
        plan.travelers = travelers
        changed = True

    message_dates = facts.get("dates")
    if _should_replace_dates_with_message_dates(
        plan.dates,
        message_dates,
        today=today,
    ):
        plan.dates = message_dates
        changed = True

    if changed:
        await phase_router.check_and_apply_transition(plan)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_count_from_dates(dates: Any | None) -> int | None:
    if dates is None:
        return None
    start = getattr(dates, "start", None)
    end = getattr(dates, "end", None)
    if not start or not end:
        return None
    try:
        start_date = date.fromisoformat(str(start))
        end_date = date.fromisoformat(str(end))
    except (TypeError, ValueError):
        return None
    return (end_date - start_date).days + 1


def _truncate_preview(value: Any, max_len: int = 120) -> str:
    """Truncate a value to a short preview string."""
    if value is None:
        return ""
    text = str(value) if not isinstance(value, str) else value
    if len(text) > max_len:
        return text[:max_len] + "…"
    return text


def _memory_hit_record_from_recall(
    memory_recall: MemoryRecallTelemetry,
):
    from telemetry.stats import MemoryHitRecord

    if not any(memory_recall.sources.values()):
        return None

    return MemoryHitRecord(
        sources=dict(memory_recall.sources),
        profile_ids=list(memory_recall.profile_ids),
        working_memory_ids=list(memory_recall.working_memory_ids),
        slice_ids=list(memory_recall.slice_ids),
        matched_reasons=list(memory_recall.matched_reasons),
    )


def _recall_telemetry_record_from_recall(
    memory_recall: MemoryRecallTelemetry,
):
    from telemetry.stats import RecallTelemetryRecord

    return RecallTelemetryRecord(
        stage0_decision=memory_recall.stage0_decision,
        stage0_reason=memory_recall.stage0_reason,
        stage0_matched_rule=memory_recall.stage0_matched_rule,
        stage0_signals=dict(memory_recall.stage0_signals),
        gate_needs_recall=memory_recall.gate_needs_recall,
        gate_intent_type=memory_recall.gate_intent_type,
        final_recall_decision=memory_recall.final_recall_decision,
        fallback_used=memory_recall.fallback_used,
        recall_skip_source=memory_recall.recall_skip_source,
        query_plan_source=memory_recall.query_plan_source,
        candidate_count=memory_recall.candidate_count,
        recall_attempted_but_zero_hit=memory_recall.recall_attempted_but_zero_hit,
        reranker_selected_ids=list(memory_recall.reranker_selected_ids),
        reranker_final_reason=memory_recall.reranker_final_reason,
        reranker_fallback=memory_recall.reranker_fallback,
        reranker_per_item_reason=dict(memory_recall.reranker_per_item_reason),
    )


def _plan_writer_updates(
    tool_name: str,
    arguments: dict[str, Any],
    result_data: dict[str, Any],
) -> list[dict[str, Any]]:
    if tool_name == "update_trip_basics":
        updated_fields = result_data.get("updated_fields")
        if not isinstance(updated_fields, list):
            return []
        return [
            {"field": field, "value": arguments.get(field)}
            for field in updated_fields
            if isinstance(field, str) and field in arguments
        ]

    if tool_name == "request_backtrack":
        return []

    mapping: dict[str, tuple[str, Any]] = {
        "set_trip_brief": ("trip_brief", arguments.get("fields")),
        "set_candidate_pool": ("candidate_pool", arguments.get("pool")),
        "set_shortlist": ("shortlist", arguments.get("items")),
        "set_skeleton_plans": ("skeleton_plans", arguments.get("plans")),
        "select_skeleton": ("selected_skeleton_id", arguments.get("id")),
        "set_transport_options": ("transport_options", arguments.get("options")),
        "select_transport": ("selected_transport", arguments.get("choice")),
        "set_accommodation_options": (
            "accommodation_options",
            arguments.get("options"),
        ),
        "set_accommodation": (
            "accommodation",
            {"area": arguments.get("area"), "hotel": arguments.get("hotel")},
        ),
        "set_risks": ("risks", arguments.get("list")),
        "set_alternatives": ("alternatives", arguments.get("list")),
        "add_preferences": ("preferences", arguments.get("items")),
        "add_constraints": ("constraints", arguments.get("items")),
        "save_day_plan": (
            "daily_plans",
            {
                "mode": arguments.get("mode"),
                "day": arguments.get("day"),
                "date": arguments.get("date"),
                "activities": arguments.get("activities"),
            },
        ),
        "replace_all_day_plans": ("daily_plans", arguments.get("days")),
    }
    if tool_name not in mapping:
        return []
    field, value = mapping[tool_name]
    return [{"field": field, "value": value}]


def _plan_writer_state_changes(
    tool_name: str,
    arguments: dict[str, Any],
    result_data: dict[str, Any],
) -> list[dict[str, Any]]:
    updates = _plan_writer_updates(tool_name, arguments, result_data)
    if not updates:
        return []

    updated_field = result_data.get("updated_field")
    if isinstance(updated_field, str) and (
        "previous_value" in result_data or "new_value" in result_data
    ):
        return [
            {
                "field": updated_field,
                "before": result_data.get("previous_value"),
                "after": result_data.get("new_value", updates[0]["value"]),
            }
        ]

    return [
        {
            "field": update["field"],
            "before": None,
            "after": update["value"],
        }
        for update in updates
    ]


def _plan_writer_updated_fields(result_data: dict[str, Any]) -> set[str]:
    fields: set[str] = set()
    updated_field = result_data.get("updated_field")
    if isinstance(updated_field, str):
        fields.add(updated_field)
    updated_fields = result_data.get("updated_fields")
    if isinstance(updated_fields, list):
        fields.update(field for field in updated_fields if isinstance(field, str))
    return fields


def _record_tool_result_stats(
    *,
    stats: SessionStats | None,
    tool_call_names: dict[str, str],
    tool_call_args: dict[str, dict],
    result: ToolResult,
    phase: int,
) -> None:
    if stats is None:
        return
    tool_name = tool_call_names.get(result.tool_call_id)
    if not tool_name:
        return
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    duration = metadata.get("duration_ms", 0.0)
    if not isinstance(duration, (int, float)):
        duration = 0.0
    parallel_group = metadata.get("parallel_group")

    # Build previews
    args = tool_call_args.get(result.tool_call_id, {})
    arguments_preview = _truncate_preview(args) if args else ""
    result_preview = _truncate_preview(result.data) if result.data else ""
    if result.error:
        result_preview = _truncate_preview(f"ERROR: {result.error}")

    stats.record_tool_call(
        tool_name=tool_name,
        duration_ms=float(duration),
        status=result.status,
        error_code=result.error_code,
        phase=phase,
        parallel_group=parallel_group,
        arguments_preview=arguments_preview,
        result_preview=result_preview,
        suggestion=getattr(result, "suggestion", None),
    )


def _record_llm_usage_stats(
    *,
    stats: SessionStats | None,
    provider: str,
    model: str,
    usage_info: dict[str, Any],
    started_at: float,
    now: float | None = None,
    phase: int,
    iteration: int,
) -> None:
    if stats is None:
        return
    current = time.monotonic() if now is None else now
    duration_ms = max(0.0, (current - started_at) * 1000)
    stats.record_llm_call(
        provider=provider,
        model=model,
        input_tokens=int(usage_info.get("input_tokens", 0) or 0),
        output_tokens=int(usage_info.get("output_tokens", 0) or 0),
        duration_ms=duration_ms,
        phase=phase,
        iteration=iteration,
    )


def create_app(config_path: str = "config.yaml") -> FastAPI:
    config = load_config(config_path)
    state_mgr = StateManager(data_dir=config.data_dir)
    memory_mgr = MemoryManager(data_dir=config.data_dir)
    phase_router = PhaseRouter()
    context_mgr = ContextManager()

    # Resolved context window — will be updated at startup via model query
    resolved_context_window: dict[str, int] = {"value": config.llm.context_window}

    # Session-level caches
    sessions: dict[str, dict] = {}  # session_id → {plan, messages, agent}
    memory_scheduler_runtimes: dict[str, MemorySchedulerRuntime] = {}
    memory_task_subscribers: dict[str, set[asyncio.Queue[str]]] = {}
    memory_active_tasks: dict[str, dict[str, InternalTask]] = {}
    reflection_cache: dict[str, ReflectionInjector] = {}
    quality_gate_retries: dict[tuple[str, int, int], int] = {}
    db = Database(db_path=str(Path(config.data_dir) / "sessions.db"))
    session_store = SessionStore(db)
    message_store = MessageStore(db)
    archive_store = ArchiveStore(db)

    async def _probe_context_window() -> None:
        """Query model API for actual context window, fallback to config default."""
        llm = create_llm_provider(config.llm)
        try:
            queried = await llm.get_context_window()
            if queried and queried > 0:
                resolved_context_window["value"] = queried
                import logging

                logging.getLogger("travel-agent-pro").info(
                    f"Context window from model API: {queried}"
                )
        except Exception:
            pass  # keep config default

    async def _ensure_storage_ready() -> None:
        await db.initialize()

    async def _run_v3_memory_cutover_cleanup_once() -> None:
        if getattr(app.state, "_v3_memory_cutover_cleanup_done", False):
            return
        await memory_mgr.v3_store.delete_all_legacy_memory_files()
        app.state._v3_memory_cutover_cleanup_done = True

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.initialize()
        await _probe_context_window()
        await _run_v3_memory_cutover_cleanup_once()
        yield
        for runtime in memory_scheduler_runtimes.values():
            task = runtime.scheduler.running_task
            if task is not None and not task.done():
                task.cancel()
        await db.close()

    app = FastAPI(title="Travel Agent Pro", lifespan=lifespan)
    app.state._run_v3_memory_cutover_cleanup_once = _run_v3_memory_cutover_cleanup_once
    setup_telemetry(app, config.telemetry)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _build_agent(plan, user_id: str, compression_events: list[dict] | None = None):
        llm = create_llm_provider(config.llm)

        def llm_factory(model: str | None = None):
            llm_config = replace(config.llm, model=model) if model else config.llm
            return create_llm_provider(llm_config)

        tool_engine = ToolEngine()

        # Create FlyAI client if enabled
        flyai_client = None
        if config.flyai.enabled:
            from tools.flyai_client import FlyAIClient

            flyai_client = FlyAIClient(
                timeout=config.flyai.cli_timeout,
                api_key=config.flyai.api_key,
            )

        for plan_tool in make_all_plan_tools(plan):
            tool_engine.register(plan_tool)
        tool_engine.register(make_search_flights_tool(config.api_keys, flyai_client))
        tool_engine.register(make_search_trains_tool(flyai_client))
        tool_engine.register(make_ai_travel_search_tool(flyai_client))
        tool_engine.register(
            make_search_accommodations_tool(config.api_keys, flyai_client)
        )
        tool_engine.register(make_get_poi_info_tool(config.api_keys, flyai_client))
        tool_engine.register(make_calculate_route_tool(config.api_keys))
        tool_engine.register(make_assemble_day_plan_tool())
        tool_engine.register(make_optimize_day_route_tool())
        tool_engine.register(make_check_availability_tool(config.api_keys))
        tool_engine.register(make_check_weather_tool(config.api_keys))
        tool_engine.register(make_generate_summary_tool(plan))
        tool_engine.register(make_quick_travel_search_tool(flyai_client))
        tool_engine.register(make_search_travel_services_tool(flyai_client))
        tool_engine.register(make_web_search_tool(config.api_keys))
        tool_engine.register(make_xiaohongshu_search_notes_tool(config.xhs))
        tool_engine.register(make_xiaohongshu_read_note_tool(config.xhs))
        tool_engine.register(make_xiaohongshu_get_comments_tool(config.xhs))

        hooks = HookManager()
        internal_task_events: list[InternalTask] = []

        async def on_tool_call(**kwargs):
            tool_name = kwargs.get("tool_name")
            if tool_name in PLAN_WRITER_TOOL_NAMES:
                result = kwargs.get("result")
                if (
                    result
                    and isinstance(result.data, dict)
                    and result.data.get("backtracked")
                ):
                    session = sessions.get(plan.session_id)
                    if session:
                        session["needs_rebuild"] = True
                return

        async def on_validate(**kwargs):
            tool_name = kwargs.get("tool_name")
            if tool_name in PLAN_WRITER_TOOL_NAMES:
                tc = kwargs.get("tool_call")
                result = kwargs.get("result")
                arguments = tc.arguments if tc and tc.arguments else {}
                session = sessions.get(plan.session_id)
                if not (
                    result
                    and result.status == "success"
                    and isinstance(result.data, dict)
                    and session
                ):
                    return

                updates = _plan_writer_updates(tool_name, arguments, result.data)
                if not updates:
                    return

                session["_pending_state_changes"] = _plan_writer_state_changes(
                    tool_name,
                    arguments,
                    result.data,
                )
                errors: list[str] = []
                for update in updates:
                    field = update["field"]
                    value = update["value"]
                    errors.extend(validate_incremental(plan, field, value))
                    if field in ("selected_transport", "accommodation"):
                        errors.extend(validate_lock_budget(plan))

                if errors:
                    session["_pending_validation_errors"] = errors
                    push_pending_system_note(
                        session,
                        "[实时约束检查]\n"
                        + "\n".join(f"- {error}" for error in errors),
                    )

        async def on_before_llm(**kwargs):
            msgs = kwargs.get("messages")
            tools = kwargs.get("tools") or []
            phase = kwargs.get("phase", plan.phase)
            if not msgs:
                return
            session = sessions.get(plan.session_id)
            if session:
                flush_pending_system_notes(session, msgs)
            prompt_budget = compute_prompt_budget(
                resolved_context_window["value"],
                config.llm.max_tokens,
            )
            estimated_tokens_before = estimate_messages_tokens(msgs, tools=tools)
            message_count_before = len(msgs)

            tool_compaction = compact_messages_for_prompt(
                msgs,
                prompt_budget=prompt_budget,
                tools=tools,
            )
            if tool_compaction.changed:
                msgs[:] = tool_compaction.messages

            estimated_after_tool_compaction = estimate_messages_tokens(
                msgs, tools=tools
            )
            if (
                tool_compaction.changed
                and estimated_after_tool_compaction <= prompt_budget
            ):
                if compression_events is not None:
                    compression_events.append(
                        {
                            "timestamp": time.time(),
                            "message_count_before": message_count_before,
                            "message_count_after": len(msgs),
                            "must_keep_count": 0,
                            "compressed_count": tool_compaction.compacted_tool_messages,
                            "estimated_tokens_before": estimated_tokens_before,
                            "estimated_tokens_after": estimated_after_tool_compaction,
                            "mode": "tool_compaction",
                            "reason": (
                                f"prompt 预算 {prompt_budget} 内进行 {tool_compaction.mode or 'moderate'}"
                                f" TOOL 压缩，usage_ratio={tool_compaction.usage_ratio_before:.2f}"
                            ),
                        }
                    )
                return

            if not context_mgr.should_compress(msgs, prompt_budget, tools=tools):
                return

            must_keep, compressible = context_mgr.classify_messages(msgs)
            recent = msgs[-4:]
            recent_ids = {id(m) for m in recent}
            older_compressible = [m for m in compressible if id(m) not in recent_ids]
            summary_source = (
                older_compressible if len(older_compressible) > 2 else compressible
            )
            if len(summary_source) <= 2:
                return

            summary_text = await context_mgr.compress_for_transition(
                messages=summary_source,
                from_phase=phase,
                to_phase=phase,
                llm_factory=None,
            )
            if not summary_text:
                return

            summary_lines = summary_text.splitlines()
            summary = Message(
                role=Role.SYSTEM,
                content="[对话摘要]\n" + "\n".join(summary_lines[-12:]),
            )

            rebuilt: list[Message] = []
            seen_ids: set[int] = set()

            def append_unique(message: Message) -> None:
                ident = id(message)
                if ident in seen_ids:
                    return
                rebuilt.append(message)
                seen_ids.add(ident)

            sys_msg = msgs[0] if msgs and msgs[0].role == Role.SYSTEM else None
            if sys_msg:
                append_unique(sys_msg)
            for message in must_keep:
                append_unique(message)
            append_unique(summary)
            for message in recent:
                append_unique(message)

            msgs[:] = rebuilt

            estimated_after_summary = estimate_messages_tokens(msgs, tools=tools)
            if compression_events is not None:
                compression_events.append(
                    {
                        "timestamp": time.time(),
                        "message_count_before": message_count_before,
                        "message_count_after": len(msgs),
                        "must_keep_count": len(must_keep),
                        "compressed_count": len(summary_source),
                        "estimated_tokens_before": estimated_tokens_before,
                        "estimated_tokens_after": estimated_after_summary,
                        "mode": "history_summary",
                        "reason": (
                            f"prompt 预算 {prompt_budget} 仍不足，"
                            f"压缩旧消息并保留最近 {len(recent)} 条"
                        ),
                    }
                )

        hooks.register("before_llm_call", on_before_llm)

        async def on_soft_judge(**kwargs):
            tool_name = kwargs.get("tool_name")
            if tool_name not in (
                "save_day_plan",
                "replace_all_day_plans",
                "generate_summary",
            ):
                return
            tool_call = kwargs.get("tool_call")
            task_id = f"soft_judge:{getattr(tool_call, 'id', tool_name)}"
            started_at = time.time()
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="soft_judge",
                    label="行程质量评审",
                    status="pending",
                    message="正在检查行程节奏、地理顺路性和个性化匹配…",
                    related_tool_call_id=getattr(tool_call, "id", None),
                    started_at=started_at,
                )
            )
            if not plan.daily_plans:
                internal_task_events.append(
                    InternalTask(
                        id=task_id,
                        kind="soft_judge",
                        label="行程质量评审",
                        status="skipped",
                        message="暂无每日行程，跳过质量评审。",
                        related_tool_call_id=getattr(tool_call, "id", None),
                        started_at=started_at,
                        ended_at=time.time(),
                    )
                )
                return
            session = sessions.get(plan.session_id)
            if not session:
                internal_task_events.append(
                    InternalTask(
                        id=task_id,
                        kind="soft_judge",
                        label="行程质量评审",
                        status="skipped",
                        message="会话已不可用，跳过质量评审。",
                        related_tool_call_id=getattr(tool_call, "id", None),
                        started_at=started_at,
                        ended_at=time.time(),
                    )
                )
                return
            try:
                prefs = {p.key: p.value for p in plan.preferences}
                prompt_text = build_judge_prompt(plan.to_dict(), prefs)
                judge_llm = create_llm_provider(config.llm)
                judge_msgs = [
                    Message(role=Role.SYSTEM, content="你是旅行行程质量评估专家。"),
                    Message(role=Role.USER, content=prompt_text),
                ]
                score_args = await _collect_forced_tool_call_arguments(
                    judge_llm,
                    messages=judge_msgs,
                    tool_def=build_judge_tool(),
                )
                score = parse_judge_tool_arguments(score_args)
            except Exception as exc:
                logger.warning("soft judge failed", exc_info=True)
                internal_task_events.append(
                    InternalTask(
                        id=task_id,
                        kind="soft_judge",
                        label="行程质量评审",
                        status="error",
                        message="质量评审未完成，不影响已保存的行程。",
                        error=str(exc),
                        related_tool_call_id=getattr(tool_call, "id", None),
                        started_at=started_at,
                        ended_at=time.time(),
                    )
                )
                return
            # Stage judge scores for the TOOL_RESULT handler to attach to ToolCallRecord
            judge_scores = {
                "overall": score.overall,
                "pace": score.pace,
                "geography": score.geography,
                "coherence": score.coherence,
                "personalization": score.personalization,
                "suggestions_count": len(score.suggestions),
            }
            session["_pending_judge_scores"] = judge_scores
            stats = session.get("stats")
            if stats and stats.tool_calls:
                latest = stats.tool_calls[-1]
                if latest.tool_name == tool_name and latest.judge_scores is None:
                    latest.judge_scores = judge_scores
            if score.suggestions:
                suggestion_text = "\n".join(f"- {s}" for s in score.suggestions)
                session["messages"].append(
                    Message(
                        role=Role.SYSTEM,
                        content=f"💡 行程质量评估（{score.overall:.1f}/5）：\n{suggestion_text}",
                    )
                )
            final_status = "warning" if score.suggestions else "success"
            final_message = (
                f"评分 {score.overall:.1f}/5，发现 {len(score.suggestions)} 条改进建议。"
                if score.suggestions
                else f"评分 {score.overall:.1f}/5，未发现需要立即处理的问题。"
            )
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="soft_judge",
                    label="行程质量评审",
                    status=final_status,
                    message=final_message,
                    related_tool_call_id=getattr(tool_call, "id", None),
                    result=judge_scores,
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )

        hooks.register("after_tool_call", on_tool_call)
        hooks.register("after_tool_call", on_validate)
        hooks.register("after_tool_result", on_soft_judge)

        async def on_before_phase_transition(**kwargs):
            target_plan = kwargs.get("plan", plan)
            from_phase = int(kwargs.get("from_phase", target_plan.phase))
            to_phase = int(kwargs.get("to_phase", from_phase))
            session = sessions.get(target_plan.session_id)
            task_id = f"quality_gate:{target_plan.session_id}:{from_phase}:{to_phase}"
            started_at = time.time()
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="quality_gate",
                    label="阶段推进检查",
                    status="pending",
                    message=f"正在判断 Phase {from_phase} 是否可以进入 Phase {to_phase}…",
                    blocking=True,
                    scope="turn",
                    result={"from_phase": from_phase, "to_phase": to_phase},
                    started_at=started_at,
                )
            )

            # Feasibility gate: catch impossible plans early (Phase 1→3)
            if from_phase == 1 and to_phase == 3:
                from harness.feasibility import check_feasibility

                days_count = _days_count_from_dates(target_plan.dates)
                budget_total = None
                if target_plan.budget and target_plan.budget.total:
                    budget_total = target_plan.budget.total
                feas = check_feasibility(
                    target_plan.destination, budget_total, days_count
                )
                if not feas.feasible:
                    feedback = (
                        "[可行性检查]\n当前旅行计划存在以下问题：\n"
                        + "\n".join(f"- {r}" for r in feas.reasons)
                        + "\n请调整后再继续。"
                    )
                    if session:
                        session["messages"].append(
                            Message(role=Role.SYSTEM, content=feedback)
                        )
                    internal_task_events.append(
                        InternalTask(
                            id=task_id,
                            kind="quality_gate",
                            label="阶段推进检查",
                            status="warning",
                            message="可行性检查未通过，暂不推进阶段。",
                            blocking=True,
                            scope="turn",
                            result={"reasons": feas.reasons},
                            started_at=started_at,
                            ended_at=time.time(),
                        )
                    )
                    return GateResult(allowed=False, feedback=feedback)

            errors = validate_hard_constraints(target_plan)
            if errors:
                feedback = "[质量门控]\n硬约束冲突，必须修正：\n" + "\n".join(
                    f"- {error}" for error in errors
                )
                if session:
                    session["messages"].append(
                        Message(role=Role.SYSTEM, content=feedback)
                    )
                internal_task_events.append(
                    InternalTask(
                        id=task_id,
                        kind="quality_gate",
                        label="阶段推进检查",
                        status="warning",
                        message="发现硬约束冲突，暂不推进阶段。",
                        blocking=True,
                        scope="turn",
                        result={"errors": errors},
                        started_at=started_at,
                        ended_at=time.time(),
                    )
                )
                return GateResult(allowed=False, feedback=feedback)

            if (from_phase, to_phase) not in {(3, 5), (5, 7)}:
                internal_task_events.append(
                    InternalTask(
                        id=task_id,
                        kind="quality_gate",
                        label="阶段推进检查",
                        status="success",
                        message=f"允许进入 Phase {to_phase}。",
                        blocking=True,
                        scope="turn",
                        result={"from_phase": from_phase, "to_phase": to_phase},
                        started_at=started_at,
                        ended_at=time.time(),
                    )
                )
                return GateResult(allowed=True)

            try:
                prefs = {p.key: p.value for p in target_plan.preferences}
                prompt_text = build_judge_prompt(target_plan.to_dict(), prefs)
                judge_llm = create_llm_provider(config.llm)
                judge_msgs = [
                    Message(role=Role.SYSTEM, content="你是旅行行程质量评估专家。"),
                    Message(role=Role.USER, content=prompt_text),
                ]
                score_args = await _collect_forced_tool_call_arguments(
                    judge_llm,
                    messages=judge_msgs,
                    tool_def=build_judge_tool(),
                )
                score = parse_judge_tool_arguments(score_args)
            except Exception as exc:
                internal_task_events.append(
                    InternalTask(
                        id=task_id,
                        kind="quality_gate",
                        label="阶段推进检查",
                        status="skipped",
                        message="阶段推进检查不可用，已跳过并允许主流程继续。",
                        blocking=True,
                        scope="turn",
                        error=str(exc),
                        started_at=started_at,
                        ended_at=time.time(),
                    )
                )
                return GateResult(allowed=True)
            if score.overall >= config.quality_gate.threshold:
                quality_gate_retries.pop(
                    (target_plan.session_id, from_phase, to_phase),
                    None,
                )
                internal_task_events.append(
                    InternalTask(
                        id=task_id,
                        kind="quality_gate",
                        label="阶段推进检查",
                        status="success",
                        message=f"评分 {score.overall:.1f}/5，可以进入 Phase {to_phase}。",
                        blocking=True,
                        scope="turn",
                        result={"overall": score.overall},
                        started_at=started_at,
                        ended_at=time.time(),
                    )
                )
                return GateResult(allowed=True)

            retry_key = (target_plan.session_id, from_phase, to_phase)
            retry_count = quality_gate_retries.get(retry_key, 0)
            if retry_count >= config.quality_gate.max_retries:
                quality_gate_retries.pop(retry_key, None)
                internal_task_events.append(
                    InternalTask(
                        id=task_id,
                        kind="quality_gate",
                        label="阶段推进检查",
                        status="warning",
                        message="质量门控已达到重试上限，本次允许继续。",
                        blocking=True,
                        scope="turn",
                        result={"overall": score.overall},
                        started_at=started_at,
                        ended_at=time.time(),
                    )
                )
                return GateResult(allowed=True)

            quality_gate_retries[retry_key] = retry_count + 1
            suggestions = score.suggestions or [
                "请根据当前旅行画像补强方案质量后再推进阶段。"
            ]
            suggestion_text = "\n".join(f"- {suggestion}" for suggestion in suggestions)
            feedback = (
                f"[质量门控]\n当前方案评分 {score.overall:.1f}/5，"
                f"低于阈值 {config.quality_gate.threshold:.1f}。"
                f"请修正后再进入 Phase {to_phase}：\n{suggestion_text}"
            )
            if session:
                session["messages"].append(Message(role=Role.SYSTEM, content=feedback))
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="quality_gate",
                    label="阶段推进检查",
                    status="warning",
                    message=f"评分 {score.overall:.1f}/5，低于阈值 {config.quality_gate.threshold:.1f}。",
                    blocking=True,
                    scope="turn",
                    result={"overall": score.overall, "suggestions": suggestions},
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return GateResult(allowed=False, feedback=feedback)

        hooks.register_gate("before_phase_transition", on_before_phase_transition)

        reflection = reflection_cache.setdefault(plan.session_id, ReflectionInjector())
        tool_choice_decider = ToolChoiceDecider()
        guardrail = (
            ToolGuardrail(disabled_rules=config.guardrails.disabled_rules)
            if config.guardrails.enabled
            else None
        )

        return AgentLoop(
            llm=llm,
            tool_engine=tool_engine,
            hooks=hooks,
            max_retries=config.max_retries,
            phase_router=phase_router,
            context_manager=context_mgr,
            plan=plan,
            llm_factory=llm_factory,
            memory_mgr=memory_mgr,
            memory_enabled=config.memory.enabled,
            user_id=user_id,
            compression_events=compression_events,
            reflection=reflection,
            tool_choice_decider=tool_choice_decider,
            guardrail=guardrail,
            parallel_tool_execution=config.parallel_tool_execution,
            phase5_parallel_config=config.phase5_parallel,
            internal_task_events=internal_task_events,
        )

    def _memory_plan_facts(plan: TravelPlanState) -> dict[str, Any]:
        return {
            "session_id": plan.session_id,
            "trip_id": plan.trip_id,
            "phase": plan.phase,
            "destination": plan.destination,
            "dates": plan.dates.to_dict() if plan.dates else None,
            "travelers": plan.travelers.to_dict() if plan.travelers else None,
            "budget": plan.budget.to_dict() if plan.budget else None,
            "selected_skeleton_id": plan.selected_skeleton_id,
            "selected_transport": plan.selected_transport,
            "accommodation": plan.accommodation.to_dict()
            if plan.accommodation
            else None,
            "phase3_step": plan.phase3_step,
        }

    async def _append_memory_event_nonfatal(event: MemoryAuditEvent) -> None:
        if not config.memory.enabled:
            return
        try:
            await memory_mgr.v3_store.append_event(event)
        except Exception:
            return

    def _schedule_memory_event(
        *,
        user_id: str,
        session_id: str,
        event_type: str,
        object_type: str,
        object_payload: dict[str, Any],
        reason_text: str | None = None,
    ) -> None:
        if not config.memory.enabled:
            return
        event = MemoryAuditEvent(
            id=f"{session_id}:{event_type}:{_now_iso()}",
            user_id=user_id,
            session_id=session_id,
            event_type=event_type,
            object_type=object_type,
            object_payload=object_payload,
            reason_text=reason_text,
            created_at=_now_iso(),
        )
        asyncio.create_task(_append_memory_event_nonfatal(event))

    _GATE_MAX_USER_MESSAGES = 3
    _GATE_MAX_CHARS = 1200
    _EXTRACTION_MAX_USER_MESSAGES = 8
    _EXTRACTION_MAX_CHARS = 3000
    _EXTRACTION_GATE_TIMEOUT_SECONDS = 30.0
    _EXTRACTION_TIMEOUT_SECONDS = 40.0

    async def _build_gate_memory_summary(
        *,
        user_id: str,
        session_id: str,
        plan_snapshot: TravelPlanState,
    ) -> dict[str, Any]:
        profile = await memory_mgr.v3_store.load_profile(user_id)
        working_memory = await memory_mgr.v3_store.load_working_memory(
            user_id, session_id, plan_snapshot.trip_id
        )
        return {
            "profile_counts": {
                "constraints": len(profile.constraints),
                "rejections": len(profile.rejections),
                "stable_preferences": len(profile.stable_preferences),
                "preference_hypotheses": len(profile.preference_hypotheses),
            },
            "profile_keys": {
                "constraints": [item.key for item in profile.constraints[:8]],
                "rejections": [item.key for item in profile.rejections[:8]],
                "stable_preferences": [
                    item.key for item in profile.stable_preferences[:8]
                ],
                "preference_hypotheses": [
                    item.key for item in profile.preference_hypotheses[:8]
                ],
            },
            "working_memory_count": len(working_memory.items),
            "working_memory_preview": [
                item.content for item in working_memory.items[:5]
            ],
        }

    def _publish_memory_task(session_id: str, task: InternalTask) -> None:
        active = memory_active_tasks.setdefault(session_id, {})
        active[task.id] = task
        cutoff = time.time() - 300
        for task_id, existing_task in list(active.items()):
            ended_at = getattr(existing_task, "ended_at", None)
            if ended_at is not None and ended_at < cutoff:
                active.pop(task_id, None)
        if len(active) > 20:
            ordered_task_ids = sorted(
                active,
                key=lambda task_id: (
                    active[task_id].ended_at is None,
                    active[task_id].ended_at or active[task_id].started_at or 0,
                ),
            )
            for task_id in ordered_task_ids[: len(active) - 20]:
                active.pop(task_id, None)

        payload = json.dumps(
            {"type": "internal_task", "task": task.to_dict()},
            ensure_ascii=False,
        )
        subscribers = list(memory_task_subscribers.get(session_id, set()))
        delivered_count = 0
        dropped_count = 0
        for queue in list(memory_task_subscribers.get(session_id, set())):
            try:
                queue.put_nowait(payload)
                delivered_count += 1
            except asyncio.QueueFull:
                dropped_count += 1
                continue
        logger.warning(
            "后台记忆任务发布 session=%s task_id=%s kind=%s status=%s scope=%s subscribers=%s delivered=%s dropped=%s active_tasks=%s",
            session_id,
            task.id,
            task.kind,
            task.status,
            task.scope,
            len(subscribers),
            delivered_count,
            dropped_count,
            len(active),
        )

    def _get_memory_scheduler_runtime(session_id: str) -> MemorySchedulerRuntime:
        runtime = memory_scheduler_runtimes.get(session_id)
        if runtime is not None:
            return runtime

        async def _runner(snapshot: MemoryJobSnapshot) -> None:
            await _run_memory_job(snapshot)

        runtime = MemorySchedulerRuntime(scheduler=MemoryJobScheduler(runner=_runner))
        memory_scheduler_runtimes[session_id] = runtime
        return runtime

    def _build_memory_job_snapshot(
        *,
        session_id: str,
        user_id: str,
        messages: list[Message],
        plan: TravelPlanState,
    ) -> MemoryJobSnapshot:
        user_messages = [
            message.content
            for message in messages
            if message.role == Role.USER and message.content
        ]
        return MemoryJobSnapshot(
            session_id=session_id,
            user_id=user_id,
            turn_id=str(uuid.uuid4()),
            user_messages=list(user_messages),
            submitted_user_count=len(user_messages),
            plan_snapshot=TravelPlanState.from_dict(plan.to_dict()),
        )

    def _submit_memory_snapshot(snapshot: MemoryJobSnapshot) -> None:
        if not config.memory.enabled or not config.memory.extraction.enabled:
            logger.warning(
                "记忆提取快照未提交 session=%s turn=%s reason=disabled memory_enabled=%s extraction_enabled=%s",
                snapshot.session_id,
                snapshot.turn_id,
                config.memory.enabled,
                config.memory.extraction.enabled,
            )
            return
        if config.memory.extraction.trigger != "each_turn":
            logger.warning(
                "记忆提取快照未提交 session=%s turn=%s reason=trigger_not_matched trigger=%s",
                snapshot.session_id,
                snapshot.turn_id,
                config.memory.extraction.trigger,
            )
            return
        runtime = _get_memory_scheduler_runtime(snapshot.session_id)
        logger.warning(
            "记忆提取快照提交 session=%s turn=%s user=%s user_messages=%s submitted_user_count=%s scheduler_running=%s has_pending=%s",
            snapshot.session_id,
            snapshot.turn_id,
            snapshot.user_id,
            len(snapshot.user_messages),
            snapshot.submitted_user_count,
            runtime.scheduler.running_task is not None
            and not runtime.scheduler.running_task.done(),
            runtime.scheduler.pending_snapshot is not None,
        )
        runtime.scheduler.submit(snapshot)

    async def _decide_memory_recall(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
        memory_summary: dict[str, Any] | None = None,
    ) -> MemoryRecallDecision:
        stage0 = apply_recall_short_circuit(user_messages[-1] if user_messages else "")
        stage0_signals = _stage0_signals_to_dict(stage0.signals)
        if stage0.decision == "force_recall":
            return MemoryRecallDecision(
                needs_recall=True,
                stage0_decision=stage0.decision,
                stage0_reason=stage0.reason,
                stage0_matched_rule=stage0.matched_rule,
                stage0_signals=stage0_signals,
                intent_type="",
                reason=stage0.reason,
                memory_summary=memory_summary or {},
            )
        if stage0.decision == "skip_recall":
            return MemoryRecallDecision(
                needs_recall=False,
                stage0_decision=stage0.decision,
                stage0_reason=stage0.reason,
                stage0_matched_rule=stage0.matched_rule,
                stage0_signals=stage0_signals,
                intent_type="",
                reason=stage0.reason,
                recall_skip_source="stage0_skip",
                memory_summary=memory_summary or {},
            )
        if not config.memory.retrieval.recall_gate_enabled:
            return MemoryRecallDecision(
                needs_recall=False,
                stage0_decision=stage0.decision,
                stage0_reason=stage0.reason,
                stage0_matched_rule=stage0.matched_rule,
                stage0_signals=stage0_signals,
                intent_type="no_recall_needed",
                reason="recall_gate_disabled",
                recall_skip_source="gate_disabled",
                memory_summary=memory_summary or {},
            )
        gate_window = build_gate_user_window(
            user_messages=user_messages,
            max_messages=_GATE_MAX_USER_MESSAGES,
            max_chars=_GATE_MAX_CHARS,
        )
        if not gate_window:
            return MemoryRecallDecision(
                needs_recall=False,
                stage0_decision=stage0.decision,
                stage0_reason=stage0.reason,
                stage0_matched_rule=stage0.matched_rule,
                stage0_signals=stage0_signals,
                intent_type="no_recall_needed",
                reason="no_user_messages",
                recall_skip_source="no_user_messages",
                memory_summary=memory_summary or {},
            )

        if memory_summary is None:
            memory_summary = await _build_gate_memory_summary(
                user_id=user_id,
                session_id=session_id,
                plan_snapshot=plan_snapshot,
            )
        prompt = "\n".join(
            [
                "你是旅行记忆召回判定器。",
                "如果用户在问当前行程事实、继续当前规划或无需引用过往偏好/历史经历，则 needs_recall=false。",
                "只有在需要调取长期画像、历史偏好或过往旅行经历时，needs_recall=true。",
                f"最近用户消息：{json.dumps(gate_window, ensure_ascii=False)}",
                f"当前旅行事实：{json.dumps(_memory_plan_facts(plan_snapshot), ensure_ascii=False)}",
                f"现有记忆概览：{json.dumps(memory_summary, ensure_ascii=False)}",
                "必须调用 decide_memory_recall 工具输出结果。",
            ]
        )
        recall_gate_model = config.memory.retrieval.recall_gate_model or config.llm.model
        gate_llm = create_llm_provider(replace(config.llm, model=recall_gate_model))
        try:
            tool_args = await asyncio.wait_for(
                _collect_forced_tool_call_arguments(
                    gate_llm,
                    messages=[Message(role=Role.USER, content=prompt)],
                    tool_def=build_recall_gate_tool(),
                ),
                timeout=config.memory.retrieval.recall_gate_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "记忆召回判定超时 session=%s user=%s timeout_seconds=%s",
                session_id,
                user_id,
                config.memory.retrieval.recall_gate_timeout_seconds,
            )
            return _gate_failure_recall_decision_from_heuristic(
                user_message=user_messages[-1] if user_messages else "",
                stage0=stage0,
                stage0_signals=stage0_signals,
                reason="gate_timeout",
                fallback_used="gate_timeout",
            )
        except Exception:
            logger.exception(
                "记忆召回判定失败 session=%s user=%s",
                session_id,
                user_id,
            )
            return _gate_failure_recall_decision_from_heuristic(
                user_message=user_messages[-1] if user_messages else "",
                stage0=stage0,
                stage0_signals=stage0_signals,
                reason="gate_error",
                fallback_used="gate_error",
            )
        gate_decision = parse_recall_gate_tool_arguments(tool_args)
        gate_intent_type = gate_decision.intent_type
        if gate_decision.fallback_used == "invalid_tool_payload":
            return _gate_failure_recall_decision_from_heuristic(
                user_message=user_messages[-1] if user_messages else "",
                stage0=stage0,
                stage0_signals=stage0_signals,
                reason="invalid_tool_payload",
                fallback_used="invalid_tool_payload",
            )
        return MemoryRecallDecision(
            needs_recall=gate_decision.needs_recall,
            stage0_decision=stage0.decision,
            stage0_reason=stage0.reason,
            stage0_matched_rule=stage0.matched_rule,
            stage0_signals=stage0_signals,
            intent_type=gate_intent_type,
            reason=gate_decision.reason,
            confidence=gate_decision.confidence,
            fallback_used=gate_decision.fallback_used,
            recall_skip_source="" if gate_decision.needs_recall else "gate_false",
            gate_user_window=list(gate_window),
            memory_summary=dict(memory_summary),
        )

    async def _build_recall_retrieval_plan(
        *,
        session_id: str,
        user_id: str,
        user_message: str,
        user_messages: list[str],
        gate_intent_type: str,
        gate_reason: str,
        gate_confidence: float | None,
        stage0_decision: str,
        stage0_signals: dict[str, list[str]],
        plan_snapshot: TravelPlanState,
        memory_summary: dict[str, Any] | None = None,
    ) -> RecallQueryPlanResult:
        query_llm = create_llm_provider(config.llm)
        recent_user_window = build_gate_user_window(
            user_messages=user_messages,
            max_messages=_GATE_MAX_USER_MESSAGES,
            max_chars=_GATE_MAX_CHARS,
        )
        if memory_summary is None:
            memory_summary = await _build_gate_memory_summary(
                user_id=user_id,
                session_id=session_id,
                plan_snapshot=plan_snapshot,
            )
        prompt = _build_recall_query_prompt(
            user_message=user_message,
            recent_user_window=recent_user_window,
            gate_intent_type=gate_intent_type,
            gate_reason=gate_reason,
            gate_confidence=gate_confidence,
            stage0_signals=stage0_signals,
            plan_facts=_memory_plan_facts(plan_snapshot),
            memory_summary=memory_summary,
        )
        try:
            tool_args = await asyncio.wait_for(
                _collect_forced_tool_call_arguments(
                    query_llm,
                    messages=[
                        Message(
                            role=Role.USER,
                            content=prompt,
                        )
                    ],
                    tool_def=_build_recall_query_tool(),
                ),
                timeout=config.memory.retrieval.recall_gate_timeout_seconds,
            )
        except asyncio.TimeoutError:
            heuristic_plan = heuristic_retrieval_plan_from_message(
                user_message,
                stage0_decision=stage0_decision,
                stage0_signals=stage0_signals,
            )
            return RecallQueryPlanResult(
                plan=heuristic_plan,
                query_plan_source="heuristic_fallback",
                query_plan_fallback="query_plan_timeout",
            )
        except Exception:
            logger.exception("记忆召回 query tool 调用失败")
            heuristic_plan = heuristic_retrieval_plan_from_message(
                user_message,
                stage0_decision=stage0_decision,
                stage0_signals=stage0_signals,
            )
            return RecallQueryPlanResult(
                plan=heuristic_plan,
                query_plan_source="heuristic_fallback",
                query_plan_fallback="query_plan_error",
            )

        plan = parse_recall_query_tool_arguments(tool_args)
        if not plan.reason:
            plan.reason = "query_plan_generated"
        return RecallQueryPlanResult(
            plan=plan,
            query_plan_source=(
                "default_fallback" if plan.fallback_used != "none" else "llm"
            ),
            query_plan_fallback=plan.fallback_used,
        )

    async def _decide_memory_extraction(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
    ) -> MemoryExtractionGateDecision:
        if not config.memory.enabled or not config.memory.extraction.enabled:
            return MemoryExtractionGateDecision(
                should_extract=False,
                reason="disabled",
                message="记忆提取未启用",
            )
        if config.memory.extraction.trigger != "each_turn":
            return MemoryExtractionGateDecision(
                should_extract=False,
                reason="trigger_not_matched",
                message="当前提取策略未在本轮触发",
            )
        if not user_messages:
            return MemoryExtractionGateDecision(
                should_extract=False,
                reason="no_user_messages",
                message="本轮没有可提取的用户消息",
            )

        gate_window = build_gate_user_window(
            user_messages=user_messages,
            max_messages=_GATE_MAX_USER_MESSAGES,
            max_chars=_GATE_MAX_CHARS,
        )
        if not gate_window:
            return MemoryExtractionGateDecision(
                should_extract=False,
                reason="no_user_messages",
                message="本轮没有可提取的用户消息",
            )

        memory_summary = await _build_gate_memory_summary(
            user_id=user_id,
            session_id=session_id,
            plan_snapshot=plan_snapshot,
        )
        prompt = build_v3_extraction_gate_prompt(
            user_messages=gate_window,
            plan_facts=_memory_plan_facts(plan_snapshot),
            existing_memory_summary=memory_summary,
        )
        gate_llm = create_llm_provider(config.llm)
        logger.warning(
            "记忆提取判定开始调用模型 session=%s user=%s model=%s prompt_chars=%s user_messages=%s",
            session_id,
            user_id,
            config.llm.model,
            len(prompt),
            len(gate_window),
        )
        try:
            tool_args = await asyncio.wait_for(
                _collect_forced_tool_call_arguments(
                    gate_llm,
                    messages=[Message(role=Role.USER, content=prompt)],
                    tool_def=build_v3_extraction_gate_tool(),
                ),
                timeout=_EXTRACTION_GATE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "记忆提取判定超时 session=%s user=%s timeout_seconds=%s",
                session_id,
                user_id,
                _EXTRACTION_GATE_TIMEOUT_SECONDS,
            )
            return MemoryExtractionGateDecision(
                should_extract=False,
                reason="timeout",
                message="记忆提取判定超时，已跳过本轮提取。",
            )
        except Exception:
            logger.exception(
                "记忆提取判定失败 session=%s user=%s",
                session_id,
                user_id,
            )
            return MemoryExtractionGateDecision(
                should_extract=False,
                reason="error",
                message="记忆提取判定失败，已跳过本轮提取。",
                error="记忆提取判定异常，请检查后端日志。",
            )

        decision = parse_v3_extraction_gate_tool_arguments(tool_args)
        if not decision.reason:
            decision.reason = (
                "memory_routes_detected"
                if decision.should_extract
                else "no_memory_routes"
            )
        if not decision.message:
            decision.message = (
                "检测到需要提取的记忆信号"
                if decision.should_extract
                else "本轮未发现需要提取的长期画像或工作记忆信号"
            )
        routes = {
            "profile": decision.routes.profile,
            "working_memory": decision.routes.working_memory,
        }
        logger.warning(
            "记忆提取判定完成 session=%s user=%s should_extract=%s reason=%s routes=%s",
            session_id,
            user_id,
            decision.should_extract,
            decision.reason,
            routes,
        )
        return MemoryExtractionGateDecision(
            should_extract=decision.should_extract,
            reason=decision.reason,
            message=decision.message,
            routes=routes,
        )

    async def _extract_memory_candidates(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
        routes: dict[str, bool] | None = None,
        turn_id: str | None = None,
    ) -> MemoryExtractionOutcome:
        if not config.memory.enabled or not config.memory.extraction.enabled:
            return MemoryExtractionOutcome(
                status="skipped",
                message="记忆提取未启用",
                item_ids=[],
                reason="disabled",
            )
        if config.memory.extraction.trigger != "each_turn":
            return MemoryExtractionOutcome(
                status="skipped",
                message="当前提取策略未在本轮触发",
                item_ids=[],
                reason="trigger_not_matched",
            )
        if not user_messages:
            return MemoryExtractionOutcome(
                status="skipped",
                message="本轮没有可提取的用户消息",
                item_ids=[],
                reason="no_user_messages",
            )

        progress = MemoryExtractionProgress()
        try:
            return await asyncio.wait_for(
                _do_extract_memory_candidates(
                    session_id=session_id,
                    user_id=user_id,
                    user_messages=user_messages,
                    plan_snapshot=plan_snapshot,
                    routes=routes,
                    turn_id=turn_id,
                    progress=progress,
                ),
                timeout=_EXTRACTION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "记忆提取超时 session=%s user=%s timeout_seconds=%s",
                session_id,
                user_id,
                _EXTRACTION_TIMEOUT_SECONDS,
            )
            message = "记忆提取超时，本轮未写入记忆。"
            if progress.saved_total > 0:
                message = "记忆提取超时，已保留部分写入结果，剩余内容将稍后重试。"
            return MemoryExtractionOutcome(
                status="warning",
                message=message,
                item_ids=list(progress.pending_ids),
                saved_profile_count=progress.saved_profile_count,
                saved_working_count=progress.saved_working_count,
                reason="timeout",
            )
        except Exception:
            logger.exception(
                "记忆提取失败 session=%s user=%s",
                session_id,
                user_id,
            )
            return MemoryExtractionOutcome(
                status="error",
                message="记忆提取失败，本轮未写入记忆。",
                item_ids=[],
                reason="error",
                error="记忆提取异常，请检查后端日志。",
            )

    async def _extract_combined_memory_items(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
        profile: Any,
        working_memory: Any,
    ) -> V3ExtractionResult:
        prompt = build_v3_extraction_prompt(
            user_messages=user_messages,
            profile=profile,
            working_memory=working_memory,
            plan_facts=_memory_plan_facts(plan_snapshot),
        )
        extraction_llm = create_llm_provider(config.llm)
        logger.warning(
            "兼容记忆提取开始调用模型 session=%s user=%s model=%s prompt_chars=%s user_messages=%s",
            session_id,
            user_id,
            config.llm.model,
            len(prompt),
            len(user_messages),
        )
        tool_args = await _collect_forced_tool_call_arguments(
            extraction_llm,
            messages=[Message(role=Role.USER, content=prompt)],
            tool_def=build_v3_extraction_tool(),
        )
        logger.warning(
            "兼容记忆提取模型返回 session=%s user=%s has_arguments=%s argument_keys=%s",
            session_id,
            user_id,
            bool(tool_args),
            sorted(tool_args.keys()) if isinstance(tool_args, dict) else [],
        )
        return parse_v3_extraction_tool_arguments(tool_args)

    async def _extract_profile_memory_items(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
        profile: Any,
    ) -> V3ExtractionResult:
        prompt = build_v3_profile_extraction_prompt(
            user_messages=user_messages,
            profile=profile,
            plan_facts=_memory_plan_facts(plan_snapshot),
        )
        extraction_llm = create_llm_provider(config.llm)
        logger.warning(
            "长期画像记忆提取开始调用模型 session=%s user=%s model=%s prompt_chars=%s user_messages=%s",
            session_id,
            user_id,
            config.llm.model,
            len(prompt),
            len(user_messages),
        )
        tool_args = await _collect_forced_tool_call_arguments(
            extraction_llm,
            messages=[Message(role=Role.USER, content=prompt)],
            tool_def=build_v3_profile_extraction_tool(),
        )
        logger.warning(
            "长期画像记忆提取模型返回 session=%s user=%s has_arguments=%s argument_keys=%s",
            session_id,
            user_id,
            bool(tool_args),
            sorted(tool_args.keys()) if isinstance(tool_args, dict) else [],
        )
        return parse_v3_profile_extraction_tool_arguments(tool_args)

    async def _extract_working_memory_items(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
        working_memory: Any,
    ) -> V3ExtractionResult:
        prompt = build_v3_working_memory_extraction_prompt(
            user_messages=user_messages,
            working_memory=working_memory,
            plan_facts=_memory_plan_facts(plan_snapshot),
        )
        extraction_llm = create_llm_provider(config.llm)
        logger.warning(
            "工作记忆提取开始调用模型 session=%s user=%s model=%s prompt_chars=%s user_messages=%s",
            session_id,
            user_id,
            config.llm.model,
            len(prompt),
            len(user_messages),
        )
        tool_args = await _collect_forced_tool_call_arguments(
            extraction_llm,
            messages=[Message(role=Role.USER, content=prompt)],
            tool_def=build_v3_working_memory_extraction_tool(),
        )
        logger.warning(
            "工作记忆提取模型返回 session=%s user=%s has_arguments=%s argument_keys=%s",
            session_id,
            user_id,
            bool(tool_args),
            sorted(tool_args.keys()) if isinstance(tool_args, dict) else [],
        )
        return parse_v3_working_memory_extraction_tool_arguments(tool_args)

    def _count_profile_updates(profile_updates: Any) -> int:
        return sum(
            len(items)
            for items in (
                profile_updates.constraints,
                profile_updates.rejections,
                profile_updates.stable_preferences,
                profile_updates.preference_hypotheses,
            )
        )

    async def _save_profile_updates(
        *,
        user_id: str,
        profile_updates: Any,
        policy: MemoryPolicy,
        now: str,
        route_progress: MemoryRouteSaveProgress,
        aggregate_progress: MemoryExtractionProgress,
    ) -> None:
        def _profile_items_match(
            comparison_bucket: str,
            existing_item: Any,
            incoming_item: Any,
        ) -> bool:
            if comparison_bucket in {"constraints", "stable_preferences"}:
                return (
                    existing_item.domain == incoming_item.domain
                    and existing_item.key == incoming_item.key
                )
            return (
                existing_item.domain == incoming_item.domain
                and existing_item.key == incoming_item.key
                and existing_item.value == incoming_item.value
            )

        def _candidate_profile_buckets(
            bucket_name: str,
        ) -> tuple[str, ...]:
            if bucket_name == "preference_hypotheses":
                return ("preference_hypotheses", "stable_preferences")
            return (bucket_name,)

        def _find_matching_profile_item_location(
            comparison_bucket: str,
            candidate_buckets: tuple[str, ...],
            incoming_item: Any,
        ) -> tuple[str, int] | None:
            for candidate_bucket in candidate_buckets:
                candidate_items = getattr(profile, candidate_bucket)
                for index, existing_item in enumerate(candidate_items):
                    if _profile_items_match(
                        comparison_bucket, existing_item, incoming_item
                    ):
                        return candidate_bucket, index
            return None

        def _upsert_profile_item_in_memory(
            bucket_name: str,
            item: Any,
        ) -> None:
            bucket_items = getattr(profile, bucket_name)
            for index, existing_item in enumerate(bucket_items):
                if existing_item.id == item.id:
                    bucket_items[index] = item
                    break
            else:
                bucket_items.append(item)

        profile = await memory_mgr.v3_store.load_profile(user_id)
        buckets = (
            ("constraints", profile_updates.constraints),
            ("rejections", profile_updates.rejections),
            ("stable_preferences", profile_updates.stable_preferences),
            ("preference_hypotheses", profile_updates.preference_hypotheses),
        )
        for bucket, items in buckets:
            for raw_item in items:
                normalized = normalize_profile_item(bucket, raw_item)
                match_location = _find_matching_profile_item_location(
                    bucket,
                    _candidate_profile_buckets(bucket),
                    normalized,
                )
                matched_bucket_name: str | None = None
                matched_index: int | None = None
                if match_location is not None:
                    matched_bucket_name, matched_index = match_location
                existing_items = (
                    [getattr(profile, matched_bucket_name)[matched_index]]
                    if matched_bucket_name is not None and matched_index is not None
                    else []
                )
                merged_bucket, merged_item = merge_profile_item_with_existing(
                    bucket,
                    normalized,
                    existing_items,
                )
                action = policy.classify_v3_profile_item(merged_bucket, merged_item)
                if action == "drop":
                    continue
                sanitized = policy.sanitize_v3_profile_item(merged_item)
                sanitized.status = action
                sanitized.updated_at = now
                if not sanitized.created_at:
                    sanitized.created_at = now
                sanitized.id = generate_profile_item_id(merged_bucket, sanitized)
                if (
                    matched_bucket_name is not None
                    and matched_index is not None
                    and matched_bucket_name != merged_bucket
                ):
                    del getattr(profile, matched_bucket_name)[matched_index]
                    await memory_mgr.v3_store.save_profile(profile)
                await memory_mgr.v3_store.upsert_profile_item(
                    user_id, merged_bucket, sanitized
                )
                _upsert_profile_item_in_memory(merged_bucket, sanitized)
                route_progress.saved_count += 1
                aggregate_progress.saved_profile_count += 1
                if action in {"pending", "pending_conflict"}:
                    route_progress.pending_ids.append(sanitized.id)
                    aggregate_progress.pending_ids.append(sanitized.id)

    async def _save_working_memory_items(
        *,
        user_id: str,
        session_id: str,
        plan_snapshot: TravelPlanState,
        working_memory_items: list[Any],
        policy: MemoryPolicy,
        now: str,
        route_progress: MemoryRouteSaveProgress,
        aggregate_progress: MemoryExtractionProgress,
    ) -> None:
        for raw_working_item in working_memory_items:
            sanitized_working = policy.sanitize_working_memory_item(raw_working_item)
            if not sanitized_working.created_at:
                sanitized_working.created_at = now
            await memory_mgr.v3_store.upsert_working_memory_item(
                user_id,
                session_id,
                plan_snapshot.trip_id,
                sanitized_working,
            )
            route_progress.saved_count += 1
            aggregate_progress.saved_working_count += 1

    def _publish_split_memory_task(
        *,
        session_id: str,
        task_id: str,
        kind: str,
        label: str,
        status: str,
        message: str,
        started_at: float,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        ended_at: float | None = None,
    ) -> None:
        _publish_memory_task(
            session_id,
            InternalTask(
                id=task_id,
                kind=kind,
                label=label,
                status=status,
                message=message,
                blocking=False,
                scope="background",
                result=result,
                error=error,
                started_at=started_at,
                ended_at=ended_at,
            ),
        )

    async def _do_extract_memory_candidates(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
        routes: dict[str, bool] | None = None,
        turn_id: str | None = None,
        progress: MemoryExtractionProgress | None = None,
    ) -> MemoryExtractionOutcome:
        route_flags = routes or {"profile": True, "working_memory": True}
        run_profile = bool(route_flags.get("profile"))
        run_working = bool(route_flags.get("working_memory"))
        if not run_profile and not run_working:
            return MemoryExtractionOutcome(
                status="skipped",
                message="本轮没有新的可复用记忆",
                item_ids=[],
                reason="no_routes",
            )

        profile = await memory_mgr.v3_store.load_profile(user_id)
        working_memory = await memory_mgr.v3_store.load_working_memory(
            user_id, session_id, plan_snapshot.trip_id
        )
        logger.warning(
            "记忆提取路由开始 session=%s user=%s routes=%s user_messages=%s",
            session_id,
            user_id,
            route_flags,
            len(user_messages),
        )

        policy = MemoryPolicy(
            auto_save_low_risk=config.memory.policy.auto_save_low_risk,
            auto_save_medium_risk=config.memory.policy.auto_save_medium_risk,
        )
        now = _now_iso()
        aggregate_progress = progress or MemoryExtractionProgress()
        parsed_profile_count = 0
        parsed_working_count = 0
        route_failures: list[tuple[str, str]] = []
        task_turn_id = turn_id or str(uuid.uuid4())

        if run_profile:
            profile_progress = MemoryRouteSaveProgress()
            profile_task_id = f"profile_memory_extraction:{session_id}:{task_turn_id}"
            profile_started_at = time.time()
            _publish_split_memory_task(
                session_id=session_id,
                task_id=profile_task_id,
                kind="profile_memory_extraction",
                label="长期画像提取",
                status="pending",
                message="正在提取长期画像记忆…",
                started_at=profile_started_at,
            )
            try:
                profile_result = await _extract_profile_memory_items(
                    session_id=session_id,
                    user_id=user_id,
                    user_messages=user_messages,
                    plan_snapshot=plan_snapshot,
                    profile=profile,
                )
                parsed_profile_count = _count_profile_updates(
                    profile_result.profile_updates
                )
                await _save_profile_updates(
                    user_id=user_id,
                    profile_updates=profile_result.profile_updates,
                    policy=policy,
                    now=now,
                    route_progress=profile_progress,
                    aggregate_progress=aggregate_progress,
                )
                profile_status = (
                    "success" if profile_progress.saved_count > 0 else "skipped"
                )
                profile_message = (
                    f"已保存 {profile_progress.saved_count} 条长期画像记忆"
                    if profile_progress.saved_count > 0
                    else "本轮没有新的长期画像记忆"
                )
                _publish_split_memory_task(
                    session_id=session_id,
                    task_id=profile_task_id,
                    kind="profile_memory_extraction",
                    label="长期画像提取",
                    status=profile_status,
                    message=profile_message,
                    result={
                        "saved_profile_count": profile_progress.saved_count,
                        "pending_profile_count": len(profile_progress.pending_ids),
                        "pending_profile_ids": list(profile_progress.pending_ids),
                        "parsed_profile_count": parsed_profile_count,
                    },
                    started_at=profile_started_at,
                    ended_at=time.time(),
                )
            except asyncio.CancelledError:
                _publish_split_memory_task(
                    session_id=session_id,
                    task_id=profile_task_id,
                    kind="profile_memory_extraction",
                    label="长期画像提取",
                    status="warning",
                    message="长期画像提取已取消，未完成部分将稍后重试。",
                    result={
                        "saved_profile_count": profile_progress.saved_count,
                        "pending_profile_count": len(profile_progress.pending_ids),
                        "pending_profile_ids": list(profile_progress.pending_ids),
                        "parsed_profile_count": parsed_profile_count,
                    },
                    error="profile_memory_extraction_cancelled",
                    started_at=profile_started_at,
                    ended_at=time.time(),
                )
                raise
            except Exception:
                logger.exception(
                    "长期画像记忆提取失败 session=%s user=%s",
                    session_id,
                    user_id,
                )
                route_failures.append(
                    ("profile", "profile_memory_extraction_failed")
                )
                _publish_split_memory_task(
                    session_id=session_id,
                    task_id=profile_task_id,
                    kind="profile_memory_extraction",
                    label="长期画像提取",
                    status="error",
                    message="长期画像提取失败，本轮将稍后重试。",
                    result={
                        "saved_profile_count": profile_progress.saved_count,
                        "pending_profile_count": len(profile_progress.pending_ids),
                        "pending_profile_ids": list(profile_progress.pending_ids),
                        "parsed_profile_count": parsed_profile_count,
                    },
                    error="profile_memory_extraction_failed",
                    started_at=profile_started_at,
                    ended_at=time.time(),
                )
        if run_working:
            working_progress = MemoryRouteSaveProgress()
            working_task_id = (
                f"working_memory_extraction:{session_id}:{task_turn_id}"
            )
            working_started_at = time.time()
            _publish_split_memory_task(
                session_id=session_id,
                task_id=working_task_id,
                kind="working_memory_extraction",
                label="工作记忆提取",
                status="pending",
                message="正在提取工作记忆…",
                started_at=working_started_at,
            )
            try:
                working_result = await _extract_working_memory_items(
                    session_id=session_id,
                    user_id=user_id,
                    user_messages=user_messages,
                    plan_snapshot=plan_snapshot,
                    working_memory=working_memory,
                )
                parsed_working_count = len(working_result.working_memory)
                await _save_working_memory_items(
                    user_id=user_id,
                    session_id=session_id,
                    plan_snapshot=plan_snapshot,
                    working_memory_items=working_result.working_memory,
                    policy=policy,
                    now=now,
                    route_progress=working_progress,
                    aggregate_progress=aggregate_progress,
                )
                working_status = (
                    "success" if working_progress.saved_count > 0 else "skipped"
                )
                working_message = (
                    f"已保存 {working_progress.saved_count} 条工作记忆"
                    if working_progress.saved_count > 0
                    else "本轮没有新的工作记忆"
                )
                _publish_split_memory_task(
                    session_id=session_id,
                    task_id=working_task_id,
                    kind="working_memory_extraction",
                    label="工作记忆提取",
                    status=working_status,
                    message=working_message,
                    result={
                        "saved_working_count": working_progress.saved_count,
                        "parsed_working_count": parsed_working_count,
                    },
                    started_at=working_started_at,
                    ended_at=time.time(),
                )
            except asyncio.CancelledError:
                _publish_split_memory_task(
                    session_id=session_id,
                    task_id=working_task_id,
                    kind="working_memory_extraction",
                    label="工作记忆提取",
                    status="warning",
                    message="工作记忆提取已取消，未完成部分将稍后重试。",
                    result={
                        "saved_working_count": working_progress.saved_count,
                        "parsed_working_count": parsed_working_count,
                    },
                    error="working_memory_extraction_cancelled",
                    started_at=working_started_at,
                    ended_at=time.time(),
                )
                raise
            except Exception:
                logger.exception(
                    "工作记忆提取失败 session=%s user=%s",
                    session_id,
                    user_id,
                )
                route_failures.append(
                    ("working_memory", "working_memory_extraction_failed")
                )
                _publish_split_memory_task(
                    session_id=session_id,
                    task_id=working_task_id,
                    kind="working_memory_extraction",
                    label="工作记忆提取",
                    status="error",
                    message="工作记忆提取失败，本轮将稍后重试。",
                    result={
                        "saved_working_count": working_progress.saved_count,
                        "parsed_working_count": parsed_working_count,
                    },
                    error="working_memory_extraction_failed",
                    started_at=working_started_at,
                    ended_at=time.time(),
                )

        if parsed_profile_count == 0 and parsed_working_count == 0 and not route_failures:
            logger.warning(
                "记忆提取未产生任何结构化结果 session=%s user=%s routes=%s",
                session_id,
                user_id,
                route_flags,
            )
        else:
            logger.warning(
                "记忆提取解析完成 session=%s user=%s profile_items=%s working_items=%s",
                session_id,
                user_id,
                parsed_profile_count,
                parsed_working_count,
            )

        saved_total = (
            aggregate_progress.saved_profile_count
            + aggregate_progress.saved_working_count
        )
        if route_failures:
            failure_errors = [failure_error for _, failure_error in route_failures]
            error = (
                failure_errors[0]
                if len(failure_errors) == 1
                else "multiple_memory_extraction_routes_failed"
            )
            return MemoryExtractionOutcome(
                status="warning",
                message="部分记忆提取失败，本轮将稍后重试。",
                item_ids=list(aggregate_progress.pending_ids),
                saved_profile_count=aggregate_progress.saved_profile_count,
                saved_working_count=aggregate_progress.saved_working_count,
                reason="partial_failure",
                error=error,
            )

        if saved_total == 0:
            return MemoryExtractionOutcome(
                status="skipped",
                message="本轮没有新的可复用记忆",
                item_ids=[],
                reason="no_structured_result",
            )

        pending_count = len(aggregate_progress.pending_ids)
        if pending_count == 0:
            message = f"已提取 {saved_total} 条记忆"
        elif pending_count == saved_total:
            message = f"已提取 {pending_count} 条待确认记忆"
        else:
            message = (
                f"已提取 {saved_total} 条记忆，其中 {pending_count} 条待确认"
            )

        return MemoryExtractionOutcome(
            status="success",
            message=message,
            item_ids=list(aggregate_progress.pending_ids),
            saved_profile_count=aggregate_progress.saved_profile_count,
            saved_working_count=aggregate_progress.saved_working_count,
            reason="saved",
        )

    async def _run_memory_job(snapshot: MemoryJobSnapshot) -> None:
        runtime = _get_memory_scheduler_runtime(snapshot.session_id)
        plan_snapshot = (
            snapshot.plan_snapshot
            if isinstance(snapshot.plan_snapshot, TravelPlanState)
            else TravelPlanState(session_id=snapshot.session_id)
        )
        logger.warning(
            "记忆提取后台任务开始 session=%s turn=%s user=%s user_messages=%s submitted_user_count=%s last_consumed_user_count=%s trip_id=%s",
            snapshot.session_id,
            snapshot.turn_id,
            snapshot.user_id,
            len(snapshot.user_messages),
            snapshot.submitted_user_count,
            runtime.last_consumed_user_count,
            getattr(plan_snapshot, "trip_id", None),
        )
        gate_task_id = f"memory_extraction_gate:{snapshot.session_id}:{snapshot.turn_id}"
        gate_started_at = time.time()
        _publish_memory_task(
            snapshot.session_id,
            InternalTask(
                id=gate_task_id,
                kind="memory_extraction_gate",
                label="记忆提取判定",
                status="pending",
                message="正在判断本轮是否值得提取记忆…",
                blocking=False,
                scope="background",
                started_at=gate_started_at,
            ),
        )
        gate_decision = await _decide_memory_extraction(
            session_id=snapshot.session_id,
            user_id=snapshot.user_id,
            user_messages=snapshot.user_messages,
            plan_snapshot=plan_snapshot,
        )
        _publish_memory_task(
            snapshot.session_id,
            InternalTask(
                id=gate_task_id,
                kind="memory_extraction_gate",
                label="记忆提取判定",
                status=gate_decision.status,
                message=gate_decision.message,
                blocking=False,
                scope="background",
                result=gate_decision.to_result(),
                error=gate_decision.error,
                started_at=gate_started_at,
                ended_at=time.time(),
            ),
        )
        if not gate_decision.should_extract:
            if gate_decision.status == "skipped":
                runtime.last_consumed_user_count = max(
                    runtime.last_consumed_user_count,
                    snapshot.submitted_user_count,
                )
            logger.warning(
                "记忆提取后台任务结束 session=%s turn=%s gate_status=%s should_extract=%s reason=%s last_consumed_user_count=%s",
                snapshot.session_id,
                snapshot.turn_id,
                gate_decision.status,
                gate_decision.should_extract,
                gate_decision.reason,
                runtime.last_consumed_user_count,
            )
            return

        extraction_window = build_extraction_user_window(
            user_messages=snapshot.user_messages,
            last_consumed_user_count=runtime.last_consumed_user_count,
            submitted_user_count=snapshot.submitted_user_count,
            max_messages=_EXTRACTION_MAX_USER_MESSAGES,
            max_chars=_EXTRACTION_MAX_CHARS,
        )
        logger.warning(
            "记忆提取窗口构建完成 session=%s turn=%s user_messages=%s extraction_window=%s last_consumed_user_count=%s submitted_user_count=%s",
            snapshot.session_id,
            snapshot.turn_id,
            len(snapshot.user_messages),
            len(extraction_window),
            runtime.last_consumed_user_count,
            snapshot.submitted_user_count,
        )
        task_id = f"memory_extraction:{snapshot.session_id}:{snapshot.turn_id}"
        started_at = time.time()
        _publish_memory_task(
            snapshot.session_id,
            InternalTask(
                id=task_id,
                kind="memory_extraction",
                label="记忆提取",
                status="pending",
                message="正在提取可复用的旅行偏好…",
                blocking=False,
                scope="background",
                started_at=started_at,
            ),
        )
        outcome = await _extract_memory_candidates(
            session_id=snapshot.session_id,
            user_id=snapshot.user_id,
            user_messages=extraction_window,
            plan_snapshot=plan_snapshot,
            routes=gate_decision.routes,
            turn_id=snapshot.turn_id,
        )
        _publish_memory_task(
            snapshot.session_id,
            InternalTask(
                id=task_id,
                kind="memory_extraction",
                label="记忆提取",
                status=outcome.status,
                message=outcome.message,
                blocking=False,
                scope="background",
                result=outcome.to_result(),
                error=outcome.error,
                started_at=started_at,
                ended_at=time.time(),
            ),
        )
        if outcome.status in {"success", "skipped"}:
            runtime.last_consumed_user_count = max(
                runtime.last_consumed_user_count,
                snapshot.submitted_user_count,
            )
        logger.warning(
            "记忆提取后台任务结束 session=%s turn=%s extraction_status=%s reason=%s item_ids=%s saved_profile=%s saved_working=%s last_consumed_user_count=%s",
            snapshot.session_id,
            snapshot.turn_id,
            outcome.status,
            outcome.reason,
            len(outcome.item_ids),
            outcome.saved_profile_count,
            outcome.saved_working_count,
            runtime.last_consumed_user_count,
        )

    async def _memory_task_stream(session_id: str, request: Request):
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        subscribers = memory_task_subscribers.setdefault(session_id, set())
        subscribers.add(queue)
        logger.warning(
            "后台记忆任务 SSE 订阅打开 session=%s subscribers=%s active_tasks=%s",
            session_id,
            len(subscribers),
            len(memory_active_tasks.get(session_id, {})),
        )

        try:
            for task in memory_active_tasks.get(session_id, {}).values():
                logger.warning(
                    "后台记忆任务 SSE 重放 session=%s task_id=%s kind=%s status=%s",
                    session_id,
                    task.id,
                    task.kind,
                    task.status,
                )
                yield json.dumps(
                    {"type": "internal_task", "task": task.to_dict()},
                    ensure_ascii=False,
                )

            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(
                        queue.get(),
                        timeout=KEEPALIVE_INTERVAL_S,
                    )
                except asyncio.TimeoutError:
                    yield json.dumps({"type": "keepalive"}, ensure_ascii=False)
                    continue
                yield payload
        finally:
            subscribers.discard(queue)
            if not subscribers:
                memory_task_subscribers.pop(session_id, None)
            logger.warning(
                "后台记忆任务 SSE 订阅关闭 session=%s subscribers=%s",
                session_id,
                len(memory_task_subscribers.get(session_id, set())),
            )

    async def _append_archived_trip_episode_once(
        *,
        user_id: str,
        session_id: str,
        plan: TravelPlanState,
    ) -> bool:
        episode = build_archived_trip_episode(
            user_id=user_id,
            session_id=session_id,
            plan=plan,
            now=_now_iso(),
        )
        episodes = await memory_mgr.v3_store.list_episodes(user_id)
        if any(existing.id == episode.id for existing in episodes):
            await _append_episode_slices(episode)
            return False
        await memory_mgr.v3_store.append_episode(episode)
        await _append_episode_slices(episode)
        return True

    async def _append_episode_slices(episode) -> None:
        now = _now_iso()
        for slice_ in build_episode_slices(episode, now=now):
            await memory_mgr.v3_store.append_episode_slice(slice_)

    # Backtrack detection patterns
    _BACKTRACK_PATTERNS: dict[int, list[str]] = {
        1: [
            "重新开始",
            "从头来",
            "换个需求",
            "换个目的地",
            "不想去这里",
            "不去了",
            "换地方",
        ],
        3: ["改日期", "换时间", "日期不对", "换住宿", "不住这", "换个区域"],
    }

    def _detect_backtrack(message: str, plan: TravelPlanState) -> int | None:
        for target_phase, patterns in _BACKTRACK_PATTERNS.items():
            if target_phase >= plan.phase:
                continue
            if any(p in message for p in patterns):
                return target_phase
        return None

    _TRIP_RESET_PATTERNS = tuple(_BACKTRACK_PATTERNS[1]) + (
        "换目的地",
        "改目的地",
        "新行程",
        "重新规划",
    )

    def _is_new_trip_backtrack(to_phase: int, reason_text: str) -> bool:
        return to_phase == 1 and any(
            pattern in reason_text for pattern in _TRIP_RESET_PATTERNS
        )

    async def _rotate_trip_on_reset_backtrack(
        *,
        user_id: str,
        plan: TravelPlanState,
        to_phase: int,
        reason_text: str,
    ) -> bool:
        if not _is_new_trip_backtrack(to_phase, reason_text):
            return False
        plan.trip_id = f"trip_{uuid.uuid4().hex[:12]}"
        del user_id
        return True

    def _generate_title(plan: TravelPlanState) -> str:
        destination = plan.destination or "未定"
        if plan.dates:
            days = plan.dates.total_days
            nights = max(days - 1, 0)
            return f"{destination} · {days}天{nights}晚"
        return f"{destination} · 新会话"

    def _serialize_tool_result_data(data: object) -> str | None:
        if data is None:
            return None
        if isinstance(data, str):
            return data
        return json.dumps(data, ensure_ascii=False)

    def _deserialize_message_content(content: str | None) -> object:
        if content is None:
            return None
        try:
            return json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return content

    async def _persist_messages(session_id: str, messages: list[Message]) -> None:
        await _ensure_storage_ready()
        await db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        rows: list[dict[str, object]] = []
        for index, message in enumerate(messages):
            tool_calls_json = None
            if message.tool_calls:
                tool_calls_json = json.dumps(
                    [
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "arguments": tool_call.arguments,
                            "human_label": tool_call.human_label,
                        }
                        for tool_call in message.tool_calls
                    ],
                    ensure_ascii=False,
                )

            content = message.content
            tool_call_id = None
            if message.tool_result is not None:
                content = _serialize_tool_result_data(message.tool_result.data)
                tool_call_id = message.tool_result.tool_call_id

            rows.append(
                {
                    "role": message.role.value,
                    "content": content,
                    "tool_calls": tool_calls_json,
                    "tool_call_id": tool_call_id,
                    "seq": index,
                }
            )

        await message_store.append_batch(session_id, rows)

    async def _restore_session(session_id: str) -> dict | None:
        await _ensure_storage_ready()
        meta = await session_store.load(session_id)
        if meta is None or meta["status"] == "deleted":
            return None

        try:
            plan = await state_mgr.load(session_id)
        except FileNotFoundError:
            snapshot = await archive_store.load_latest_snapshot(session_id)
            if snapshot is None:
                return None
            plan = TravelPlanState.from_dict(json.loads(snapshot["plan_json"]))

        restored_messages: list[Message] = []
        for row in await message_store.load_all(session_id):
            role = Role(row["role"])
            tool_calls = None
            if row.get("tool_calls"):
                tool_calls = [
                    ToolCall(
                        id=payload["id"],
                        name=payload["name"],
                        arguments=payload["arguments"],
                        human_label=payload.get("human_label"),
                    )
                    for payload in json.loads(row["tool_calls"])
                ]

            tool_result = None
            if row.get("tool_call_id"):
                tool_result = ToolResult(
                    tool_call_id=row["tool_call_id"],
                    status="success",
                    data=_deserialize_message_content(row.get("content")),
                )

            restored_messages.append(
                Message(
                    role=role,
                    content=row.get("content") if tool_result is None else None,
                    tool_calls=tool_calls,
                    tool_result=tool_result,
                )
            )

        phase_router.sync_phase_state(plan)
        compression_events: list[dict] = []
        agent = _build_agent(
            plan,
            meta["user_id"],
            compression_events=compression_events,
        )
        return {
            "plan": plan,
            "messages": restored_messages,
            "agent": agent,
            "needs_rebuild": False,
            "user_id": meta["user_id"],
            "compression_events": compression_events,
            "stats": SessionStats(),
            "_pending_system_notes": [],
        }

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/sessions")
    async def create_session():
        await _ensure_storage_ready()
        plan = await state_mgr.create_session()
        compression_events: list[dict] = []
        agent = _build_agent(
            plan, "default_user", compression_events=compression_events
        )
        sessions[plan.session_id] = {
            "plan": plan,
            "messages": [],
            "agent": agent,
            "needs_rebuild": False,
            "user_id": "default_user",
            "compression_events": compression_events,
            "stats": SessionStats(),
            "_pending_system_notes": [],
        }
        await session_store.create(plan.session_id, "default_user")
        return {"session_id": plan.session_id, "phase": plan.phase}

    @app.get("/api/sessions")
    async def list_sessions():
        await _ensure_storage_ready()
        rows = await session_store.list_sessions()
        return [
            {
                "session_id": row["session_id"],
                "title": row["title"],
                "phase": row["phase"],
                "status": row["status"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    @app.get("/api/plan/{session_id}")
    async def get_plan(session_id: str):
        await _ensure_storage_ready()
        session = sessions.get(session_id)
        if not session:
            restored = await _restore_session(session_id)
            if restored is not None:
                sessions[session_id] = restored
                session = restored
            else:
                try:
                    plan = await state_mgr.load(session_id)
                    phase_router.sync_phase_state(plan)
                    return plan.to_dict()
                except (FileNotFoundError, ValueError):
                    raise HTTPException(status_code=404, detail="Session not found")
        phase_router.sync_phase_state(session["plan"])
        return session["plan"].to_dict()

    @app.get("/api/sessions/{session_id}/stats")
    async def get_session_stats(session_id: str):
        session = sessions.get(session_id)
        if not session:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        stats: SessionStats = session.get("stats", SessionStats())
        return stats.to_dict()

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        await _ensure_storage_ready()
        meta = await session_store.load(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Session not found")
        await session_store.soft_delete(session_id)
        sessions.pop(session_id, None)
        reflection_cache.pop(session_id, None)
        for key in list(quality_gate_retries):
            if key[0] == session_id:
                quality_gate_retries.pop(key, None)
        return {"status": "deleted"}

    @app.get("/api/messages/{session_id}")
    async def get_messages(session_id: str):
        await _ensure_storage_ready()
        meta = await session_store.load(session_id)
        if meta is None or meta["status"] == "deleted":
            raise HTTPException(status_code=404, detail="Session not found")
        rows = await message_store.load_all(session_id)
        return [
            {
                "role": row["role"],
                "content": row["content"],
                "tool_calls": (
                    json.loads(row["tool_calls"]) if row.get("tool_calls") else None
                ),
                "tool_call_id": row.get("tool_call_id"),
                "seq": row["seq"],
            }
            for row in rows
        ]

    @app.get("/api/archives/{session_id}")
    async def get_archive(session_id: str):
        await _ensure_storage_ready()
        result = await archive_store.load(session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Archive not found")
        return {
            "session_id": result["session_id"],
            "plan": json.loads(result["plan_json"]),
            "summary": result["summary"],
            "created_at": result["created_at"],
        }

    @app.get("/api/memory/{user_id}/profile")
    async def get_memory_profile(user_id: str):
        await _ensure_storage_ready()
        profile = await memory_mgr.v3_store.load_profile(user_id)
        return profile.to_dict()

    @app.get("/api/memory/{user_id}/episode-slices")
    async def list_memory_episode_slices(user_id: str):
        await _ensure_storage_ready()
        slices = await memory_mgr.v3_store.list_episode_slices(user_id)
        return {"slices": [slice_.to_dict() for slice_ in slices]}

    @app.get("/api/memory/{user_id}/sessions/{session_id}/working-memory")
    async def get_session_working_memory(user_id: str, session_id: str):
        await _ensure_storage_ready()
        session = sessions.get(session_id)
        if session is None:
            restored = await _restore_session(session_id)
            if restored is not None:
                sessions[session_id] = restored
                session = restored
        trip_id: str | None = None
        if session is not None:
            plan = session.get("plan")
            if plan is not None:
                trip_id = getattr(plan, "trip_id", None)
        memory = await memory_mgr.v3_store.load_working_memory(
            user_id, session_id, trip_id
        )
        return memory.to_dict()

    async def _set_v3_profile_item_status(
        user_id: str,
        item_id: str,
        status: str,
    ) -> bool:
        profile = await memory_mgr.v3_store.load_profile(user_id)
        updated = False
        now = _now_iso()

        for bucket in (
            "constraints",
            "rejections",
            "stable_preferences",
            "preference_hypotheses",
        ):
            items = getattr(profile, bucket)
            for index, item in enumerate(items):
                if item.id != item_id:
                    continue
                should_remove = status == "obsolete" or (
                    bucket == "preference_hypotheses" and status == "rejected"
                )
                if should_remove:
                    del items[index]
                else:
                    item.status = status
                    item.updated_at = now
                updated = True
                break
            if updated:
                break

        if not updated:
            return False

        await memory_mgr.v3_store.save_profile(profile)
        return True

    @app.post("/api/memory/{user_id}/profile/{item_id}/confirm")
    async def confirm_profile_item(user_id: str, item_id: str):
        await _ensure_storage_ready()
        if not await _set_v3_profile_item_status(user_id, item_id, "active"):
            raise HTTPException(status_code=404, detail="Profile item not found")
        return {"item_id": item_id, "status": "active"}

    @app.post("/api/memory/{user_id}/profile/{item_id}/reject")
    async def reject_profile_item(user_id: str, item_id: str):
        await _ensure_storage_ready()
        if not await _set_v3_profile_item_status(user_id, item_id, "rejected"):
            raise HTTPException(status_code=404, detail="Profile item not found")
        return {"item_id": item_id, "status": "rejected"}

    @app.get("/api/memory/{user_id}/episodes")
    async def list_memory_episodes(user_id: str):
        await _ensure_storage_ready()
        episodes = await memory_mgr.v3_store.list_episodes(user_id)
        return {"episodes": [episode.to_dict() for episode in episodes]}

    @app.delete("/api/memory/{user_id}/profile/{item_id}")
    async def delete_profile_item(user_id: str, item_id: str):
        await _ensure_storage_ready()
        if not await _set_v3_profile_item_status(user_id, item_id, "obsolete"):
            raise HTTPException(status_code=404, detail="Profile item not found")
        return {"item_id": item_id, "status": "obsolete"}

    @app.post("/api/backtrack/{session_id}")
    async def backtrack(session_id: str, req: BacktrackRequest):
        await _ensure_storage_ready()
        session = sessions.get(session_id)
        if not session:
            restored = await _restore_session(session_id)
            if restored is None:
                raise HTTPException(status_code=404, detail="Session not found")
            sessions[session_id] = restored
            session = restored
        plan = session["plan"]
        if req.to_phase == 2:
            req.to_phase = 1
        if req.to_phase >= plan.phase:
            raise HTTPException(status_code=400, detail="只能回退到更早的阶段")
        snapshot_path = await state_mgr.save_snapshot(plan)
        phase_router.prepare_backtrack(
            plan, req.to_phase, req.reason or "用户主动回退", snapshot_path
        )
        await state_mgr.clear_deliverables(session_id)
        await _rotate_trip_on_reset_backtrack(
            user_id=session.get("user_id", "default_user"),
            plan=plan,
            to_phase=req.to_phase,
            reason_text=req.reason,
        )
        await state_mgr.save(plan)
        session["agent"] = _build_agent(
            plan,
            session.get("user_id", "default_user"),
            compression_events=session.get("compression_events"),
        )
        session["needs_rebuild"] = False
        await session_store.update(
            session_id,
            phase=plan.phase,
            title=_generate_title(plan),
        )
        await archive_store.save_snapshot(
            session_id,
            plan.phase,
            json.dumps(plan.to_dict(), ensure_ascii=False),
        )
        return {"phase": plan.phase, "plan": plan.to_dict()}

    _LLM_ERROR_MESSAGES: dict[LLMErrorCode, str] = {
        LLMErrorCode.TRANSIENT: "模型服务暂时繁忙，本轮回复已中断。请稍后重试。",
        LLMErrorCode.RATE_LIMITED: "请求过于频繁，请稍后再试。",
        LLMErrorCode.BAD_REQUEST: "请求参数异常，请缩短对话长度后重试。",
        LLMErrorCode.STREAM_INTERRUPTED: "模型回复过程中连接中断。请重试。",
        LLMErrorCode.PROTOCOL_ERROR: "模型返回格式异常，请重试或切换模型。",
    }

    def _user_friendly_message(exc: LLMError) -> str:
        return _LLM_ERROR_MESSAGES.get(exc.code, "系统内部错误，请稍后重试。")

    async def _persist_phase7_deliverables(
        plan: TravelPlanState,
        result_data: dict,
    ) -> None:
        if plan.deliverables:
            raise RuntimeError("deliverables already frozen")

        travel_md = str(result_data["travel_plan_markdown"])
        checklist_md = str(result_data["checklist_markdown"])

        try:
            await state_mgr.save_deliverable(
                plan.session_id, "travel_plan.md", travel_md
            )
            await state_mgr.save_deliverable(
                plan.session_id, "checklist.md", checklist_md
            )
        except Exception:
            await state_mgr.clear_deliverables(plan.session_id)
            raise

        plan.deliverables = {
            "travel_plan_md": "travel_plan.md",
            "checklist_md": "checklist.md",
            "generated_at": _now_iso(),
        }

    async def _run_agent_stream(
        session,
        plan,
        messages,
        agent,
        run,
        cancel_event,
        phase_before_run,
        *,
        user_message: str | None = None,
    ):
        """Shared agent streaming logic for chat and continue endpoints.

        Parameters
        ----------
        user_message : str | None
            The original user message text. Used for fallback backtrack
            detection and ``_apply_message_fallbacks``. Pass ``None`` in the
            continue endpoint where no new user message exists — backtrack
            detection will be skipped.
        """
        from run import IterationProgress

        keepalive_queue: asyncio.Queue[str] = asyncio.Queue()

        async def _keepalive_loop():
            try:
                while True:
                    await asyncio.sleep(KEEPALIVE_INTERVAL_S)
                    await keepalive_queue.put(json.dumps({"type": "keepalive"}))
            except asyncio.CancelledError:
                pass

        tool_call_names: dict[str, str] = {}
        tool_call_args: dict[str, dict] = {}

        keepalive_task = asyncio.create_task(_keepalive_loop())
        try:
            accum_text = ""  # 追踪本轮 LLM 输出的文本，供中断恢复使用
            llm_started_at = time.monotonic()
            usage_iteration = 0
            try:
                async for chunk in agent.run(messages, phase=plan.phase):
                    if chunk.type.value == "keepalive":
                        yield {"comment": "ping"}
                        continue
                    if chunk.type == ChunkType.DONE:
                        continue
                    if chunk.type == ChunkType.USAGE and chunk.usage_info:
                        _record_llm_usage_stats(
                            stats=session.get("stats"),
                            provider=config.llm.provider,
                            model=config.llm.model,
                            usage_info=chunk.usage_info,
                            started_at=llm_started_at,
                            phase=plan.phase,
                            iteration=usage_iteration,
                        )
                        usage_iteration += 1
                        llm_started_at = time.monotonic()
                        continue
                    if chunk.type == ChunkType.CONTEXT_COMPRESSION:
                        yield json.dumps(
                            {
                                "type": "context_compression",
                                "compression_info": chunk.compression_info,
                            },
                            ensure_ascii=False,
                        )
                        continue
                    if (
                        chunk.type == ChunkType.PHASE_TRANSITION
                        and chunk.phase_info is not None
                    ):
                        yield json.dumps(
                            {"type": "phase_transition", **chunk.phase_info},
                            ensure_ascii=False,
                        )
                        continue
                    if (
                        chunk.type == ChunkType.AGENT_STATUS
                        and chunk.agent_status is not None
                    ):
                        yield json.dumps(
                            {"type": "agent_status", **chunk.agent_status},
                            ensure_ascii=False,
                        )
                        continue
                    if (
                        chunk.type == ChunkType.INTERNAL_TASK
                        and chunk.internal_task is not None
                    ):
                        yield json.dumps(
                            {
                                "type": "internal_task",
                                "task": chunk.internal_task.to_dict(),
                            },
                            ensure_ascii=False,
                        )
                        continue
                    event_type = (
                        "tool_call"
                        if chunk.tool_call and chunk.type.value == "tool_call_start"
                        else "tool_result"
                        if chunk.tool_result and chunk.type.value == "tool_result"
                        else chunk.type.value
                    )
                    event_data = {"type": event_type}
                    if chunk.content:
                        accum_text += chunk.content
                        event_data["content"] = chunk.content
                    if chunk.tool_call:
                        tool_call_names[chunk.tool_call.id] = chunk.tool_call.name
                        tool_call_args[chunk.tool_call.id] = (
                            chunk.tool_call.arguments or {}
                        )
                        event_data["tool_call"] = {
                            "id": chunk.tool_call.id,
                            "name": chunk.tool_call.name,
                            "arguments": chunk.tool_call.arguments,
                            "human_label": chunk.tool_call.human_label,
                        }
                    if chunk.tool_result:
                        event_data["tool_result"] = {
                            "tool_call_id": chunk.tool_result.tool_call_id,
                            "status": chunk.tool_result.status,
                            "data": chunk.tool_result.data,
                            "error": chunk.tool_result.error,
                            "error_code": chunk.tool_result.error_code,
                            "suggestion": chunk.tool_result.suggestion,
                        }
                        _record_tool_result_stats(
                            stats=session.get("stats"),
                            tool_call_names=tool_call_names,
                            tool_call_args=tool_call_args,
                            result=chunk.tool_result,
                            phase=plan.phase,
                        )
                        # Apply pending state_changes / validation_errors from on_validate hook
                        _stats = session.get("stats")
                        if _stats and _stats.tool_calls:
                            _pending_sc = session.pop("_pending_state_changes", None)
                            if _pending_sc is not None:
                                _stats.tool_calls[-1].state_changes = _pending_sc
                            _pending_ve = session.pop(
                                "_pending_validation_errors", None
                            )
                            if _pending_ve is not None:
                                _stats.tool_calls[-1].validation_errors = _pending_ve
                            _pending_js = session.pop("_pending_judge_scores", None)
                            if _pending_js is not None:
                                _stats.tool_calls[-1].judge_scores = _pending_js
                    while not keepalive_queue.empty():
                        yield keepalive_queue.get_nowait()
                    yield json.dumps(event_data, ensure_ascii=False)
                    tool_name = (
                        tool_call_names.get(chunk.tool_result.tool_call_id)
                        if chunk.tool_result
                        else None
                    )
                    if (
                        chunk.tool_result
                        and chunk.tool_result.status == "success"
                        and (
                            tool_name in PLAN_WRITER_TOOL_NAMES
                            or tool_name == "generate_summary"
                        )
                    ):
                        result_data = (
                            chunk.tool_result.data
                            if isinstance(chunk.tool_result.data, dict)
                            else {}
                        )
                        updated_fields = _plan_writer_updated_fields(result_data)
                        if tool_name == "generate_summary":
                            await _persist_phase7_deliverables(plan, result_data)
                        elif result_data.get("backtracked"):
                            await state_mgr.clear_deliverables(plan.session_id)
                            await _rotate_trip_on_reset_backtrack(
                                user_id=session["user_id"],
                                plan=plan,
                                to_phase=int(result_data.get("to_phase", plan.phase)),
                                reason_text=str(result_data.get("reason", "")),
                            )
                        elif "selected_skeleton_id" in updated_fields:
                            _schedule_memory_event(
                                user_id=session["user_id"],
                                session_id=plan.session_id,
                                event_type="accept",
                                object_type="skeleton",
                                object_payload=chunk.tool_result.data or {},
                            )
                        elif "selected_transport" in updated_fields:
                            _schedule_memory_event(
                                user_id=session["user_id"],
                                session_id=plan.session_id,
                                event_type="accept",
                                object_type="transport",
                                object_payload=chunk.tool_result.data or {},
                            )
                        elif "accommodation" in updated_fields:
                            _schedule_memory_event(
                                user_id=session["user_id"],
                                session_id=plan.session_id,
                                event_type="accept",
                                object_type="hotel",
                                object_payload=chunk.tool_result.data or {},
                            )
                        # 增量持久化：工具写入成功后立即保存，防止 SSE 中断丢失状态
                        await state_mgr.save(plan)
                        # 同步更新 session meta，确保 plan 文件与数据库一致
                        try:
                            await session_store.update(
                                plan.session_id,
                                phase=plan.phase,
                                title=_generate_title(plan),
                            )
                        except Exception:
                            logger.warning(
                                "增量 session meta 更新失败 session=%s",
                                plan.session_id,
                                exc_info=True,
                            )
                        yield json.dumps(
                            {"type": "state_update", "plan": plan.to_dict()},
                            ensure_ascii=False,
                        )
                        _pending_step = session.pop(
                            "_pending_phase_step_transition", None
                        )
                        if _pending_step is not None:
                            yield json.dumps(
                                {"type": "phase_transition", **_pending_step},
                                ensure_ascii=False,
                            )
            except LLMError as exc:
                if exc.failure_phase == "cancelled":
                    run.status = "cancelled"
                    run.finished_at = time.time()
                    yield json.dumps(
                        {
                            "type": "done",
                            "run_id": run.run_id,
                            "run_status": "cancelled",
                        },
                        ensure_ascii=False,
                    )
                else:
                    run.status = "failed"
                    run.error_code = exc.code.value
                    run.finished_at = time.time()
                    logger.exception(
                        "LLM error for session %s: %s",
                        plan.session_id,
                        exc.code.value,
                    )

                    progress = agent.progress
                    can_continue = progress in (
                        IterationProgress.PARTIAL_TEXT,
                        IterationProgress.TOOLS_READ_ONLY,
                    )

                    if can_continue and accum_text.strip():
                        # 把不完整的 assistant 消息追加到历史
                        messages.append(
                            Message(
                                role=Role.ASSISTANT,
                                content=accum_text,
                                incomplete=True,
                            )
                        )
                        run.continuation_context = {
                            "type": progress.value,
                            "partial_assistant_text": accum_text,
                        }
                        if progress == IterationProgress.TOOLS_READ_ONLY:
                            run.continuation_context["completed_tool_count"] = sum(
                                1 for m in messages if m.role == Role.TOOL
                            )

                    run.can_continue = can_continue

                    yield json.dumps(
                        {
                            "type": "error",
                            "error_code": exc.code.value,
                            "retryable": exc.retryable,
                            "can_continue": can_continue,
                            "provider": exc.provider,
                            "model": exc.model,
                            "failure_phase": exc.failure_phase,
                            "message": _user_friendly_message(exc),
                            "error": exc.raw_error,
                        },
                        ensure_ascii=False,
                    )
            except Exception as exc:
                run.status = "failed"
                run.error_code = "AGENT_STREAM_ERROR"
                run.finished_at = time.time()
                logger.exception("Agent stream failed for session %s", plan.session_id)
                yield json.dumps(
                    {
                        "type": "error",
                        "error_code": "AGENT_STREAM_ERROR",
                        "retryable": False,
                        "can_continue": False,
                        "message": "系统内部错误，请稍后重试。",
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                )

            # Fallback：如果本轮 agent 没触发 backtrack，检查关键词 fallback
            # 仅在有用户消息时进行（continue 场景无新用户消息，跳过）
            if user_message is not None and plan.phase == phase_before_run:
                backtrack_target = _detect_backtrack(user_message, plan)
                if backtrack_target is not None:
                    reason = f"fallback回退：{user_message[:50]}"
                    tool_call_id = f"fallback.request_backtrack:{plan.version}"
                    yield json.dumps(
                        {
                            "type": "tool_call",
                            "tool_call": {
                                "id": tool_call_id,
                                "name": "request_backtrack",
                                "arguments": {
                                    "to_phase": backtrack_target,
                                    "reason": reason,
                                },
                                "human_label": "回退到之前阶段",
                            },
                        },
                        ensure_ascii=False,
                    )
                    snapshot_path = await state_mgr.save_snapshot(plan)
                    from_phase = plan.phase
                    phase_router.prepare_backtrack(
                        plan,
                        backtrack_target,
                        reason,
                        snapshot_path,
                    )
                    await state_mgr.clear_deliverables(plan.session_id)
                    await _rotate_trip_on_reset_backtrack(
                        user_id=session["user_id"],
                        plan=plan,
                        to_phase=backtrack_target,
                        reason_text=user_message,
                    )
                    session["needs_rebuild"] = True
                    yield json.dumps(
                        {
                            "type": "tool_result",
                            "tool_result": {
                                "tool_call_id": tool_call_id,
                                "status": "success",
                                "data": {
                                    "backtracked": True,
                                    "from_phase": from_phase,
                                    "to_phase": backtrack_target,
                                    "reason": reason,
                                    "next_action": "请向用户确认回退结果，不要继续调用其他工具",
                                },
                                "error": None,
                                "error_code": None,
                                "suggestion": None,
                            },
                        },
                        ensure_ascii=False,
                    )
                    _schedule_memory_event(
                        user_id=session["user_id"],
                        session_id=plan.session_id,
                        event_type="reject",
                        object_type="phase_output",
                        object_payload={
                            "from_phase": from_phase,
                            "to_phase": backtrack_target,
                            "reason": reason,
                        },
                        reason_text=reason,
                    )

            if user_message is not None and plan.phase < phase_before_run:
                await _apply_message_fallbacks(plan, user_message, phase_router)

            if run.status == "running":
                run.status = "completed"
                run.finished_at = time.time()

            await state_mgr.save(plan)
            await _persist_messages(plan.session_id, messages)
            await session_store.update(
                plan.session_id,
                phase=plan.phase,
                title=_generate_title(plan),
                last_run_id=run.run_id,
                last_run_status=run.status,
                last_run_error=run.error_code,
            )
            if plan.phase != phase_before_run:
                await archive_store.save_snapshot(
                    plan.session_id,
                    plan.phase,
                    json.dumps(plan.to_dict(), ensure_ascii=False),
                )
            if plan.phase == 7:
                await archive_store.save(
                    plan.session_id,
                    json.dumps(plan.to_dict(), ensure_ascii=False),
                    summary=_generate_title(plan),
                )
                await session_store.update(plan.session_id, status="archived")
                if config.memory.enabled:
                    try:
                        await _append_archived_trip_episode_once(
                            user_id=session["user_id"],
                            session_id=plan.session_id,
                            plan=plan,
                        )
                    except Exception:
                        pass
            yield json.dumps(
                {"type": "state_update", "plan": plan.to_dict()},
                ensure_ascii=False,
            )
            if run.status == "completed":
                yield json.dumps(
                    {
                        "type": "done",
                        "run_id": run.run_id,
                        "run_status": run.status,
                    },
                    ensure_ascii=False,
                )
            elif run.status == "cancelled":
                yield json.dumps(
                    {
                        "type": "done",
                        "run_id": run.run_id,
                        "run_status": run.status,
                    },
                    ensure_ascii=False,
                )

        finally:
            # 保底持久化：即使流异常中断，也尝试保存当前状态
            try:
                if run.status == "running":
                    run.status = "cancelled"
                    run.finished_at = time.time()
                await state_mgr.save(plan)
                await _persist_messages(plan.session_id, messages)
                await session_store.update(
                    plan.session_id,
                    phase=plan.phase,
                    title=_generate_title(plan),
                    last_run_id=run.run_id,
                    last_run_status=run.status,
                    last_run_error=run.error_code,
                )
            except Exception:
                logger.warning(
                    "保底持久化失败 session=%s",
                    plan.session_id,
                    exc_info=True,
                )
            keepalive_task.cancel()
            session.pop("_cancel_event", None)
            # 当 run 可以继续时，保留 _current_run 以供 continue endpoint 使用
            if not run.can_continue:
                session.pop("_current_run", None)

    @app.post("/api/chat/{session_id}")
    async def chat(session_id: str, req: ChatRequest):
        await _ensure_storage_ready()
        session = sessions.get(session_id)
        if not session:
            restored = await _restore_session(session_id)
            if restored is None:
                raise HTTPException(status_code=404, detail="Session not found")
            sessions[session_id] = restored
            session = restored

        plan = session["plan"]
        messages = session["messages"]
        session["user_id"] = req.user_id

        # 检查是否需要重建 agent（上一轮回退导致）
        if session.get("needs_rebuild"):
            session["agent"] = _build_agent(
                plan,
                session["user_id"],
                compression_events=session.get("compression_events"),
            )
            session["needs_rebuild"] = False

        agent = session["agent"]
        agent.user_id = session["user_id"]

        # Build system message
        phase_router.sync_phase_state(plan)
        phase_prompt = phase_router.get_prompt_for_plan(plan)
        available_tools = [
            tool["name"]
            for tool in agent.tool_engine.get_tools_for_phase(plan.phase, plan)
        ]
        # 记录 agent.run 之前的 phase，用于判断是否发生了回退
        phase_before_run = plan.phase

        async def event_stream():
            for task in session.pop("_background_internal_tasks", []):
                if getattr(task, "kind", None) == "memory_extraction":
                    continue
                yield json.dumps(
                    {"type": "internal_task", "task": task.to_dict()},
                    ensure_ascii=False,
                )

            messages.append(Message(role=Role.USER, content=req.message))
            _submit_memory_snapshot(
                _build_memory_job_snapshot(
                    session_id=plan.session_id,
                    user_id=session["user_id"],
                    messages=messages,
                    plan=plan,
                )
            )

            if config.memory.enabled:
                memory_recall_task_id = (
                    f"memory_recall:{plan.session_id}:{int(time.time())}"
                )
                memory_recall_started_at = time.time()
                yield json.dumps(
                    {
                        "type": "internal_task",
                        "task": InternalTask(
                            id=memory_recall_task_id,
                            kind="memory_recall",
                            label="记忆召回",
                            status="pending",
                            message="正在检索本轮可用旅行记忆…",
                            blocking=True,
                            scope="turn",
                            started_at=memory_recall_started_at,
                        ).to_dict(),
                    },
                    ensure_ascii=False,
                )

                recall_decision = await _decide_memory_recall(
                    session_id=plan.session_id,
                    user_id=req.user_id,
                    user_messages=[
                        message.content
                        for message in messages
                        if message.role == Role.USER and message.content
                    ],
                    plan_snapshot=plan,
                )
                retrieval_plan = None
                query_plan_source = ""
                query_plan_fallback = "none"
                if recall_decision.needs_recall:
                    recall_user_messages = [
                        message.content
                        for message in messages
                        if message.role == Role.USER and message.content
                    ]
                    recall_memory_summary = recall_decision.memory_summary
                    if not recall_memory_summary:
                        recall_memory_summary = await _build_gate_memory_summary(
                            user_id=req.user_id,
                            session_id=plan.session_id,
                            plan_snapshot=plan,
                        )
                    if (
                        recall_decision.fallback_used
                        in _GATE_HEURISTIC_RECALL_FALLBACKS
                    ):
                        query_plan_source = "heuristic_fallback"
                        query_plan_fallback = recall_decision.fallback_used
                    else:
                        query_plan_result = await _build_recall_retrieval_plan(
                            session_id=plan.session_id,
                            user_id=req.user_id,
                            user_message=req.message,
                            user_messages=recall_user_messages,
                            gate_intent_type=recall_decision.intent_type,
                            gate_reason=recall_decision.reason,
                            gate_confidence=recall_decision.confidence,
                            stage0_decision=recall_decision.stage0_decision,
                            stage0_signals=recall_decision.stage0_signals,
                            plan_snapshot=plan,
                            memory_summary=recall_memory_summary,
                        )
                        retrieval_plan = query_plan_result.plan
                        query_plan_source = query_plan_result.query_plan_source
                        query_plan_fallback = query_plan_result.query_plan_fallback
                memory_result = await memory_mgr.generate_context(
                    req.user_id,
                    plan,
                    user_message=req.message,
                    recall_gate=recall_decision.needs_recall,
                    short_circuit=recall_decision.stage0_decision,
                    retrieval_plan=retrieval_plan,
                    stage0_matched_rule=recall_decision.stage0_matched_rule,
                    stage0_signals=recall_decision.stage0_signals,
                    query_plan_source=query_plan_source,
                    query_plan_fallback=query_plan_fallback,
                )
                if (
                    isinstance(memory_result, tuple)
                    and len(memory_result) == 2
                    and isinstance(memory_result[1], MemoryRecallTelemetry)
                ):
                    memory_context, memory_recall = memory_result
                else:
                    memory_context = memory_result[0]
                    memory_recall = MemoryRecallTelemetry()
                memory_recall.stage0_decision = recall_decision.stage0_decision
                memory_recall.stage0_reason = recall_decision.stage0_reason
                memory_recall.stage0_matched_rule = recall_decision.stage0_matched_rule
                memory_recall.stage0_signals = dict(recall_decision.stage0_signals)
                memory_recall.gate_needs_recall = recall_decision.needs_recall
                memory_recall.gate_intent_type = recall_decision.intent_type
                memory_recall.gate_confidence = recall_decision.confidence
                memory_recall.gate_reason = recall_decision.reason
                memory_recall.fallback_used = recall_decision.fallback_used
                memory_recall.recall_skip_source = recall_decision.recall_skip_source
                if query_plan_source and not memory_recall.query_plan_source:
                    memory_recall.query_plan_source = query_plan_source
                if query_plan_fallback != "none":
                    memory_recall.query_plan_fallback = query_plan_fallback
                    memory_recall.fallback_used = query_plan_fallback
                if retrieval_plan is not None:
                    if not memory_recall.query_plan:
                        memory_recall.query_plan = _query_plan_summary(retrieval_plan)
                    if memory_recall.query_plan_fallback == "none":
                        memory_recall.query_plan_fallback = retrieval_plan.fallback_used
                    if (
                        retrieval_plan.fallback_used != "none"
                        and memory_recall.fallback_used == "none"
                    ):
                        memory_recall.fallback_used = retrieval_plan.fallback_used
                memory_recall.final_recall_decision = (
                    memory_recall.final_recall_decision
                    or _final_recall_decision_from_gate(recall_decision.needs_recall)
                )
            else:
                memory_context = "暂无相关用户记忆"
                memory_recall = None

            if memory_recall is not None:
                recalled_ids = list(
                    dict.fromkeys(
                        [
                            *memory_recall.profile_ids,
                            *memory_recall.working_memory_ids,
                            *memory_recall.slice_ids,
                        ]
                    )
                )

                yield json.dumps(
                    {
                        "type": "internal_task",
                        "task": InternalTask(
                            id=memory_recall_task_id,
                            kind="memory_recall",
                            label="记忆召回",
                            status="success" if recalled_ids else "skipped",
                            message=(
                                f"本轮使用 {len(recalled_ids)} 条旅行记忆"
                                if recalled_ids
                                else "未找到本轮可用记忆"
                            ),
                            blocking=True,
                            scope="turn",
                            result={
                                "item_ids": recalled_ids,
                                "count": len(recalled_ids),
                                "sources": dict(memory_recall.sources),
                                "gate": memory_recall.gate_needs_recall,
                                "stage0_matched_rule": memory_recall.stage0_matched_rule,
                                "query_plan_source": memory_recall.query_plan_source,
                                "candidate_count": memory_recall.candidate_count,
                                "recall_attempted_but_zero_hit": (
                                    memory_recall.recall_attempted_but_zero_hit
                                ),
                                "reranker_selected_ids": list(
                                    memory_recall.reranker_selected_ids
                                ),
                                "reranker_final_reason": memory_recall.reranker_final_reason,
                                "reranker_fallback": memory_recall.reranker_fallback,
                                "reranker_per_item_reason": dict(
                                    memory_recall.reranker_per_item_reason
                                ),
                            },
                            started_at=memory_recall_started_at,
                            ended_at=time.time(),
                        ).to_dict(),
                    },
                    ensure_ascii=False,
                )

                session_stats = session.get("stats")
                if session_stats is not None:
                    session_stats.recall_telemetry.append(
                        _recall_telemetry_record_from_recall(memory_recall)
                    )
                    memory_hit_record = _memory_hit_record_from_recall(memory_recall)
                    if memory_hit_record is not None:
                        session_stats.memory_hits.append(memory_hit_record)

                yield json.dumps(
                    {
                        "type": "memory_recall",
                        "gate": memory_recall.gate_needs_recall,
                        **memory_recall.to_dict(),
                    },
                    ensure_ascii=False,
                )

            sys_msg = context_mgr.build_system_message(
                plan,
                phase_prompt,
                memory_context,
                available_tools=available_tools,
            )

            # Prepend system message
            if messages and messages[0].role == Role.SYSTEM:
                messages[0] = sys_msg
            else:
                messages.insert(0, sys_msg)

            from run import RunRecord

            run = RunRecord(
                run_id=str(uuid.uuid4()), session_id=plan.session_id, status="running"
            )
            session["_current_run"] = run
            cancel_event = asyncio.Event()
            session["_cancel_event"] = cancel_event
            agent.cancel_event = cancel_event

            async for event in _run_agent_stream(
                session,
                plan,
                messages,
                agent,
                run,
                cancel_event,
                phase_before_run,
                user_message=req.message,
            ):
                yield event

        return EventSourceResponse(event_stream())

    @app.get("/api/internal-tasks/{session_id}/stream")
    async def stream_internal_tasks(session_id: str, request: Request):
        await _ensure_storage_ready()
        session = sessions.get(session_id)
        if not session:
            restored = await _restore_session(session_id)
            if restored is None:
                raise HTTPException(status_code=404, detail="Session not found")
            sessions[session_id] = restored

        logger.warning(
            "后台记忆任务 SSE 请求 session=%s restored=%s active_tasks=%s subscribers=%s",
            session_id,
            session is None,
            len(memory_active_tasks.get(session_id, {})),
            len(memory_task_subscribers.get(session_id, set())),
        )
        return EventSourceResponse(_memory_task_stream(session_id, request))

    @app.get("/api/internal-tasks/{session_id}")
    async def list_internal_tasks(session_id: str):
        await _ensure_storage_ready()
        session = sessions.get(session_id)
        if not session:
            restored = await _restore_session(session_id)
            if restored is None:
                raise HTTPException(status_code=404, detail="Session not found")
            sessions[session_id] = restored

        tasks = sorted(
            memory_active_tasks.get(session_id, {}).values(),
            key=lambda task: task.started_at or task.ended_at or 0,
        )
        logger.warning(
            "后台记忆任务快照请求 session=%s tasks=%s kinds=%s",
            session_id,
            len(tasks),
            [task.kind for task in tasks],
        )
        return {"tasks": [task.to_dict() for task in tasks]}

    @app.post("/api/chat/{session_id}/cancel")
    async def cancel_chat(session_id: str):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        cancel_event = session.get("_cancel_event")
        if cancel_event:
            cancel_event.set()
        return {"status": "cancelled"}

    @app.post("/api/chat/{session_id}/continue")
    async def continue_chat(session_id: str):
        await _ensure_storage_ready()
        session = sessions.get(session_id)
        if not session:
            restored = await _restore_session(session_id)
            if restored is None:
                raise HTTPException(status_code=404, detail="Session not found")
            sessions[session_id] = restored
            session = restored

        last_run = session.get("_current_run")
        if not last_run or not last_run.can_continue:
            raise HTTPException(status_code=400, detail="Cannot continue this run")

        plan = session["plan"]
        messages = session["messages"]
        agent = session["agent"]
        ctx = last_run.continuation_context or {}
        ctx_type = ctx.get("type", "")

        if ctx_type == "partial_text":
            messages.append(
                Message(
                    role=Role.SYSTEM,
                    content="你的上一轮回复因网络中断未完成，请从断点继续，不要重复已说的内容。",
                )
            )
        elif ctx_type == "tools_read_only":
            messages.append(
                Message(
                    role=Role.SYSTEM,
                    content="你已经调用了工具并获得结果，但总结被中断了。请根据已有的工具结果继续回复。",
                )
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown continuation type: {ctx_type}",
            )

        from run import RunRecord

        run = RunRecord(
            run_id=str(uuid.uuid4()),
            session_id=plan.session_id,
            status="running",
        )
        session["_current_run"] = run
        cancel_event = asyncio.Event()
        session["_cancel_event"] = cancel_event
        agent.cancel_event = cancel_event

        phase_before_run = plan.phase

        async def event_stream():
            async for event in _run_agent_stream(
                session,
                plan,
                messages,
                agent,
                run,
                cancel_event,
                phase_before_run,
            ):
                yield event

        return EventSourceResponse(event_stream())

    from api.trace import build_trace

    @app.get("/api/sessions/{session_id}/trace")
    async def get_session_trace(session_id: str):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        agent = session.get("agent")
        engine = getattr(agent, "tool_engine", None) if agent else None
        return build_trace(session_id, session, tool_engine=engine)

    @app.get("/api/sessions/{session_id}/deliverables/{filename}")
    async def download_deliverable(session_id: str, filename: str):
        await _ensure_storage_ready()
        meta = await session_store.load(session_id)
        if meta is None or meta["status"] == "deleted":
            raise HTTPException(status_code=404, detail="Session not found")

        try:
            content = await state_mgr.read_deliverable(session_id, filename)
        except ValueError:
            raise HTTPException(status_code=404, detail="Deliverable not found")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Deliverable not found")

        return Response(
            content=content,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    app.state.memory_scheduler_runtimes = memory_scheduler_runtimes
    app.state.memory_active_tasks = memory_active_tasks
    app.state.run_memory_job = _run_memory_job
    app.state.extract_memory_candidates = _extract_memory_candidates

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
