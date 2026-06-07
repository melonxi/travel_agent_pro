from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from evals.trace_models import TraceEvent
from run import RunRecord
from state.models import TravelPlanState
from storage.trace_store import TraceStore
from telemetry.stats import (
    MemoryHitRecord,
    RecallTelemetryRecord,
    SessionStats,
    ToolCallRecord,
    lookup_pricing,
)

logger = logging.getLogger(__name__)


def _timestamp_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


@dataclass
class _RawTraceRecord:
    timestamp: float
    priority: int
    event_type: str
    payload: dict[str, Any]
    phase: int | None = None
    phase2_step: str | None = None
    iteration: int | None = None
    tool_name: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    status: str | None = None
    duration_ms: float | None = None
    cost_usd: float | None = None


def _llm_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = lookup_pricing(model)
    if not pricing:
        return 0.0
    cost = (input_tokens / 1_000_000) * pricing["input"]
    cost += (output_tokens / 1_000_000) * pricing["output"]
    return round(cost, 6)


def _memory_hit_payload(hit: MemoryHitRecord) -> dict[str, Any]:
    return hit.to_dict()


def _recall_payload(hit: RecallTelemetryRecord) -> dict[str, Any]:
    return hit.to_dict()


def _tool_payload(record: ToolCallRecord, side_effect: str) -> dict[str, Any]:
    return {
        "tool_name": record.tool_name,
        "status": record.status,
        "error_code": record.error_code,
        "suggestion": record.suggestion,
        "parallel_group": record.parallel_group,
        "arguments_preview": record.arguments_preview,
        "result_preview": record.result_preview,
        "state_changes": record.state_changes,
        "validation_errors": record.validation_errors,
        "judge_scores": record.judge_scores,
        "side_effect": side_effect,
    }


def build_trace_events_from_stats(
    *,
    run_id: str,
    stats: SessionStats,
    phase2_step: str | None,
    tool_side_effects: dict[str, str],
) -> list[TraceEvent]:
    raw_records: list[_RawTraceRecord] = []

    for record in stats.recall_telemetry:
        raw_records.append(
            _RawTraceRecord(
                timestamp=record.timestamp,
                priority=0,
                event_type="memory_recall",
                payload=_recall_payload(record),
            )
        )

    for record in stats.llm_calls:
        cost = _llm_cost_usd(record.model, record.input_tokens, record.output_tokens)
        raw_records.append(
            _RawTraceRecord(
                timestamp=record.timestamp,
                priority=1,
                event_type="llm_call",
                phase=record.phase,
                phase2_step=phase2_step,
                iteration=record.iteration,
                llm_provider=record.provider,
                llm_model=record.model,
                duration_ms=record.duration_ms,
                cost_usd=cost,
                payload={
                    "provider": record.provider,
                    "model": record.model,
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                    "duration_ms": record.duration_ms,
                    "cost_usd": cost,
                    "phase": record.phase,
                    "iteration": record.iteration,
                },
            )
        )

    for record in stats.tool_calls:
        side_effect = tool_side_effects.get(record.tool_name, "read")
        raw_records.append(
            _RawTraceRecord(
                timestamp=record.timestamp,
                priority=2,
                event_type="tool_call",
                phase=record.phase,
                phase2_step=phase2_step,
                tool_name=record.tool_name,
                status=record.status,
                duration_ms=record.duration_ms,
                payload=_tool_payload(record, side_effect),
            )
        )

    for record in stats.memory_hits:
        raw_records.append(
            _RawTraceRecord(
                timestamp=record.timestamp,
                priority=3,
                event_type="memory_hit",
                payload=_memory_hit_payload(record),
            )
        )

    raw_records.sort(key=lambda item: (item.timestamp, item.priority))

    events: list[TraceEvent] = []
    for index, record in enumerate(raw_records, start=1):
        events.append(
            TraceEvent(
                event_id=f"evt_{run_id}_{index:04d}",
                run_id=run_id,
                sequence=index,
                event_type=record.event_type,
                phase=record.phase,
                phase2_step=record.phase2_step,
                iteration=record.iteration,
                tool_name=record.tool_name,
                llm_provider=record.llm_provider,
                llm_model=record.llm_model,
                status=record.status,
                duration_ms=record.duration_ms,
                cost_usd=record.cost_usd,
                payload=record.payload,
                created_at=_timestamp_iso(record.timestamp),
            )
        )
    return events


async def ensure_trace_run_started(
    *,
    trace_store: TraceStore | None,
    session: dict,
    plan: TravelPlanState,
    run: RunRecord,
) -> None:
    if trace_store is None:
        return
    try:
        await trace_store.create_run(
            run_id=run.run_id,
            session_id=plan.session_id,
            trip_id=getattr(plan, "trip_id", None),
            context_epoch=session.get("current_context_epoch"),
            started_at=_timestamp_iso(run.started_at),
            status=run.status,
        )
    except Exception:
        logger.warning(
            "trace run create failed session=%s run=%s",
            plan.session_id,
            run.run_id,
            exc_info=True,
        )


async def persist_trace_run_safely(
    *,
    trace_store: TraceStore | None,
    session: dict,
    plan: TravelPlanState,
    run: RunRecord,
    tool_side_effects: dict[str, str],
) -> None:
    if trace_store is None:
        return
    stats = session.get("stats")
    if not isinstance(stats, SessionStats):
        return
    try:
        await ensure_trace_run_started(
            trace_store=trace_store,
            session=session,
            plan=plan,
            run=run,
        )
        events = build_trace_events_from_stats(
            run_id=run.run_id,
            stats=stats,
            phase2_step=getattr(plan, "phase2_step", None),
            tool_side_effects=tool_side_effects,
        )
        await trace_store.replace_events(run.run_id, events)
        total_cost_usd = round(sum(event.cost_usd or 0.0 for event in events), 6)
        total_duration_ms = sum(event.duration_ms or 0.0 for event in events)
        await trace_store.update_run_summary(
            run_id=run.run_id,
            ended_at=_timestamp_iso(run.finished_at or time.time()),
            status=run.status,
            final_phase=plan.phase,
            final_phase2_step=getattr(plan, "phase2_step", None),
            total_input_tokens=stats.total_input_tokens,
            total_output_tokens=stats.total_output_tokens,
            total_cost_usd=total_cost_usd,
            total_duration_ms=total_duration_ms,
        )
    except Exception:
        logger.warning(
            "trace persistence failed session=%s run=%s",
            plan.session_id,
            run.run_id,
            exc_info=True,
        )
        try:
            await trace_store.mark_run_trace_failed(run.run_id)
        except Exception:
            logger.warning(
                "trace failure marker failed session=%s run=%s",
                plan.session_id,
                run.run_id,
                exc_info=True,
            )
