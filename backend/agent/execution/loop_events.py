from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from agent.execution.phase_transition import PhaseTransitionRequest
from agent.execution.tool_batches import ToolBatchOutcome
from agent.internal_tasks import InternalTask
from agent.types import Message
from llm.types import ChunkType, LLMChunk
from storage.trace_redaction import stable_content_hash
from telemetry.trace_recorder import TraceContext

logger = logging.getLogger(__name__)


@dataclass
class PhaseTransitionOutcome:
    messages: list[Message]
    current_phase: int
    tools: list[dict]


async def emit_phase_gate_trace(
    loop: Any,
    *,
    phase_before_batch: int,
    phase2_step_before_batch: str | None,
    transition_detection: Any,
    batch_outcome: ToolBatchOutcome,
) -> None:
    if loop.trace_recorder is None or loop.trace_context is None:
        return
    request = transition_detection.request
    phase_after = loop.plan.phase if loop.plan is not None else phase_before_batch
    phase2_step_after = transition_detection.phase2_step_after_batch
    allowed = request is not None or phase2_step_after != phase2_step_before_batch
    quality_retry_count = None
    for task in transition_detection.internal_tasks:
        if getattr(task, "kind", None) != "quality_gate":
            continue
        result = getattr(task, "result", None)
        if isinstance(result, dict) and isinstance(result.get("retry_count"), int):
            quality_retry_count = result["retry_count"]
            break
    context = TraceContext(
        run_id=loop.trace_context.run_id,
        session_id=loop.trace_context.session_id,
        trip_id=loop.trace_context.trip_id,
        context_epoch=loop.trace_context.context_epoch,
        phase=phase_before_batch,
        phase2_step=phase2_step_before_batch,
        correlation_id=f"phase_gate:{loop.trace_context.run_id}:{phase_before_batch}",
        actor="phase_router",
        metadata=dict(loop.trace_context.metadata),
    )
    event = await loop.trace_recorder.emit_event(
        context,
        event_type="phase_gate",
        status="allowed" if allowed else "blocked",
        actor="phase_router",
        payload={
            "gate_name": "detect_phase_transition",
            "from_phase": phase_before_batch,
            "from_step": phase2_step_before_batch,
            "to_phase_candidate": request.to_phase if request else phase_after,
            "to_step_candidate": phase2_step_after,
            "allowed": allowed,
            "blocked": not allowed,
            "reason": request.reason if request else "no_transition",
            "saw_state_update": batch_outcome.saw_state_update,
            "needs_rebuild": batch_outcome.needs_rebuild,
            "internal_task_count": len(transition_detection.internal_tasks),
            "quality_gate_retry_count": quality_retry_count,
            "gate_input_hash": stable_content_hash(
                {
                    "phase_before_batch": phase_before_batch,
                    "phase2_step_before_batch": phase2_step_before_batch,
                    "phase_after": phase_after,
                    "phase2_step_after": phase2_step_after,
                    "saw_state_update": batch_outcome.saw_state_update,
                    "needs_rebuild": batch_outcome.needs_rebuild,
                }
            ),
            "blockers": [] if allowed else ["no_phase_or_step_change"],
            "warnings": [],
        },
    )
    loop._last_phase_gate_event_id = event.event_id if event is not None else None


async def emit_phase_transition_trace(
    loop: Any,
    *,
    from_phase: int,
    from_step: str | None,
    to_phase: int,
    to_step: str | None,
    reason: str,
) -> None:
    if loop.trace_recorder is None or loop.trace_context is None:
        return
    context = TraceContext(
        run_id=loop.trace_context.run_id,
        session_id=loop.trace_context.session_id,
        trip_id=loop.trace_context.trip_id,
        context_epoch=loop.trace_context.context_epoch,
        phase=from_phase,
        phase2_step=from_step,
        parent_event_id=loop._last_phase_gate_event_id,
        root_event_id=loop._last_phase_gate_event_id,
        correlation_id=(
            f"phase_transition:{loop.trace_context.run_id}:{from_phase}:{to_phase}"
        ),
        actor="phase_router",
        metadata=dict(loop.trace_context.metadata),
    )
    await loop.trace_recorder.emit_event(
        context,
        event_type="phase_transition",
        status="success",
        actor="phase_router",
        parent_event_id=loop._last_phase_gate_event_id,
        root_event_id=loop._last_phase_gate_event_id,
        payload={
            "from_phase": from_phase,
            "from_step": from_step,
            "to_phase": to_phase,
            "to_step": to_step,
            "reason": reason,
        },
    )


