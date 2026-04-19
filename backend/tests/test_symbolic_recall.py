from memory.symbolic_recall import (
    build_recall_query,
    rank_episode_slices,
    rank_profile_items,
    should_trigger_memory_recall,
)
from memory.v3_models import EpisodeSlice, MemoryProfileItem, UserMemoryProfile


def _profile_item(**overrides):
    base = dict(
        id="constraints:flight:avoid_red_eye",
        domain="flight",
        key="avoid_red_eye",
        value=True,
        polarity="avoid",
        stability="explicit_declared",
        confidence=0.95,
        status="active",
        context={},
        applicability="适用于所有旅行。",
        recall_hints={"domains": ["flight"], "keywords": ["红眼航班"]},
        source_refs=[],
        created_at="2026-04-19T00:00:00",
        updated_at="2026-04-19T00:00:00",
    )
    base.update(overrides)
    return MemoryProfileItem(**base)


def _slice(**overrides):
    base = dict(
        id="slice_ep_kyoto_01",
        user_id="u1",
        source_episode_id="ep_kyoto",
        source_trip_id="trip_1",
        slice_type="accommodation_decision",
        domains=["hotel", "accommodation"],
        entities={"destination": "京都"},
        keywords=["住宿", "酒店"],
        content="上次京都住町屋。",
        applicability="仅供住宿偏好参考。",
        created_at="2026-04-19T00:00:00",
    )
    base.update(overrides)
    return EpisodeSlice(**base)


def test_history_question_triggers_recall():
    assert should_trigger_memory_recall("我上次去京都住哪里？") is True


def test_current_trip_question_does_not_trigger_recall():
    assert should_trigger_memory_recall("这次预算多少？") is False


def test_mixed_current_and_history_query_still_triggers_recall():
    query = build_recall_query("这次和上次一样住哪里？")

    assert query.needs_memory is True
    assert query.include_slices is True


def test_hotel_query_maps_domains_and_destination():
    query = build_recall_query("我上次去京都住哪里？")
    assert query.needs_memory is True
    assert "hotel" in query.domains
    assert query.entities["destination"] == "京都"
    assert query.include_slices is True


def test_long_term_preference_query_includes_profile():
    query = build_recall_query("我是不是说过不坐红眼航班？")
    assert query.include_profile is True
    assert "flight" in query.domains


def test_lodging_preference_query_includes_profile_and_hotel_domain():
    query = build_recall_query("我以前不住青旅吗？")

    assert query.needs_memory is True
    assert query.include_profile is True
    assert "hotel" in query.domains or "accommodation" in query.domains


def test_rank_profile_items_prefers_constraints_over_hypotheses():
    query = build_recall_query("我是不是说过不坐红眼航班？")
    profile = UserMemoryProfile(
        schema_version=3,
        user_id="u1",
        constraints=[
            _profile_item(
                id="constraints:flight:avoid_red_eye",
                domain="flight",
                key="avoid_red_eye",
                value=True,
                stability="explicit_declared",
            )
        ],
        rejections=[],
        stable_preferences=[],
        preference_hypotheses=[
            _profile_item(
                id="preference_hypotheses:flight:avoid_red_eye",
                domain="flight",
                key="avoid_red_eye",
                value=True,
                stability="hypothesis",
                confidence=0.6,
                recall_hints={"domains": ["flight"], "keywords": ["红眼航班"]},
            )
        ],
    )

    ranked = rank_profile_items(query, profile)

    assert [bucket for bucket, _, _ in ranked] == [
        "constraints",
        "preference_hypotheses",
    ]
    assert ranked[0][2].startswith("exact domain match")


def test_rank_profile_items_prefers_rejections_over_stable_preferences():
    query = build_recall_query("我是不是说过不住青旅？")
    profile = UserMemoryProfile(
        schema_version=3,
        user_id="u1",
        constraints=[],
        rejections=[
            _profile_item(
                id="rejections:hotel:avoid_hostel",
                domain="hotel",
                key="avoid_hostel",
                value="青旅",
                polarity="avoid",
                stability="explicit_declared",
                recall_hints={"domains": ["hotel"], "keywords": ["青旅"]},
            )
        ],
        stable_preferences=[
            _profile_item(
                id="stable_preferences:hotel:preferred_room",
                domain="hotel",
                key="preferred_room",
                value="双人间",
                polarity="prefer",
                stability="stable",
                recall_hints={"domains": ["hotel"], "keywords": ["青旅"]},
            )
        ],
        preference_hypotheses=[],
    )

    ranked = rank_profile_items(query, profile)

    assert [bucket for bucket, _, _ in ranked] == [
        "rejections",
        "stable_preferences",
    ]
    assert ranked[0][2].startswith("exact domain match")


def test_rank_episode_slices_prefers_exact_destination_domain_matches():
    query = build_recall_query("我上次去京都住哪里？")
    slices = [
        _slice(
            id="slice_keyword_only",
            entities={"destination": "", "trip_id": "trip_1"},
            keywords=["住宿", "京都"],
            content="住过一家町屋。",
        ),
        _slice(
            id="slice_exact",
            domains=["hotel"],
            entities={"destination": "京都", "trip_id": "trip_1"},
            keywords=["住宿", "酒店"],
            content="上次京都住町屋。",
        ),
    ]

    ranked = rank_episode_slices(query, slices)

    assert [slice_.id for slice_, _ in ranked] == ["slice_exact", "slice_keyword_only"]
    assert ranked[0][1].startswith("exact destination match")


def test_current_trip_question_produces_no_recall_hits():
    query = build_recall_query("这次预算多少？")
    profile = UserMemoryProfile.empty("u1")
    slices = [_slice()]

    assert query.needs_memory is False
    assert rank_profile_items(query, profile) == []
    assert rank_episode_slices(query, slices) == []
