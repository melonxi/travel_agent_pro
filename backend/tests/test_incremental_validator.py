from __future__ import annotations

from harness.validator import validate_incremental
from state.models import Activity, DayPlan, Location, TravelPlanState


def _activity(
    name: str,
    start: str,
    end: str,
    *,
    cost: float = 0,
    transport_duration_min: int = 0,
) -> Activity:
    return Activity(
        name=name,
        location=Location(lat=35.0, lng=139.0, name=name),
        start_time=start,
        end_time=end,
        category="景点",
        cost=cost,
        transport_duration_min=transport_duration_min,
    )


def test_budget_normal_write_without_daily_plans_passes() -> None:
    plan = TravelPlanState(session_id="s1")

    errors = validate_incremental(plan, "budget", {"total": 10_000})

    assert errors == []


def test_budget_write_detects_existing_daily_plan_overrun() -> None:
    plan = TravelPlanState(
        session_id="s1",
        daily_plans=[
            DayPlan(
                day=1,
                date="2026-05-01",
                activities=[_activity("浅草寺", "09:00", "10:00", cost=5_000)],
            )
        ],
    )

    errors = validate_incremental(plan, "budget", {"total": 1_000})

    assert any("超出预算" in error for error in errors)


def test_dates_write_triggers_feasibility_check() -> None:
    plan = TravelPlanState(session_id="s1", destination="东京")

    errors = validate_incremental(
        plan,
        "dates",
        {"start": "2026-05-01", "end": "2026-05-02"},
    )

    assert any("东京建议至少3天" in error for error in errors)


def test_daily_plans_write_detects_time_conflict() -> None:
    plan = TravelPlanState(
        session_id="s1",
        daily_plans=[
            DayPlan(
                day=1,
                date="2026-05-01",
                activities=[
                    _activity("浅草寺", "09:00", "10:00"),
                    _activity("上野公园", "10:05", "11:00", transport_duration_min=20),
                ],
            )
        ],
    )

    errors = validate_incremental(plan, "daily_plans", None)

    assert any("时间冲突" in error for error in errors)


def test_unmonitored_field_passes() -> None:
    plan = TravelPlanState(session_id="s1", destination="京都")

    errors = validate_incremental(plan, "destination", "大阪")

    assert errors == []
