from memory.recall_gate import (
    apply_recall_short_circuit,
    build_recall_gate_tool,
    parse_recall_gate_tool_arguments,
)


def test_short_circuit_skips_current_trip_fact_question():
    decision = apply_recall_short_circuit("这次预算多少？")

    assert decision.decision == "skip_recall"
    assert decision.reason == "current_trip_fact_question"


def test_short_circuit_forces_obvious_history_question():
    decision = apply_recall_short_circuit("我是不是说过不坐红眼航班？")

    assert decision.decision == "force_recall"
    assert decision.reason == "explicit_profile_history_query"


def test_short_circuit_leaves_ambiguous_message_to_gate():
    decision = apply_recall_short_circuit("还是按我常规偏好来")

    assert decision.decision == "undecided"
    assert decision.reason == "needs_llm_gate"


def test_short_circuit_does_not_skip_preference_arrangement_request():
    decision = apply_recall_short_circuit("这次按我常规偏好安排")

    assert decision.decision == "undecided"
    assert decision.reason == "needs_llm_gate"


def test_parse_recall_gate_tool_arguments_honors_schema_fields():
    decision = parse_recall_gate_tool_arguments(
        {
            "needs_recall": True,
            "intent_type": "profile_preference_recall",
            "reason": "user asks to apply prior preference",
            "confidence": 0.81,
        }
    )

    assert decision.needs_recall is True
    assert decision.intent_type == "profile_preference_recall"
    assert decision.reason == "user asks to apply prior preference"
    assert decision.confidence == 0.81


def test_parse_recall_gate_tool_arguments_defaults_invalid_payload_to_safe_false():
    decision = parse_recall_gate_tool_arguments({"confidence": "oops"})

    assert decision.needs_recall is False
    assert decision.intent_type == "no_recall_needed"
    assert decision.reason == "invalid_tool_payload"
    assert decision.confidence == 0.0


def test_parse_recall_gate_tool_arguments_rejects_unknown_intent_type():
    decision = parse_recall_gate_tool_arguments(
        {
            "needs_recall": True,
            "intent_type": "totally_unknown_intent",
            "reason": "bad enum value",
            "confidence": 0.91,
        }
    )

    assert decision.needs_recall is False
    assert decision.intent_type == "no_recall_needed"
    assert decision.reason == "invalid_tool_payload"
    assert decision.confidence == 0.0


def test_parse_recall_gate_tool_arguments_rejects_bool_confidence():
    true_decision = parse_recall_gate_tool_arguments(
        {
            "needs_recall": True,
            "intent_type": "profile_preference_recall",
            "reason": "bool confidence true",
            "confidence": True,
        }
    )
    false_decision = parse_recall_gate_tool_arguments(
        {
            "needs_recall": False,
            "intent_type": "no_recall_needed",
            "reason": "bool confidence false",
            "confidence": False,
        }
    )

    assert true_decision.needs_recall is False
    assert true_decision.intent_type == "no_recall_needed"
    assert true_decision.reason == "invalid_tool_payload"
    assert true_decision.confidence == 0.0
    assert false_decision.needs_recall is False
    assert false_decision.intent_type == "no_recall_needed"
    assert false_decision.reason == "invalid_tool_payload"
    assert false_decision.confidence == 0.0


def test_parse_recall_gate_tool_arguments_rejects_false_needs_recall_with_recall_intent():
    decision = parse_recall_gate_tool_arguments(
        {
            "needs_recall": False,
            "intent_type": "profile_preference_recall",
            "reason": "contradictory payload",
            "confidence": 0.72,
        }
    )

    assert decision.needs_recall is False
    assert decision.intent_type == "no_recall_needed"
    assert decision.reason == "invalid_tool_payload"
    assert decision.confidence == 0.0


def test_parse_recall_gate_tool_arguments_rejects_true_needs_recall_with_non_recall_intent():
    decision = parse_recall_gate_tool_arguments(
        {
            "needs_recall": True,
            "intent_type": "no_recall_needed",
            "reason": "contradictory payload",
            "confidence": 0.88,
        }
    )

    assert decision.needs_recall is False
    assert decision.intent_type == "no_recall_needed"
    assert decision.reason == "invalid_tool_payload"
    assert decision.confidence == 0.0


def test_build_recall_gate_tool_exposes_required_enum_schema():
    tool = build_recall_gate_tool()

    assert tool["parameters"]["properties"]["intent_type"]["enum"] == [
        "current_trip_fact",
        "profile_preference_recall",
        "profile_constraint_recall",
        "past_trip_experience_recall",
        "mixed_or_ambiguous",
        "no_recall_needed",
    ]
    assert tool["parameters"]["required"] == [
        "needs_recall",
        "intent_type",
        "reason",
        "confidence",
    ]
