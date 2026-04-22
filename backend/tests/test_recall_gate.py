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


def test_short_circuit_forces_common_preference_phrase():
    """STYLE signal '常规偏好' triggers P1 force_recall."""
    decision = apply_recall_short_circuit("还是按我常规偏好来")

    assert decision.decision == "force_recall"
    assert decision.reason == "explicit_profile_history_query"


def test_short_circuit_forces_preference_arrangement_with_style_signal():
    """STYLE signal '常规偏好' mixed with '这次' still triggers P1."""
    decision = apply_recall_short_circuit("这次按我常规偏好安排")

    assert decision.decision == "force_recall"
    assert decision.reason == "explicit_profile_history_query"


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


# --- Parametrized coverage for recall_gate redesign ---

import pytest

_FORCE_CASES = [
    # (msg, expected_rule)
    ("我是不是说过不坐红眼航班？", "P1"),
    ("按我的习惯来", "P1"),
    ("还是按我以前喜欢的节奏", "P1"),
    ("像上次那样安排", "P1"),
    ("这次酒店还是按我以前不住民宿的习惯吗？", "P1"),
    ("这次航班还是避开红眼吧，跟之前一样", "P1"),
    ("照旧安排就行", "P1"),
    ("老样子，别太折腾", "P1"),
    ("老规矩就行", "P1"),
    ("像我平时喜欢的那种", "P1"),
    ("这次预算和上次一样", "P1"),
    ("这次酒店按我以前的习惯推荐一家", "P1"),
]

_UNDECIDED_RECOMMEND_CASES = [
    ("帮我选酒店", "P2"),
    ("这几个目的地哪个更适合我", "P2"),
    ("推荐几个餐厅", "P2"),
    ("这次酒店订哪里？", "P2"),
    ("这次航班怎么订？帮我选一个", "P2"),
    ("这次车次选哪趟更合适？", "P2"),
    ("当前酒店换一家吧", "P2"),
]

_SKIP_FACT_CASES = [
    ("这次预算多少？", "P3"),
    ("当前预算是多少", "P3"),
    ("本次出发是几号？", "P3"),
    ("这次选的是哪个骨架", "P3"),
    ("这次订的航班是哪一班", "P3"),
]

_SKIP_ACK_CASES = [
    ("OK 就这个", "P4"),
    ("继续", "P4"),
    ("好的", "P4"),
    ("重新开始", "P4"),
]

_UNDECIDED_EMPTY_CASES = [
    ("", "P5"),
    ("   ", "P5"),
    ("\t\n", "P5"),
]


@pytest.mark.parametrize("msg,rule", _FORCE_CASES)
def test_rule_engine_force_recall(msg, rule):
    d = apply_recall_short_circuit(msg)
    assert d.decision == "force_recall", f"{msg!r} -> {d}"
    assert d.matched_rule == rule


@pytest.mark.parametrize("msg,rule", _UNDECIDED_RECOMMEND_CASES)
def test_rule_engine_recommend_downgrade(msg, rule):
    d = apply_recall_short_circuit(msg)
    assert d.decision == "undecided", f"{msg!r} -> {d}"
    assert d.matched_rule == rule


@pytest.mark.parametrize("msg,rule", _SKIP_FACT_CASES)
def test_rule_engine_skip_pure_fact(msg, rule):
    d = apply_recall_short_circuit(msg)
    assert d.decision == "skip_recall", f"{msg!r} -> {d}"
    assert d.matched_rule == rule


@pytest.mark.parametrize("msg,rule", _SKIP_ACK_CASES)
def test_rule_engine_skip_ack(msg, rule):
    d = apply_recall_short_circuit(msg)
    assert d.decision == "skip_recall", f"{msg!r} -> {d}"
    assert d.matched_rule == rule


@pytest.mark.parametrize("msg,rule", _UNDECIDED_EMPTY_CASES)
def test_rule_engine_empty_message(msg, rule):
    d = apply_recall_short_circuit(msg)
    assert d.decision == "undecided", f"{msg!r} -> {d}"
    assert d.matched_rule == rule


def test_rule_engine_history_beats_recommend_and_fact():
    """Priority P1 > P2 > P3 must hold for mixed messages."""
    d = apply_recall_short_circuit("这次酒店按我以前的习惯推荐一家")
    assert d.decision == "force_recall"
    assert d.matched_rule == "P1"


def test_rule_engine_recommend_beats_fact():
    d = apply_recall_short_circuit("这次酒店订哪里？")
    assert d.decision == "undecided"
    assert d.matched_rule == "P2"


def test_rule_engine_fact_requires_clean_signals():
    """A fact-scope+field message that also carries ack_sys stays in P3."""
    d = apply_recall_short_circuit("OK 这次预算多少")
    # ack + fact-scope + fact-field: P1/P2 miss => P3 wins.
    assert d.decision == "skip_recall"
    assert d.matched_rule == "P3"


def test_rule_engine_exposes_signals_on_force():
    d = apply_recall_short_circuit("照旧安排就行")
    flat = {name: hits for name, hits in d.signals}
    assert "照旧" in flat["style"]
