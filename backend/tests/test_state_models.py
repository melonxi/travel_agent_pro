# backend/tests/test_state_models.py
from state.models import (
    TravelPlanState,
    DateRange,
    Travelers,
    Budget,
    Accommodation,
    DayPlan,
    Activity,
    Constraint,
    Preference,
    BacktrackEvent,
    Location,
)


def test_create_empty_plan():
    plan = TravelPlanState(session_id="sess_001")
    assert plan.phase == 1
    assert plan.phase3_step == "brief"
    assert plan.destination is None
    assert plan.daily_plans == []
    assert plan.backtrack_history == []
    assert plan.version == 1


def test_date_range():
    dr = DateRange(start="2026-04-10", end="2026-04-15")
    assert dr.total_days == 6


def test_activity():
    loc = Location(lat=35.0116, lng=135.7681, name="金阁寺")
    act = Activity(
        name="金阁寺",
        location=loc,
        start_time="09:00",
        end_time="10:30",
        category="景点",
    )
    assert act.duration_minutes == 90


def test_plan_serialization():
    plan = TravelPlanState(
        session_id="sess_001",
        destination="Kyoto",
        phase=3,
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        trip_brief={"goal": "慢旅行"},
        skeleton_plans=[{"id": "balanced"}],
        selected_skeleton_id="balanced",
    )
    d = plan.to_dict()
    assert d["session_id"] == "sess_001"
    assert d["destination"] == "Kyoto"
    assert d["phase3_step"] == "brief"
    assert d["selected_skeleton_id"] == "balanced"

    restored = TravelPlanState.from_dict(d)
    assert restored.session_id == "sess_001"
    assert restored.destination == "Kyoto"
    assert restored.trip_brief["goal"] == "慢旅行"
    assert restored.selected_skeleton_id == "balanced"


def test_location_from_dict_tolerates_string():
    """LLM 有时把 location 直接写成字符串名字；必须优雅降级。"""
    loc = Location.from_dict("明治神宫")
    assert loc.name == "明治神宫"
    assert loc.lat == 0.0
    assert loc.lng == 0.0


def test_location_from_dict_tolerates_none():
    loc = Location.from_dict(None)
    assert loc.name == ""
    assert loc.lat == 0.0
    assert loc.lng == 0.0


def test_location_from_dict_tolerates_partial_dict_with_address_alias():
    loc = Location.from_dict({"address": "东京都涩谷区"})
    assert loc.name == "东京都涩谷区"


def test_location_from_dict_tolerates_non_numeric_lat_lng():
    loc = Location.from_dict({"name": "x", "lat": "abc", "lng": None})
    assert loc.name == "x"
    assert loc.lat == 0.0
    assert loc.lng == 0.0


def test_activity_from_dict_with_string_location_and_missing_category():
    """LLM 常见错误：location 传字符串 + 省略 category。必须不崩。"""
    act = Activity.from_dict(
        {
            "name": "明治神宫",
            "location": "明治神宫",
            "start_time": "09:00",
            "end_time": "11:00",
        }
    )
    assert act.name == "明治神宫"
    assert isinstance(act.location, Location)
    assert act.location.name == "明治神宫"
    assert act.category == "activity"
    assert act.cost == 0
    assert act.transport_duration_min == 0


def test_activity_from_dict_raises_on_non_dict():
    import pytest

    with pytest.raises(TypeError):
        Activity.from_dict("not a dict")


def test_day_plan_from_dict_tolerates_loose_activities():
    dp = DayPlan.from_dict(
        {
            "day": "2",  # 字符串数字
            "date": "2026-05-02",
            "activities": [
                {"name": "清水寺", "location": "京都清水寺"},  # location 是字符串
            ],
        }
    )
    assert dp.day == 2
    assert len(dp.activities) == 1
    assert dp.activities[0].location.name == "京都清水寺"


def test_day_plan_from_dict_raises_on_non_dict():
    import pytest

    with pytest.raises(TypeError):
        DayPlan.from_dict(["not a dict"])


def test_plan_clear_downstream_from_phase_3():
    plan = TravelPlanState(
        session_id="sess_001",
        phase=5,
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        phase3_step="lock",
        trip_brief={"goal": "文化体验"},
        candidate_pool=[{"name": "清水寺"}],
        skeleton_plans=[{"id": "balanced"}],
        selected_skeleton_id="balanced",
        accommodation=Accommodation(area="祇园", hotel="Hotel Gion"),
        daily_plans=[DayPlan(day=1, date="2026-04-10", activities=[])],
        constraints=[Constraint(type="hard", description="预算 1 万")],
    )
    plan.clear_downstream(from_phase=3)
    assert plan.phase3_step == "brief"
    assert plan.trip_brief == {}
    assert plan.candidate_pool == []
    assert plan.skeleton_plans == []
    assert plan.selected_skeleton_id is None
    assert plan.accommodation is None
    assert plan.daily_plans == []
    assert plan.destination == "Kyoto"
    assert len(plan.constraints) == 1


def test_plan_serialization_roundtrips_deliverables():
    plan = TravelPlanState(
        session_id="sess_001",
        deliverables={
            "travel_plan_md": "travel_plan.md",
            "checklist_md": "checklist.md",
            "generated_at": "2026-04-18T22:30:00+08:00",
        },
    )

    data = plan.to_dict()
    assert data["deliverables"]["travel_plan_md"] == "travel_plan.md"

    restored = TravelPlanState.from_dict(data)
    assert restored.deliverables == plan.deliverables


def test_clear_downstream_from_phase_5_clears_deliverables():
    plan = TravelPlanState(
        session_id="sess_001",
        phase=7,
        deliverables={
            "travel_plan_md": "travel_plan.md",
            "checklist_md": "checklist.md",
            "generated_at": "2026-04-18T22:30:00+08:00",
        },
        daily_plans=[DayPlan(day=1, date="2026-04-10", activities=[])],
    )

    plan.clear_downstream(from_phase=5)
    assert plan.daily_plans == []
    assert plan.deliverables is None
