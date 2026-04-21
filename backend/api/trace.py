from __future__ import annotations
from telemetry.stats import (
    SessionStats,
    ToolCallRecord,
    MemoryHitRecord,
    RecallTelemetryRecord,
    LLMCallRecord,
    lookup_pricing,
)
from tools.plan_tools import PLAN_WRITER_TOOL_NAMES

# Fallback write-effect tools — used when no ToolEngine is available
_WRITE_TOOLS = frozenset(
    PLAN_WRITER_TOOL_NAMES
    | {
        "generate_summary",
    }
)


def _collect_state_changes(tool_calls: list[ToolCallRecord]) -> list[dict]:
    """Extract state_changes from plan-writer ToolCallRecords."""
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


def _serialize_memory_hit(hit: MemoryHitRecord) -> dict:
    return {
        "sources": dict(hit.sources),
        "profile_ids": list(hit.profile_ids),
        "working_memory_ids": list(hit.working_memory_ids),
        "slice_ids": list(hit.slice_ids),
        "matched_reasons": list(hit.matched_reasons),
    }


def _serialize_recall_telemetry(hit: RecallTelemetryRecord) -> dict:
    return {
        "stage0_decision": hit.stage0_decision,
        "stage0_reason": hit.stage0_reason,
        "gate_needs_recall": hit.gate_needs_recall,
        "gate_intent_type": hit.gate_intent_type,
        "final_recall_decision": hit.final_recall_decision,
        "fallback_used": hit.fallback_used,
    }


def _classify_significance(iteration: dict) -> str:
    """Classify iteration significance for frontend display priority.

    Returns one of: 'high', 'medium', 'low', 'none'.
    - high:   state_changes, validation_errors, judge_scores, or write-side-effect tools
    - medium: read-side-effect tools only
    - low:    compression_event or memory_hits only (no tools)
    - none:   pure thinking (LLM only, nothing else)
    """
    if iteration.get("state_changes"):
        return "high"
    for tc in iteration.get("tool_calls", []):
        if tc.get("validation_errors") or tc.get("judge_scores"):
            return "high"
    tool_calls = iteration.get("tool_calls", [])
    if tool_calls:
        has_write = any(tc.get("side_effect") == "write" for tc in tool_calls)
        return "high" if has_write else "medium"
    if (
        iteration.get("compression_event")
        or iteration.get("memory_hits")
        or iteration.get("memory_recall")
    ):
        return "low"
    return "none"


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
    memory_hits = list(stats.memory_hits)
    recall_telemetry = list(stats.recall_telemetry)

    tool_idx = 0
    memory_idx = 0
    recall_idx = 0
    for i, llm in enumerate(llm_calls):
        prev_llm_ts = llm_calls[i - 1].timestamp if i > 0 else float("-inf")
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
                    "arguments_preview": tc.arguments_preview,
                    "result_preview": tc.result_preview,
                    "error_code": tc.error_code,
                    "suggestion": tc.suggestion,
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

        memory_hit = None
        if memory_idx < len(memory_hits):
            candidate = memory_hits[memory_idx]
            if prev_llm_ts < candidate.timestamp <= llm.timestamp + 5.0:
                memory_hit = _serialize_memory_hit(candidate)
                memory_idx += 1

        memory_recall = None
        if recall_idx < len(recall_telemetry):
            candidate = recall_telemetry[recall_idx]
            if prev_llm_ts < candidate.timestamp <= llm.timestamp + 5.0:
                memory_recall = _serialize_recall_telemetry(candidate)
                recall_idx += 1

        iter_dict = {
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
            "memory_hits": memory_hit,
            "memory_recall": memory_recall,
        }
        iter_dict["significance"] = _classify_significance(iter_dict)
        iterations.append(iter_dict)

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
                "error_code": tc.error_code,
                "suggestion": tc.suggestion,
                "parallel_group": tc.parallel_group,
                "validation_errors": tc.validation_errors,
                "judge_scores": tc.judge_scores,
            }
        )
        tool_idx += 1

    if remaining_tool_dicts:
        orphan_dict = {
            "index": len(iterations) + 1,
            "phase": remaining_tools[0].phase if remaining_tools else 0,
            "llm_call": None,
            "tool_calls": remaining_tool_dicts,
            "state_changes": _collect_state_changes(remaining_tools),
            "compression_event": None,
            "memory_hits": None,
            "memory_recall": None,
        }
        orphan_dict["significance"] = _classify_significance(orphan_dict)
        iterations.append(orphan_dict)

    return {
        "session_id": session_id,
        "total_iterations": len(iterations),
        "summary": summary,
        "iterations": iterations,
    }
