# backend/tests/test_reflection.py
import pytest

from agent.reflection import ReflectionInjector
from agent.types import Message, Role
from state.models import TravelPlanState, Preference, Constraint


@pytest.fixture
def injector():
    return ReflectionInjector()


def _make_plan(**overrides) -> TravelPlanState:
    defaults = {"session_id": "s1", "phase": 3, "destination": "京都"}
    defaults.update(overrides)
    return TravelPlanState(**defaults)


def test_phase3_lock_triggers_reflection(injector):
    plan = _make_plan(
        phase3_step="lock",
        preferences=[Preference(category="节奏", value="轻松", source="user")],
        constraints=[Constraint(type="hard", description="不坐红眼航班", source="user")],
    )
    result = injector.check_and_inject(
        messages=[], plan=plan, prev_step="skeleton"
    )
    assert result is not None
    assert "自检" in result
    assert "轻松" in result
    assert "红眼航班" in result


def test_phase3_lock_does_not_trigger_twice(injector):
    plan = _make_plan(
        phase3_step="lock",
        preferences=[Preference(category="节奏", value="轻松", source="user")],
    )
    first = injector.check_and_inject(messages=[], plan=plan, prev_step="skeleton")
    second = injector.check_and_inject(messages=[], plan=plan, prev_step="skeleton")
    assert first is not None
    assert second is None


def test_no_trigger_when_step_unchanged(injector):
    plan = _make_plan(phase3_step="skeleton")
    result = injector.check_and_inject(messages=[], plan=plan, prev_step="skeleton")
    assert result is None


def test_phase5_complete_triggers_reflection(injector):
    from state.models import DayPlan, DateRange
    plan = _make_plan(
        phase=5,
        dates=DateRange(start="2026-04-10", end="2026-04-12"),
        daily_plans=[
            DayPlan(day=1, date="2026-04-10"),
            DayPlan(day=2, date="2026-04-11"),
        ],
        preferences=[Preference(category="节奏", value="密集", source="user")],
    )
    result = injector.check_and_inject(messages=[], plan=plan, prev_step=None)
    assert result is not None
    assert "自检" in result
    assert "密集" in result


def test_phase5_incomplete_does_not_trigger(injector):
    from state.models import DayPlan, DateRange
    plan = _make_plan(
        phase=5,
        dates=DateRange(start="2026-04-10", end="2026-04-12"),
        daily_plans=[DayPlan(day=1, date="2026-04-10")],  # only 1 of 2
    )
    result = injector.check_and_inject(messages=[], plan=plan, prev_step=None)
    assert result is None


def test_no_preferences_still_triggers_with_placeholder(injector):
    plan = _make_plan(phase3_step="lock")
    result = injector.check_and_inject(messages=[], plan=plan, prev_step="skeleton")
    assert result is not None
    assert "自检" in result
