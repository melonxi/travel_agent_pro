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
        daily_plans=[DayPlan(day=i, date=f"2026-04-{10 + i}") for i in range(5)],
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
    assert plan.trip_brief["total_days"] == 5
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
        "assemble_day_plan",
        "get_poi_info",
        "calculate_route",
        "check_availability",
        "check_weather",
        "xiaohongshu_search",
        "append_day_plan",
    ]:
        assert tool_name in prompt


def test_phase5_prompt_avoids_unavailable_phase5_tools(router):
    prompt = router.get_prompt(5)
    for tool_name in [
        "web_search",
        "search_flights",
        "search_trains",
        "search_accommodations",
    ]:
        assert tool_name not in prompt


def test_phase1_prompt_encourages_reading_recommendation_posts_and_comments(router):
    prompt = router.get_prompt(1)
    assert "不要只停留在标题层判断" in prompt
    assert "求推荐旅行目的地" in prompt
    assert "评论区提炼高频候选" in prompt


def test_phase1_prompt_skips_search_when_destination_is_already_confirmed(router):
    prompt = router.get_prompt(1)
    assert "不要先调 `xiaohongshu_search` 或 `web_search`" in prompt


def test_phase3_prompt_prioritizes_brief_sync_before_external_search(router):
    prompt = router.get_prompt(3)
    assert "优先先写 `trip_brief` 并进入 `candidate`" in prompt
    assert "不要在 brief 已经足够成型时先去做外部搜索" in prompt


def test_phase3_candidate_prompt_limits_search_and_forbids_search_narration(router):
    from phase.prompts import build_phase3_prompt
    prompt = build_phase3_prompt("candidate")
    assert "先写状态，再按需补充验证" in prompt
    assert "优先控制在 1 次 `xiaohongshu_search` 加 0-1 次 `web_search`" in prompt
    assert "不要在正文里反复说“我先搜一下”" in prompt


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
