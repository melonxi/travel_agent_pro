from config import MemoryRetrievalConfig, Stage3RecallConfig, load_config


def test_memory_retrieval_config_stage3_defaults():
    cfg = MemoryRetrievalConfig()

    assert isinstance(cfg.stage3, Stage3RecallConfig)
    assert cfg.stage3.symbolic.enabled is True
    assert cfg.stage3.lexical.enabled is False
    assert cfg.stage3.semantic.enabled is False
    assert cfg.stage3.entity.enabled is False
    assert cfg.stage3.temporal.enabled is False
    assert cfg.stage3.destination_normalization_enabled is False
    assert cfg.stage3.source_widening.enabled is False
    assert cfg.stage3.fusion.lane_weights == (
        ("symbolic", 1.0),
        ("lexical", 0.6),
        ("semantic", 0.8),
        ("entity", 0.4),
        ("temporal", 0.2),
    )


def test_memory_retrieval_config_reranker_defaults_include_evidence_blocks():
    cfg = MemoryRetrievalConfig()

    assert cfg.reranker.small_candidate_set_threshold == 3
    assert cfg.reranker.evidence.symbolic_hit_weight == 0.0
    assert cfg.reranker.evidence.lexical_hit_weight == 0.0
    assert cfg.reranker.evidence.semantic_hit_weight == 0.0
    assert cfg.reranker.evidence.lane_fused_weight == 0.0
    assert cfg.reranker.dynamic_budget.enabled is False
    assert dict(cfg.reranker.intent_weights)["profile"].profile_source_prior == 1.0


def test_load_config_reranker_missing_blocks_fall_back_to_defaults(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """\
memory:
  retrieval:
    reranker:
      hybrid_top_n: 5
""",
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_file))

    assert cfg.memory.retrieval.reranker.hybrid_top_n == 5
    assert cfg.memory.retrieval.reranker.evidence.semantic_score_weight == 0.0
    assert cfg.memory.retrieval.reranker.dynamic_budget.enabled is False


def test_load_config_parses_stage3_recall_config(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """\
memory:
  retrieval:
    stage3:
      destination_normalization_enabled: true
      symbolic:
        enabled: true
        top_k: 11
        timeout_ms: 30
      lexical:
        enabled: true
        top_k: 7
        timeout_ms: 18
      semantic:
        enabled: true
        provider: fastembed
        model_name: BAAI/bge-small-zh-v1.5
        cache_dir: backend/data/embedding_cache
        local_files_only: true
        min_score: 0.61
        top_k: 9
      entity:
        enabled: true
        top_k: 5
        timeout_ms: 14
      temporal:
        enabled: true
        top_k: 4
        timeout_ms: 12
      source_widening:
        enabled: true
        min_primary_candidates: 2
        max_secondary_candidates: 1
"""
    )

    cfg = load_config(str(cfg_file))

    assert cfg.memory.retrieval.stage3.destination_normalization_enabled is True
    assert cfg.memory.retrieval.stage3.symbolic.enabled is True
    assert cfg.memory.retrieval.stage3.symbolic.top_k == 11
    assert cfg.memory.retrieval.stage3.symbolic.timeout_ms == 30
    assert cfg.memory.retrieval.stage3.lexical.enabled is True
    assert cfg.memory.retrieval.stage3.lexical.top_k == 7
    assert cfg.memory.retrieval.stage3.lexical.timeout_ms == 18
    assert cfg.memory.retrieval.stage3.semantic.enabled is True
    assert cfg.memory.retrieval.stage3.semantic.provider == "fastembed"
    assert cfg.memory.retrieval.stage3.semantic.model_name == "BAAI/bge-small-zh-v1.5"
    assert cfg.memory.retrieval.stage3.semantic.cache_dir == "backend/data/embedding_cache"
    assert cfg.memory.retrieval.stage3.semantic.local_files_only is True
    assert cfg.memory.retrieval.stage3.semantic.min_score == 0.61
    assert cfg.memory.retrieval.stage3.semantic.top_k == 9
    assert cfg.memory.retrieval.stage3.entity.enabled is True
    assert cfg.memory.retrieval.stage3.entity.top_k == 5
    assert cfg.memory.retrieval.stage3.entity.timeout_ms == 14
    assert cfg.memory.retrieval.stage3.temporal.enabled is True
    assert cfg.memory.retrieval.stage3.temporal.top_k == 4
    assert cfg.memory.retrieval.stage3.temporal.timeout_ms == 12
    assert cfg.memory.retrieval.stage3.source_widening.enabled is True
    assert cfg.memory.retrieval.stage3.source_widening.min_primary_candidates == 2
    assert cfg.memory.retrieval.stage3.source_widening.max_secondary_candidates == 1


def test_load_config_stage3_semantic_null_fields_fall_back_to_defaults(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """\
memory:
  retrieval:
    stage3:
      semantic:
        enabled: true
        provider: null
        model_name: null
        cache_dir: null
      fusion:
        lane_weights:
          - [symbolic, 1.1]
          - [semantic, 0.9]
"""
    )

    cfg = load_config(str(cfg_file))

    assert cfg.memory.retrieval.stage3.semantic.enabled is True
    assert cfg.memory.retrieval.stage3.semantic.provider == "fastembed"
    assert cfg.memory.retrieval.stage3.semantic.model_name == "BAAI/bge-small-zh-v1.5"
    assert cfg.memory.retrieval.stage3.semantic.cache_dir == "backend/data/embedding_cache"
    assert cfg.memory.retrieval.stage3.entity.enabled is False
    assert cfg.memory.retrieval.stage3.temporal.enabled is False
    assert cfg.memory.retrieval.stage3.fusion.lane_weights == (
        ("symbolic", 1.1),
        ("semantic", 0.9),
    )
