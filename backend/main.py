# backend/main.py
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from agent.compaction import (
    compact_messages_for_prompt,
    compute_prompt_budget,
    estimate_messages_tokens,
)
from agent.hooks import GateResult, HookManager
from agent.loop import AgentLoop
from agent.reflection import ReflectionInjector
from agent.tool_choice import ToolChoiceDecider
from agent.types import Message, Role, ToolCall, ToolResult
from config import load_config
from telemetry import setup_telemetry
from telemetry.stats import SessionStats
from context.manager import ContextManager
from harness.guardrail import ToolGuardrail
from harness.judge import build_judge_prompt, parse_judge_response
from harness.validator import (
    validate_hard_constraints,
    validate_incremental,
    validate_lock_budget,
)
from llm.errors import LLMError, LLMErrorCode
from llm.factory import create_llm_provider
from llm.types import ChunkType
from memory.extraction import (
    build_candidate_extraction_prompt,
    parse_candidate_extraction_response,
)
from memory.manager import MemoryManager
from memory.models import MemoryCandidate, MemoryEvent, MemoryItem, TripEpisode
from memory.policy import MemoryMerger as PolicyMemoryMerger, MemoryPolicy
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
from tools.calculate_route import make_calculate_route_tool
from tools.check_availability import make_check_availability_tool
from tools.check_weather import make_check_weather_tool
from tools.generate_summary import make_generate_summary_tool
from tools.get_poi_info import make_get_poi_info_tool
from tools.search_accommodations import make_search_accommodations_tool
from tools.search_flights import make_search_flights_tool
from tools.search_trains import make_search_trains_tool
from tools.ai_travel_search import make_ai_travel_search_tool
from tools.update_plan_state import make_update_plan_state_tool
from tools.quick_travel_search import make_quick_travel_search_tool
from tools.search_travel_services import make_search_travel_services_tool
from tools.web_search import make_web_search_tool
from tools.xiaohongshu_search import make_xiaohongshu_search_tool

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str
    user_id: str = "default_user"


class BacktrackRequest(BaseModel):
    to_phase: int
    reason: str = ""


class MemoryItemRequest(BaseModel):
    item_id: str


class MemoryEventRequest(BaseModel):
    event_type: str
    object_type: str
    object_payload: dict[str, Any]
    reason_text: str | None = None


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


def _memory_summary(candidate: MemoryCandidate) -> str:
    value = candidate.value
    if isinstance(value, (dict, list)):
        value_text = json.dumps(value, ensure_ascii=False)
    else:
        value_text = "" if value is None else str(value)
    return f"[{candidate.domain}] {candidate.key}: {value_text}"


def _memory_pending_event(
    candidates: list[MemoryCandidate],
    item_ids: list[str],
) -> str:
    items: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        item_id = item_ids[index] if index < len(item_ids) else None
        items.append(
            {
                "id": item_id,
                "status": "pending",
                "summary": _memory_summary(candidate),
                "candidate": candidate.to_dict(),
            }
        )
    return json.dumps(
        {
            "type": "memory_pending",
            "item_ids": item_ids,
            "items": items,
        },
        ensure_ascii=False,
    )


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
    return (end_date - start_date).days


def _truncate_preview(value: Any, max_len: int = 120) -> str:
    """Truncate a value to a short preview string."""
    if value is None:
        return ""
    text = str(value) if not isinstance(value, str) else value
    if len(text) > max_len:
        return text[:max_len] + "…"
    return text


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


