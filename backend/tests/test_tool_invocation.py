from __future__ import annotations

from agent.execution.tool_invocation import (
    SearchHistoryTracker,
    pre_execution_skip_result,
)
from agent.types import ToolCall


def _tc(name: str, arguments: dict | None = None) -> ToolCall:
    return ToolCall(id=f"call-{name}", name=name, arguments=arguments or {})


def test_pre_execution_skip_result_does_not_block_phase2_progression_tools():
    result = pre_execution_skip_result(
        tool_call=_tc("set_candidate_pool"),
        guardrail=None,
        search_history=SearchHistoryTracker(),
    )

    assert result is None


def test_pre_execution_skip_result_still_skips_redundant_search():
    search_history = SearchHistoryTracker()
    call = _tc("web_search", {"query": "东京 三日游"})

    assert (
        pre_execution_skip_result(
            tool_call=call,
            guardrail=None,
            search_history=search_history,
        )
        is None
    )
    assert (
        pre_execution_skip_result(
            tool_call=call,
            guardrail=None,
            search_history=search_history,
        )
        is None
    )

    result = pre_execution_skip_result(
        tool_call=call,
        guardrail=None,
        search_history=search_history,
    )

    assert result is not None
    assert result.status == "skipped"
    assert result.error_code == "REDUNDANT_SEARCH"
