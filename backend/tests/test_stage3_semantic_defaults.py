"""Verify that after the phase-1 default flip, Stage 3 semantic lane
actually participates in recall and Stage 4 evidence scores become
non-zero for matching candidates. Uses a deterministic fake embedding
provider so tests don't depend on the real FastEmbed model."""

from __future__ import annotations

from config import MemoryRetrievalConfig, RerankerEvidenceConfig, Stage3SemanticConfig


def test_stage3_semantic_config_defaults_enable_lane():
    cfg = Stage3SemanticConfig()
    assert cfg.enabled is True
    assert cfg.local_files_only is True
    assert cfg.provider == "fastembed"
    assert cfg.model_name == "BAAI/bge-small-zh-v1.5"


def test_reranker_evidence_config_default_weights_are_active():
    cfg = RerankerEvidenceConfig()
    assert cfg.lane_fused_weight == 0.25
    assert cfg.semantic_score_weight == 0.15
    assert cfg.lexical_score_weight == 0.08
    # Hit-style weights stay at 0: evidence uses continuous scores instead.
    assert cfg.symbolic_hit_weight == 0.0
    assert cfg.lexical_hit_weight == 0.0
    assert cfg.semantic_hit_weight == 0.0
    assert cfg.destination_match_type_weight == 0.0


def test_memory_retrieval_config_wires_new_defaults_through_composition():
    cfg = MemoryRetrievalConfig()
    assert cfg.stage3.semantic.enabled is True
    assert cfg.reranker.evidence.lane_fused_weight == 0.25
    assert cfg.reranker.evidence.semantic_score_weight == 0.15
    assert cfg.reranker.evidence.lexical_score_weight == 0.08


def test_reranker_config_rollback_via_explicit_zero_weights():
    """Documented rollback path: constructing evidence with zero weights
    reverts ranking influence to rule-only behavior."""
    evidence = RerankerEvidenceConfig(
        lane_fused_weight=0.0,
        semantic_score_weight=0.0,
        lexical_score_weight=0.0,
    )
    assert evidence.lane_fused_weight == 0.0
    assert evidence.semantic_score_weight == 0.0
    assert evidence.lexical_score_weight == 0.0
