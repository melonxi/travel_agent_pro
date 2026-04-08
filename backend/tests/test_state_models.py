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
    assert dr.total_days == 5


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
