from __future__ import annotations

import time
from datetime import date
from typing import Any

from agent.types import ToolResult
from memory.formatter import MemoryRecallTelemetry
from telemetry.stats import SessionStats


def _days_count_from_dates(dates: Any | None) -> int | None:
    if dates is None:
        return None
    start = getattr(dates, "start", None)
    end = getattr(dates, "end", None)
    if not start or not end:
        return None
    try:
        start_date = date.fromisoformat(str(start))
        end_date = date.fromisoformat(str(end))
    except (TypeError, ValueError):
        return None
    return (end_date - start_date).days + 1


def _truncate_preview(value: Any, max_len: int = 120) -> str:
    """Truncate a value to a short preview string."""
    if value is None:
        return ""
    text = str(value) if not isinstance(value, str) else value
    if len(text) > max_len:
        return text[:max_len] + "…"
    return text


def _memory_hit_record_from_recall(
    memory_recall: MemoryRecallTelemetry,
):
    from telemetry.stats import MemoryHitRecord

    if not any(memory_recall.sources.values()):
        return None

    return MemoryHitRecord(
        sources=dict(memory_recall.sources),
        profile_ids=list(memory_recall.profile_ids),
        working_memory_ids=list(memory_recall.working_memory_ids),
        slice_ids=list(memory_recall.slice_ids),
        matched_reasons=list(memory_recall.matched_reasons),
    )


def _recall_telemetry_record_from_recall(
    memory_recall: MemoryRecallTelemetry,
):
    from telemetry.stats import RecallTelemetryRecord

    return RecallTelemetryRecord(
        stage0_decision=memory_recall.stage0_decision,
        stage0_reason=memory_recall.stage0_reason,
        stage0_matched_rule=memory_recall.stage0_matched_rule,
        stage0_signals=dict(memory_recall.stage0_signals),
        gate_needs_recall=memory_recall.gate_needs_recall,
        gate_intent_type=memory_recall.gate_intent_type,
        final_recall_decision=memory_recall.final_recall_decision,
        fallback_used=memory_recall.fallback_used,
        recall_skip_source=memory_recall.recall_skip_source,
        query_plan_source=memory_recall.query_plan_source,
        candidate_count=memory_recall.candidate_count,
        recall_attempted_but_zero_hit=memory_recall.recall_attempted_but_zero_hit,
        reranker_selected_ids=list(memory_recall.reranker_selected_ids),
        reranker_final_reason=memory_recall.reranker_final_reason,
        reranker_fallback=memory_recall.reranker_fallback,
        reranker_per_item_reason=dict(memory_recall.reranker_per_item_reason),
        reranker_per_item_scores=dict(memory_recall.reranker_per_item_scores),
        reranker_intent_label=memory_recall.reranker_intent_label,
        reranker_selection_metrics=dict(memory_recall.reranker_selection_metrics),
    )


def _plan_writer_updates(
    tool_name: str,
    arguments: dict[str, Any],
    result_data: dict[str, Any],
) -> list[dict[str, Any]]:
    if tool_name == "update_trip_basics":
        updated_fields = result_data.get("updated_fields")
        if not isinstance(updated_fields, list):
            return []
        return [
            {"field": field, "value": arguments.get(field)}
            for field in updated_fields
            if isinstance(field, str) and field in arguments
        ]

    if tool_name == "request_backtrack":
        return []

    mapping: dict[str, tuple[str, Any]] = {
        "set_trip_brief": ("trip_brief", arguments.get("fields")),
        "set_candidate_pool": ("candidate_pool", arguments.get("pool")),
        "set_shortlist": ("shortlist", arguments.get("items")),
        "set_skeleton_plans": ("skeleton_plans", arguments.get("plans")),
        "select_skeleton": ("selected_skeleton_id", arguments.get("id")),
        "set_transport_options": ("transport_options", arguments.get("options")),
        "select_transport": ("selected_transport", arguments.get("choice")),
        "set_accommodation_options": (
            "accommodation_options",
            arguments.get("options"),
        ),
        "set_accommodation": (
            "accommodation",
            {"area": arguments.get("area"), "hotel": arguments.get("hotel")},
        ),
        "set_risks": ("risks", arguments.get("list")),
        "set_alternatives": ("alternatives", arguments.get("list")),
        "add_preferences": ("preferences", arguments.get("items")),
        "add_constraints": ("constraints", arguments.get("items")),
        "save_day_plan": (
            "daily_plans",
            {
                "mode": arguments.get("mode"),
                "day": arguments.get("day"),
                "date": arguments.get("date"),
                "activities": arguments.get("activities"),
            },
        ),
        "replace_all_day_plans": ("daily_plans", arguments.get("days")),
    }
    if tool_name not in mapping:
        return []
    field, value = mapping[tool_name]
    return [{"field": field, "value": value}]


