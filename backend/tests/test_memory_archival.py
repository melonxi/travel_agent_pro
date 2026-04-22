from memory.archival import build_archived_trip_episode
from state.models import (
    Accommodation,
    Activity,
    Budget,
    DateRange,
    DayPlan,
    Location,
    Travelers,
    TravelPlanState,
)


def _plan() -> TravelPlanState:
    plan = TravelPlanState(
        session_id="s1",
        trip_id="trip_123",
        phase=7,
        destination="京都",
        dates=DateRange(start="2026-05-01", end="2026-05-05"),
        travelers=Travelers(adults=2, children=0),
        budget=Budget(total=20000, currency="CNY"),
        skeleton_plans=[
            {"id": "slow", "name": "慢游", "summary": "东山、四条、岚山慢节奏。"},
        ],
        selected_skeleton_id="slow",
        selected_transport={"mode": "train", "arrival_station": "京都站"},
        accommodation=Accommodation(area="四条", hotel="町屋"),
        daily_plans=[
            DayPlan(
                day=1,
                date="2026-05-01",
                activities=[
                    Activity(
                        name="锦市场",
                        location=Location(lat=35.005, lng=135.764, name="锦市场"),
                        start_time="10:00",
                        end_time="12:00",
                        category="food",
                    )
                ],
                notes="抵达后轻松安排。",
            )
        ],
    )
    plan.decision_events = [
        {
            "type": "accepted",
            "category": "skeleton",
            "value": {"id": "slow"},
            "reason": "selected",
            "timestamp": "2026-05-05T00:00:00+00:00",
        },
        {
            "type": "rejected",
            "category": "hotel",
            "value": {"name": "商务连锁酒店"},
            "reason": "用户更想住町屋",
            "timestamp": "2026-05-05T00:00:00+00:00",
        },
    ]
    plan.lesson_events = [
        {
            "kind": "pitfall",
            "content": "岚山返程要避开晚高峰。",
            "timestamp": "2026-05-05T00:00:00+00:00",
        }
    ]
    return plan


def test_build_archived_trip_episode_uses_v3_state_only():
    episode = build_archived_trip_episode(
        user_id="default_user",
        session_id="s1",
        plan=_plan(),
        now="2026-05-05T00:00:00+00:00",
    )

    assert episode.id == "ep_trip_123"
    assert episode.user_id == "default_user"
    assert episode.trip_id == "trip_123"
    assert episode.destination == "京都"
    assert episode.dates == {"start": "2026-05-01", "end": "2026-05-05", "total_days": 5}
    assert episode.selected_skeleton == {
        "id": "slow",
        "name": "慢游",
        "summary": "东山、四条、岚山慢节奏。",
    }
    assert episode.selected_transport == {"mode": "train", "arrival_station": "京都站"}
    assert episode.accommodation == {"area": "四条", "hotel": "町屋"}
    assert episode.daily_plan_summary == [
        {
            "day": 1,
            "date": "2026-05-01",
            "areas": ["锦市场"],
            "activity_count": 1,
            "notes": "抵达后轻松安排。",
        }
    ]
    assert any(
        item["type"] == "accepted" and item["category"] == "skeleton"
        for item in episode.decision_log
    )
    assert any(
        item["type"] == "rejected" and item["category"] == "hotel"
        for item in episode.decision_log
    )
    assert episode.lesson_log == _plan().lesson_events
