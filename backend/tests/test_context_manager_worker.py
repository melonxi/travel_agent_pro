# backend/tests/test_context_manager_worker.py
from context.manager import ContextManager
from state.models import (
    TravelPlanState,
    DateRange,
    Travelers,
    Accommodation,
    Preference,
    Constraint,
)


def _make_plan() -> TravelPlanState:
    plan = TravelPlanState(session_id="test-worker-ctx")
    plan.destination = "东京"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-05")
    plan.travelers = Travelers(adults=2)
    plan.trip_brief = {"goal": "文化探索", "pace": "balanced", "departure_city": "上海"}
    plan.accommodation = Accommodation(area="新宿")
    plan.preferences = [Preference(key="must_do", value="浅草寺")]
    plan.constraints = [Constraint(type="hard", description="不去迪士尼")]
    return plan


def test_build_worker_context_returns_dict():
    cm = ContextManager()
    plan = _make_plan()
    ctx = cm.build_worker_context(plan)
    assert isinstance(ctx, dict)
    assert "destination" in ctx
    assert ctx["destination"] == "东京"
    assert "trip_brief" in ctx
    assert "accommodation_area" in ctx


def test_build_worker_context_excludes_mutable_state():
    """Worker context 不应包含 daily_plans 等可变状态。"""
    cm = ContextManager()
    plan = _make_plan()
    ctx = cm.build_worker_context(plan)
    assert "daily_plans" not in ctx
    assert "skeleton_plans" not in ctx
