from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.types import ToolCall, ToolResult


SEARCH_TOOLS = {
    "web_search",
    "xiaohongshu_search",
    "xiaohongshu_search_notes",
    "quick_travel_search",
}


@dataclass
class SearchHistoryTracker:
    max_size: int = 20
    recent_queries: list[str] = field(default_factory=list)

    def should_skip_redundant_update(self, tool_call: ToolCall) -> bool:
        if tool_call.name not in SEARCH_TOOLS:
            return False

        argument_name = (
            "keyword" if tool_call.name == "xiaohongshu_search_notes" else "query"
        )
        query = (tool_call.arguments or {}).get(argument_name, "")
        if not isinstance(query, str) or not query.strip():
            return False

        normalized = query.strip().lower()
        count = sum(1 for seen_query in self.recent_queries if seen_query == normalized)
        self.recent_queries.append(normalized)
        if len(self.recent_queries) > self.max_size:
            del self.recent_queries[:-self.max_size]
        return count >= 2


def is_backtrack_result(result: ToolResult) -> bool:
    return (
        result.status == "success"
        and isinstance(result.data, dict)
        and bool(result.data.get("backtracked"))
    )


def build_skipped_tool_result(
    tool_call_id: str,
    *,
    error: str,
    error_code: str,
    suggestion: str,
) -> ToolResult:
    return ToolResult(
        tool_call_id=tool_call_id,
        status="skipped",
        error=error,
        error_code=error_code,
        suggestion=suggestion,
    )


def pre_execution_skip_result(
    *,
    tool_call: ToolCall,
    guardrail: Any | None,
    search_history: SearchHistoryTracker,
) -> ToolResult | None:
    if search_history.should_skip_redundant_update(tool_call):
        query = (tool_call.arguments or {}).get("query", "")
        return build_skipped_tool_result(
            tool_call.id,
            error=f'相同查询 "{query}" 已搜索过多次且未得到新结果。',
            error_code="REDUNDANT_SEARCH",
            suggestion=(
                "请不要重复搜索相同内容。"
                "如果搜索没有找到需要的信息，请换一个查询方向，"
                "或直接根据已有信息推进规划（调用状态写入工具写入产物）。"
            ),
        )

    if guardrail is None:
        return None

    guardrail_result = guardrail.validate_input(tool_call)
    if guardrail_result.allowed:
        return None
    return build_skipped_tool_result(
        tool_call.id,
        error=guardrail_result.reason,
        error_code="GUARDRAIL_REJECTED",
        suggestion=guardrail_result.reason,
    )


def validate_tool_output(
    *,
    guardrail: Any | None,
    tool_call: ToolCall,
    result: ToolResult,
) -> ToolResult:
    if guardrail is None or result.status != "success":
        return result

    output_check = guardrail.validate_output(tool_call.name, result.data)
    if output_check.level != "warn" or not output_check.reason:
        return result
    return ToolResult(
        tool_call_id=result.tool_call_id,
        status=result.status,
        data=result.data,
        metadata=result.metadata,
        suggestion=output_check.reason,
    )


def is_parallel_read_call(
    *,
    parallel_tool_execution: bool,
    tool_engine: Any,
    tool_call: ToolCall,
) -> bool:
    if not parallel_tool_execution:
        return False
    tool_def = tool_engine.get_tool(tool_call.name)
    return tool_def is None or tool_def.side_effect != "write"
