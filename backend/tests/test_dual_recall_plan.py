from api.orchestration.memory.recall_planning import build_dual_recall_plan
from memory.recall_query import (
    RecallRetrievalPlan,
    dual_plan_from_gate,
    dual_plan_from_retrieval_plan,
)


def _legacy(source: str, *, reason: str = "test") -> RecallRetrievalPlan:
    return RecallRetrievalPlan(
        source=source,
        buckets=["constraints", "rejections", "stable_preferences"],
        domains=["hotel"],
        destination="京都",
        keywords=["住宿"],
        top_k=5,
        reason=reason,
    )


def test_dual_plan_from_profile_source_enables_profile_only():
    dual = dual_plan_from_retrieval_plan(_legacy("profile"))

    assert dual.need_profile is True
    assert dual.need_episode is False
    assert dual.profile_buckets == ["constraints", "rejections", "stable_preferences"]
    assert dual.domains == ["hotel"]
    assert dual.destination == "京都"
    assert dual.top_k == 5


def test_dual_plan_from_episode_source_enables_episode_only():
    dual = dual_plan_from_retrieval_plan(_legacy("episode_slice"))

    assert dual.need_profile is False
    assert dual.need_episode is True
    assert dual.profile_buckets == []


def test_dual_plan_from_hybrid_source_enables_both_without_mixed_sorting():
    dual = dual_plan_from_retrieval_plan(_legacy("hybrid_history"))

    assert dual.need_profile is True
    assert dual.need_episode is True
    assert dual.reason == "test"


def test_gate_profile_intent_enables_profile_only():
    dual = dual_plan_from_gate(
        intent_type="profile_preference_recall",
        user_message="按我的习惯住宿",
        stage0_reason="needs_llm_gate",
        stage0_signals={},
    )

    assert dual.need_profile is True
    assert dual.need_episode is False


def test_stage0_history_destination_query_enables_episode_not_profile():
    dual = dual_plan_from_gate(
        intent_type="",
        user_message="上次京都住哪里",
        stage0_reason="explicit_profile_history_query",
        stage0_signals={"history": ["上次"], "style": []},
    )

    assert dual.need_profile is False
    assert dual.need_episode is True


def test_stage0_style_query_enables_profile_not_episode():
    dual = dual_plan_from_gate(
        intent_type="",
        user_message="按我的习惯住宿",
        stage0_reason="explicit_profile_history_query",
        stage0_signals={"history": [], "style": ["习惯"]},
    )

    assert dual.need_profile is True
    assert dual.need_episode is False


def test_stage0_history_profile_query_enables_both():
    dual = dual_plan_from_gate(
        intent_type="",
        user_message="上次我是不是说过不住青旅",
        stage0_reason="explicit_profile_history_query",
        stage0_signals={"history": ["上次"], "style": []},
    )

    assert dual.need_profile is True
    assert dual.need_episode is True


def test_build_dual_recall_plan_prefers_legacy_tool_parameters():
    dual = build_dual_recall_plan(
        retrieval_plan=_legacy("episode_slice"),
        gate_intent_type="mixed_or_ambiguous",
        user_message="按我的习惯住宿",
        stage0_reason="needs_llm_gate",
        stage0_signals={},
    )

    assert dual.need_profile is False
    assert dual.need_episode is True
    assert dual.destination == "京都"


def test_build_dual_recall_plan_falls_back_to_gate_mapping_without_tool_plan():
    dual = build_dual_recall_plan(
        retrieval_plan=None,
        gate_intent_type="profile_constraint_recall",
        user_message="不要红眼航班",
        stage0_reason="needs_llm_gate",
        stage0_signals={},
    )

    assert dual.need_profile is True
    assert dual.need_episode is False
    assert dual.profile_buckets == ["constraints", "rejections", "stable_preferences"]
