from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from agent.compaction import estimate_messages_tokens
from agent.internal_tasks import InternalTask
from agent.message_filters import strip_non_initial_system_messages
from agent.narration import compute_narration
from agent.tagged_context import runtime_notice_message
from agent.types import Message, Role, ToolCall
from llm.types import ChunkType, LLMChunk
from run import IterationProgress
from storage.trace_redaction import stable_content_hash
from telemetry.stats import estimate_llm_cost_usd
from telemetry.trace_recorder import TraceContext, TraceRecorder

logger = logging.getLogger(__name__)


@dataclass
class LlmTurnOutcome:
    text_chunks: list[str]
    tool_calls: list[ToolCall]
    provider_state: dict[str, Any] | None
    progress: IterationProgress
    next_iteration_idx: int
    previous_phase2_step: str | None
    llm_call_event_id: str | None = None
    llm_output_event_id: str | None = None
    correlation_id: str | None = None


def _attr_value(obj: Any, name: str, default: Any = None) -> Any:
    value = getattr(obj, name, default)
    return value() if callable(value) else value


def _message_payload(messages: list[Message]) -> list[dict[str, Any]]:
    return [message.to_dict() for message in messages]


def _content_preview(value: Any, limit: int = 500) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _usage_tokens(usage_info: dict[str, Any] | None, key: str) -> int:
    if not usage_info:
        return 0
    value = usage_info.get(key)
    if isinstance(value, int):
        return value
    if key == "input_tokens":
        fallback = usage_info.get("prompt_tokens")
    elif key == "output_tokens":
        fallback = usage_info.get("completion_tokens")
    else:
        fallback = None
    return fallback if isinstance(fallback, int) else 0


def _trace_context_for_turn(
    trace_context: TraceContext | None,
    *,
    current_phase: int,
    phase2_step: str | None,
    correlation_id: str,
    actor: str,
) -> TraceContext | None:
    if trace_context is None:
        return None
    return TraceContext(
        run_id=trace_context.run_id,
        session_id=trace_context.session_id,
        trip_id=trace_context.trip_id,
        context_epoch=trace_context.context_epoch,
        phase=current_phase,
        phase2_step=phase2_step,
        parent_event_id=trace_context.parent_event_id,
        root_event_id=trace_context.root_event_id,
        correlation_id=correlation_id,
        actor=actor,
        metadata=dict(trace_context.metadata),
    )


def _merge_provider_state_delta(
    provider_state: dict[str, Any], delta: dict[str, Any]
) -> None:
    for key, value in delta.items():
        if isinstance(value, str) and isinstance(provider_state.get(key), str):
            provider_state[key] += value
        else:
            provider_state[key] = value


