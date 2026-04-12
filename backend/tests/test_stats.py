import pytest
import time
from telemetry.stats import SessionStats, LLMCallRecord, ToolCallRecord


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
        status="success", error_code=None, phase=3,
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
