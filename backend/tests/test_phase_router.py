# backend/tests/test_phase_router.py
import pytest

from phase.router import PhaseRouter
from state.models import (
    TravelPlanState,
    DateRange,
    Accommodation,
    DayPlan,
    Preference,
)


@pytest.fixture
def router():
    return PhaseRouter()


def test_infer_phase_empty(router):
    plan = TravelPlanState(session_id="s1")
    assert router.infer_phase(plan) == 1


def test_infer_phase_has_preferences_no_destination(router):
    plan = TravelPlanState(
        session_id="s1",
        preferences=[Preference(key="style", value="relaxed")],
    )
    assert router.infer_phase(plan) == 1


def test_infer_phase_has_destination_no_dates(router):
    plan = TravelPlanState(session_id="s1", destination="Kyoto")
    assert router.infer_phase(plan) == 3


def test_infer_phase_has_dates_no_accommodation(router):
    """After phase 3+4 merge: dates set but no accommodation → still phase 3."""
    plan = TravelPlanState(
        session_id="s1",
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
    )
    assert router.infer_phase(plan) == 3


def test_infer_phase_has_accommodation_no_plans(router):
    plan = TravelPlanState(
        session_id="s1",
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        selected_skeleton_id="balanced",
        accommodation=Accommodation(area="祇園"),
    )
    assert router.infer_phase(plan) == 5


def test_infer_phase_plans_complete(router):
    plan = TravelPlanState(
        session_id="s1",
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        selected_skeleton_id="balanced",
        accommodation=Accommodation(area="祇園"),
        daily_plans=[DayPlan(day=i + 1, date=f"2026-04-{10 + i}") for i in range(6)],
    )
    assert router.infer_phase(plan) == 7


def test_infer_phase_keeps_phase3_until_skeleton_selected(router):
    plan = TravelPlanState(
        session_id="s1",
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        accommodation=Accommodation(area="祇園"),
    )
    assert router.infer_phase(plan) == 3


def test_sync_phase_state_updates_phase3_step(router):
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        trip_brief={"goal": "慢旅行"},
        skeleton_plans=[{"id": "balanced"}],
    )
    router.sync_phase_state(plan)
    assert plan.phase3_step == "skeleton"
    assert plan.trip_brief["destination"] == "Kyoto"
    assert plan.trip_brief["dates"]["start"] == "2026-04-10"


def test_sync_phase_state_hydrates_minimal_trip_brief_from_explicit_state(router):
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
    )
    router.sync_phase_state(plan)
    assert plan.trip_brief["destination"] == "Kyoto"
    assert plan.trip_brief["total_days"] == 6
    assert plan.phase3_step == "candidate"


def test_get_prompt_for_phase(router):
    prompt = router.get_prompt(1)
    assert "目的地收敛顾问" in prompt


def test_phase5_prompt_mentions_daily_plan_commit_and_backtrack(router):
    prompt = router.get_prompt(5)
    assert "daily_plans" in prompt
    assert "backtrack" in prompt
    assert "selected_skeleton_id" in prompt


def test_phase5_prompt_mentions_actual_phase5_tools(router):
    prompt = router.get_prompt(5)
    for tool_name in [
        "optimize_day_route",
        "get_poi_info",
        "calculate_route",
        "check_weather",
        "web_search",
        "xiaohongshu_search_notes",
        "save_day_plan",
        "replace_all_day_plans",
    ]:
        assert tool_name in prompt


def test_phase5_prompt_does_not_mention_legacy_phase5_plan_tools(router):
    prompt = router.get_prompt(5)
    for tool_name in ["append_day_plan", "replace_daily_plans", "assemble_day_plan"]:
        assert tool_name not in prompt


def test_phase5_prompt_avoids_unavailable_phase5_tools(router):
    prompt = router.get_prompt(5)
    for tool_name in [
        "check_availability",
        "search_flights",
        "search_trains",
        "search_accommodations",
    ]:
        assert tool_name not in prompt


def test_phase1_prompt_encourages_reading_recommendation_posts_and_comments(router):
    prompt = router.get_prompt(1)
    assert "不要只看标题就下结论" in prompt
    assert "求推荐旅行目的地" in prompt
    assert "评论区提炼高频候选" in prompt


