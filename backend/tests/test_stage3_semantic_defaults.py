"""Verify the default Stage 3 semantic lane and split Stage 4 reranker config."""

from __future__ import annotations

from config import MemoryRetrievalConfig, Stage3SemanticConfig


def test_stage3_semantic_config_defaults_enable_lane():
    cfg = Stage3SemanticConfig()
    assert cfg.enabled is True
    assert cfg.local_files_only is True
    assert cfg.provider == "fastembed"
    assert cfg.model_name == "BAAI/bge-small-zh-v1.5"


def test_reranker_defaults_are_source_specific():
    cfg = MemoryRetrievalConfig().reranker
    assert cfg.profile.profile_top_n == 4
    assert cfg.profile.w_bucket == 0.40
    assert cfg.profile.recency_half_life_days == 180
    assert cfg.episode.episode_top_n == 3
    assert cfg.episode.w_rel == 0.45
    assert cfg.episode.recency_half_life_days == 365


def test_memory_retrieval_config_wires_new_defaults_through_composition():
    cfg = MemoryRetrievalConfig()
    assert cfg.stage3.semantic.enabled is True
    assert cfg.reranker.profile.profile_top_n == 4
    assert cfg.reranker.episode.episode_top_n == 3
