from dataclasses import replace

from config import Stage3RecallConfig, Stage3SemanticConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3 import retrieve_recall_candidates
from memory.v3_models import MemoryProfileItem, UserMemoryProfile
from state.models import TravelPlanState


class FakeEmbeddingProvider:
    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            if "安静" in text or "清静" in text:
                vectors.append([1, 0])
            elif "红眼" in text:
                vectors.append([0, 1])
            else:
                vectors.append([0.8, 0.2])
        return vectors


def test_semantic_lane_recalls_synonymous_profile_when_enabled() -> None:
    profile = UserMemoryProfile(
        schema_version=3,
        user_id="u1",
        stable_preferences=[
            MemoryProfileItem(
                id="stable_preferences:hotel:quiet",
                domain="hotel",
                key="quiet",
                value="偏好清静的住宿环境",
                polarity="prefer",
                stability="stable",
                confidence=0.9,
                status="active",
                applicability="适用于住宿选择。",
                created_at="2026-04-01T00:00:00",
                updated_at="2026-04-02T00:00:00",
            )
        ],
    )
    config = replace(
        Stage3RecallConfig(),
        semantic=Stage3SemanticConfig(enabled=True, min_score=0.7, top_k=5),
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
        embedding_provider=FakeEmbeddingProvider(),
    )

    assert [candidate.item_id for candidate in result.candidates] == [
        "stable_preferences:hotel:quiet"
    ]
    evidence = result.evidence_by_id["stable_preferences:hotel:quiet"]
    assert "semantic" in evidence.lanes
    assert evidence.semantic_score is not None
