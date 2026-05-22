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
