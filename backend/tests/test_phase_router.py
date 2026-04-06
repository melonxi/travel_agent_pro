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
        accommodation=Accommodation(area="祇園"),
    )
    assert router.infer_phase(plan) == 5


def test_infer_phase_plans_complete(router):
    plan = TravelPlanState(
        session_id="s1",
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        accommodation=Accommodation(area="祇園"),
        daily_plans=[DayPlan(day=i, date=f"2026-04-{10 + i}") for i in range(5)],
    )
    assert router.infer_phase(plan) == 7


def test_get_prompt_for_phase(router):
    prompt = router.get_prompt(1)
    assert "目的地收敛顾问" in prompt


def test_phase1_prompt_encourages_reading_recommendation_posts_and_comments(router):
    prompt = router.get_prompt(1)
    assert "不要只停留在标题层判断" in prompt
    assert "求推荐旅行目的地" in prompt
    assert "评论区提炼高频候选" in prompt


def test_get_prompt_for_all_phases(router):
    for phase in [1, 3, 5, 7]:
        prompt = router.get_prompt(phase)
        assert len(prompt) > 50


def test_check_transition_no_change(router):
    plan = TravelPlanState(session_id="s1", phase=1)
    changed = router.check_and_apply_transition(plan)
    assert not changed
    assert plan.phase == 1


def test_check_transition_phase_advance(router):
    plan = TravelPlanState(session_id="s1", phase=1, destination="Kyoto")
    changed = router.check_and_apply_transition(plan)
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
