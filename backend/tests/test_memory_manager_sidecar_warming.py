from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from config import (
    MemoryRetrievalConfig,
    Stage3LaneConfig,
    Stage3RecallConfig,
    Stage3SemanticConfig,
    Stage3SemanticEmbeddingIndexConfig,
)
from memory.manager import MemoryManager


def test_manager_creates_sidecar_store_when_index_enabled(tmp_path: Path):
    cfg = MemoryRetrievalConfig(
        stage3=replace(
            Stage3RecallConfig(),
            symbolic=Stage3LaneConfig(enabled=False),
            semantic=Stage3SemanticConfig(
                enabled=True,
                embedding_index=Stage3SemanticEmbeddingIndexConfig(enabled=True),
            ),
        )
    )
    manager = MemoryManager(data_dir=str(tmp_path), retrieval_config=cfg)
    store = manager._get_sidecar_store()
    assert store is not None
    assert store.data_dir == Path(tmp_path)


def test_manager_returns_none_sidecar_when_index_disabled(tmp_path: Path):
    cfg = MemoryRetrievalConfig()  # 默认 embedding_index.enabled=False
    manager = MemoryManager(data_dir=str(tmp_path), retrieval_config=cfg)
    assert manager._get_sidecar_store() is None


from memory.v3_models import MemoryProfileItem


class FakeEmbeddingProvider:
    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]


def _stable_pref_item() -> MemoryProfileItem:
    return MemoryProfileItem(
        id="stable_preferences:hotel:quiet",
        domain="hotel",
        key="quiet",
        value="偏好清静的住宿环境",
        polarity="prefer",
        stability="stable",
        confidence=0.9,
        status="active",
        applicability="适用于住宿选择。",
        created_at="t",
        updated_at="t",
    )


@pytest.mark.asyncio
async def test_warm_profile_item_writes_sidecar_row(tmp_path: Path):
    cfg = MemoryRetrievalConfig(
        stage3=replace(
            Stage3RecallConfig(),
            semantic=Stage3SemanticConfig(
                enabled=True,
                embedding_index=Stage3SemanticEmbeddingIndexConfig(
                    enabled=True, warm_on_write=True
                ),
            ),
        )
    )
    manager = MemoryManager(data_dir=str(tmp_path), retrieval_config=cfg)
    manager._embedding_provider = FakeEmbeddingProvider()

    await manager.warm_profile_item("u1", "stable_preferences", _stable_pref_item())

    store = manager._get_sidecar_store()
    fetched = store.fetch_many(
        "u1", [("profile", "stable_preferences:hotel:quiet")]
    )
    assert ("profile", "stable_preferences:hotel:quiet") in fetched


@pytest.mark.asyncio
async def test_warm_profile_item_skips_buckets_not_in_warm_buckets(tmp_path: Path):
    cfg = MemoryRetrievalConfig(
        stage3=replace(
            Stage3RecallConfig(),
            semantic=Stage3SemanticConfig(
                enabled=True,
                embedding_index=Stage3SemanticEmbeddingIndexConfig(
                    enabled=True,
                    warm_on_write=True,
                    warm_buckets=("constraints",),
                ),
            ),
        )
    )
    manager = MemoryManager(data_dir=str(tmp_path), retrieval_config=cfg)
    manager._embedding_provider = FakeEmbeddingProvider()

    hypothesis = MemoryProfileItem(
        id="preference_hypotheses:hotel:quiet:ctx",
        domain="hotel",
        key="quiet",
        value="x",
        polarity="prefer",
        stability="hypothesis",
        confidence=0.4,
        status="active",
        applicability="x",
        created_at="t",
        updated_at="t",
    )
    await manager.warm_profile_item("u1", "preference_hypotheses", hypothesis)

    store = manager._get_sidecar_store()
    fetched = store.fetch_many(
        "u1", [("profile", "preference_hypotheses:hotel:quiet:ctx")]
    )
    assert fetched == {}


@pytest.mark.asyncio
async def test_warm_profile_item_noop_when_warm_on_write_disabled(tmp_path: Path):
    cfg = MemoryRetrievalConfig(
        stage3=replace(
            Stage3RecallConfig(),
            semantic=Stage3SemanticConfig(
                enabled=True,
                embedding_index=Stage3SemanticEmbeddingIndexConfig(
                    enabled=True, warm_on_write=False
                ),
            ),
        )
    )
    manager = MemoryManager(data_dir=str(tmp_path), retrieval_config=cfg)
    provider = FakeEmbeddingProvider()
    manager._embedding_provider = provider

    await manager.warm_profile_item("u1", "stable_preferences", _stable_pref_item())
    assert provider.calls == []


from memory.v3_models import EpisodeSlice


def _slice() -> EpisodeSlice:
    return EpisodeSlice(
        id="slice_abc",
        user_id="u1",
        source_episode_id="ep1",
        source_trip_id="t1",
        slice_type="poi_choice",
        domains=["accommodation"],
        entities={"destination": "杭州"},
        keywords=["民宿", "清静"],
        content="用户选择了远离市中心的清静民宿。",
        applicability="适用于住宿决策。",
        created_at="t",
    )


@pytest.mark.asyncio
async def test_warm_episode_slice_writes_sidecar_row(tmp_path: Path):
    cfg = MemoryRetrievalConfig(
        stage3=replace(
            Stage3RecallConfig(),
            semantic=Stage3SemanticConfig(
                enabled=True,
                embedding_index=Stage3SemanticEmbeddingIndexConfig(
                    enabled=True, warm_on_write=True
                ),
            ),
        )
    )
    manager = MemoryManager(data_dir=str(tmp_path), retrieval_config=cfg)
    manager._embedding_provider = FakeEmbeddingProvider()

    await manager.warm_episode_slice("u1", _slice())

    store = manager._get_sidecar_store()
    fetched = store.fetch_many("u1", [("episode_slice", "slice_abc")])
    assert ("episode_slice", "slice_abc") in fetched


@pytest.mark.asyncio
async def test_warm_episode_slice_noop_when_warm_on_write_disabled(tmp_path: Path):
    cfg = MemoryRetrievalConfig(
        stage3=replace(
            Stage3RecallConfig(),
            semantic=Stage3SemanticConfig(
                enabled=True,
                embedding_index=Stage3SemanticEmbeddingIndexConfig(
                    enabled=True, warm_on_write=False
                ),
            ),
        )
    )
    manager = MemoryManager(data_dir=str(tmp_path), retrieval_config=cfg)
    provider = FakeEmbeddingProvider()
    manager._embedding_provider = provider

    await manager.warm_episode_slice("u1", _slice())
    assert provider.calls == []
