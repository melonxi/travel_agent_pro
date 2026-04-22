from memory.episode_slices import build_episode_slices
from memory.v3_models import ArchivedTripEpisode


def _episode(**overrides):
    base = {
        "id": "ep_kyoto_2026",
        "user_id": "default_user",
        "session_id": "s1",
        "trip_id": "trip_123",
        "destination": "京都",
        "dates": {"start": "2026-05-01", "end": "2026-05-05", "total_days": 5},
        "travelers": {"adults": 2},
        "budget": {"amount": 20000, "currency": "CNY"},
        "selected_skeleton": {
            "id": "balanced",
            "name": "轻松版",
            "summary": "节奏舒适，保留自由活动时间。",
        },
        "selected_transport": {"mode": "train", "arrival_station": "京都站"},
        "accommodation": {"area": "四条", "hotel": "町屋"},
        "daily_plan_summary": [
            {
                "day": 1,
                "date": "2026-05-01",
                "areas": ["锦市场"],
                "activity_count": 1,
                "notes": "抵达后轻松安排。",
            }
        ],
        "final_plan_summary": "这次京都之行选择了轻松节奏和町屋住宿。",
        "decision_log": [
            {"type": "accepted", "category": "skeleton", "value": {"id": "balanced"}},
            {"type": "rejected", "category": "hotel", "value": {"name": "商务连锁酒店"}},
            {"type": "rejected", "category": "activity", "value": {"name": "高强度打卡行程"}},
        ],
        "lesson_log": [
            {"kind": "pitfall", "content": "上午安排太满会让后半天疲劳。"},
            {"kind": "pitfall", "content": "交通衔接要给步行留余量。"},
        ],
        "created_at": "2026-04-19T00:00:00",
        "completed_at": "2026-04-19T00:00:00",
    }
    base.update(overrides)
    return ArchivedTripEpisode(**base)


def test_build_episode_slices_generates_v3_taxonomy():
    episode = _episode()

    slices = build_episode_slices(episode, now="2026-04-19T00:00:00")
    slice_types = {item.slice_type for item in slices}

    assert "itinerary_pattern" in slice_types
    assert "stay_choice" in slice_types
    assert "transport_choice" in slice_types
    assert "budget_signal" in slice_types
    assert "rejected_option" in slice_types
    assert "pitfall" in slice_types
    assert "accepted_pattern" not in slice_types
    assert all(item.source_episode_id == episode.id for item in slices)


def test_rejected_option_only_comes_from_rejected_decision_log_entries():
    episode = _episode(
        decision_log=[
            {"type": "accepted", "category": "hotel", "value": {"name": "町屋"}},
            {"type": "rejected", "category": "hotel", "value": {"name": "商务连锁酒店"}},
        ]
    )

    slices = build_episode_slices(episode, now="2026-04-19T00:00:00")

    rejected_slices = [item for item in slices if item.slice_type == "rejected_option"]
    assert len(rejected_slices) == 1
    assert "商务连锁酒店" in rejected_slices[0].content
    assert "町屋" not in rejected_slices[0].content


def test_pitfall_only_comes_from_lesson_log():
    episode = _episode(
        lesson_log=[{"kind": "pitfall", "content": "交通衔接要给步行留余量。"}],
        decision_log=[
            {"type": "rejected", "category": "hotel", "value": {"name": "商务连锁酒店"}},
            {"type": "rejected", "category": "pace", "value": {"note": "上午安排太满会累"}},
        ],
    )

    slices = build_episode_slices(episode, now="2026-04-19T00:00:00")

    pitfall_slices = [item for item in slices if item.slice_type == "pitfall"]
    assert len(pitfall_slices) == 1
    assert "交通衔接要给步行留余量" in pitfall_slices[0].content
    assert "上午安排太满会累" not in pitfall_slices[0].content


def test_build_episode_slices_truncates_long_content_to_180_chars():
    episode = _episode(
        selected_skeleton={"id": "balanced", "summary": "A" * 220},
        accommodation={"area": "B" * 220, "hotel": "C" * 220},
        selected_transport={"mode": "train", "notes": "D" * 220},
        decision_log=[{"type": "rejected", "category": "hotel", "value": {"name": "E" * 220}}],
        lesson_log=[{"kind": "pitfall", "content": "F" * 220}],
        final_plan_summary="G" * 220,
    )

    slices = build_episode_slices(episode, now="2026-04-19T00:00:00")

    assert all(len(item.content) <= 180 for item in slices)


def test_episode_slice_entities_do_not_store_unbounded_rendered_values():
    episode = _episode(
        selected_skeleton={f"k{i}": f"v{i}" for i in range(220)},
        accommodation={f"field{i}": f"value{i}" for i in range(220)},
        selected_transport={f"route{i}": f"value{i}" for i in range(220)},
        decision_log=[
            {"type": "rejected", "category": "hotel", "value": {f"field{i}": f"value{i}" for i in range(220)}}
        ],
        lesson_log=[{"kind": "pitfall", "content": "D" * 220}],
        budget={"amount": 20000, "currency": "CNY", **{f"meta{i}": f"value{i}" for i in range(220)}},
    )

    slices = build_episode_slices(episode, now="2026-04-19T00:00:00")

    for slice_ in slices:
        for key, value in slice_.entities.items():
            if isinstance(value, str):
                assert len(value) <= 180


def test_episode_slice_base_entities_are_bounded():
    episode = _episode(
        destination="京都" + "D" * 320,
        trip_id="trip_" + "T" * 320,
        session_id="session_" + "S" * 320,
    )

    slices = build_episode_slices(episode, now="2026-04-19T00:00:00")

    assert slices
    for slice_ in slices:
        for value in slice_.entities.values():
            if isinstance(value, str):
                assert len(value) <= 180
