from __future__ import annotations

import json
from typing import Any

from agent.types import Message
from llm.errors import LLMError, LLMErrorCode
from llm.types import ChunkType
from memory.recall_query import (
    ALLOWED_PROFILE_BUCKETS,
    ALLOWED_RECALL_DOMAINS,
    RecallRetrievalPlan,
)
from memory.symbolic_recall import heuristic_retrieval_plan_from_message

from api.orchestration.memory.contracts import MemoryRecallDecision


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
    latest_user_message: str,
    previous_user_messages: list[str],
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
            "检索计划必须主要基于 latest_user_message。",
            "previous_user_messages 只用于解析 latest_user_message 中的省略、指代和承接关系，不能让更早消息覆盖当前请求。",
            "plan_facts 只用于抽取目的地、当前对象、预算、同行人等检索参数，不用于重新判断是否需要 recall。",
            "你的计划会驱动符号检索，不是语义向量检索；domains、destination、keywords 必须精确、可被下游字符串匹配消费。",
            "source 决策规则：profile_preference_recall -> profile；profile_constraint_recall -> profile；past_trip_experience_recall -> episode_slice 或 hybrid_history；mixed_or_ambiguous -> hybrid_history。",
            "buckets 仅在 source=profile 或 hybrid_history 时填写。profile_constraint_recall 优先 constraints/rejections；profile_preference_recall 优先 constraints/rejections/stable_preferences；mixed_or_ambiguous 可加 preference_hypotheses。",
            f"合法 domains 只有：{json.dumps(list(ALLOWED_RECALL_DOMAINS), ensure_ascii=False)}",
            "destination 只允许填写目的地名称；无法可靠推断时填空字符串。",
            "top_k 表示每个 source 的候选预算，不是总候选数。",
            "reason 只写一行简短遥测说明，不要展开推理过程。",
            f"latest_user_message={json.dumps(latest_user_message, ensure_ascii=False)}",
            f"previous_user_messages={json.dumps(previous_user_messages, ensure_ascii=False)}",
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
    forced_choice = {"type": "function", "function": {"name": tool_name}}
    try:
        return await _collect_tool_call_arguments(
            llm,
            messages=messages,
            tool_def=tool_def,
            tool_choice=forced_choice,
        )
    except LLMError as exc:
        if not _is_unsupported_tool_choice_error(exc):
            raise
    return await _collect_tool_call_arguments(
        llm,
        messages=messages,
        tool_def=tool_def,
        tool_choice=None,
    )


def _is_unsupported_tool_choice_error(exc: LLMError) -> bool:
    if exc.code != LLMErrorCode.BAD_REQUEST:
        return False
    text = f"{exc} {exc.raw_error}".lower()
    return "tool_choice" in text and "not support" in text


async def _collect_tool_call_arguments(
    llm,
    *,
    messages: list[Message],
    tool_def: dict[str, Any],
    tool_choice: dict[str, Any] | None,
) -> dict[str, Any] | None:
    tool_name = tool_def["name"]
    async for chunk in llm.chat(
        messages,
        tools=[tool_def],
        stream=True,
        tool_choice=tool_choice,
    ):
        if chunk.type == ChunkType.TOOL_CALL_START and chunk.tool_call:
            if chunk.tool_call.name == tool_name:
                return chunk.tool_call.arguments
    return None
