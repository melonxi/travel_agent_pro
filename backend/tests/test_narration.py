"""Tests for agent.narration.compute_narration."""

from agent.narration import compute_narration
from state.models import TravelPlanState


def test_phase1_no_destination_returns_inspiration_hint():
    plan = TravelPlanState(session_id="s", phase=1, destination=None)
    assert compute_narration(plan) == "先搞清楚你想去哪，然后翻点真实游记"


def test_phase1_with_destination_returns_detail_hint():
    plan = TravelPlanState(session_id="s", phase=1, destination="京都")
    assert compute_narration(plan) == "围绕目的地再收几条真实游记，定细节"


def test_phase3_brief_step():
    plan = TravelPlanState(session_id="s", phase=3, phase3_step="brief")
    assert "画像" in compute_narration(plan)


def test_phase3_candidate_step():
    plan = TravelPlanState(session_id="s", phase=3, phase3_step="candidate")
    assert "候选" in compute_narration(plan)


def test_phase3_skeleton_step():
    plan = TravelPlanState(session_id="s", phase=3, phase3_step="skeleton")
    assert "骨架" in compute_narration(plan)


def test_phase3_lock_step():
    plan = TravelPlanState(session_id="s", phase=3, phase3_step="lock")
    assert "锁定" in compute_narration(plan)


def test_phase5():
    plan = TravelPlanState(session_id="s", phase=5)
    assert "日程" in compute_narration(plan)


def test_phase7():
    plan = TravelPlanState(session_id="s", phase=7)
    assert "检查清单" in compute_narration(plan)


def test_unrecognized_state_returns_none():
    plan = TravelPlanState(session_id="s", phase=99)
    assert compute_narration(plan) is None
