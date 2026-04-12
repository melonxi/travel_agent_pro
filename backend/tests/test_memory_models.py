from memory.models import (
    MemoryCandidate,
    MemoryItem,
    MemorySource,
    TripEpisode,
    generate_memory_id,
)


def test_preference_id_is_stable_for_same_key():
    first = generate_memory_id(
        user_id="u1",
        type="preference",
        domain="pace",
        key="preferred_pace",
        scope="global",
    )
    second = generate_memory_id(
        user_id="u1",
        type="preference",
        domain="pace",
        key="preferred_pace",
        scope="global",
    )
    assert first == second
    assert len(first) == 16


def test_rejection_id_includes_value():
    red_eye = generate_memory_id(
        user_id="u1",
        type="rejection",
        domain="flight",
        key="avoid",
        scope="global",
        value="red_eye",
    )
    layover = generate_memory_id(
        user_id="u1",
        type="rejection",
        domain="flight",
        key="avoid",
        scope="global",
        value="long_layover",
    )
    assert red_eye != layover


def test_rejection_id_normalizes_string_value():
    compact = generate_memory_id(
        user_id="u1",
        type="rejection",
        domain="flight",
        key="avoid",
        scope="global",
        value="red_eye",
    )
    spaced = generate_memory_id(
        user_id="u1",
        type="rejection",
        domain="flight",
        key="avoid",
        scope="global",
        value=" Red_Eye ",
    )
    assert compact == spaced


def test_memory_item_round_trip():
    item = MemoryItem(
        id="mem123",
        user_id="u1",
        type="preference",
        domain="food",
        key="dietary_restrictions",
        value=["no_spicy"],
        scope="global",
        polarity="avoid",
        confidence=0.9,
        status="active",
        source=MemorySource(kind="message", session_id="s1", quote="我不吃辣"),
        created_at="2026-04-11T00:00:00",
        updated_at="2026-04-11T00:00:00",
    )
    loaded = MemoryItem.from_dict(item.to_dict())
    assert loaded == item


def test_candidate_to_dict_preserves_risk_and_evidence():
    candidate = MemoryCandidate(
        type="preference",
        domain="flight",
        key="avoid_red_eye",
        value=True,
        scope="global",
        polarity="avoid",
        confidence=0.95,
        risk="medium",
        evidence="以后我都不坐红眼航班",
        reason="用户明确表达长期偏好",
    )
    data = candidate.to_dict()
    assert data["risk"] == "medium"
    assert data["evidence"] == "以后我都不坐红眼航班"


def test_trip_episode_round_trip():
    episode = TripEpisode(
        id="ep1",
        user_id="u1",
        session_id="s1",
        trip_id="trip1",
        destination="东京",
        dates="2026-05",
        travelers={"adults": 2},
        budget={"total": 30000, "currency": "CNY"},
        selected_skeleton={"id": "sk1"},
        final_plan_summary="东京轻松五日游",
        accepted_items=[{"type": "skeleton", "id": "sk1"}],
        rejected_items=[{"type": "poi", "name": "迪士尼"}],
        lessons=["用户不喜欢排队时间长的热门景点"],
        satisfaction=None,
        created_at="2026-04-11T00:00:00",
    )
    assert TripEpisode.from_dict(episode.to_dict()) == episode
