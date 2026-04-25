from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from dataclasses import replace
from functools import partial
from typing import Any, Callable

from agent.internal_tasks import InternalTask
from agent.types import Message, Role
from llm.types import ChunkType
from memory.async_jobs import (
    MemoryJobSnapshot,
    build_gate_user_window,
)
from memory.formatter import MemoryRecallTelemetry
from memory.recall_gate import (
    apply_recall_short_circuit,
    build_recall_gate_tool,
    parse_recall_gate_tool_arguments,
)
from memory.recall_query import RecallRetrievalPlan, parse_recall_query_tool_arguments
from memory.symbolic_recall import heuristic_retrieval_plan_from_message
from memory.v3_models import MemoryAuditEvent
from state.models import TravelPlanState

from api.orchestration.memory.episodes import append_archived_trip_episode_once
from api.orchestration.memory.extraction import create_memory_extraction_runtime
from api.orchestration.memory.contracts import (
    MemoryRecallDecision,
    MemorySchedulerRuntime,
    RecallQueryPlanResult,
)
from api.orchestration.memory.recall_planning import (
    _build_recall_query_prompt,
    _build_recall_query_tool,
    _final_recall_decision_from_gate,
    _gate_failure_recall_decision_from_heuristic,
    _query_plan_summary,
    _stage0_signals_to_dict,
)
from api.orchestration.memory.tasks import create_memory_task_runtime

logger = logging.getLogger(__name__)


