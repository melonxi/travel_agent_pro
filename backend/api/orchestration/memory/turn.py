from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from agent.internal_tasks import InternalTask
from agent.types import Role
from memory.formatter import MemoryRecallTelemetry

from api.orchestration.memory.recall_planning import (
    _GATE_HEURISTIC_RECALL_FALLBACKS,
    _final_recall_decision_from_gate,
    _query_plan_summary,
)
from api.orchestration.common.telemetry_helpers import (
    _memory_hit_record_from_recall,
    _recall_telemetry_record_from_recall,
)


@dataclass
class MemoryTurnResult:
    memory_context: str
    events: list[str]


async def build_memory_context_for_turn(
    *,
    config: Any,
    memory_mgr: Any,
    session: dict,
    plan: Any,
    messages: list,
    user_id: str,
    user_message: str,
    decide_memory_recall,
    build_recall_retrieval_plan,
) -> MemoryTurnResult:
    if not config.memory.enabled:
        return MemoryTurnResult(memory_context="暂无相关用户记忆", events=[])

    events: list[str] = []
    memory_recall_task_id = f"memory_recall:{plan.session_id}:{int(time.time())}"
    memory_recall_started_at = time.time()
    events.append(
        json.dumps(
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
    )

    user_messages = [
        message.content
        for message in messages
        if message.role == Role.USER and message.content
    ]
    recall_decision = await decide_memory_recall(
        session_id=plan.session_id,
        user_id=user_id,
        user_messages=user_messages,
        plan_snapshot=plan,
    )
    retrieval_plan = None
    query_plan_source = ""
    query_plan_fallback = "none"
    if recall_decision.needs_recall:
        if recall_decision.fallback_used in _GATE_HEURISTIC_RECALL_FALLBACKS:
            query_plan_source = "heuristic_fallback"
            query_plan_fallback = recall_decision.fallback_used
        else:
            query_plan_result = await build_recall_retrieval_plan(
                session_id=plan.session_id,
                user_id=user_id,
                user_message=user_message,
                user_messages=user_messages,
                gate_intent_type=recall_decision.intent_type,
                gate_reason=recall_decision.reason,
                gate_confidence=recall_decision.confidence,
                stage0_decision=recall_decision.stage0_decision,
                stage0_signals=recall_decision.stage0_signals,
                plan_snapshot=plan,
            )
            retrieval_plan = query_plan_result.plan
            query_plan_source = query_plan_result.query_plan_source
            query_plan_fallback = query_plan_result.query_plan_fallback

    memory_result = await memory_mgr.generate_context(
        user_id,
        plan,
        user_message=user_message,
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

    recalled_ids = list(
        dict.fromkeys(
            [
                *memory_recall.profile_ids,
                *memory_recall.working_memory_ids,
                *memory_recall.slice_ids,
            ]
        )
    )
    events.append(
        json.dumps(
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
                        "reranker_per_item_scores": dict(
                            memory_recall.reranker_per_item_scores
                        ),
                        "reranker_intent_label": memory_recall.reranker_intent_label,
                        "reranker_selection_metrics": dict(
                            memory_recall.reranker_selection_metrics
                        ),
                    },
                    started_at=memory_recall_started_at,
                    ended_at=time.time(),
                ).to_dict(),
            },
            ensure_ascii=False,
        )
    )

    session_stats = session.get("stats")
    if session_stats is not None:
        session_stats.recall_telemetry.append(
            _recall_telemetry_record_from_recall(memory_recall)
        )
        memory_hit_record = _memory_hit_record_from_recall(memory_recall)
        if memory_hit_record is not None:
            session_stats.memory_hits.append(memory_hit_record)

    events.append(
        json.dumps(
            {
                "type": "memory_recall",
                "gate": memory_recall.gate_needs_recall,
                **memory_recall.to_dict(),
            },
            ensure_ascii=False,
        )
    )
    return MemoryTurnResult(memory_context=memory_context, events=events)
