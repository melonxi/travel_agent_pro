from config import Stage3RecallConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3 import retrieve_recall_candidates
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
