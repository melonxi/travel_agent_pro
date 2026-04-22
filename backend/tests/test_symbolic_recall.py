from memory.recall_query import RecallRetrievalPlan
from memory.symbolic_recall import (
    heuristic_retrieval_plan_from_message,
    rank_episode_slices,
    rank_profile_items,
    should_trigger_memory_recall,
)
from memory.retrieval_candidates import RecallCandidate
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


def _plan(**overrides):
    base = dict(
        source="hybrid_history",
        buckets=["constraints", "rejections", "stable_preferences"],
        domains=["hotel"],
        entities={"destination": "京都"},
        keywords=["住宿", "青旅"],
        aliases=["住哪里"],
        strictness="soft",
        top_k=5,
        reason="test plan",
    )
    base.update(overrides)
    return RecallRetrievalPlan(**base)


def test_history_question_triggers_recall():
    assert should_trigger_memory_recall("我上次去京都住哪里？") is True


def test_current_trip_question_does_not_trigger_recall():
    assert should_trigger_memory_recall("这次预算多少？") is False


def test_mixed_current_and_history_query_still_triggers_recall():
    plan = heuristic_retrieval_plan_from_message("这次和上次一样住哪里？")

    assert plan.source == "hybrid_history"


def test_hotel_query_maps_domains_and_destination():
    plan = heuristic_retrieval_plan_from_message("我上次去京都住哪里？")
    assert "hotel" in plan.domains
    assert plan.entities["destination"] == "京都"
    assert plan.source == "hybrid_history"


def test_long_term_preference_query_includes_profile():
    plan = heuristic_retrieval_plan_from_message("我是不是说过不坐红眼航班？")
    assert plan.source == "profile"
    assert "flight" in plan.domains


def test_lodging_preference_query_includes_profile_and_hotel_domain():
    plan = heuristic_retrieval_plan_from_message("我以前不住青旅吗？")

    assert plan.source == "profile"
    assert "hotel" in plan.domains or "accommodation" in plan.domains


def test_hospitalization_query_does_not_trigger_lodging_domain():
    plan = heuristic_retrieval_plan_from_message("我之前住院了吗？")

    assert "hotel" not in plan.domains
    assert "accommodation" not in plan.domains
    assert plan.source == "hybrid_history"


def test_direct_lodging_preference_query_triggers_profile_recall():
    plan = heuristic_retrieval_plan_from_message("我不住青旅吗？")

    assert plan.source == "profile"
    assert "hotel" in plan.domains or "accommodation" in plan.domains


def test_direct_train_preference_query_triggers_profile_recall():
    plan = heuristic_retrieval_plan_from_message("我不坐高铁吗？")

    assert plan.source == "profile"
    assert "train" in plan.domains


def test_rank_profile_items_returns_recall_candidates():
    query = _plan(domains=["flight"], keywords=["红眼航班"], aliases=[], buckets=["constraints", "preference_hypotheses"], source="profile")
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

    assert [candidate.bucket for candidate in ranked] == ["constraints", "preference_hypotheses"]
    assert isinstance(ranked[0], RecallCandidate)
    assert ranked[0].source == "profile"
    assert ranked[0].item_id == "constraints:flight:avoid_red_eye"
    assert ranked[0].score > 0
    assert ranked[0].matched_reason[0].startswith("exact domain match")


def test_rank_profile_items_prefers_rejections_over_stable_preferences():
    query = _plan(source="profile", domains=["hotel"], keywords=["青旅"], aliases=[])
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

    assert [candidate.bucket for candidate in ranked] == ["rejections", "stable_preferences"]
    assert ranked[0].matched_reason[0].startswith("exact domain match")


def test_rank_profile_items_respects_allowed_buckets():
    query = _plan(source="profile", buckets=["rejections"], domains=["hotel"], entities={}, keywords=["青旅"], aliases=[], strictness="strict")
    profile = UserMemoryProfile(
        schema_version=3,
        user_id="u1",
        constraints=[
            _profile_item(
                id="constraints:hotel:avoid_shared_bathroom",
                domain="hotel",
                key="avoid_shared_bathroom",
                value=True,
                polarity="avoid",
                stability="explicit_declared",
                recall_hints={"domains": ["hotel"], "keywords": ["青旅"]},
            )
        ],
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
        stable_preferences=[],
        preference_hypotheses=[],
    )

    ranked = rank_profile_items(query, profile)

    assert [candidate.bucket for candidate in ranked] == ["rejections"]


def test_rank_profile_items_uses_conservative_default_when_allowed_buckets_is_empty():
    query = _plan(source="profile", buckets=[], domains=["flight"], entities={}, keywords=["红眼航班"], aliases=[])
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
                recall_hints={"domains": ["flight"], "keywords": ["红眼航班"]},
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

    assert [candidate.bucket for candidate in ranked] == ["constraints"]


def test_rank_episode_slices_returns_recall_candidates():
    query = _plan(source="episode_slice", domains=["hotel"], entities={"destination": "京都"}, keywords=["住宿"], aliases=[])
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

    assert [candidate.item_id for candidate in ranked] == ["slice_exact", "slice_keyword_only"]
    assert isinstance(ranked[0], RecallCandidate)
    assert ranked[0].source == "episode_slice"
    assert ranked[0].bucket == "accommodation_decision"
    assert ranked[0].matched_reason[0].startswith("exact destination match")


def test_current_trip_question_produces_no_recall_hits():
    query = _plan(source="episode_slice", domains=[], entities={}, keywords=[], aliases=[])
    profile = UserMemoryProfile.empty("u1")
    slices = [_slice()]

    assert rank_profile_items(_plan(source="episode_slice"), profile) == []
    assert rank_episode_slices(_plan(source="profile"), slices) == []
