import pytest
from api.trace import _classify_significance, build_trace
from telemetry.stats import SessionStats


class TestClassifySignificance:
    """Unit tests for _classify_significance()."""

    def test_high_when_state_changes(self):
        iteration = {
            "tool_calls": [
                {
                    "name": "update_trip_basics",
                    "side_effect": "write",
                    "validation_errors": None,
                    "judge_scores": None,
                }
            ],
            "state_changes": [
                {"field": "destination", "before": None, "after": "東京"}
            ],
            "compression_event": None,
            "memory_hits": None,
        }
        assert _classify_significance(iteration) == "high"

    def test_high_when_validation_errors(self):
        iteration = {
            "tool_calls": [
                {
                    "name": "replace_all_day_plans",
                    "side_effect": "write",
                    "validation_errors": ["時間冲突"],
                    "judge_scores": None,
                }
            ],
            "state_changes": [],
            "compression_event": None,
            "memory_hits": None,
        }
        assert _classify_significance(iteration) == "high"

    def test_high_when_judge_scores(self):
        iteration = {
            "tool_calls": [
                {
                    "name": "save_day_plan",
                    "side_effect": "write",
                    "validation_errors": None,
                    "judge_scores": {"pace": 4},
                }
            ],
            "state_changes": [],
            "compression_event": None,
            "memory_hits": None,
        }
        assert _classify_significance(iteration) == "high"

    def test_high_when_write_tool(self):
        iteration = {
            "tool_calls": [
                {
                    "name": "update_trip_basics",
                    "side_effect": "write",
                    "validation_errors": None,
                    "judge_scores": None,
                }
            ],
            "state_changes": [],
            "compression_event": None,
            "memory_hits": None,
        }
        assert _classify_significance(iteration) == "high"

    def test_medium_when_read_tools(self):
        iteration = {
            "tool_calls": [
                {
                    "name": "web_search",
                    "side_effect": "read",
                    "validation_errors": None,
                    "judge_scores": None,
                }
            ],
            "state_changes": [],
            "compression_event": None,
            "memory_hits": None,
        }
        assert _classify_significance(iteration) == "medium"

    def test_low_when_compression_event(self):
        iteration = {
            "tool_calls": [],
            "state_changes": [],
            "compression_event": "tool_compaction: test",
            "memory_hits": None,
        }
        assert _classify_significance(iteration) == "low"

    def test_low_when_memory_hits(self):
        iteration = {
            "tool_calls": [],
            "state_changes": [],
            "compression_event": None,
            "memory_hits": {
                "item_ids": ["m1"],
                "core": 1,
                "trip": 0,
                "phase": 0,
            },
        }
        assert _classify_significance(iteration) == "low"

    def test_none_when_pure_thinking(self):
        iteration = {
            "tool_calls": [],
            "state_changes": [],
            "compression_event": None,
            "memory_hits": None,
        }
        assert _classify_significance(iteration) == "none"

    def test_high_takes_priority_over_medium(self):
        """Write tool + read tool in same iteration -> high (not medium)."""
        iteration = {
            "tool_calls": [
                {
                    "name": "web_search",
                    "side_effect": "read",
                    "validation_errors": None,
                    "judge_scores": None,
                },
                {
                    "name": "update_trip_basics",
                    "side_effect": "write",
                    "validation_errors": None,
                    "judge_scores": None,
                },
            ],
            "state_changes": [],
            "compression_event": None,
            "memory_hits": None,
        }
        assert _classify_significance(iteration) == "high"


def test_build_trace_includes_significance():
    """build_trace adds significance field to each iteration."""
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        duration_ms=200.0,
        phase=1,
        iteration=1,
    )
    result = build_trace("test", {"stats": stats})
    assert len(result["iterations"]) == 1
    assert result["iterations"][0]["significance"] == "none"


def test_build_trace_significance_with_tools():
    """Iteration with read tool gets medium significance."""
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        duration_ms=200.0,
        phase=1,
        iteration=1,
    )
    stats.record_tool_call(
        tool_name="web_search",
        duration_ms=100.0,
        status="ok",
        error_code=None,
        phase=1,
    )
    result = build_trace("test", {"stats": stats})
    assert result["iterations"][0]["significance"] == "medium"
