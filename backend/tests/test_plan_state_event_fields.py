from state.models import TravelPlanState


def test_plan_state_roundtrip_preserves_event_fields():
    plan = TravelPlanState(session_id="s1")
    plan.decision_events.append(
        {
            "type": "rejected",
            "category": "hotel",
            "value": {"name": "商务连锁"},
            "reason": "用户更想住町屋",
            "timestamp": "2026-04-22T10:00:00Z",
        }
    )
    plan.lesson_events.append(
        {
            "kind": "pitfall",
            "content": "上午排太满下午会累",
            "timestamp": "2026-04-22T18:00:00Z",
        }
    )

    restored = TravelPlanState.from_dict(plan.to_dict())

    assert restored.decision_events == plan.decision_events
    assert restored.lesson_events == plan.lesson_events
