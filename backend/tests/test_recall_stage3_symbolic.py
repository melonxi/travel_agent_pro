import pytest

from config import Stage3RecallConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3 import retrieve_recall_candidates
from memory.recall_stage3_lanes import (
    SymbolicLane,
    _evidence_from_candidate,
    _plan_for_source_policy,
)
from memory.recall_stage3_models import RecallQueryEnvelope, SourcePolicy
from memory.retrieval_candidates import RecallCandidate
from memory.symbolic_recall import rank_episode_slices, rank_profile_items
from memory.v3_models import EpisodeSlice, MemoryProfileItem, UserMemoryProfile
from state.models import TravelPlanState


def _profile() -> UserMemoryProfile:
    return UserMemoryProfile(
        schema_version=3,
        user_id="u1",
        stable_preferences=[
            MemoryProfileItem(
                id="stable_preferences:hotel:preferred_area",
                domain="hotel",
                key="preferred_area",
                value="京都四条附近",
                polarity="prefer",
                stability="stable",
                confidence=0.9,
                status="active",
                recall_hints={"domains": ["hotel"], "keywords": ["住宿", "住哪里"]},
                applicability="适用于大多数住宿选择。",
                created_at="2026-04-01T00:00:00",
                updated_at="2026-04-02T00:00:00",
            )
        ],
    )


def _slices() -> list[EpisodeSlice]:
    return [
        EpisodeSlice(
            id="slice_1",
            user_id="u1",
            source_episode_id="ep1",
            source_trip_id="old_trip",
            slice_type="accommodation_decision",
            domains=["hotel"],
            entities={"destination": "京都"},
            keywords=["住宿"],
            content="上次京都住四条附近的町屋。",
            applicability="仅供住宿选择参考。",
            created_at="2026-04-03T00:00:00",
        )
    ]


def _query(source: str = "hybrid_history") -> RecallRetrievalPlan:
    return RecallRetrievalPlan(
        source=source,
        buckets=["stable_preferences"],
        domains=["hotel"],
        destination="京都",
        keywords=["住宿"],
        top_k=5,
        reason="test",
    )


def _large_profile(count: int = 20) -> UserMemoryProfile:
    return UserMemoryProfile(
        schema_version=3,
        user_id="u1",
        stable_preferences=[
            MemoryProfileItem(
                id=f"stable_preferences:hotel:preferred_area:{index:02d}",
                domain="hotel",
                key=f"preferred_area_{index:02d}",
                value=f"京都四条附近 {index:02d}",
                polarity="prefer",
                stability="stable",
                confidence=0.9,
                status="active",
                recall_hints={"domains": ["hotel"], "keywords": ["住宿"]},
                applicability="适用于大多数住宿选择。",
                created_at="2026-04-01T00:00:00",
                updated_at="2026-04-02T00:00:00",
            )
            for index in range(count)
        ],
    )


def _large_slices(count: int = 20) -> list[EpisodeSlice]:
    return [
        EpisodeSlice(
            id=f"slice_{index:02d}",
            user_id="u1",
            source_episode_id=f"ep{index:02d}",
            source_trip_id="old_trip",
            slice_type="accommodation_decision",
            domains=["hotel"],
            entities={"destination": "京都"},
            keywords=["住宿"],
            content=f"上次京都住四条附近的町屋 {index:02d}。",
            applicability="仅供住宿选择参考。",
            created_at="2026-04-03T00:00:00",
        )
        for index in range(count)
    ]


def test_stage3_symbolic_default_matches_existing_symbolic_candidates() -> None:
    query = _query()
    profile = _profile()
    slices = _slices()
    expected = [
        *rank_profile_items(query, profile)[: query.top_k],
        *rank_episode_slices(query, slices)[: query.top_k],
    ]

    result = retrieve_recall_candidates(
        query=query,
        profile=profile,
        slices=slices,
        user_message="上次京都住哪里",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=Stage3RecallConfig(),
    )

    assert [candidate.item_id for candidate in result.candidates] == [
        candidate.item_id for candidate in expected
    ]
    assert result.telemetry.lanes_attempted == ["symbolic"]
    assert result.telemetry.zero_hit is False
    assert set(result.evidence_by_id) == {
        "stable_preferences:hotel:preferred_area",
        "slice_1",
    }


def test_stage3_symbolic_default_large_result_matches_existing_order_without_fusion_caps() -> None:
    query = _query()
    query.top_k = 20
    profile = _large_profile()
    slices = _large_slices()
    expected = [
        *rank_profile_items(query, profile)[: query.top_k],
        *rank_episode_slices(query, slices)[: query.top_k],
    ]

    result = retrieve_recall_candidates(
        query=query,
        profile=profile,
        slices=slices,
        user_message="上次京都住哪里",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=Stage3RecallConfig(),
    )

    assert [candidate.item_id for candidate in result.candidates] == [
        candidate.item_id for candidate in expected
    ]
    assert len(result.candidates) == 40
    assert len(result.evidence_by_id) == 40