def _memory_pending_event_from_items(items: list[MemoryItem]) -> str:
    candidates: list[MemoryCandidate] = []
    item_ids: list[str] = []
    for item in items:
        item_ids.append(item.id)
        candidates.append(
            MemoryCandidate(
                type=item.type,
                domain=item.domain,
                key=item.key,
                value=item.value,
                scope=item.scope,
                polarity=item.polarity,
                confidence=item.confidence,
                risk=item.status,
                evidence=item.source.quote or "",
                reason=str(item.attributes.get("reason", "")),
                attributes=dict(item.attributes),
            )
        )
    payload = json.loads(_memory_pending_event(candidates, item_ids))
    for item_payload, item in zip(payload["items"], items, strict=False):
        item_payload["status"] = item.status
        item_payload["item"] = item.to_dict()
    return json.dumps(payload, ensure_ascii=False)


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
    memory_extraction_tasks: dict[str, asyncio.Task] = {}
    memory_extraction_pending: dict[
        str, tuple[str, list[Message], TravelPlanState]
    ] = {}
    memory_pending_seen: dict[tuple[str, str], set[str]] = {}
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.initialize()
        await _probe_context_window()
        yield
        for task in list(memory_extraction_tasks.values()):
            task.cancel()
        memory_extraction_pending.clear()
        await db.close()

    app = FastAPI(title="Travel Agent Pro", lifespan=lifespan)
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

        tool_engine.register(make_update_plan_state_tool(plan))
        tool_engine.register(make_search_flights_tool(config.api_keys, flyai_client))
        tool_engine.register(make_search_trains_tool(flyai_client))
        tool_engine.register(make_ai_travel_search_tool(flyai_client))
        tool_engine.register(
            make_search_accommodations_tool(config.api_keys, flyai_client)
        )
        tool_engine.register(make_get_poi_info_tool(config.api_keys, flyai_client))
        tool_engine.register(make_calculate_route_tool(config.api_keys))
        tool_engine.register(make_assemble_day_plan_tool())
        tool_engine.register(make_check_availability_tool(config.api_keys))
        tool_engine.register(make_check_weather_tool(config.api_keys))
        tool_engine.register(make_generate_summary_tool())
        tool_engine.register(make_quick_travel_search_tool(flyai_client))
        tool_engine.register(make_search_travel_services_tool(flyai_client))
        tool_engine.register(make_web_search_tool(config.api_keys))
        tool_engine.register(make_xiaohongshu_search_tool(config.xhs))

        hooks = HookManager()

        async def on_tool_call(**kwargs):
            if kwargs.get("tool_name") == "update_plan_state":
                result = kwargs.get("result")
                if result and result.data and result.data.get("backtracked"):
                    session = sessions.get(plan.session_id)
                    if session:
                        session["needs_rebuild"] = True
                return

        async def on_validate(**kwargs):
            if kwargs.get("tool_name") == "update_plan_state":
                tc = kwargs.get("tool_call")
                result = kwargs.get("result")
                arguments = tc.arguments if tc and tc.arguments else {}
                field = arguments.get("field", "")
                value = arguments.get("value")

                # Capture state_changes from previous_value in tool result
                session = sessions.get(plan.session_id)
                if (
                    result
                    and result.status == "success"
                    and isinstance(result.data, dict)
                    and session
                ):
                    prev_val = result.data.get("previous_value")
                    session["_pending_state_changes"] = [
                        {"field": field, "before": prev_val, "after": value}
                    ]
                    if result.data.get("updated_field") == "phase3_step":
                        session["_pending_phase_step_transition"] = {
                            "from_phase": plan.phase,
                            "to_phase": plan.phase,
                            "from_step": result.data.get("previous_value"),
                            "to_step": result.data.get("new_value"),
                            "reason": "phase3_step_change",
                        }

                errors = validate_incremental(plan, field, value)
                if field in ("selected_transport", "accommodation"):
                    errors.extend(validate_lock_budget(plan))

                if errors:
                    if session:
                        session["_pending_validation_errors"] = errors
                        session["messages"].append(
                            Message(
                                role=Role.SYSTEM,
                                content="[实时约束检查]\n"
                                + "\n".join(f"- {error}" for error in errors),
                            )
                        )

        async def on_before_llm(**kwargs):
            msgs = kwargs.get("messages")
            tools = kwargs.get("tools") or []
            phase = kwargs.get("phase", plan.phase)
            if not msgs:
                return
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
            if tool_name not in ("assemble_day_plan", "generate_summary"):
                return
            if not plan.daily_plans:
                return
            session = sessions.get(plan.session_id)
            if not session:
                return
            prefs = {p.key: p.value for p in plan.preferences}
            prompt_text = build_judge_prompt(plan.to_dict(), prefs)
            judge_llm = create_llm_provider(config.llm)
            judge_msgs = [
                Message(role=Role.SYSTEM, content="你是旅行行程质量评估专家。"),
                Message(role=Role.USER, content=prompt_text),
            ]
            result_parts: list[str] = []
            async for chunk in judge_llm.chat(judge_msgs, tools=[], stream=True):
                if chunk.content:
                    result_parts.append(chunk.content)
            score = parse_judge_response("".join(result_parts))
            # Stage judge scores for the TOOL_RESULT handler to attach to ToolCallRecord
            session["_pending_judge_scores"] = {
                "overall": score.overall,
                "pace": score.pace,
                "geography": score.geography,
                "coherence": score.coherence,
                "personalization": score.personalization,
                "suggestions_count": len(score.suggestions),
            }
            if score.suggestions:
                suggestion_text = "\n".join(f"- {s}" for s in score.suggestions)
                session["messages"].append(
                    Message(
                        role=Role.SYSTEM,
                        content=f"💡 行程质量评估（{score.overall:.1f}/5）：\n{suggestion_text}",
                    )
                )

        hooks.register("after_tool_call", on_tool_call)
        hooks.register("after_tool_call", on_validate)
        hooks.register("after_tool_call", on_soft_judge)

        async def on_before_phase_transition(**kwargs):
            target_plan = kwargs.get("plan", plan)
            from_phase = int(kwargs.get("from_phase", target_plan.phase))
            to_phase = int(kwargs.get("to_phase", from_phase))
            session = sessions.get(target_plan.session_id)

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
                return GateResult(allowed=False, feedback=feedback)

            if (from_phase, to_phase) not in {(3, 5), (5, 7)}:
                return GateResult(allowed=True)

            try:
                prefs = {p.key: p.value for p in target_plan.preferences}
                prompt_text = build_judge_prompt(target_plan.to_dict(), prefs)
                judge_llm = create_llm_provider(config.llm)
                judge_msgs = [
                    Message(role=Role.SYSTEM, content="你是旅行行程质量评估专家。"),
                    Message(role=Role.USER, content=prompt_text),
                ]
                result_parts: list[str] = []
                async for chunk in judge_llm.chat(judge_msgs, tools=[], stream=True):
                    if chunk.content:
                        result_parts.append(chunk.content)
                score = parse_judge_response("".join(result_parts))
            except Exception:
                return GateResult(allowed=True)
            if score.overall >= config.quality_gate.threshold:
                quality_gate_retries.pop(
                    (target_plan.session_id, from_phase, to_phase),
                    None,
                )
                return GateResult(allowed=True)

            retry_key = (target_plan.session_id, from_phase, to_phase)
            retry_count = quality_gate_retries.get(retry_key, 0)
            if retry_count >= config.quality_gate.max_retries:
                quality_gate_retries.pop(retry_key, None)
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

    async def _append_memory_event_nonfatal(event: MemoryEvent) -> None:
        if not config.memory.enabled:
            return
        try:
            await memory_mgr.store.append_event(event)
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
        event = MemoryEvent(
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

    _EXTRACTION_TIMEOUT_SECONDS = 20.0

    async def _extract_memory_candidates(
        *,
        session_id: str,
        user_id: str,
        messages_snapshot: list[Message],
        plan_snapshot: TravelPlanState,
    ) -> list[str]:
        if not config.memory.enabled or not config.memory.extraction.enabled:
            return []
        if config.memory.extraction.trigger != "each_turn":
            return []

        user_messages = [
            message.content
            for message in messages_snapshot
            if message.role == Role.USER and message.content
        ]
        if not user_messages:
            return []

        user_messages = user_messages[-config.memory.extraction.max_user_messages :]
        try:
            return await asyncio.wait_for(
                _do_extract_memory_candidates(
                    session_id=session_id,
                    user_id=user_id,
                    user_messages=user_messages,
                    plan_snapshot=plan_snapshot,
                ),
                timeout=_EXTRACTION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return []
        except Exception:
            return []

    async def _do_extract_memory_candidates(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
    ) -> list[str]:
        existing_items = await memory_mgr.store.list_items(user_id)
        prompt = build_candidate_extraction_prompt(
            user_messages=user_messages,
            existing_items=existing_items,
            plan_facts=_memory_plan_facts(plan_snapshot),
        )
        extraction_llm = create_llm_provider(
            replace(config.llm, model=config.memory.extraction.model)
        )
        response_parts: list[str] = []
        async for chunk in extraction_llm.chat(
            [Message(role=Role.USER, content=prompt)],
            tools=[],
            stream=True,
        ):
            if chunk.content:
                response_parts.append(chunk.content)

        candidates = parse_candidate_extraction_response("".join(response_parts))
        if not candidates:
            return []

        policy = MemoryPolicy(
            auto_save_low_risk=config.memory.policy.auto_save_low_risk,
            auto_save_medium_risk=config.memory.policy.auto_save_medium_risk,
        )
        merger = PolicyMemoryMerger()
        now = _now_iso()
        merged_items = existing_items
        for candidate in candidates:
            action = policy.classify(candidate)
            if action == "drop":
                continue
            item = policy.to_item(
                candidate,
                user_id=user_id,
                session_id=session_id,
                now=now,
                trip_id=plan_snapshot.trip_id,
            )
            merged_items = merger.merge(merged_items, item)

        for item in merged_items:
            await memory_mgr.store.upsert_item(item)

        pending_ids = [
            item.id
            for item in merged_items
            if item.status in {"pending", "pending_conflict"}
        ]
        return pending_ids

    def _cancel_memory_extraction(session_id: str) -> None:
        task = memory_extraction_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
        memory_extraction_pending.pop(session_id, None)

    def _start_memory_extraction(
        *,
        session_id: str,
        user_id: str,
        messages_snapshot: list[Message],
        plan_snapshot: TravelPlanState,
    ) -> None:
        task = asyncio.create_task(
            _extract_memory_candidates(
                session_id=session_id,
                user_id=user_id,
                messages_snapshot=messages_snapshot,
                plan_snapshot=plan_snapshot,
            )
        )
        memory_extraction_tasks[session_id] = task

        def _cleanup(done_task: asyncio.Task) -> None:
            if memory_extraction_tasks.get(session_id) is done_task:
                memory_extraction_tasks.pop(session_id, None)
            queued = memory_extraction_pending.pop(session_id, None)
            if queued is not None and not done_task.cancelled():
                next_user_id, next_messages, next_plan = queued
                _start_memory_extraction(
                    session_id=session_id,
                    user_id=next_user_id,
                    messages_snapshot=next_messages,
                    plan_snapshot=next_plan,
                )

        task.add_done_callback(_cleanup)

    def _schedule_memory_extraction(
        *,
        session_id: str,
        user_id: str,
        messages_snapshot: list[Message],
        plan_snapshot: TravelPlanState,
    ) -> None:
        if not config.memory.enabled or not config.memory.extraction.enabled:
            return
        if config.memory.extraction.trigger != "each_turn":
            return
        existing = memory_extraction_tasks.get(session_id)
        if existing is not None and not existing.done():
            memory_extraction_pending[session_id] = (
                user_id,
                messages_snapshot,
                plan_snapshot,
            )
            return
        _start_memory_extraction(
            session_id=session_id,
            user_id=user_id,
            messages_snapshot=messages_snapshot,
            plan_snapshot=plan_snapshot,
        )

    async def _build_trip_episode(
        *,
        user_id: str,
        session_id: str,
        plan: TravelPlanState,
    ) -> TripEpisode:
        items: list[MemoryItem] = []
        if config.memory.enabled:
            items = await memory_mgr.store.list_items(user_id)
        session_items = []
        for item in items:
            same_session = item.session_id == session_id
            same_trip = bool(plan.trip_id) and item.trip_id == plan.trip_id
            if same_session or same_trip:
                session_items.append(item)
        accepted_items = [
            item.to_dict() for item in session_items if item.status == "active"
        ]
        rejected_items = [
            item.to_dict()
            for item in session_items
            if item.status in {"rejected", "obsolete"}
        ]
        selected_skeleton = None
        if plan.selected_skeleton_id:
            for skeleton in plan.skeleton_plans:
                if not isinstance(skeleton, dict):
                    continue
                if skeleton.get("id") == plan.selected_skeleton_id:
                    selected_skeleton = skeleton
                    break
            if selected_skeleton is None:
                selected_skeleton = {"id": plan.selected_skeleton_id}

        lessons = [
            item.attributes.get("reason", "")
            for item in session_items
            if item.attributes.get("reason")
        ]
        return TripEpisode(
            id=f"{session_id}:episode",
            user_id=user_id,
            session_id=session_id,
            trip_id=plan.trip_id,
            destination=plan.destination,
            dates=(f"{plan.dates.start} - {plan.dates.end}" if plan.dates else None),
            travelers=plan.travelers.to_dict() if plan.travelers else None,
            budget=plan.budget.to_dict() if plan.budget else None,
            selected_skeleton=selected_skeleton,
            final_plan_summary=_generate_title(plan),
            accepted_items=accepted_items,
            rejected_items=rejected_items,
            lessons=lessons,
            satisfaction=None,
            created_at=_now_iso(),
        )

    async def _append_trip_episode_once(
        *,
        user_id: str,
        session_id: str,
        plan: TravelPlanState,
    ) -> bool:
        episodes = await memory_mgr.store.list_episodes(user_id)
        if any(episode.session_id == session_id for episode in episodes):
            return False
        episode = await _build_trip_episode(
            user_id=user_id,
            session_id=session_id,
            plan=plan,
        )
        await memory_mgr.store.append_episode(episode)
        return True

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
        old_trip_id = plan.trip_id
        plan.trip_id = f"trip_{uuid.uuid4().hex[:12]}"
        if not config.memory.enabled or not old_trip_id:
            return True
        for item in await memory_mgr.store.list_items(user_id):
            if item.scope == "trip" and item.trip_id == old_trip_id:
                await memory_mgr.store.update_status(user_id, item.id, "obsolete")
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

    @app.get("/api/memory/{user_id}")
    async def get_memory(user_id: str):
        await _ensure_storage_ready()
        items = await memory_mgr.store.list_items(user_id)
        return {"items": [item.to_dict() for item in items]}

    async def _set_memory_item_status(
        user_id: str,
        item_id: str,
        status: str,
    ) -> None:
        items = await memory_mgr.store.list_items(user_id)
        current = next((item for item in items if item.id == item_id), None)
        if current is None:
            raise HTTPException(status_code=404, detail="Memory item not found")
        if current.status == status:
            return
        changed = await memory_mgr.store.update_status(user_id, item_id, status)
        if changed:
            return
        items = await memory_mgr.store.list_items(user_id)
        current = next((item for item in items if item.id == item_id), None)
        if current is not None and current.status == status:
            return
        raise HTTPException(status_code=404, detail="Memory item not found")

    @app.post("/api/memory/{user_id}/confirm")
    async def confirm_memory_item(user_id: str, req: MemoryItemRequest):
        await _ensure_storage_ready()
        await _set_memory_item_status(user_id, req.item_id, "active")
        return {"item_id": req.item_id, "status": "active"}

    @app.post("/api/memory/{user_id}/reject")
    async def reject_memory_item(user_id: str, req: MemoryItemRequest):
        await _ensure_storage_ready()
        await _set_memory_item_status(user_id, req.item_id, "rejected")
        return {"item_id": req.item_id, "status": "rejected"}

    @app.post("/api/memory/{user_id}/events")
    async def append_memory_event(user_id: str, req: MemoryEventRequest):
        await _ensure_storage_ready()
        event = MemoryEvent(
            id=f"{user_id}:{req.event_type}:{_now_iso()}",
            user_id=user_id,
            session_id="",
            event_type=req.event_type,
            object_type=req.object_type,
            object_payload=req.object_payload,
            reason_text=req.reason_text,
            created_at=_now_iso(),
        )
        await memory_mgr.store.append_event(event)
        return {"ok": True}

    @app.get("/api/memory/{user_id}/episodes")
    async def list_memory_episodes(user_id: str):
        await _ensure_storage_ready()
        episodes = await memory_mgr.store.list_episodes(user_id)
        return {"episodes": [episode.to_dict() for episode in episodes]}

    @app.delete("/api/memory/{user_id}/{item_id}")
    async def delete_memory_item(user_id: str, item_id: str):
        await _ensure_storage_ready()
        await _set_memory_item_status(user_id, item_id, "obsolete")
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
        await _rotate_trip_on_reset_backtrack(
            user_id=session.get("user_id", "default_user"),
            plan=plan,
            to_phase=req.to_phase,
            reason_text=req.reason,
        )
        _cancel_memory_extraction(session_id)
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
                    await asyncio.sleep(15)
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
                    if (
                        chunk.tool_result
                        and chunk.tool_result.status == "success"
                        and tool_call_names.get(chunk.tool_result.tool_call_id)
                        == "update_plan_state"
                    ):
                        result_data = (
                            chunk.tool_result.data
                            if isinstance(chunk.tool_result.data, dict)
                            else {}
                        )
                        updated_field = None
                        if isinstance(chunk.tool_result.data, dict):
                            updated_field = chunk.tool_result.data.get("updated_field")
                        if result_data.get("backtracked"):
                            await _rotate_trip_on_reset_backtrack(
                                user_id=session["user_id"],
                                plan=plan,
                                to_phase=int(result_data.get("to_phase", plan.phase)),
                                reason_text=str(result_data.get("reason", "")),
                            )
                        elif updated_field == "selected_skeleton_id":
                            _schedule_memory_event(
                                user_id=session["user_id"],
                                session_id=plan.session_id,
                                event_type="accept",
                                object_type="skeleton",
                                object_payload=chunk.tool_result.data or {},
                            )
                        elif updated_field == "selected_transport":
                            _schedule_memory_event(
                                user_id=session["user_id"],
                                session_id=plan.session_id,
                                event_type="accept",
                                object_type="transport",
                                object_payload=chunk.tool_result.data or {},
                            )
                        elif updated_field == "accommodation":
                            _schedule_memory_event(
                                user_id=session["user_id"],
                                session_id=plan.session_id,
                                event_type="accept",
                                object_type="hotel",
                                object_payload=chunk.tool_result.data or {},
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
                    tool_call_id = f"fallback.update_plan_state:{plan.version}"
                    yield json.dumps(
                        {
                            "type": "tool_call",
                            "tool_call": {
                                "id": tool_call_id,
                                "name": "update_plan_state",
                                "arguments": {
                                    "field": "backtrack",
                                    "value": {
                                        "to_phase": backtrack_target,
                                        "reason": reason,
                                    },
                                },
                                "human_label": "更新旅行计划",
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
                _cancel_memory_extraction(plan.session_id)
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
                        await _append_trip_episode_once(
                            user_id=session["user_id"],
                            session_id=plan.session_id,
                            plan=plan,
                        )
                    except Exception:
                        pass
            _schedule_memory_extraction(
                session_id=plan.session_id,
                user_id=session["user_id"],
                messages_snapshot=list(messages),
                plan_snapshot=plan,
            )
            yield json.dumps(
                {"type": "state_update", "plan": plan.to_dict()},
                ensure_ascii=False,
            )

        finally:
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
        phase_prompt = phase_router.get_prompt(plan.phase)
        available_tools = [
            tool["name"]
            for tool in agent.tool_engine.get_tools_for_phase(plan.phase, plan)
        ]
        memory_context, recalled_ids, mem_core, mem_trip, mem_phase = (
            await memory_mgr.generate_context(req.user_id, plan)
            if config.memory.enabled
            else ("暂无相关用户记忆", [], 0, 0, 0)
        )

        if recalled_ids:
            from telemetry.stats import MemoryHitRecord

            session_stats = session.get("stats")
            if session_stats is not None:
                session_stats.memory_hits.append(
                    MemoryHitRecord(
                        item_ids=recalled_ids,
                        core_count=mem_core,
                        trip_count=mem_trip,
                        phase_count=mem_phase,
                    )
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

        messages.append(Message(role=Role.USER, content=req.message))

        # 记录 agent.run 之前的 phase，用于判断是否发生了回退
        phase_before_run = plan.phase

        async def event_stream():
            if recalled_ids:
                yield json.dumps(
                    {
                        "type": "memory_recall",
                        "item_ids": recalled_ids,
                    },
                    ensure_ascii=False,
                )
            if config.memory.enabled:
                seen_key = (session["user_id"], plan.session_id)
                seen_item_ids = memory_pending_seen.setdefault(seen_key, set())
                pending_items = [
                    item
                    for item in await memory_mgr.store.list_items(session["user_id"])
                    if item.status in {"pending", "pending_conflict"}
                    and f"{item.id}:{item.status}:{item.updated_at}"
                    not in seen_item_ids
                ]
                if pending_items:
                    seen_item_ids.update(
                        f"{item.id}:{item.status}:{item.updated_at}"
                        for item in pending_items
                    )
                    yield _memory_pending_event_from_items(pending_items)

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

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
