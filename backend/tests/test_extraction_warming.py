from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from config import (
    MemoryRetrievalConfig,
    Stage3RecallConfig,
    Stage3SemanticConfig,
    Stage3SemanticEmbeddingIndexConfig,
)
from memory.manager import MemoryManager
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
        keywords=["民宿"],
        content="...",
        applicability="...",
        created_at="t",
    )


@pytest.mark.asyncio
async def test_append_episode_slices_calls_warm_episode_slice(tmp_path: Path):
    from api.orchestration.memory import episodes as ep_module
    from api.orchestration.memory.episodes import append_episode_slices

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
    manager.warm_episode_slice = AsyncMock()

    class _Episode:
        pass

    orig_build = ep_module.build_episode_slices
    ep_module.build_episode_slices = lambda episode, now: [_slice()]
    try:
        await append_episode_slices(
            _Episode(), memory_mgr=manager, now_iso=lambda: "t"
        )
    finally:
        ep_module.build_episode_slices = orig_build

    manager.warm_episode_slice.assert_awaited_once()
    args, _ = manager.warm_episode_slice.call_args
    assert args[0] == "u1"
    assert args[1].id == "slice_abc"


def test_extraction_module_calls_warm_profile_item_after_upsert():
    import inspect

    import api.orchestration.memory.extraction as ext_mod

    source = inspect.getsource(ext_mod)
    upsert_idx = source.index("v3_store.upsert_profile_item")
    warm_idx = source.find("warm_profile_item", upsert_idx)
    assert warm_idx > upsert_idx, "warm_profile_item must be awaited after upsert"