async def emit_internal_task_trace(
    loop: Any,
    task: InternalTask,
    *,
    parent_event_id: str | None = None,
) -> None:
    if (
        loop.trace_recorder is None
        or loop.trace_context is None
        or task.kind not in {"quality_gate", "validation", "soft_judge"}
    ):
        return
    result = dict(task.result or {})
    actor = {
        "quality_gate": "quality_gate",
        "validation": "validator",
        "soft_judge": "soft_judge",
    }[task.kind]
    context = TraceContext(
        run_id=loop.trace_context.run_id,
        session_id=loop.trace_context.session_id,
        trip_id=loop.trace_context.trip_id,
        context_epoch=loop.trace_context.context_epoch,
        phase=loop.trace_context.phase,
        phase2_step=loop.trace_context.phase2_step,
        parent_event_id=parent_event_id,
        root_event_id=parent_event_id,
        correlation_id=f"{task.kind}:{loop.trace_context.run_id}:{task.id}",
        actor=actor,
        metadata=dict(loop.trace_context.metadata),
    )
    await loop.trace_recorder.emit_event(
        context,
        event_type=task.kind,
        status=task.status,
        actor=actor,
        parent_event_id=parent_event_id,
        root_event_id=parent_event_id,
        payload={
            "task_id": task.id,
            "label": task.label,
            "message": task.message,
            "blocking": task.blocking,
            "scope": task.scope,
            "related_tool_call_id": task.related_tool_call_id,
            "result": result,
            "rubric_ids": list(result.keys()) if task.kind == "quality_gate" else [],
            "scores": result,
            "blockers": (
                result.get("errors", [])
                if isinstance(result.get("errors"), list)
                else []
            ),
            "feedback": task.message,
            "retry_count": result.get("retry_count"),
            "final_action": result.get("final_action")
            or ("block" if task.status == "warning" and task.blocking else "allow"),
        },
    )


async def notify_context_rebuild(
    loop: Any,
    *,
    messages: list[Message],
    from_phase: int,
    from_phase2_step: str | None,
    to_phase: int,
    to_phase2_step: str | None,
    rebuild_reason: str,
) -> None:
    from_epoch = (
        loop.trace_context.context_epoch if loop.trace_context is not None else None
    )
    to_epoch = from_epoch + 1 if isinstance(from_epoch, int) else None
    try:
        if loop.on_context_rebuild is not None:
            await loop.on_context_rebuild(
                messages=messages,
                from_phase=from_phase,
                from_phase2_step=from_phase2_step,
                to_phase=to_phase,
                to_phase2_step=to_phase2_step,
                rebuild_reason=rebuild_reason,
            )
    except Exception:
        logger.warning(
            "context rebuild callback failed phase=%s phase2_step=%s reason=%s",
            from_phase,
            from_phase2_step,
            rebuild_reason,
            exc_info=True,
        )
    if loop.trace_recorder is None or loop.trace_context is None:
        return
    event_context = TraceContext(
        run_id=loop.trace_context.run_id,
        session_id=loop.trace_context.session_id,
        trip_id=loop.trace_context.trip_id,
        context_epoch=from_epoch,
        phase=from_phase,
        phase2_step=from_phase2_step,
        correlation_id=f"context_rebuild:{loop.trace_context.run_id}:{from_epoch}",
        actor="context_manager",
        metadata=dict(loop.trace_context.metadata),
    )
    await loop.trace_recorder.emit_event(
        event_context,
        event_type="context_rebuild",
        status="success",
        actor="context_manager",
        payload={
            "from_epoch": from_epoch,
            "to_epoch": to_epoch,
            "from_phase": from_phase,
            "from_phase2_step": from_phase2_step,
            "to_phase": to_phase,
            "to_phase2_step": to_phase2_step,
            "rebuild_reason": rebuild_reason,
            "input_message_count": len(messages),
            "input_messages_hash": stable_content_hash(
                [message.to_dict() for message in messages]
            ),
        },
    )
    loop.trace_context = TraceContext(
        run_id=loop.trace_context.run_id,
        session_id=loop.trace_context.session_id,
        trip_id=loop.trace_context.trip_id,
        context_epoch=to_epoch,
        phase=to_phase,
        phase2_step=to_phase2_step,
        correlation_id=loop.trace_context.correlation_id,
        actor=loop.trace_context.actor,
        metadata=dict(loop.trace_context.metadata),
    )


async def handle_phase_transition(
    loop: Any,
    *,
    messages: list[Message],
    request: PhaseTransitionRequest,
    original_user_message: Message,
) -> AsyncIterator[LLMChunk | PhaseTransitionOutcome]:
    await emit_phase_transition_trace(
        loop,
        from_phase=request.from_phase,
        from_step=request.from_step,
        to_phase=request.to_phase,
        to_step=getattr(loop.plan, "phase2_step", None),
        reason=request.reason,
    )
    yield LLMChunk(
        type=ChunkType.PHASE_TRANSITION,
        phase_info={
            "from_phase": request.from_phase,
            "to_phase": request.to_phase,
            "from_step": request.from_step,
            "to_step": getattr(loop.plan, "phase2_step", None),
            "reason": request.reason,
        },
    )
    await loop._flush_before_message_rebuild(
        messages=messages,
        from_phase=request.from_phase,
        from_phase2_step=request.from_step,
    )
    rebuilt_messages = await loop._rebuild_messages_for_phase_change(
        messages=messages,
        from_phase=request.from_phase,
        to_phase=request.to_phase,
        original_user_message=original_user_message,
        result=request.result,
    )
    yield PhaseTransitionOutcome(
        messages=rebuilt_messages,
        current_phase=request.to_phase,
        tools=loop.tool_engine.get_tools_for_phase(request.to_phase, loop.plan),
    )
