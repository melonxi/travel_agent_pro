from dataclasses import replace

from config import Stage3LaneConfig, Stage3RecallConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3 import retrieve_recall_candidates
from memory.v3_models import EpisodeSlice, MemoryProfileItem, UserMemoryProfile
from state.models import TravelPlanState


def _lexical_config(symbolic_enabled: bool = False) -> Stage3RecallConfig:
    return replace(
        Stage3RecallConfig(),
        symbolic=Stage3LaneConfig(enabled=symbolic_enabled, top_k=20, timeout_ms=25),
        lexical=Stage3LaneConfig(enabled=True, top_k=20, timeout_ms=20),
    )


def _quiet_profile() -> UserMemoryProfile:
    return UserMemoryProfile(
        schema_version=3,
        user_id="u1",
        stable_preferences=[
            MemoryProfileItem(
                id="stable_preferences:hotel:quiet_stay",
                domain="hotel",
                key="quiet_stay",
                value="喜欢安静的旅馆",
                polarity="prefer",
                stability="stable",
                confidence=0.9,
                status="active",
                recall_hints={"keywords": ["旅馆", "安静"]},
                applicability="适用于住宿选择。",
                created_at="2026-04-01T00:00:00",
                updated_at="2026-04-02T00:00:00",
            )
        ],
    )


def _query(
    *,
    source: str = "profile",
    keywords: list[str] | None = None,
    domains: list[str] | None = None,
) -> RecallRetrievalPlan:
    return RecallRetrievalPlan(
        source=source,
        buckets=["stable_preferences"],
        domains=domains if domains is not None else ["hotel"],
        destination="",
        keywords=keywords if keywords is not None else ["住宿"],
        top_k=5,
        reason="test",
    )


def test_lexical_lane_recalls_profile_by_expanded_keyword_when_symbolic_misses() -> None:
    result = retrieve_recall_candidates(
        query=_query(),
        profile=_quiet_profile(),
        slices=[],
        user_message="这次住宿想安静一点",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=_lexical_config(),
    )

    assert [candidate.item_id for candidate in result.candidates] == [
        "stable_preferences:hotel:quiet_stay"
    ]
    evidence = result.evidence_by_id["stable_preferences:hotel:quiet_stay"]
    assert evidence.lexical_score is not None
    assert "lexical" in evidence.lanes
    assert "安静" in evidence.matched_keywords or "旅馆" in evidence.matched_keywords
    assert not any(
        len(keyword) == 1 and keyword.isascii() for keyword in evidence.matched_keywords
    )


def test_lexical_lane_does_not_recall_by_incidental_metadata_overlap() -> None:
    profile = UserMemoryProfile(
        schema_version=3,
        user_id="u1",
        stable_preferences=[
            MemoryProfileItem(
                id="stable_preferences:hotel:abc_metadata",
                domain="hotel",
                key="abc_metadata",
                value="喜欢热闹的夜市",
                polarity="prefer",
                stability="stable",
                confidence=0.9,
                status="active",
                recall_hints={"keywords": ["夜市"]},
                applicability="适用于餐饮选择。",
                created_at="2026-04-01T00:00:00",
                updated_at="2026-04-02T00:00:00",
            )
        ],
    )
    slices = [
        EpisodeSlice(
            id="abc_slice",
            user_id="u1",
            source_episode_id="ep1",
            source_trip_id="old_trip",
            slice_type="abc_metadata",
            domains=["hotel"],
            entities={},
            keywords=["夜市"],
            content="上次喜欢热闹的夜市。",
            applicability="适用于餐饮选择。",
            created_at="2026-04-03T00:00:00",
        )
    ]

    result = retrieve_recall_candidates(
        query=_query(source="hybrid_history", keywords=["abc"], domains=[]),
        profile=profile,
        slices=slices,
        user_message="abc",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=_lexical_config(),
    )

    assert result.candidates == []


def test_lexical_disabled_preserves_default_symbolic_only_output() -> None:
    profile = _quiet_profile()
    query = _query()

    default_result = retrieve_recall_candidates(
        query=query,
        profile=profile,
        slices=[],
        user_message="住宿按我习惯",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=Stage3RecallConfig(),
    )
    disabled_result = retrieve_recall_candidates(
        query=query,
        profile=profile,
        slices=[],
        user_message="住宿按我习惯",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=replace(Stage3RecallConfig(), lexical=Stage3LaneConfig(enabled=False)),
    )

    assert [candidate.item_id for candidate in disabled_result.candidates] == [
        candidate.item_id for candidate in default_result.candidates
    ]
    assert disabled_result.telemetry.lanes_attempted == ["symbolic"]


def test_lexical_source_policy_excludes_unrequested_sources() -> None:
    profile = _quiet_profile()
    slices = [
        EpisodeSlice(
            id="slice_quiet",
            user_id="u1",
            source_episode_id="ep1",
            source_trip_id="old_trip",
            slice_type="accommodation_decision",
            domains=["hotel"],
            entities={"destination": "京都"},
            keywords=["安静"],
            content="上次住宿也想安静。",
            applicability="适用于住宿选择。",
            created_at="2026-04-03T00:00:00",
        )
    ]

    profile_only = retrieve_recall_candidates(
        query=_query(source="profile"),
        profile=profile,
        slices=slices,
        user_message="这次住宿想安静一点",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=_lexical_config(),
    )
    slice_only = retrieve_recall_candidates(
        query=_query(source="episode_slice"),
        profile=profile,
        slices=slices,
        user_message="这次住宿想安静一点",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=_lexical_config(),
    )

    assert [candidate.source for candidate in profile_only.candidates] == ["profile"]
    assert [candidate.source for candidate in slice_only.candidates] == [
        "episode_slice"
    ]


def test_lexical_and_symbolic_duplicate_candidate_merges_evidence_lanes() -> None:
    result = retrieve_recall_candidates(
        query=_query(keywords=["旅馆"]),
        profile=_quiet_profile(),
        slices=[],
        user_message="住宿想找安静旅馆",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=_lexical_config(symbolic_enabled=True),
    )

    assert [candidate.item_id for candidate in result.candidates] == [
        "stable_preferences:hotel:quiet_stay"
    ]
    evidence = result.evidence_by_id["stable_preferences:hotel:quiet_stay"]
    assert evidence.lanes == ["symbolic", "lexical"]
    assert evidence.lexical_score is not None
    assert set(evidence.lane_ranks) == {"symbolic", "lexical"}
