from __future__ import annotations
from telemetry.stats import SessionStats, lookup_pricing

# Fallback write-effect tools — used when no ToolEngine is available
_WRITE_TOOLS = frozenset({
    "update_plan_state",
    "assemble_day_plan",
    "generate_summary",
})


def build_trace(session_id: str, session: dict, *, tool_engine=None) -> dict:
    """Build structured trace from session's stats data."""
    stats: SessionStats = session.get("stats", SessionStats())
    summary = stats.to_dict()

    def _get_side_effect(tool_name: str) -> str:
        if tool_engine is not None:
            tool_def = tool_engine._tools.get(tool_name)
            if tool_def is not None:
                return tool_def.side_effect
        return "write" if tool_name in _WRITE_TOOLS else "read"

    # Enrich summary with cost_usd per model
    for model_name, model_data in summary.get("by_model", {}).items():
        pricing = lookup_pricing(model_name)
        if pricing:
            cost = (model_data["input_tokens"] / 1_000_000) * pricing["input"]
            cost += (model_data["output_tokens"] / 1_000_000) * pricing["output"]
            model_data["cost_usd"] = round(cost, 6)
        else:
            model_data["cost_usd"] = 0.0
        model_data.pop("duration_ms", None)

    # Enrich by_tool with avg_duration_ms and rename duration_ms to total_duration_ms
    for tool_data in summary.get("by_tool", {}).values():
        calls = tool_data.get("calls", 0)
        total_dur = tool_data.pop("duration_ms", 0.0)
        tool_data.pop("errors", None)
        tool_data["total_duration_ms"] = total_dur
        tool_data["avg_duration_ms"] = round(total_dur / calls, 1) if calls > 0 else 0.0

    # Build iterations — each LLM call starts a new iteration
    iterations = []
    llm_calls = stats.llm_calls
    tool_calls = list(stats.tool_calls)

    tool_idx = 0
    for i, llm in enumerate(llm_calls):
        next_llm_ts = llm_calls[i + 1].timestamp if i + 1 < len(llm_calls) else float("inf")
        iter_tools = []
        while tool_idx < len(tool_calls) and tool_calls[tool_idx].timestamp < next_llm_ts:
            tc = tool_calls[tool_idx]
            iter_tools.append({
                "name": tc.tool_name,
                "duration_ms": round(tc.duration_ms, 1),
                "status": tc.status,
                "side_effect": _get_side_effect(tc.tool_name),
                "arguments_preview": "",
                "result_preview": "",
            })
            tool_idx += 1

        pricing = lookup_pricing(llm.model)
        cost = 0.0
        if pricing:
            cost = (llm.input_tokens / 1_000_000) * pricing["input"]
            cost += (llm.output_tokens / 1_000_000) * pricing["output"]

        iterations.append({
            "index": i + 1,
            "phase": llm.phase,
            "llm_call": {
                "provider": llm.provider,
                "model": llm.model,
                "input_tokens": llm.input_tokens,
                "output_tokens": llm.output_tokens,
                "duration_ms": round(llm.duration_ms, 1),
                "cost_usd": round(cost, 6),
            },
            "tool_calls": iter_tools,
            "state_changes": [],
            "compression_event": None,
        })

    # Handle remaining/orphan tool calls (no parent LLM call)
    remaining_tools = []
    while tool_idx < len(tool_calls):
        tc = tool_calls[tool_idx]
        remaining_tools.append({
            "name": tc.tool_name,
            "duration_ms": round(tc.duration_ms, 1),
            "status": tc.status,
            "side_effect": _get_side_effect(tc.tool_name),
            "arguments_preview": "",
            "result_preview": "",
        })
        tool_idx += 1

    if remaining_tools:
        iterations.append({
            "index": len(iterations) + 1,
            "phase": tool_calls[tool_idx - len(remaining_tools)].phase if remaining_tools else 0,
            "llm_call": None,
            "tool_calls": remaining_tools,
            "state_changes": [],
            "compression_event": None,
        })

    return {
        "session_id": session_id,
        "total_iterations": len(iterations),
        "summary": summary,
        "iterations": iterations,
    }