async def run_llm_turn(
    *,
    llm: Any,
    tool_engine: Any,
    hooks: Any,
    messages: list[Message],
    tools: list[dict],
    current_phase: int,
    plan: Any | None,
    reflection: Any | None,
    tool_choice_decider: Any | None,
    compression_events: list[dict],
    iteration_idx: int,
    previous_iteration_had_tools: bool,
    phase_changed_in_previous_iteration: bool,
    previous_phase2_step: str | None,
    check_cancelled: Callable[[], None],
    update_progress: Callable[[IterationProgress], None],
    trace_recorder: TraceRecorder | None = None,
    trace_context: TraceContext | None = None,
) -> AsyncIterator[LLMChunk | LlmTurnOutcome]:
    await hooks.run(
        "before_llm_call",
        messages=messages,
        phase=current_phase,
        tools=tools,
    )

    if compression_events:
        compaction_started_at = time.time()
        yield LLMChunk(
            type=ChunkType.INTERNAL_TASK,
            internal_task=InternalTask(
                id=f"context_compaction:{iteration_idx}",
                kind="context_compaction",
                label="上下文整理",
                status="pending",
                message="正在整理上下文以控制提示词长度…",
                blocking=True,
                scope="turn",
                started_at=compaction_started_at,
            ),
        )
        yield LLMChunk(
            type=ChunkType.AGENT_STATUS,
            agent_status={"stage": "compacting"},
        )
    else:
        compaction_started_at = None

    last_compression_info: dict[str, Any] | None = None
    while compression_events:
        info = compression_events.pop(0)
        last_compression_info = dict(info)
        if trace_recorder is not None and trace_context is not None:
            compacted_summary_hash = info.get("compacted_summary_hash")
            if compacted_summary_hash is None and info.get("summary_text") is not None:
                compacted_summary_hash = stable_content_hash(info.get("summary_text"))
            if compacted_summary_hash is not None:
                last_compression_info["compacted_summary_hash"] = compacted_summary_hash
            compression_trace = _trace_context_for_turn(
                trace_context,
                current_phase=current_phase,
                phase2_step=(
                    getattr(plan, "phase2_step", None) if plan is not None else None
                ),
                correlation_id=f"context_compression_{iteration_idx}_{uuid.uuid4().hex}",
                actor="context_manager",
            )
            await trace_recorder.emit_event(
                compression_trace,
                event_type="context_compression",
                status="success",
                iteration=iteration_idx,
                payload={
                    "mode": info.get("mode"),
                    "reason": info.get("reason"),
                    "message_count_before": info.get("message_count_before"),
                    "message_count_after": info.get("message_count_after"),
                    "compressed_count": info.get("compressed_count"),
                    "must_keep_count": info.get("must_keep_count"),
                    "estimated_tokens_before": info.get("estimated_tokens_before"),
                    "estimated_tokens_after": info.get("estimated_tokens_after"),
                    "compression_info_hash": stable_content_hash(info),
                    "compacted_summary_hash": compacted_summary_hash,
                },
            )
        yield LLMChunk(
            type=ChunkType.CONTEXT_COMPRESSION,
            compression_info=info,
        )

    if compaction_started_at is not None:
        yield LLMChunk(
            type=ChunkType.INTERNAL_TASK,
            internal_task=InternalTask(
                id=f"context_compaction:{iteration_idx}",
                kind="context_compaction",
                label="上下文整理",
                status="success",
                message="上下文整理完成",
                blocking=True,
                scope="turn",
                started_at=compaction_started_at,
                ended_at=time.time(),
            ),
        )

    stage = (
        "summarizing"
        if previous_iteration_had_tools and not phase_changed_in_previous_iteration
        else "thinking"
    )
    hint = compute_narration(plan) if plan else None
    yield LLMChunk(
        type=ChunkType.AGENT_STATUS,
        agent_status={
            "stage": stage,
            "iteration": iteration_idx,
            "hint": hint,
        },
    )
    next_iteration_idx = iteration_idx + 1

    next_previous_phase2_step = previous_phase2_step
    if reflection is not None and plan is not None:
        reflection_msg = reflection.check_and_inject(
            messages,
            plan,
            previous_phase2_step,
        )
        if reflection_msg:
            messages.append(runtime_notice_message("reflection", reflection_msg))
            now = time.time()
            yield LLMChunk(
                type=ChunkType.INTERNAL_TASK,
                internal_task=InternalTask(
                    id=f"reflection:{next_iteration_idx - 1}",
                    kind="reflection",
                    label="反思注入",
                    status="success",
                    message="已注入阶段自检提示",
                    blocking=False,
                    scope="turn",
                    result={"message": reflection_msg},
                    started_at=now,
                    ended_at=now,
                ),
            )
        next_previous_phase2_step = getattr(plan, "phase2_step", None)

    tool_choice = "auto"
    if tool_choice_decider is not None and plan is not None:
        tool_choice = tool_choice_decider.decide(
            plan,
            messages,
            current_phase,
        )

    chat_kwargs: dict[str, Any] = {
        "tools": tools,
        "stream": True,
    }
    if tool_choice != "auto":
        chat_kwargs["tool_choice"] = tool_choice

    strip_non_initial_system_messages(
        messages,
        logger=logger,
        context="main AgentLoop LLM prompt",
    )

    correlation_id = f"llm_turn_{iteration_idx}_{uuid.uuid4().hex}"
    phase2_step = getattr(plan, "phase2_step", None) if plan is not None else None
    context_event = None
    llm_call_event = None
    prompt_payload = _message_payload(messages)
    tools_schema_hash = stable_content_hash(tools)
    system_prompt = next(
        (
            message.content
            for message in messages
            if message.role == Role.SYSTEM and message.content is not None
        ),
        "",
    )
    system_prompt_hash = stable_content_hash(system_prompt)
    plan_snapshot = (
        plan.to_dict() if plan is not None and hasattr(plan, "to_dict") else None
    )
    plan_snapshot_hash = (
        stable_content_hash(plan_snapshot) if plan_snapshot is not None else None
    )
    if trace_recorder is not None and trace_context is not None:
        context_trace = _trace_context_for_turn(
            trace_context,
            current_phase=current_phase,
            phase2_step=phase2_step,
            correlation_id=correlation_id,
            actor="context_manager",
        )
        trace_metadata = dict(trace_context.metadata)
        phase_prompt_id = trace_metadata.get("phase_prompt_id") or (
            f"phase:{current_phase}:step:{phase2_step or 'none'}"
        )
        phase_prompt_hash = trace_metadata.get("phase_prompt_hash")
        context_event = await trace_recorder.emit_event(
            context_trace,
            event_type="context_build",
            status="success",
            iteration=iteration_idx,
            payload={
                "message_count": len(messages),
                "tool_names": [tool.get("name") for tool in tools if isinstance(tool, dict)],
                "tool_count": len(tools),
                "tool_schema_hash": tools_schema_hash,
                "system_prompt_hash": system_prompt_hash,
                "phase_prompt_id": phase_prompt_id,
                "phase_prompt_hash": phase_prompt_hash or system_prompt_hash,
                "plan_snapshot_hash": plan_snapshot_hash,
                "prompt_hash": stable_content_hash(prompt_payload),
                "estimated_prompt_tokens": estimate_messages_tokens(messages, tools=tools),
                "memory_candidate_ids": list(
                    trace_metadata.get("memory_candidate_ids") or []
                ),
                "memory_hit_event_id": trace_metadata.get("memory_hit_event_id"),
                "memory_context_hash": trace_metadata.get("memory_context_hash"),
                "message_count_before_compaction": (
                    last_compression_info.get("message_count_before")
                    if last_compression_info
                    else len(messages)
                ),
                "message_count_after_compaction": (
                    last_compression_info.get("message_count_after")
                    if last_compression_info
                    else len(messages)
                ),
                "compacted_summary_hash": (
                    last_compression_info.get("compacted_summary_hash")
                    if last_compression_info
                    else None
                ),
            },
        )
        if context_event is not None:
            await trace_recorder.attach_artifact(
                context_trace,
                event_id=context_event.event_id,
                kind="context_snapshot",
                content={"messages": prompt_payload, "tools": tools},
            )

        llm_trace = _trace_context_for_turn(
            trace_context,
            current_phase=current_phase,
            phase2_step=phase2_step,
            correlation_id=correlation_id,
            actor="main_agent",
        )
        llm_call_event = await trace_recorder.emit_event(
            llm_trace,
            event_type="llm_call",
            iteration=iteration_idx,
            llm_provider=str(_attr_value(llm, "provider_name", "unknown")),
            llm_model=str(_attr_value(llm, "model", "unknown")),
            status="started",
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            parent_event_id=(
                context_event.event_id if context_event is not None else None
            ),
            root_event_id=(
                context_event.root_event_id or context_event.event_id
                if context_event is not None
                else None
            ),
            payload={
                "provider": str(_attr_value(llm, "provider_name", "unknown")),
                "model": str(_attr_value(llm, "model", "unknown")),
                "temperature": _attr_value(llm, "temperature", None),
                "max_tokens": _attr_value(llm, "max_tokens", None),
                "tool_choice": tool_choice,
                "stream": True,
                "tool_names": [tool.get("name") for tool in tools if isinstance(tool, dict)],
                "tool_schema_hash": tools_schema_hash,
                "system_prompt_hash": system_prompt_hash,
                "plan_snapshot_hash": plan_snapshot_hash,
                "prompt_hash": stable_content_hash(prompt_payload),
                "message_count": len(messages),
                "estimated_prompt_tokens": estimate_messages_tokens(messages, tools=tools),
            },
        )
        if llm_call_event is not None:
            await trace_recorder.attach_artifact(
                llm_trace,
                event_id=llm_call_event.event_id,
                kind="llm_prompt",
                content={"messages": prompt_payload, "tools": tools},
            )

    tool_calls: list[ToolCall] = []
    text_chunks: list[str] = []
    provider_state: dict[str, Any] = {}
    progress = IterationProgress.NO_OUTPUT
    usage_info: dict[str, Any] | None = None
    llm_started_at = time.monotonic()

    try:
        async for chunk in llm.chat(messages, **chat_kwargs):
            check_cancelled()
            if chunk.type == ChunkType.PROVIDER_STATE_DELTA:
                if chunk.provider_state:
                    _merge_provider_state_delta(provider_state, chunk.provider_state)
            elif chunk.type == ChunkType.TEXT_DELTA:
                if progress == IterationProgress.NO_OUTPUT:
                    progress = IterationProgress.PARTIAL_TEXT
                    update_progress(progress)
                text_chunks.append(chunk.content or "")
                yield chunk
            elif chunk.type == ChunkType.USAGE:
                usage_info = dict(chunk.usage_info or {})
                yield chunk
            elif chunk.type == ChunkType.TOOL_CALL_START and chunk.tool_call:
                progress = IterationProgress.PARTIAL_TOOL_CALL
                update_progress(progress)
                if chunk.tool_call.human_label is None:
                    tool_def = tool_engine.get_tool(chunk.tool_call.name)
                    if tool_def is not None:
                        chunk.tool_call.human_label = tool_def.human_label
                tool_calls.append(chunk.tool_call)
                yield chunk
            elif chunk.type == ChunkType.DONE:
                pass
    except Exception as exc:
        if trace_recorder is not None and trace_context is not None:
            llm_trace = _trace_context_for_turn(
                trace_context,
                current_phase=current_phase,
                phase2_step=phase2_step,
                correlation_id=correlation_id,
                actor="main_agent",
            )
            await trace_recorder.emit_event(
                llm_trace,
                event_type="error",
                iteration=iteration_idx,
                llm_provider=str(_attr_value(llm, "provider_name", "unknown")),
                llm_model=str(_attr_value(llm, "model", "unknown")),
                status="error",
                parent_event_id=(
                    llm_call_event.event_id if llm_call_event is not None else None
                ),
                root_event_id=(
                    llm_call_event.root_event_id or llm_call_event.event_id
                    if llm_call_event is not None
                    else None
                ),
                duration_ms=(time.monotonic() - llm_started_at) * 1000,
                payload={
                    "error_type": type(exc).__name__,
                    "error_preview": _content_preview(exc),
                },
            )
        raise

    llm_output_event = None
    if trace_recorder is not None and trace_context is not None:
        duration_ms = (time.monotonic() - llm_started_at) * 1000
        output_text = "".join(text_chunks)
        input_tokens = _usage_tokens(usage_info, "input_tokens")
        output_tokens = _usage_tokens(usage_info, "output_tokens")
        model = str(_attr_value(llm, "model", "unknown"))
        llm_trace = _trace_context_for_turn(
            trace_context,
            current_phase=current_phase,
            phase2_step=phase2_step,
            correlation_id=correlation_id,
            actor="llm_provider",
        )
        llm_output_event = await trace_recorder.emit_event(
            llm_trace,
            event_type="llm_output",
            iteration=iteration_idx,
            llm_provider=str(_attr_value(llm, "provider_name", "unknown")),
            llm_model=model,
            status="success",
            duration_ms=duration_ms,
            cost_usd=round(
                estimate_llm_cost_usd(model, input_tokens, output_tokens, usage_info),
                6,
            ),
            parent_event_id=(
                llm_call_event.event_id if llm_call_event is not None else None
            ),
            root_event_id=(
                llm_call_event.root_event_id or llm_call_event.event_id
                if llm_call_event is not None
                else None
            ),
            payload={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "usage": usage_info or {},
                "duration_ms": duration_ms,
                "output_hash": stable_content_hash(
                    {
                        "text": output_text,
                        "tool_calls": [tool_call.__dict__ for tool_call in tool_calls],
                    }
                ),
                "output_preview": _content_preview(output_text),
                "tool_call_ids": [tool_call.id for tool_call in tool_calls],
                "tool_call_names": [tool_call.name for tool_call in tool_calls],
            },
        )
        if llm_output_event is not None:
            await trace_recorder.attach_artifact(
                llm_trace,
                event_id=llm_output_event.event_id,
                kind="llm_response",
                content={
                    "text": output_text,
                    "tool_calls": [tool_call.__dict__ for tool_call in tool_calls],
                    "usage": usage_info or {},
                },
            )

    yield LlmTurnOutcome(
        text_chunks=text_chunks,
        tool_calls=tool_calls,
        provider_state=provider_state or None,
        progress=progress,
        next_iteration_idx=next_iteration_idx,
        previous_phase2_step=next_previous_phase2_step,
        llm_call_event_id=(
            llm_call_event.event_id if llm_call_event is not None else None
        ),
        llm_output_event_id=(
            llm_output_event.event_id if llm_output_event is not None else None
        ),
        correlation_id=correlation_id,
    )
