from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from evals.trace_models import TraceEvent
from run import RunRecord
from state.models import TravelPlanState
from storage.trace_store import TraceStore
from telemetry.stats import (
    LLM_CACHE_USAGE_KEYS,
    MemoryHitRecord,
    RecallTelemetryRecord,
    SessionStats,
    ToolCallRecord,
    estimate_llm_cost_usd,
)

logger = logging.getLogger(__name__)

_TRACE_RUN_OFFSETS_KEY = "_trace_run_stats_offsets"


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


@dataclass(frozen=True)
class _StatsOffsets:
    recall_telemetry: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    memory_hits: int = 0


def _capture_stats_offsets(stats: SessionStats) -> _StatsOffsets:
    return _StatsOffsets(
        recall_telemetry=len(stats.recall_telemetry),
        llm_calls=len(stats.llm_calls),
        tool_calls=len(stats.tool_calls),
        memory_hits=len(stats.memory_hits),
    )


def _coerce_offset(offsets: Mapping[str, Any] | _StatsOffsets | None, key: str) -> int:
    if offsets is None:
        return 0
    if isinstance(offsets, _StatsOffsets):
        value = getattr(offsets, key)
    else:
        value = offsets.get(key, 0)
    return value if isinstance(value, int) and value > 0 else 0


def _slice_from_offset(records: list[Any], offset: int) -> list[Any]:
    return records[min(offset, len(records)) :]


def _get_run_offsets(session: dict, run_id: str) -> _StatsOffsets | None:
    all_offsets = session.get(_TRACE_RUN_OFFSETS_KEY)
    if not isinstance(all_offsets, dict):
        return None
    offsets = all_offsets.get(run_id)
    return offsets if isinstance(offsets, _StatsOffsets) else None


def _ensure_run_offsets(session: dict, run_id: str, stats: SessionStats) -> None:
    all_offsets = session.setdefault(_TRACE_RUN_OFFSETS_KEY, {})
    if isinstance(all_offsets, dict) and run_id not in all_offsets:
        all_offsets[run_id] = _capture_stats_offsets(stats)


def _llm_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    metadata: dict[str, Any] | None = None,
) -> float:
    return round(
        estimate_llm_cost_usd(model, input_tokens, output_tokens, metadata),
        6,
    )


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
        "metadata": dict(record.metadata or {}),
    }


def _llm_cache_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: metadata[key] for key in LLM_CACHE_USAGE_KEYS if key in metadata}


def build_trace_events_from_stats(
    *,
    run_id: str,
    stats: SessionStats,
    phase2_step: str | None,
    tool_side_effects: dict[str, str],
    offsets: Mapping[str, Any] | _StatsOffsets | None = None,
) -> list[TraceEvent]:
    raw_records: list[_RawTraceRecord] = []

    recall_records = _slice_from_offset(
        stats.recall_telemetry,
        _coerce_offset(offsets, "recall_telemetry"),
    )
    llm_records = _slice_from_offset(
        stats.llm_calls,
        _coerce_offset(offsets, "llm_calls"),
    )
    tool_records = _slice_from_offset(
        stats.tool_calls,
        _coerce_offset(offsets, "tool_calls"),
    )
    memory_records = _slice_from_offset(
        stats.memory_hits,
        _coerce_offset(offsets, "memory_hits"),
    )

    for record in recall_records:
        raw_records.append(
            _RawTraceRecord(
                timestamp=record.timestamp,
                priority=0,
                event_type="memory_recall",
                payload=_recall_payload(record),
            )
        )

    for record in llm_records:
        metadata = dict(record.metadata or {})
        cost = _llm_cost_usd(
            record.model,
            record.input_tokens,
            record.output_tokens,
            metadata,
        )
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
                    "metadata": metadata,
                    **_llm_cache_payload(metadata),
                },
            )
        )

    for record in tool_records:
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

    for record in memory_records:
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
    capture_stats_offset: bool = True,
) -> None:
    stats = session.get("stats")
    if capture_stats_offset and isinstance(stats, SessionStats):
        _ensure_run_offsets(session, run.run_id, stats)
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
            capture_stats_offset=False,
        )
        offsets = _get_run_offsets(session, run.run_id)
        events = build_trace_events_from_stats(
            run_id=run.run_id,
            stats=stats,
            phase2_step=getattr(plan, "phase2_step", None),
            tool_side_effects=tool_side_effects,
            offsets=offsets,
        )
        await trace_store.replace_events(run.run_id, events)
        total_cost_usd = round(sum(event.cost_usd or 0.0 for event in events), 6)
        total_duration_ms = sum(event.duration_ms or 0.0 for event in events)
        total_input_tokens = sum(
            int(event.payload.get("input_tokens", 0) or 0)
            for event in events
            if event.event_type == "llm_call"
        )
        total_output_tokens = sum(
            int(event.payload.get("output_tokens", 0) or 0)
            for event in events
            if event.event_type == "llm_call"
        )
        await trace_store.update_run_summary(
            run_id=run.run_id,
            ended_at=_timestamp_iso(run.finished_at or time.time()),
            status=run.status,
            final_phase=plan.phase,
            final_phase2_step=getattr(plan, "phase2_step", None),
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
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
