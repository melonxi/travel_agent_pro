from __future__ import annotations

from dataclasses import dataclass
from typing import Any


VALID_RECALL_INTENT_TYPES = (
    "current_trip_fact",
    "profile_preference_recall",
    "profile_constraint_recall",
    "past_trip_experience_recall",
    "mixed_or_ambiguous",
    "no_recall_needed",
)

RECALL_REQUIRED_INTENT_TYPES = {
    "profile_preference_recall",
    "profile_constraint_recall",
    "past_trip_experience_recall",
}

NON_RECALL_INTENT_TYPES = {
    "current_trip_fact",
    "mixed_or_ambiguous",
    "no_recall_needed",
}


@dataclass(frozen=True)
class RecallShortCircuitDecision:
    decision: str
    reason: str
    matched_rule: str = ""
    signals: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass
class RecallGateDecision:
    needs_recall: bool
    intent_type: str
    reason: str
    confidence: float = 0.0
    fallback_used: str = "none"


def apply_recall_short_circuit(message: str) -> RecallShortCircuitDecision:
    text = (message or "").strip()
    if not text:
        return RecallShortCircuitDecision("undecided", "needs_llm_gate")

    if any(token in text for token in ("我是不是说过", "按我的习惯", "上次", "之前", "以前")):
        return RecallShortCircuitDecision("force_recall", "explicit_profile_history_query")

    if any(token in text for token in ("这次", "本次", "当前")) and any(
        token in text for token in ("预算", "几号", "出发", "骨架", "日期", "酒店", "航班", "车次")
    ):
        return RecallShortCircuitDecision("skip_recall", "current_trip_fact_question")

    return RecallShortCircuitDecision("undecided", "needs_llm_gate")


def build_recall_gate_tool() -> dict[str, Any]:
    return {
        "name": "decide_memory_recall",
        "description": "Decide whether the current user message needs profile recall.",
        "parameters": {
            "type": "object",
            "properties": {
                "needs_recall": {"type": "boolean"},
                "intent_type": {
                    "type": "string",
                    "enum": list(VALID_RECALL_INTENT_TYPES),
                },
                "reason": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["needs_recall", "intent_type", "reason", "confidence"],
        },
    }


def parse_recall_gate_tool_arguments(payload: dict[str, Any] | None) -> RecallGateDecision:
    if not isinstance(payload, dict):
        return RecallGateDecision(
            needs_recall=False,
            intent_type="no_recall_needed",
            reason="invalid_tool_payload",
            confidence=0.0,
            fallback_used="invalid_tool_payload",
        )

    needs_recall = payload.get("needs_recall")
    intent_type = payload.get("intent_type")
    reason = payload.get("reason")
    confidence = payload.get("confidence")

    if (
        isinstance(needs_recall, bool)
        and isinstance(intent_type, str)
        and intent_type in VALID_RECALL_INTENT_TYPES
        and isinstance(reason, str)
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
    ):
        if needs_recall and intent_type not in RECALL_REQUIRED_INTENT_TYPES:
            return RecallGateDecision(
                needs_recall=False,
                intent_type="no_recall_needed",
                reason="invalid_tool_payload",
                confidence=0.0,
                fallback_used="invalid_tool_payload",
            )
        if not needs_recall and intent_type not in NON_RECALL_INTENT_TYPES:
            return RecallGateDecision(
                needs_recall=False,
                intent_type="no_recall_needed",
                reason="invalid_tool_payload",
                confidence=0.0,
                fallback_used="invalid_tool_payload",
            )
        return RecallGateDecision(
            needs_recall=needs_recall,
            intent_type=intent_type,
            reason=reason,
            confidence=float(confidence),
        )

    return RecallGateDecision(
        needs_recall=False,
        intent_type="no_recall_needed",
        reason="invalid_tool_payload",
        confidence=0.0,
        fallback_used="invalid_tool_payload",
    )
