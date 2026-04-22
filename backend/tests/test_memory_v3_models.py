from memory.v3_models import (
    ArchivedTripEpisode,
    EpisodeSlice,
    MemoryAuditEvent,
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


def test_archived_trip_episode_round_trip():
    episode = ArchivedTripEpisode(
        id="ep_trip_123",
        user_id="u1",
        session_id="s1",
        trip_id="trip_123",
        destination="京都",
        dates={"start": "2026-05-01", "end": "2026-05-05", "total_days": 5},
        travelers={"adults": 2, "children": 0},
        budget={"total": 20000, "currency": "CNY"},
        selected_skeleton={"id": "slow", "name": "慢游"},
        selected_transport={"mode": "train"},
        accommodation={"area": "四条", "hotel": "町屋"},
        daily_plan_summary=[],
        final_plan_summary="京都慢游。",
        decision_log=[{"type": "accepted", "category": "skeleton", "value": {"id": "slow"}}],
        lesson_log=[
            {
                "kind": "pitfall",
                "content": "交通衔接要留余量。",
                "timestamp": "2026-05-05T00:00:00+00:00",
            }
        ],
        created_at="2026-05-05T00:00:00+00:00",
        completed_at="2026-05-05T00:00:00+00:00",
    )

    restored = ArchivedTripEpisode.from_dict(episode.to_dict())

    assert restored == episode


def test_memory_audit_event_round_trip():
    event = MemoryAuditEvent(
        id="evt_1",
        user_id="u1",
        session_id="s1",
        event_type="reject",
        object_type="phase_output",
        object_payload={"to_phase": 3},
        reason_text="用户要求回退",
        created_at="2026-05-05T00:00:00+00:00",
    )

    restored = MemoryAuditEvent.from_dict(event.to_dict())

    assert restored == event


def test_null_collection_fields_deserialize_safely():
    profile_item = MemoryProfileItem.from_dict(
        {
            "id": "constraints:flight:avoid_red_eye",
            "domain": "flight",
            "key": "avoid_red_eye",
            "value": True,
            "polarity": "avoid",
            "stability": "explicit_declared",
            "confidence": 0.95,
            "status": "active",
            "context": None,
            "applicability": "适用于所有旅行。",
            "recall_hints": None,
            "source_refs": None,
            "created_at": "2026-04-19T00:00:00",
            "updated_at": "2026-04-19T00:00:00",
        }
    )
    working = WorkingMemoryItem.from_dict(
        {
            "id": "wm_001",
            "phase": 3,
            "kind": "temporary_rejection",
            "domains": None,
            "content": "用户说先别考虑迪士尼。",
            "reason": "当前候选筛选阶段避免重复推荐。",
            "status": "active",
            "expires": None,
            "created_at": "2026-04-19T00:00:00",
        }
    )
    memory = SessionWorkingMemory.from_dict(
        {
            "schema_version": 1,
            "user_id": "default_user",
            "session_id": "s1",
            "trip_id": "trip_123",
            "items": None,
        }
    )
    slice_ = EpisodeSlice.from_dict(
        {
            "id": "slice_001",
            "user_id": "default_user",
            "source_episode_id": "ep_kyoto",
            "source_trip_id": "trip_123",
            "slice_type": "accommodation_decision",
            "domains": None,
            "entities": None,
            "keywords": None,
            "content": "上次京都选择町屋。",
            "applicability": "仅供住宿偏好参考。",
            "created_at": "2026-04-19T00:00:00",
        }
    )

    assert profile_item.context == {}
    assert profile_item.recall_hints == {}
    assert profile_item.source_refs == []
    assert working.domains == []
    assert working.expires == {}
    assert memory.items == []
    assert slice_.domains == []
    assert slice_.entities == {}
    assert slice_.keywords == []


def test_normalization_distinguishes_scalar_and_container_values():
    scalar = MemoryProfileItem(
        id="",
        domain="hotel",
        key="avoid",
        value="x",
        polarity="avoid",
        stability="explicit_declared",
        confidence=0.9,
        status="active",
        context={"a": "x"},
        applicability="",
        recall_hints={},
        source_refs=[],
        created_at="",
        updated_at="",
    )
    list_value = MemoryProfileItem(
        id="",
        domain="hotel",
        key="avoid",
        value=["x"],
        polarity="avoid",
        stability="explicit_declared",
        confidence=0.9,
        status="active",
        context={"a": ["x"]},
        applicability="",
        recall_hints={},
        source_refs=[],
        created_at="",
        updated_at="",
    )

    assert generate_profile_item_id("rejections", scalar) == "rejections:hotel:avoid:x"
    assert generate_profile_item_id("rejections", list_value) == "rejections:hotel:avoid:[\"x\"]"
    assert generate_profile_item_id("preference_hypotheses", scalar) != generate_profile_item_id(
        "preference_hypotheses", list_value
    )
