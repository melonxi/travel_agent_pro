from dataclasses import replace

from config import Stage3LaneConfig, Stage3RecallConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3 import retrieve_recall_candidates
from memory.v3_models import MemoryProfileItem, UserMemoryProfile
from state.models import TravelPlanState


def test_lexical_lane_recalls_profile_by_expanded_keyword_when_symbolic_misses() -> None:
    profile = UserMemoryProfile(
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
    config = replace(
        Stage3RecallConfig(),
        symbolic=Stage3LaneConfig(enabled=False, top_k=20, timeout_ms=25),
        lexical=Stage3LaneConfig(enabled=True, top_k=20, timeout_ms=20),
    )
    query = RecallRetrievalPlan(
        source="profile",
        buckets=["stable_preferences"],
        domains=["hotel"],
        destination="",
        keywords=["住宿"],
        top_k=5,
        reason="test",
    )

    result = retrieve_recall_candidates(
        query=query,
        profile=profile,
        slices=[],
        user_message="这次住宿想安静一点",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=config,
    )

    assert [candidate.item_id for candidate in result.candidates] == [
        "stable_preferences:hotel:quiet_stay"
    ]
    evidence = result.evidence_by_id["stable_preferences:hotel:quiet_stay"]
    assert evidence.lexical_score is not None
    assert "lexical" in evidence.lanes
