from memory.v3_models import (
    EpisodeSlice,
    MemoryProfileItem,
    SessionWorkingMemory,
    WorkingMemoryItem,
    generate_profile_item_id,
)


def test_profile_item_round_trips_with_recall_hints():
    item = MemoryProfileItem(
        id="",
        domain="flight",
        key="avoid_red_eye",
        value=True,
        polarity="avoid",
        stability="explicit_declared",
        confidence=0.95,
        status="active",
        context={},
        applicability="适用于所有旅行，除非用户明确临时允许。",
        recall_hints={"domains": ["flight"], "keywords": ["红眼航班"], "priority": "high"},
        source_refs=[{"kind": "message", "session_id": "s1", "quote": "以后不坐红眼航班"}],
        created_at="2026-04-19T00:00:00",
        updated_at="2026-04-19T00:00:00",
    )
    item.id = generate_profile_item_id("constraints", item)

    restored = MemoryProfileItem.from_dict(item.to_dict())

    assert restored.id == "constraints:flight:avoid_red_eye"
    assert restored.recall_hints["keywords"] == ["红眼航班"]
    assert restored.stability == "explicit_declared"


def test_rejection_id_includes_value():
    first = MemoryProfileItem(
        id="",
        domain="hotel",
        key="avoid",
        value="青旅",
        polarity="avoid",
        stability="explicit_declared",
        confidence=0.9,
        status="active",
        context={},
        applicability="适用于所有旅行。",
        recall_hints={},
        source_refs=[],
        created_at="2026-04-19T00:00:00",
        updated_at="2026-04-19T00:00:00",
    )
    second = MemoryProfileItem.from_dict({**first.to_dict(), "value": "红眼航班"})

    assert generate_profile_item_id("rejections", first) == "rejections:hotel:avoid:青旅"
    assert generate_profile_item_id("rejections", second) == "rejections:hotel:avoid:红眼航班"


def test_working_memory_round_trip():
    item = WorkingMemoryItem(
        id="wm_001",
        phase=3,
        kind="temporary_rejection",
        domains=["attraction"],
        content="用户说先别考虑迪士尼。",
        reason="当前候选筛选阶段避免重复推荐。",
        status="active",
        expires={"on_session_end": True, "on_trip_change": True, "on_phase_exit": False},
        created_at="2026-04-19T00:00:00",
    )
    memory = SessionWorkingMemory(
        schema_version=1,
        user_id="default_user",
        session_id="s1",
        trip_id="trip_123",
        items=[item],
    )

    restored = SessionWorkingMemory.from_dict(memory.to_dict())

    assert restored.items[0].kind == "temporary_rejection"
    assert restored.items[0].expires["on_trip_change"] is True


def test_episode_slice_round_trip():
    slice_ = EpisodeSlice(
        id="slice_001",
        user_id="default_user",
        source_episode_id="ep_kyoto",
        source_trip_id="trip_123",
        slice_type="accommodation_decision",
        domains=["hotel", "accommodation"],
        entities={"destination": "京都"},
        keywords=["住宿", "酒店"],
        content="上次京都选择町屋。",
        applicability="仅供住宿偏好参考。",
        created_at="2026-04-19T00:00:00",
    )

    restored = EpisodeSlice.from_dict(slice_.to_dict())

    assert restored.source_episode_id == "ep_kyoto"
    assert restored.keywords == ["住宿", "酒店"]
