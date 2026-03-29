# backend/tests/test_harness_validator.py
import pytest

from harness.validator import validate_hard_constraints
from state.models import (
    TravelPlanState,
    DateRange,
    Budget,
    DayPlan,
    Activity,
    Location,
    Preference,
)


def _make_activity(name, start, end, lat=35.0, lng=135.7, cost=0):
    return Activity(
        name=name,
        location=Location(lat=lat, lng=lng, name=name),
        start_time=start,
        end_time=end,
        category="景点",
        cost=cost,
        transport_duration_min=0,
    )


def test_no_errors_on_valid_plan():
    plan = TravelPlanState(
        session_id="s1",
        dates=DateRange(start="2026-04-10", end="2026-04-12"),
        budget=Budget(total=10000),
        daily_plans=[
            DayPlan(
                day=1,
                date="2026-04-10",
                activities=[
                    _make_activity("金阁寺", "09:00", "10:30", cost=500),
                    _make_activity("龙安寺", "11:00", "12:00", cost=500),
                ],
            ),
            DayPlan(
                day=2,
                date="2026-04-11",
                activities=[
                    _make_activity("伏见稻荷", "09:00", "11:00"),
                ],
            ),
        ],
    )
    errors = validate_hard_constraints(plan)
    assert errors == []


def test_time_conflict():
    plan = TravelPlanState(
        session_id="s1",
        dates=DateRange(start="2026-04-10", end="2026-04-11"),
        daily_plans=[
            DayPlan(
                day=1,
                date="2026-04-10",
                activities=[
                    _make_activity("A", "09:00", "10:30"),
                    _make_activity("B", "10:00", "11:00"),  # overlaps with A
                ],
            ),
        ],
    )
    errors = validate_hard_constraints(plan)
    assert len(errors) == 1
    assert "时间冲突" in errors[0] or "A" in errors[0]


def test_budget_exceeded():
    plan = TravelPlanState(
        session_id="s1",
        dates=DateRange(start="2026-04-10", end="2026-04-11"),
        budget=Budget(total=1000),
        daily_plans=[
            DayPlan(
                day=1,
                date="2026-04-10",
                activities=[
                    _make_activity("A", "09:00", "10:00", cost=600),
                    _make_activity("B", "11:00", "12:00", cost=600),
                ],
            ),
        ],
    )
    errors = validate_hard_constraints(plan)
    assert any("预算" in e for e in errors)


def test_too_many_days():
    plan = TravelPlanState(
        session_id="s1",
        dates=DateRange(start="2026-04-10", end="2026-04-12"),  # 2 days
        daily_plans=[
            DayPlan(day=i, date=f"2026-04-{10 + i}")
            for i in range(3)  # 3 plans
        ],
    )
    errors = validate_hard_constraints(plan)
    assert any("天数" in e for e in errors)


def test_no_errors_on_empty_plan():
    plan = TravelPlanState(session_id="s1")
    errors = validate_hard_constraints(plan)
    assert errors == []
