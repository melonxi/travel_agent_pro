import pytest
import time
from telemetry.stats import (
    estimate_llm_cost_usd,
    MemoryHitRecord,
    RecallTelemetryRecord,
    SessionStats,
    LLMCallRecord,
    ToolCallRecord,
)


def test_empty_stats():
    stats = SessionStats()
    assert stats.total_input_tokens == 0
    assert stats.total_output_tokens == 0
    assert stats.estimated_cost_usd == 0.0
    assert stats.to_dict()["total_input_tokens"] == 0


def test_record_llm_call():
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai", model="gpt-4o",
        input_tokens=1000, output_tokens=500,
        duration_ms=1200.0, phase=1, iteration=0,
    )
    assert stats.total_input_tokens == 1000
    assert stats.total_output_tokens == 500
    assert len(stats.llm_calls) == 1


def test_record_tool_call():
    stats = SessionStats()
    stats.record_tool_call(
        tool_name="search_flights", duration_ms=350.0,
        status="success", error_code=None, phase=2,
    )
    assert len(stats.tool_calls) == 1
    assert stats.total_tool_duration_ms == 350.0


def test_cost_calculation_gpt4o():
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai", model="gpt-4o",
        input_tokens=1_000_000, output_tokens=1_000_000,
        duration_ms=5000.0, phase=1, iteration=0,
    )
    # gpt-4o: $2.50/1M input + $10.00/1M output = $12.50
    assert abs(stats.estimated_cost_usd - 12.50) < 0.01


def test_cost_calculation_claude():
    stats = SessionStats()
    stats.record_llm_call(
        provider="anthropic", model="claude-sonnet-4-20250514",
        input_tokens=1_000_000, output_tokens=1_000_000,
        duration_ms=5000.0, phase=1, iteration=0,
    )
    # claude-sonnet-4: $3.00/1M input + $15.00/1M output = $18.00
    assert abs(stats.estimated_cost_usd - 18.00) < 0.01


def test_cost_calculation_deepseek_v4_flash_uses_cache_buckets():
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai",
        model="deepseek-v4-flash",
        input_tokens=2_000_000,
        output_tokens=1_000_000,
        duration_ms=5000.0,
        phase=1,
        iteration=0,
        metadata={
            "prompt_cache_hit_tokens": 1_000_000,
            "prompt_cache_miss_tokens": 1_000_000,
        },
    )

    assert stats.estimated_cost_usd == pytest.approx(0.4228)
    model_data = stats.to_dict()["by_model"]["deepseek-v4-flash"]
    assert model_data["prompt_cache_hit_rate"] == 0.5


def test_deepseek_pricing_matches_namespaced_model_ids():
    cost = estimate_llm_cost_usd(
        "deepseek/deepseek-v4-pro",
        input_tokens=2_000_000,
        output_tokens=1_000_000,
        metadata={
            "prompt_cache_hit_tokens": 1_000_000,
            "prompt_cache_miss_tokens": 1_000_000,
        },
    )

    assert cost == pytest.approx(1.308625)


def test_to_dict_structure():
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai", model="gpt-4o",
        input_tokens=100, output_tokens=50,
        duration_ms=500.0, phase=1, iteration=0,
    )
    stats.record_tool_call(
        tool_name="web_search", duration_ms=200.0,
        status="success", error_code=None, phase=1,
    )
    d = stats.to_dict()
    assert "total_input_tokens" in d
    assert "total_output_tokens" in d
    assert "estimated_cost_usd" in d
    assert "llm_call_count" in d
    assert "tool_call_count" in d
    assert "by_model" in d
    assert "by_tool" in d


def test_to_dict_keeps_memory_hit_count_for_real_hits_only():
    stats = SessionStats()
    stats.memory_hits.append(
        MemoryHitRecord(
            sources={"query_profile": 1},
            profile_ids=["m1"],
        )
    )
    stats.recall_telemetry.append(
        RecallTelemetryRecord(
            stage0_decision="force_recall",
            stage0_reason="explicit_profile_history_query",
            stage0_matched_rule="P1",
            stage0_signals={"history": ["我是不是说过"]},
            gate_needs_recall=True,
            gate_intent_type="",
            final_recall_decision="query_recall_enabled",
            fallback_used="none",
            query_plan_source="llm",
            candidate_count=4,
            recall_attempted_but_zero_hit=False,
            reranker_selected_ids=["m1"],
            reranker_final_reason="selected by reranker",
            reranker_fallback="none",
            reranker_per_item_scores={"m1": {"rule_score": 0.71, "final_score": 2.0}},
            reranker_intent_label="profile",
            reranker_selection_metrics={
                "selected_pairwise_similarity_max": None,
                "selected_pairwise_similarity_avg": None,
            },
        )
    )

    d = stats.to_dict()

    assert d["memory_hit_count"] == 1
    assert d["last_memory_recall"]["stage0_decision"] == "force_recall"
    assert d["last_memory_recall"]["stage0_reason"] == "explicit_profile_history_query"
    assert d["last_memory_recall"]["stage0_matched_rule"] == "P1"
    assert d["last_memory_recall"]["stage0_signals"] == {"history": ["我是不是说过"]}
    assert d["last_memory_recall"]["gate_needs_recall"] is True
    assert d["last_memory_recall"]["gate_intent_type"] == ""
    assert (
        d["last_memory_recall"]["final_recall_decision"]
        == "query_recall_enabled"
    )
    assert d["last_memory_recall"]["query_plan_source"] == "llm"
    assert d["last_memory_recall"]["candidate_count"] == 4
    assert d["last_memory_recall"]["recall_attempted_but_zero_hit"] is False
    assert d["last_memory_recall"]["reranker_selected_ids"] == ["m1"]
    assert d["last_memory_recall"]["reranker_final_reason"] == "selected by reranker"
    assert d["last_memory_recall"]["reranker_fallback"] == "none"
    assert d["last_memory_recall"]["reranker_intent_label"] == "profile"
    assert d["last_memory_recall"]["reranker_per_item_scores"]["m1"]["rule_score"] == 0.71
    assert (
        d["last_memory_recall"]["reranker_selection_metrics"][
            "selected_pairwise_similarity_max"
        ]
        is None
    )


def test_recall_telemetry_record_serializes_dual_recall_fields():
    record = RecallTelemetryRecord(
        profile_reranker_selected_ids=["p1"],
        episode_reranker_selected_ids=["e1"],
        profile_reranker_final_reason="profile rerank selected 1 items",
        episode_reranker_final_reason="episode rerank selected 1 items",
        dual_recall_plan={"need_profile": True, "need_episode": True},
        stage3_profile={"source": "profile"},
        stage3_episode={"source": "episode_slice"},
    )

    payload = record.to_dict()

    assert payload["profile_reranker_selected_ids"] == ["p1"]
    assert payload["episode_reranker_selected_ids"] == ["e1"]
    assert payload["dual_recall_plan"] == {
        "need_profile": True,
        "need_episode": True,
    }