def test_phase1_prompt_skips_search_when_destination_is_already_confirmed(router):
    prompt = router.get_prompt(1)
    assert "不要先调 `xiaohongshu_search_notes` 或 `web_search`" in prompt


def test_phase3_prompt_prioritizes_brief_sync_before_external_search(router):
    prompt = router.get_prompt(3)
    assert "优先先写 `trip_brief` 并进入 `candidate`" in prompt
    assert "不要在 brief 已经足够成型时先去做外部搜索" in prompt


def test_phase3_candidate_prompt_limits_search_and_forbids_search_narration(router):
    from phase.prompts import build_phase3_prompt

    prompt = build_phase3_prompt("candidate")
    assert (
        "获取到足够信息后应立即写入 `set_candidate_pool` 和 `set_shortlist`" in prompt
    )
    assert '不要为了"查全"而反复搜索延迟写入' in prompt
    assert '不要在正文里反复说"我先搜一下"' in prompt


def test_get_prompt_for_all_phases(router):
    for phase in [1, 3, 5, 7]:
        prompt = router.get_prompt(phase)
        assert len(prompt) > 50


@pytest.mark.asyncio
async def test_check_transition_no_change(router):
    plan = TravelPlanState(session_id="s1", phase=1)
    changed = await router.check_and_apply_transition(plan)
    assert not changed
    assert plan.phase == 1


@pytest.mark.asyncio
async def test_check_transition_phase_advance(router):
    plan = TravelPlanState(session_id="s1", phase=1, destination="Kyoto")
    changed = await router.check_and_apply_transition(plan)
    assert changed
    assert plan.phase == 3  # destination present, no preferences → skip 2


def test_prepare_backtrack(router, tmp_path):
    plan = TravelPlanState(
        session_id="s1",
        phase=5,
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        accommodation=Accommodation(area="祇園"),
        daily_plans=[DayPlan(day=1, date="2026-04-10")],
    )
    router.prepare_backtrack(
        plan, to_phase=3, reason="预算超限", snapshot_path="/tmp/snap.json"
    )
    assert plan.phase == 3
    assert plan.accommodation is None
    assert plan.daily_plans == []
    assert plan.destination == "Kyoto"  # preserved
    assert len(plan.backtrack_history) == 1
    assert plan.backtrack_history[0].reason == "预算超限"


def test_infer_phase_blocks_phase5_when_skeleton_days_mismatch(router):
    """骨架天数与 total_days 不一致时，不应进入 Phase 5。"""
    plan = TravelPlanState(
        session_id="s1",
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),  # 6 天 (inclusive)
        selected_skeleton_id="plan_a",
        skeleton_plans=[
            {"id": "plan_a", "days": [{"day": i} for i in range(1, 8)]}
        ],  # 7 天
        accommodation=Accommodation(area="祇園"),
    )
    assert router.infer_phase(plan) == 3  # 不进入 5


def test_infer_phase_allows_phase5_when_skeleton_days_match(router):
    """骨架天数与 total_days 一致时，正常进入 Phase 5。"""
    plan = TravelPlanState(
        session_id="s1",
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),  # 6 天 (inclusive)
        selected_skeleton_id="plan_a",
        skeleton_plans=[
            {"id": "plan_a", "days": [{"day": i} for i in range(1, 7)]}
        ],  # 6 天
        accommodation=Accommodation(area="祇園"),
    )
    assert router.infer_phase(plan) == 5


def test_hydrate_phase3_brief_overwrites_stale_dates(router):
    """trip_brief 中的旧日期应被权威 plan.dates 覆盖。"""
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        destination="Kyoto",
        dates=DateRange(start="2026-05-24", end="2026-05-30"),
        trip_brief={
            "destination": "Kyoto",
            "dates": {"start": "2026-05-10", "end": "2026-05-16"},
            "total_days": 6,
        },
    )
    router.sync_phase_state(plan)
    assert plan.trip_brief["dates"] == {"start": "2026-05-24", "end": "2026-05-30"}
    assert plan.trip_brief["total_days"] == 7  # inclusive: 24-30
