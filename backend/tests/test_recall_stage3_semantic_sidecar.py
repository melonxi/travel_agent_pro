from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from config import (
    Stage3LaneConfig,
    Stage3RecallConfig,
    Stage3SemanticConfig,
    Stage3SemanticEmbeddingIndexConfig,
)
from memory.embedding_sidecar import SidecarStore
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3 import retrieve_recall_candidates
from memory.v3_models import MemoryProfileItem, UserMemoryProfile
from state.models import TravelPlanState


class CountingEmbeddingProvider:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vectors: list[list[float]] = []
        for text in texts:
            if "安静" in text or "清静" in text:
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors


def _profile() -> UserMemoryProfile:
    return UserMemoryProfile(
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


def _config_with_index(*, enabled: bool = True) -> Stage3RecallConfig:
    return replace(
        Stage3RecallConfig(),
        symbolic=Stage3LaneConfig(enabled=False),
        semantic=Stage3SemanticConfig(
            enabled=True,
            min_score=0.7,
            top_k=5,
            embedding_index=Stage3SemanticEmbeddingIndexConfig(enabled=enabled),
        ),
    )


def _query() -> RecallRetrievalPlan:
    return RecallRetrievalPlan(
        source="profile",
        buckets=["stable_preferences"],
        domains=["hotel"],
        destination="",
        keywords=["住宿"],
        top_k=5,
        reason="test",
    )


def _retrieve(provider, sidecar_store, *, profile=None, config=None):
    return retrieve_recall_candidates(
        query=_query(),
        profile=profile or _profile(),
        slices=[],
        user_message="这次住宿想安静一点",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=config or _config_with_index(),
        embedding_provider=provider,
        sidecar_store=sidecar_store,
        user_id="u1",
    )


def test_retrieve_recall_candidates_accepts_sidecar_kwargs():
    import inspect

    sig = inspect.signature(retrieve_recall_candidates)
    assert "sidecar_store" in sig.parameters
    assert "user_id" in sig.parameters


def test_first_recall_misses_and_writes_sidecar(tmp_path: Path):
    provider = CountingEmbeddingProvider()
    store = SidecarStore(data_dir=tmp_path)

    result = _retrieve(provider, store)

    assert [c.item_id for c in result.candidates] == [
        "stable_preferences:hotel:quiet"
    ]
    telemetry = result.telemetry.semantic_embedding_index
    assert telemetry["enabled"] is True
    assert telemetry["candidate_count"] == 1
    assert telemetry["hit_count"] == 0
    assert telemetry["stale_count"] == 0
    assert telemetry["miss_count"] == 1
    assert telemetry["write_count"] == 1
    assert telemetry["write_error_count"] == 0

    fetched = store.fetch_many(
        "u1", [("profile", "stable_preferences:hotel:quiet")]
    )
    assert ("profile", "stable_preferences:hotel:quiet") in fetched


def test_second_recall_hits_sidecar_for_candidates(tmp_path: Path):
    provider = CountingEmbeddingProvider()
    store = SidecarStore(data_dir=tmp_path)

    _retrieve(provider, store)
    provider.calls.clear()

    result = _retrieve(provider, store)
    telemetry = result.telemetry.semantic_embedding_index
    assert telemetry["hit_count"] == 1
    assert telemetry["miss_count"] == 0
    assert telemetry["stale_count"] == 0
    # query 仍现算一次，但候选不再 embed
    assert len(provider.calls) == 1
    assert len(provider.calls[0]) == 1


def test_text_change_invalidates_sidecar_row(tmp_path: Path):
    provider = CountingEmbeddingProvider()
    store = SidecarStore(data_dir=tmp_path)

    _retrieve(provider, store)
    provider.calls.clear()

    changed_profile = _profile()
    changed_profile.stable_preferences[0].value = "改变后的偏好描述"
    result = _retrieve(provider, store, profile=changed_profile)
    telemetry = result.telemetry.semantic_embedding_index
    assert telemetry["stale_count"] == 1
    assert telemetry["hit_count"] == 0
    assert telemetry["miss_count"] == 0
    assert telemetry["write_count"] == 1


def test_index_invariants_hold_for_all_buckets(tmp_path: Path):
    provider = CountingEmbeddingProvider()
    store = SidecarStore(data_dir=tmp_path)
    result = _retrieve(provider, store)
    t = result.telemetry.semantic_embedding_index
    assert t["hit_count"] + t["stale_count"] + t["miss_count"] == t["candidate_count"]


def test_disabled_index_preserves_current_behavior(tmp_path: Path):
    provider = CountingEmbeddingProvider()
    store = SidecarStore(data_dir=tmp_path)

    result = _retrieve(provider, store, config=_config_with_index(enabled=False))
    telemetry = result.telemetry.semantic_embedding_index
    assert telemetry.get("enabled") is False
    db_path = store._db_path("u1")
    assert not db_path.exists()


def test_candidate_count_mismatch_returns_partial_telemetry_counters(tmp_path: Path):
    from config import (
        Stage3LaneConfig,
        Stage3RecallConfig,
        Stage3SemanticConfig,
        Stage3SemanticEmbeddingIndexConfig,
    )
    from memory.embedding_sidecar import SidecarStore
    from memory.recall_query import RecallRetrievalPlan
    from memory.recall_stage3 import retrieve_recall_candidates
    from memory.v3_models import MemoryProfileItem, UserMemoryProfile
    from state.models import TravelPlanState

    class MismatchProvider:
        """First call (query embed) returns 1 vector;
        second call (candidate embed) returns the wrong count."""

        def __init__(self) -> None:
            self.call_index = 0

        def embed(self, texts: list[str]) -> list[list[float]]:
            self.call_index += 1
            if self.call_index == 1:
                return [[1.0, 0.0]]
            # Candidate embed: return too many vectors to force count mismatch.
            return [[0.5, 0.5] for _ in texts] + [[0.0, 0.0]]

    store = SidecarStore(data_dir=tmp_path)
    config = Stage3RecallConfig(
        symbolic=Stage3LaneConfig(enabled=False),
        semantic=Stage3SemanticConfig(
            enabled=True,
            min_score=0.1,
            top_k=5,
            embedding_index=Stage3SemanticEmbeddingIndexConfig(enabled=True),
        ),
    )
    profile = UserMemoryProfile(
        schema_version=3,
        user_id="u1",
        stable_preferences=[
            MemoryProfileItem(
                id="stable_preferences:hotel:quiet",
                domain="hotel",
                key="quiet",
                value="x",
                polarity="prefer",
                stability="stable",
                confidence=0.9,
                status="active",
                applicability="x",
                created_at="t",
                updated_at="t",
            )
        ],
    )
    query = RecallRetrievalPlan(
        source="profile",
        buckets=["stable_preferences"],
        domains=["hotel"],
        destination="",
        keywords=[],
        top_k=5,
        reason="test",
    )

    result = retrieve_recall_candidates(
        query=query,
        profile=profile,
        slices=[],
        user_message="x",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=config,
        embedding_provider=MismatchProvider(),
        sidecar_store=store,
        user_id="u1",
    )

    telemetry = result.telemetry.semantic_embedding_index
    assert telemetry["enabled"] is True
    assert telemetry["candidate_count"] == 1
    assert "hit_count" in telemetry
    assert "stale_count" in telemetry
    assert "miss_count" in telemetry
    assert telemetry["hit_count"] + telemetry["stale_count"] + telemetry["miss_count"] == 1
