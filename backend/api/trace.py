from __future__ import annotations
from telemetry.stats import (
    SessionStats,
    ToolCallRecord,
    MemoryHitRecord,
    LLMCallRecord,
    lookup_pricing,
)

# Fallback write-effect tools — used when no ToolEngine is available
_WRITE_TOOLS = frozenset(
    {
        "update_plan_state",
        "assemble_day_plan",
        "generate_summary",
    }
)


def _collect_state_changes(tool_calls: list[ToolCallRecord]) -> list[dict]:
    """Extract state_changes from update_plan_state ToolCallRecords."""
    changes = []
    for tc in tool_calls:
        if tc.state_changes:
            changes.extend(tc.state_changes)
    return changes


def _match_compression_event(
    llm_rec: LLMCallRecord,
    events: list[dict] | None,
) -> str | None:
    """Find compression event that occurred just before this LLM call."""
    if not events:
        return None
    best = None
    for evt in events:
        evt_ts = evt.get("timestamp", 0)
        if evt_ts <= llm_rec.timestamp:
            best = evt
    if best:
        mode = best.get("mode", "unknown")
        reason = best.get("reason", "")
        return f"{mode}: {reason}"
    return None


def _match_memory_hits(
    llm_rec: LLMCallRecord,
    hits: list[MemoryHitRecord],
) -> dict | None:
    """Find memory hit record near this LLM call timestamp."""
    if not hits:
        return None
    for hit in hits:
        if abs(hit.timestamp - llm_rec.timestamp) < 5.0:
            return {
                "item_ids": hit.item_ids,
                "core": hit.core_count,
                "trip": hit.trip_count,
                "phase": hit.phase_count,
            }
    return None


def build_trace(session_id: str, session: dict, *, tool_engine=None) -> dict:
    """Build structured trace from session's stats data."""
    stats: SessionStats = session.get("stats", SessionStats())
    compression_events: list[dict] = session.get("compression_events", [])
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
        next_llm_ts = (
            llm_calls[i + 1].timestamp if i + 1 < len(llm_calls) else float("inf")
        )
        iter_tools: list[ToolCallRecord] = []
        iter_tool_dicts = []
        while (
            tool_idx < len(tool_calls) and tool_calls[tool_idx].timestamp < next_llm_ts
        ):
            tc = tool_calls[tool_idx]
            iter_tools.append(tc)
            iter_tool_dicts.append(
                {
                    "name": tc.tool_name,
                    "duration_ms": round(tc.duration_ms, 1),
                    "status": tc.status,
                    "side_effect": _get_side_effect(tc.tool_name),
                    "arguments_preview": "",
                    "result_preview": "",
                    "parallel_group": tc.parallel_group,
                    "validation_errors": tc.validation_errors,
                    "judge_scores": tc.judge_scores,
                }
            )
            tool_idx += 1

        pricing = lookup_pricing(llm.model)
        cost = 0.0
        if pricing:
            cost = (llm.input_tokens / 1_000_000) * pricing["input"]
            cost += (llm.output_tokens / 1_000_000) * pricing["output"]

        iterations.append(
            {
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
                "tool_calls": iter_tool_dicts,
                "state_changes": _collect_state_changes(iter_tools),
                "compression_event": _match_compression_event(llm, compression_events),
                "memory_hits": _match_memory_hits(llm, stats.memory_hits),
            }
        )

    # Handle remaining/orphan tool calls (no parent LLM call)
    remaining_tools: list[ToolCallRecord] = []
    remaining_tool_dicts = []
    while tool_idx < len(tool_calls):
        tc = tool_calls[tool_idx]
        remaining_tools.append(tc)
        remaining_tool_dicts.append(
            {
                "name": tc.tool_name,
                "duration_ms": round(tc.duration_ms, 1),
                "status": tc.status,
                "side_effect": _get_side_effect(tc.tool_name),
                "arguments_preview": "",
                "result_preview": "",
                "parallel_group": tc.parallel_group,
                "validation_errors": tc.validation_errors,
                "judge_scores": tc.judge_scores,
            }
        )
        tool_idx += 1

    if remaining_tool_dicts:
        iterations.append(
            {
                "index": len(iterations) + 1,
                "phase": remaining_tools[0].phase if remaining_tools else 0,
                "llm_call": None,
                "tool_calls": remaining_tool_dicts,
                "state_changes": _collect_state_changes(remaining_tools),
                "compression_event": None,
                "memory_hits": None,
            }
        )

    return {
        "session_id": session_id,
        "total_iterations": len(iterations),
        "summary": summary,
        "iterations": iterations,
    }
