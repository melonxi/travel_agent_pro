from state.models import TravelPlanState
from state.plan_writers import (
    record_phase7_lesson,
    replace_all_daily_plans,
    write_accommodation,
    write_selected_skeleton_id,
    write_selected_transport,
)


def test_write_selected_skeleton_id_appends_decision_event():
    plan = TravelPlanState(session_id="s1")

    write_selected_skeleton_id(plan, "slow")

    assert any(ev["category"] == "skeleton" for ev in plan.decision_events)


def test_write_selected_transport_appends_decision_event():
    plan = TravelPlanState(session_id="s1")

    write_selected_transport(plan, {"mode": "train"})

    assert any(ev["category"] == "transport" for ev in plan.decision_events)


def test_write_accommodation_appends_decision_event():
    plan = TravelPlanState(session_id="s1")

    write_accommodation(plan, "四条", "町屋")

    assert any(ev["category"] == "accommodation" for ev in plan.decision_events)


def test_replace_all_daily_plans_appends_decision_event():
    plan = TravelPlanState(session_id="s1")

    replace_all_daily_plans(
        plan,
        [{"day": 1, "date": "2026-05-01", "activities": [], "notes": "轻松安排"}],
    )

    assert any(ev["category"] == "daily_plan" for ev in plan.decision_events)


def test_record_phase7_lesson_appends_lesson_event():
    plan = TravelPlanState(session_id="s1")

    record_phase7_lesson(
        plan,
        kind="pitfall",
        note="上午排太满下午会累",
        now="2026-04-22T18:00:00Z",
    )

    assert plan.lesson_events == [
        {
            "kind": "pitfall",
            "content": "上午排太满下午会累",
            "timestamp": "2026-04-22T18:00:00Z",
        }
    ]