def test_stage3_symbolic_default_reports_zero_hit() -> None:
    result = retrieve_recall_candidates(
        query=_query(source="profile"),
        profile=UserMemoryProfile.empty("u1"),
        slices=[],
        user_message="住宿按我习惯",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=Stage3RecallConfig(),
    )

    assert result.candidates == []
    assert result.telemetry.zero_hit is True


def test_stage3_symbolic_public_entrypoint_respects_profile_source_only() -> None:
    result = retrieve_recall_candidates(
        query=_query(source="profile"),
        profile=_profile(),
        slices=_slices(),
        user_message="住宿按我习惯",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=Stage3RecallConfig(),
    )

    assert [candidate.source for candidate in result.candidates] == ["profile"]
    assert [candidate.item_id for candidate in result.candidates] == [
        "stable_preferences:hotel:preferred_area"
    ]


def test_stage3_symbolic_public_entrypoint_respects_episode_slice_source_only() -> None:
    result = retrieve_recall_candidates(
        query=_query(source="episode_slice"),
        profile=_profile(),
        slices=_slices(),
        user_message="上次京都住哪里",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=Stage3RecallConfig(),
    )

    assert [candidate.source for candidate in result.candidates] == ["episode_slice"]
    assert [candidate.item_id for candidate in result.candidates] == ["slice_1"]


def test_stage3_symbolic_entrypoint_raises_symbolic_lane_errors(monkeypatch) -> None:
    def raise_symbolic_error(self, envelope, profile, slices, config):
        raise RuntimeError("symbolic boom")

    monkeypatch.setattr(SymbolicLane, "run", raise_symbolic_error)

    with pytest.raises(RuntimeError, match="symbolic boom"):
        retrieve_recall_candidates(
            query=_query(source="profile"),
            profile=_profile(),
            slices=[],
            user_message="住宿按我习惯",
            plan=TravelPlanState(session_id="s1", trip_id="now"),
            config=Stage3RecallConfig(),
        )


def test_stage3_symbolic_plan_preserves_original_source_when_policy_selects_no_lane() -> None:
    envelope = RecallQueryEnvelope(
        plan=_query(source="profile"),
        user_message="",
        source_policy=SourcePolicy(
            requested_source="profile",
            search_profile=False,
            search_slices=False,
        ),
        original_domains=("hotel",),
        expanded_domains=("hotel", "accommodation"),
        original_keywords=("住宿",),
        expanded_keywords=("住宿", "酒店"),
        destination="京都",
    )

    lane_plan = _plan_for_source_policy(envelope)

    assert lane_plan.source == "profile"


def test_stage3_symbolic_lane_returns_no_candidates_when_policy_selects_no_lane() -> None:
    envelope = RecallQueryEnvelope(
        plan=_query(),
        user_message="",
        source_policy=SourcePolicy(
            requested_source="hybrid_history",
            search_profile=False,
            search_slices=False,
        ),
        original_domains=("hotel",),
        expanded_domains=("hotel",),
        original_keywords=("住宿",),
        expanded_keywords=("住宿",),
        destination="京都",
    )

    result = SymbolicLane().run(
        envelope=envelope,
        profile=_profile(),
        slices=_slices(),
        config=Stage3RecallConfig(),
    )

    assert result.candidates == []


def test_stage3_symbolic_evidence_reports_only_domains_present_in_reasons() -> None:
    candidate = RecallCandidate(
        source="profile",
        item_id="stable_preferences:hotel:preferred_area",
        bucket="stable_preferences",
        score=1.0,
        matched_reason=["exact domain match on hotel", "keyword match on 住宿"],
        content_summary="hotel:preferred_area=京都四条附近",
        domains=["hotel", "flight"],
        applicability="适用于大多数住宿选择。",
    )

    evidence = _evidence_from_candidate(candidate, "symbolic")

    assert evidence.matched_domains == ["hotel"]


def test_stage3_telemetry_query_expansion_includes_destination_aliases_and_children() -> None:
    result = retrieve_recall_candidates(
        query=_query(source="profile"),
        profile=UserMemoryProfile.empty("u1"),
        slices=[],
        user_message="住宿按我习惯",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=Stage3RecallConfig(destination_normalization_enabled=True),
    )

    assert result.telemetry.query_expansion["destination_aliases"] == ["Kyoto"]
    assert result.telemetry.query_expansion["destination_children"] == []
