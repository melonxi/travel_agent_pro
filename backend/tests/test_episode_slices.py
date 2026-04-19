from memory.models import TripEpisode
from memory.episode_slices import build_episode_slices


def _episode(**overrides):
    base = {
        "id": "ep_kyoto_2026",
        "user_id": "default_user",
        "session_id": "s1",
        "trip_id": "trip_123",
        "destination": "京都",
        "dates": "2026-05-01 to 2026-05-05",
        "travelers": {"adults": 2},
        "budget": {"amount": 20000, "currency": "CNY"},
        "selected_skeleton": {
            "id": "balanced",
            "name": "轻松版",
            "summary": "节奏舒适，保留自由活动时间。",
        },
        "final_plan_summary": "这次京都之行选择了轻松节奏和町屋住宿。",
        "accepted_items": [
            {"type": "skeleton", "id": "balanced", "name": "轻松版"},
            {"type": "hotel", "name": "町屋"},
        ],
        "rejected_items": [
            {"type": "hotel", "name": "商务连锁酒店"},
            {"type": "activity", "name": "高强度打卡行程"},
        ],
        "lessons": [
            "上午安排太满会让后半天疲劳。",
            "交通衔接要给步行留余量。",
        ],
        "satisfaction": 5,
        "created_at": "2026-04-19T00:00:00",
    }
    base.update(overrides)
    return TripEpisode(**base)


def test_build_episode_slices_generates_expected_core_slices():
    episode = _episode()

    slices = build_episode_slices(episode, now="2026-04-19T00:00:00")

    assert any(item.slice_type == "accepted_pattern" for item in slices)
    assert any(item.slice_type == "pitfall" for item in slices)
    assert all(item.source_episode_id == episode.id for item in slices)
    assert all(
        "京都" in item.entities.values() or item.entities.get("destination") == "京都"
        for item in slices
    )
    assert len(slices) <= 8


def test_build_episode_slices_creates_rejected_option():
    episode = _episode()

    slices = build_episode_slices(episode, now="2026-04-19T00:00:00")

    rejected_slices = [item for item in slices if item.slice_type == "rejected_option"]
    assert len(rejected_slices) == 2
    assert rejected_slices[0].id == "slice_ep_kyoto_2026_rejected_option_01"
    assert "商务连锁酒店" in rejected_slices[0].content


def test_build_episode_slices_creates_budget_signal():
    episode = _episode()

    slices = build_episode_slices(episode, now="2026-04-19T00:00:00")

    budget_slices = [item for item in slices if item.slice_type == "budget_signal"]
    assert len(budget_slices) == 1
    assert budget_slices[0].domains == ["budget"]
    assert "预算" in budget_slices[0].content


def test_build_episode_slices_truncates_long_content_to_180_chars():
    episode = _episode(
        selected_skeleton={
            "id": "balanced",
            "summary": "A" * 220,
        },
        accepted_items=[{"type": "skeleton", "id": "balanced", "summary": "B" * 220}],
        rejected_items=[{"type": "hotel", "name": "C" * 220}],
        lessons=["D" * 220],
        final_plan_summary="E" * 220,
    )

    slices = build_episode_slices(episode, now="2026-04-19T00:00:00")

    assert all(len(item.content) <= 180 for item in slices)


def test_build_episode_slices_handles_missing_optional_lists():
    episode = TripEpisode(
        id="ep_missing_lists",
        user_id="default_user",
        session_id="s1",
        trip_id="trip_123",
        destination="京都",
        dates="2026-05-01 to 2026-05-05",
        travelers={"adults": 2},
        budget={"amount": 20000, "currency": "CNY"},
        selected_skeleton={"id": "balanced", "name": "轻松版"},
        final_plan_summary="这次京都之行选择了轻松节奏。",
        accepted_items=None,  # type: ignore[arg-type]
        rejected_items=None,  # type: ignore[arg-type]
        lessons=None,  # type: ignore[arg-type]
        satisfaction=5,
        created_at="2026-04-19T00:00:00",
    )

    slices = build_episode_slices(episode, now="2026-04-19T00:00:00")

    assert any(item.slice_type == "accepted_pattern" for item in slices)
    assert any(item.slice_type == "budget_signal" for item in slices)


def test_episode_slice_entities_do_not_store_unbounded_rendered_values():
    episode = _episode(
        rejected_items=[
            {"type": "hotel", "name": "C" * 220},
        ],
        lessons=["D" * 220],
    )

    slices = build_episode_slices(episode, now="2026-04-19T00:00:00")

    for slice_ in slices:
        for key, value in slice_.entities.items():
            if key == "destination":
                continue

            def _walk(node):
                if isinstance(node, str):
                    assert len(node) <= 180
                elif isinstance(node, dict):
                    for nested_key, nested_value in node.items():
                        if nested_key == "destination":
                            continue
                        _walk(nested_value)
                elif isinstance(node, (list, tuple)):
                    for item in node:
                        _walk(item)

            _walk(value)