@dataclass
class MemoryOrchestration:
    scheduler_runtimes: dict[str, MemorySchedulerRuntime]
    task_subscribers: dict[str, set[asyncio.Queue[str]]]
    active_tasks: dict[str, dict[str, InternalTask]]
    schedule_memory_event: Callable[..., None]
    build_memory_job_snapshot: Callable[..., MemoryJobSnapshot]
    submit_memory_snapshot: Callable[[MemoryJobSnapshot], None]
    decide_memory_recall: Callable[..., Any]
    build_recall_retrieval_plan: Callable[..., Any]
    extract_memory_candidates: Callable[..., Any]
    run_memory_job: Callable[[MemoryJobSnapshot], Any]
    memory_task_stream: Callable[..., Any]
    append_archived_trip_episode_once: Callable[..., Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_memory_orchestration(
    *,
    config: Any,
    memory_mgr: Any,
    create_llm_provider_func: Callable[[Any], Any],
    collect_forced_tool_call_arguments: Callable[..., Any],
    keepalive_interval_seconds: Callable[[], float],
) -> MemoryOrchestration:
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
    
    async def _build_memory_prompt_summary(
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
    
    extraction_runtime: Any = None
    task_runtime = create_memory_task_runtime(
        config=config,
        keepalive_interval_seconds=keepalive_interval_seconds,
        decide_memory_extraction=(
            lambda **kwargs: extraction_runtime.decide_memory_extraction(**kwargs)
        ),
        extract_memory_candidates=(
            lambda **kwargs: extraction_runtime.extract_memory_candidates(**kwargs)
        ),
    )
    extraction_runtime = create_memory_extraction_runtime(
        config=config,
        memory_mgr=memory_mgr,
        create_llm_provider_func=create_llm_provider_func,
        collect_forced_tool_call_arguments=collect_forced_tool_call_arguments,
        build_memory_prompt_summary=_build_memory_prompt_summary,
        memory_plan_facts=_memory_plan_facts,
        publish_memory_task=(
            lambda session_id, task: task_runtime.get_publish_memory_task()(
                session_id, task
            )
        ),
        now_iso=_now_iso,
    )
    memory_scheduler_runtimes = task_runtime.scheduler_runtimes
    memory_task_subscribers = task_runtime.task_subscribers
    memory_active_tasks = task_runtime.active_tasks
    _build_memory_job_snapshot = task_runtime.build_memory_job_snapshot
    _submit_memory_snapshot = task_runtime.submit_memory_snapshot
    _extract_memory_candidates = extraction_runtime.extract_memory_candidates
    _run_memory_job = task_runtime.run_memory_job
    _memory_task_stream = task_runtime.memory_task_stream
    _append_archived_trip_episode_once = partial(
        append_archived_trip_episode_once,
        memory_mgr=memory_mgr,
        now_iso=_now_iso,
    )

    async def _decide_memory_recall(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
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
            )
    
        latest_user_message = gate_window[-1]
        previous_user_messages = gate_window[:-1]
        prompt = "\n".join(
            [
                "你是旅行记忆召回判定器。",
                "你的任务是判断：为了更好地回答用户当前请求，是否应该调取用户的长期画像、历史偏好、长期约束、过去旅行经验或当前会话工作记忆。",
                "判定对象：必须以 latest_user_message 为主要判断依据。",
                "previous_user_messages 只用于理解 latest_user_message 中的省略、指代和承接关系，不能因为更早消息本身需要召回，就把当前轮判成 needs_recall=true。",
                "current_trip_facts 只用于识别 latest_user_message 是否在询问当前行程中已经明确存在的事实。",
                "如果 latest_user_message 是对当前行程做个性化评价、选择、优化、推荐或取舍，即使它引用了 current_trip_facts，也应倾向于 needs_recall=true。",
                "核心原则：",
                "1. 如果用户请求涉及个性化选择、推荐、排序、取舍、规划风格、目的地选择、住宿选择、交通选择、餐饮偏好、活动偏好、预算倾向、体力节奏、同行人需求、避雷避坑，即使用户没有明确说“按我的偏好”或“像以前一样”，也应倾向于 needs_recall=true。",
                "2. 如果用户表达的是“我想要/我喜欢/我不喜欢/我偏好/我受不了/我希望/我在意/我不想要”这类个人特征、偏好或约束，并且这些信息可能影响后续推荐或规划，应倾向于 needs_recall=true，用于结合已有画像避免重复提问或冲突建议。",
                "3. 如果用户提出开放式旅行请求，例如“想去好玩的地方”“想去好吃的地方”“帮我安排住宿”“这几个哪个更适合”“怎么安排比较好”，这通常需要个性化判断，应召回相关画像和历史经验。",
                "4. 如果用户只是在询问当前行程中已经明确存在的事实，例如“我这次预算是多少”“酒店是哪家”“几号出发”“现在目的地是哪里”，不需要召回历史记忆，needs_recall=false。",
                "5. 如果用户只是确认、继续、寒暄、系统操作或没有实际规划含义，例如“好的”“继续”“就这个”“重新开始”，通常 needs_recall=false。",
                "6. 如果当前请求既可能是普通查询，也可能受个人偏好影响，采用保守召回策略，needs_recall=true。召回比漏召更可接受，因为后续 reranker 会过滤无关记忆。",
                "请特别注意：",
                "- 不要只在用户显式提到“历史、上次、以前、按我偏好”时才召回。",
                "- 用户说“好吃、舒服、轻松、不折腾、适合我、好玩、自然、安静、方便、省心、亲子、老人友好、预算别太高”等主观标准时，通常都需要召回用户画像。",
                "- 如果当前请求需要回答“什么更适合这个用户”，就应该召回。",
                "- 如果只是回答“当前计划里已经写了什么事实”，才跳过召回。",
                f"latest_user_message={json.dumps(latest_user_message, ensure_ascii=False)}",
                f"previous_user_messages={json.dumps(previous_user_messages, ensure_ascii=False)}",
                f"current_trip_facts={json.dumps(_memory_plan_facts(plan_snapshot), ensure_ascii=False)}",
                "输出字段语义：needs_recall 表示 latest_user_message 是否需要召回；intent_type 必须使用工具枚举；reason 简短说明为什么需要或不需要召回；confidence 为 0 到 1 的置信度。",
            ]
        )
        recall_gate_model = config.memory.retrieval.recall_gate_model or config.llm.model
        gate_llm = create_llm_provider_func(replace(config.llm, model=recall_gate_model))
        try:
            tool_args = await asyncio.wait_for(
                collect_forced_tool_call_arguments(
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
        query_llm = create_llm_provider_func(config.llm)
        query_window = build_gate_user_window(
            user_messages=user_messages,
            max_messages=_GATE_MAX_USER_MESSAGES,
            max_chars=_GATE_MAX_CHARS,
        )
        latest_user_message = query_window[-1] if query_window else user_message
        previous_user_messages = query_window[:-1]
        if memory_summary is None:
            memory_summary = await _build_memory_prompt_summary(
                user_id=user_id,
                session_id=session_id,
                plan_snapshot=plan_snapshot,
            )
        prompt = _build_recall_query_prompt(
            latest_user_message=latest_user_message,
            previous_user_messages=previous_user_messages,
            gate_intent_type=gate_intent_type,
            gate_reason=gate_reason,
            gate_confidence=gate_confidence,
            stage0_signals=stage0_signals,
            plan_facts=_memory_plan_facts(plan_snapshot),
            memory_summary=memory_summary,
        )
        try:
            tool_args = await asyncio.wait_for(
                collect_forced_tool_call_arguments(
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
    
    return MemoryOrchestration(
        scheduler_runtimes=memory_scheduler_runtimes,
        task_subscribers=memory_task_subscribers,
        active_tasks=memory_active_tasks,
        schedule_memory_event=_schedule_memory_event,
        build_memory_job_snapshot=_build_memory_job_snapshot,
        submit_memory_snapshot=_submit_memory_snapshot,
        decide_memory_recall=_decide_memory_recall,
        build_recall_retrieval_plan=_build_recall_retrieval_plan,
        extract_memory_candidates=_extract_memory_candidates,
        run_memory_job=_run_memory_job,
        memory_task_stream=_memory_task_stream,
        append_archived_trip_episode_once=_append_archived_trip_episode_once,
    )
