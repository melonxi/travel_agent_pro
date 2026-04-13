from __future__ import annotations
from telemetry.stats import SessionStats, _lookup_pricing

# Known write-effect tools — matches tools/*.py where side_effect="write"
_WRITE_TOOLS = frozenset({
    "update_plan_state",
    "assemble_day_plan",
    "generate_summary",
})


def build_trace(session_id: str, session: dict) -> dict:
    """Build structured trace from session's stats data."""
    stats: SessionStats = session.get("stats", SessionStats())
    summary = stats.to_dict()

    # Enrich summary with cost_usd per model
    for model_name, model_data in summary.get("by_model", {}).items():
        pricing = _lookup_pricing(model_name)
        if pricing:
            cost = (model_data["input_tokens"] / 1_000_000) * pricing["input"]
            cost += (model_data["output_tokens"] / 1_000_000) * pricing["output"]
            model_data["cost_usd"] = round(cost, 6)
        else:
            model_data["cost_usd"] = 0.0

    # Enrich by_tool with avg_duration_ms and rename duration_ms to total_duration_ms
    for tool_data in summary.get("by_tool", {}).values():
        calls = tool_data.get("calls", 0)
        total_dur = tool_data.get("duration_ms", 0.0)
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
                "side_effect": "write" if tc.tool_name in _WRITE_TOOLS else "read",
                "arguments_preview": "",
                "result_preview": "",
            })
            tool_idx += 1

        pricing = _lookup_pricing(llm.model)
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

    return {
        "session_id": session_id,
        "total_iterations": len(iterations),
        "summary": summary,
        "iterations": iterations,
    }
