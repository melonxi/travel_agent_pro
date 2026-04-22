from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from memory.recall_signals import extract_signals


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
    """Rule engine (Layer 2 of recall gate).

    Priority contract (higher rule wins; first match returns):
      P1: HISTORY ∪ STYLE  → force_recall (profile signal, keep legacy reason
           "explicit_profile_history_query" for backward compatibility)
      P2: RECOMMEND        → undecided (individualized recommendation needs LLM)
      P3: FACT_SCOPE ∩ FACT_FIELD, with NO history/style/recommend
                           → skip_recall ("current_trip_fact_question")
      P4: only ACK_SYS hit, everything else empty
                           → skip_recall ("ack_or_system_meta")
      P5: message is empty / whitespace only
                           → undecided ("empty_message")
      P6: fallback         → undecided ("needs_llm_gate")

    Returns signals + matched_rule alongside the legacy decision/reason for
    tracing. Callers relying only on decision/reason are unaffected.
    """
    text = (message or "").strip()
    signals = extract_signals(message or "")
    signals_tuple = tuple(signals.items())

    if not text:
        return RecallShortCircuitDecision(
            decision="undecided",
            reason="empty_message",
            matched_rule="P5",
            signals=signals_tuple,
        )

    if signals["history"] or signals["style"]:
        return RecallShortCircuitDecision(
            decision="force_recall",
            reason="explicit_profile_history_query",
            matched_rule="P1",
            signals=signals_tuple,
        )

    if signals["recommend"]:
        return RecallShortCircuitDecision(
            decision="undecided",
            reason="needs_llm_gate_recommend",
            matched_rule="P2",
            signals=signals_tuple,
        )

    if signals["fact_scope"] and signals["fact_field"]:
        return RecallShortCircuitDecision(
            decision="skip_recall",
            reason="current_trip_fact_question",
            matched_rule="P3",
            signals=signals_tuple,
        )

    if signals["ack_sys"] and not any(
        signals[k] for k in ("history", "style", "recommend", "fact_scope", "fact_field")
    ):
        return RecallShortCircuitDecision(
            decision="skip_recall",
            reason="ack_or_system_meta",
            matched_rule="P4",
            signals=signals_tuple,
        )

    return RecallShortCircuitDecision(
        decision="undecided",
        reason="needs_llm_gate",
        matched_rule="P6",
        signals=signals_tuple,
    )


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
