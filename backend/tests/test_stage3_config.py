from config import MemoryRetrievalConfig, Stage3RecallConfig, load_config


def test_memory_retrieval_config_stage3_defaults():
    cfg = MemoryRetrievalConfig()

    assert isinstance(cfg.stage3, Stage3RecallConfig)
    assert cfg.stage3.symbolic.enabled is True
    assert cfg.stage3.lexical.enabled is False
    assert cfg.stage3.semantic.enabled is True
    assert cfg.stage3.semantic.local_files_only is True
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
    assert cfg.reranker.evidence.lane_fused_weight == 0.25
    assert cfg.reranker.evidence.lexical_score_weight == 0.08
    assert cfg.reranker.evidence.semantic_score_weight == 0.15
    assert cfg.reranker.evidence.destination_match_type_weight == 0.0
    assert cfg.reranker.dynamic_budget.enabled is False
    assert dict(cfg.reranker.intent_weights)["profile"].profile_source_prior == 1.0


def test_memory_reranker_config_has_profile_and_episode_blocks():
    from config import EpisodeRerankConfig, MemoryRerankerConfig, ProfileRerankConfig

    cfg = MemoryRerankerConfig()

    assert isinstance(cfg.profile, ProfileRerankConfig)
    assert isinstance(cfg.episode, EpisodeRerankConfig)
    assert cfg.profile.profile_top_n == 4
    assert cfg.profile.w_bucket == 0.40
    assert cfg.episode.episode_top_n == 3
    assert cfg.episode.w_rel == 0.45


def test_load_config_parses_dual_reranker_blocks(tmp_path):
    from config import load_config

    path = tmp_path / "dual-reranker.yaml"
    path.write_text(
        """
memory:
  retrieval:
    reranker:
      profile:
        profile_top_n: 6
        w_bucket: 0.50
        w_conf: 0.10
        w_match: 0.30
        w_rec: 0.10
        recency_half_life_days: 90
      episode:
        episode_top_n: 5
        w_rel: 0.40
        w_match: 0.30
        w_type: 0.20
        w_rec: 0.10
        recency_half_life_days: 400
""",
        encoding="utf-8",
    )

    cfg = load_config(str(path)).memory.retrieval.reranker

    assert cfg.profile.profile_top_n == 6
    assert cfg.profile.w_bucket == 0.50
    assert cfg.profile.recency_half_life_days == 90
    assert cfg.episode.episode_top_n == 5
    assert cfg.episode.w_rel == 0.40
    assert cfg.episode.recency_half_life_days == 400


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
    assert cfg.memory.retrieval.reranker.evidence.semantic_score_weight == 0.15
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


def test_load_config_production_rollback_yaml_restores_phase_zero_behavior(tmp_path):
    """Production can restore pre-phase-1 behavior by writing the documented rollback snippet."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """\
memory:
  retrieval:
    stage3:
      semantic:
        enabled: false
    reranker:
      evidence:
        lane_fused_weight: 0.0
        semantic_score_weight: 0.0
        lexical_score_weight: 0.0
""",
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_file))

    assert cfg.memory.retrieval.stage3.semantic.enabled is False
    assert cfg.memory.retrieval.reranker.evidence.lane_fused_weight == 0.0
    assert cfg.memory.retrieval.reranker.evidence.semantic_score_weight == 0.0
    assert cfg.memory.retrieval.reranker.evidence.lexical_score_weight == 0.0


def test_stage3_semantic_embedding_index_defaults():
    from config import Stage3SemanticConfig

    semantic = Stage3SemanticConfig()
    index = semantic.embedding_index
    assert index.enabled is False
    assert index.warm_on_write is False
    assert index.warm_buckets == ("constraints", "stable_preferences")
    assert index.max_records_per_user == 20000


def test_load_config_parses_embedding_index_block(tmp_path):
    from config import load_config

    path = tmp_path / "c.yaml"
    path.write_text(
        """
memory:
  retrieval:
    stage3:
      semantic:
        embedding_index:
          enabled: true
          warm_on_write: true
          warm_buckets:
            - stable_preferences
          max_records_per_user: 5000
""",
        encoding="utf-8",
    )
    cfg = load_config(str(path))
    index = cfg.memory.retrieval.stage3.semantic.embedding_index
    assert index.enabled is True
    assert index.warm_on_write is True
    assert index.warm_buckets == ("stable_preferences",)
    assert index.max_records_per_user == 5000