def _plan_writer_state_changes(
    tool_name: str,
    arguments: dict[str, Any],
    result_data: dict[str, Any],
) -> list[dict[str, Any]]:
    updates = _plan_writer_updates(tool_name, arguments, result_data)
    if not updates:
        return []

    updated_field = result_data.get("updated_field")
    if isinstance(updated_field, str) and (
        "previous_value" in result_data or "new_value" in result_data
    ):
        return [
            {
                "field": updated_field,
                "before": result_data.get("previous_value"),
                "after": result_data.get("new_value", updates[0]["value"]),
            }
        ]

    return [
        {
            "field": update["field"],
            "before": None,
            "after": update["value"],
        }
        for update in updates
    ]


def _plan_writer_updated_fields(result_data: dict[str, Any]) -> set[str]:
    fields: set[str] = set()
    updated_field = result_data.get("updated_field")
    if isinstance(updated_field, str):
        fields.add(updated_field)
    updated_fields = result_data.get("updated_fields")
    if isinstance(updated_fields, list):
        fields.update(field for field in updated_fields if isinstance(field, str))
    return fields


def _record_tool_result_stats(
    *,
    stats: SessionStats | None,
    tool_call_names: dict[str, str],
    tool_call_args: dict[str, dict],
    result: ToolResult,
    phase: int,
) -> None:
    if stats is None:
        return
    tool_name = tool_call_names.get(result.tool_call_id)
    if not tool_name:
        return
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    duration = metadata.get("duration_ms", 0.0)
    if not isinstance(duration, (int, float)):
        duration = 0.0
    parallel_group = metadata.get("parallel_group")

    # Build previews
    args = tool_call_args.get(result.tool_call_id, {})
    arguments_preview = _truncate_preview(args) if args else ""
    result_preview = _truncate_preview(result.data) if result.data else ""
    if result.error:
        result_preview = _truncate_preview(f"ERROR: {result.error}")

    stats.record_tool_call(
        tool_name=tool_name,
        duration_ms=float(duration),
        status=result.status,
        error_code=result.error_code,
        phase=phase,
        parallel_group=parallel_group,
        arguments_preview=arguments_preview,
        result_preview=result_preview,
        suggestion=getattr(result, "suggestion", None),
    )


def _record_llm_usage_stats(
    *,
    stats: SessionStats | None,
    provider: str,
    model: str,
    usage_info: dict[str, Any],
    started_at: float,
    now: float | None = None,
    phase: int,
    iteration: int,
) -> None:
    if stats is None:
        return
    current = time.monotonic() if now is None else now
    duration_ms = max(0.0, (current - started_at) * 1000)
    stats.record_llm_call(
        provider=provider,
        model=model,
        input_tokens=int(usage_info.get("input_tokens", 0) or 0),
        output_tokens=int(usage_info.get("output_tokens", 0) or 0),
        duration_ms=duration_ms,
        phase=phase,
        iteration=iteration,
    )
