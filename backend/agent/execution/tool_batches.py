from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from agent.execution.tool_invocation import (
    build_skipped_tool_result,
    is_backtrack_result,
    is_parallel_read_call,
    pre_execution_skip_result,
    validate_tool_output,
)
from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from run import IterationProgress
from storage.trace_redaction import redact_for_trace, stable_content_hash
from telemetry.trace_recorder import TraceContext, TraceRecorder
from tools.plan_tools import PLAN_WRITER_TOOL_NAMES


STATE_DIFF_FIELDS = (
    "destination",
    "dates",
    "budget",
    "travelers",
    "trip_brief",
    "candidate_pool",
    "shortlist",
    "skeleton_plans",
    "selected_skeleton_id",
    "selected_transport",
    "selected_accommodation",
    "daily_plans",
    "deliverables",
)


@dataclass
class ToolBatchOutcome:
    progress: IterationProgress
    saw_state_update: bool
    needs_rebuild: bool
    rebuild_result: ToolResult | None
    next_parallel_group_counter: int


def _preview(value: Any, limit: int = 500) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _trace_context_for_tool(
    trace_context: TraceContext | None,
    *,
    actor: str,
    parent_event_id: str | None,
    root_event_id: str | None,
    correlation_id: str | None,
) -> TraceContext | None:
    if trace_context is None:
        return None
    return TraceContext(
        run_id=trace_context.run_id,
        session_id=trace_context.session_id,
        trip_id=trace_context.trip_id,
        context_epoch=trace_context.context_epoch,
        phase=trace_context.phase,
        phase2_step=trace_context.phase2_step,
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
        correlation_id=correlation_id or trace_context.correlation_id,
        actor=actor,
        metadata=dict(trace_context.metadata),
    )


def _tool_schema_payload(tool_engine: Any, tool_name: str) -> dict[str, Any] | None:
    tool_def = tool_engine.get_tool(tool_name)
    if tool_def is None:
        return None
    to_schema = getattr(tool_def, "to_schema", None)
    if not callable(to_schema):
        return None
    return to_schema()


def _state_snapshot(plan: Any | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    to_dict = getattr(plan, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        return data if isinstance(data, dict) else None
    return None


def _is_writer_tool(tool_engine: Any, tool_name: str) -> bool:
    tool_def = tool_engine.get_tool(tool_name)
    side_effect = getattr(tool_def, "side_effect", None) if tool_def else None
    return side_effect == "write" or tool_name in PLAN_WRITER_TOOL_NAMES


def _state_diff_payload(
    *,
    before: dict[str, Any],
    after: dict[str, Any],
    tool_call: ToolCall,
    result: ToolResult,
) -> dict[str, Any]:
    changed_top_level_fields = sorted(
        field for field in set(before) | set(after) if before.get(field) != after.get(field)
    )
    field_diffs = {}
    for field in STATE_DIFF_FIELDS:
        before_value = before.get(field)
        after_value = after.get(field)
        if before_value == after_value:
            continue
        field_diffs[field] = {
            "before_hash": stable_content_hash(before_value),
            "after_hash": stable_content_hash(after_value),
            "before_present": field in before,
            "after_present": field in after,
        }
    return {
        "tool_call_id": tool_call.id,
        "tool_name": tool_call.name,
        "status": result.status,
        "state_hash_before": stable_content_hash(before),
        "state_hash_after": stable_content_hash(after),
        "changed_top_level_fields": changed_top_level_fields,
        "field_diffs": field_diffs,
        "no_op": before == after,
    }


async def _emit_tool_call_event(
    *,
    trace_recorder: TraceRecorder | None,
    trace_context: TraceContext | None,
    tool_engine: Any,
    tool_call: ToolCall,
    parallel_group: int | None,
    parent_event_id: str | None,
    root_event_id: str | None,
    correlation_id: str | None,
) -> Any | None:
    if trace_recorder is None or trace_context is None:
        return None
    redacted = redact_for_trace(tool_call.arguments or {})
    schema = _tool_schema_payload(tool_engine, tool_call.name)
    tool_def = tool_engine.get_tool(tool_call.name)
    side_effect = getattr(tool_def, "side_effect", "read") if tool_def else "unknown"
    context = _trace_context_for_tool(
        trace_context,
        actor="tool_engine",
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
        correlation_id=correlation_id,
    )
    event = await trace_recorder.emit_event(
        context,
        event_type="tool_call",
        tool_name=tool_call.name,
        status="started",
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
        correlation_id=correlation_id,
        payload={
            "tool_call_id": tool_call.id,
            "tool_name": tool_call.name,
            "arguments_hash": stable_content_hash(redacted.value),
            "arguments_preview": _preview(redacted.value),
            "arguments_redaction_status": redacted.redaction_status,
            "tool_schema_hash": stable_content_hash(schema) if schema else None,
            "side_effect": side_effect,
            "parallel_group": parallel_group,
        },
    )
    if event is not None:
        await trace_recorder.attach_artifact(
            context,
            event_id=event.event_id,
            kind="tool_arguments",
            content=tool_call.arguments or {},
        )
    return event


async def _emit_tool_result_event(
    *,
    trace_recorder: TraceRecorder | None,
    trace_context: TraceContext | None,
    tool_call: ToolCall,
    result: ToolResult,
    tool_call_event: Any | None,
    parallel_group: int | None,
    correlation_id: str | None,
) -> Any | None:
    if trace_recorder is None or trace_context is None:
        return None
    result_body = result.data if result.status == "success" else {
        "error": result.error,
        "error_code": result.error_code,
        "suggestion": result.suggestion,
    }
    redacted = redact_for_trace(result_body)
    duration_ms = None
    if result.metadata and isinstance(result.metadata.get("duration_ms"), (int, float)):
        duration_ms = float(result.metadata["duration_ms"])
    parent_event_id = tool_call_event.event_id if tool_call_event is not None else None
    root_event_id = (
        tool_call_event.root_event_id or tool_call_event.event_id
        if tool_call_event is not None
        else trace_context.root_event_id
    )
    context = _trace_context_for_tool(
        trace_context,
        actor="tool_engine",
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
        correlation_id=correlation_id,
    )
    event = await trace_recorder.emit_event(
        context,
        event_type="tool_result",
        tool_name=tool_call.name,
        status=result.status,
        duration_ms=duration_ms,
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
        correlation_id=correlation_id,
        payload={
            "tool_call_id": tool_call.id,
            "tool_name": tool_call.name,
            "status": result.status,
            "error_code": result.error_code,
            "suggestion": result.suggestion,
            "retryable": result.status == "error",
            "result_hash": stable_content_hash(redacted.value),
            "result_preview": _preview(redacted.value),
            "result_redaction_status": redacted.redaction_status,
            "parallel_group": parallel_group,
            "quality_flags": {
                "usable": result.status == "success" and bool(result.data),
                "empty": result.status == "success" and not bool(result.data),
                "partial": bool(result.metadata and result.metadata.get("partial")),
                "low_confidence": bool(
                    result.metadata and result.metadata.get("low_confidence")
                ),
                "error": result.status == "error",
            },
            "metadata": dict(result.metadata or {}),
            "validation_errors": (
                list(result.metadata.get("validation_errors"))
                if result.metadata and isinstance(result.metadata.get("validation_errors"), list)
                else None
            ),
            "judge_scores": (
                dict(result.metadata.get("judge_scores"))
                if result.metadata and isinstance(result.metadata.get("judge_scores"), dict)
                else None
            ),
        },
    )
    if event is not None:
        result.metadata = dict(result.metadata or {})
        result.metadata["trace_event_id"] = event.event_id
        result.metadata["trace_parent_event_id"] = parent_event_id
        result.metadata["trace_root_event_id"] = root_event_id
        await trace_recorder.attach_artifact(
            context,
            event_id=event.event_id,
            kind="tool_result",
            content=result_body,
        )
    return event


async def _emit_validation_events(
    *,
    trace_recorder: TraceRecorder | None,
    trace_context: TraceContext | None,
    tool_call: ToolCall,
    result: ToolResult,
    parent_event: Any | None,
    correlation_id: str | None,
) -> None:
    if trace_recorder is None or trace_context is None:
        return
    validation_errors = (
        result.metadata.get("validation_errors")
        if result.metadata is not None
        else None
    )
    if not isinstance(validation_errors, list) or not validation_errors:
        return
    parent_event_id = parent_event.event_id if parent_event is not None else None
    root_event_id = (
        parent_event.root_event_id or parent_event.event_id
        if parent_event is not None
        else trace_context.root_event_id
    )
    context = _trace_context_for_tool(
        trace_context,
        actor="validator",
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
        correlation_id=correlation_id,
    )
    for index, error in enumerate(validation_errors, start=1):
        await trace_recorder.emit_event(
            context,
            event_type="validation",
            tool_name=tool_call.name,
            status="fail",
            parent_event_id=parent_event_id,
            root_event_id=root_event_id,
            correlation_id=correlation_id,
            payload={
                "validation_rule_id": (
                    result.metadata.get("validation_rule_id")
                    if result.metadata is not None
                    else None
                )
                or "runtime_validation",
                "severity": "error",
                "affected_tool": tool_call.name,
                "affected_tool_call_id": tool_call.id,
                "affected_field": None,
                "status": "fail",
                "message": str(error),
                "index": index,
            },
        )


async def _emit_internal_task_trace(
    *,
    trace_recorder: TraceRecorder | None,
    trace_context: TraceContext | None,
    chunk: LLMChunk,
    parent_event: Any | None,
    correlation_id: str | None,
) -> None:
    if trace_recorder is None or trace_context is None or chunk.internal_task is None:
        return
    task = chunk.internal_task
    if task.kind not in {"soft_judge", "quality_gate", "validation"}:
        return
    parent_event_id = parent_event.event_id if parent_event is not None else None
    root_event_id = (
        parent_event.root_event_id or parent_event.event_id
        if parent_event is not None
        else trace_context.root_event_id
    )
    actor = {
        "soft_judge": "soft_judge",
        "quality_gate": "quality_gate",
        "validation": "validator",
    }.get(task.kind, "main_agent")
    context = _trace_context_for_tool(
        trace_context,
        actor=actor,
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
        correlation_id=correlation_id,
    )
    result = dict(task.result or {}) if isinstance(task.result, dict) else {}
    await trace_recorder.emit_event(
        context,
        event_type=task.kind,
        status=task.status,
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
        correlation_id=correlation_id,
        payload={
            "task_id": task.id,
            "label": task.label,
            "message": task.message,
            "related_tool_call_id": task.related_tool_call_id,
            "blocking": task.blocking,
            "scope": task.scope,
            "result": result,
            "judge_scores": result if task.kind == "soft_judge" else None,
            "action_items": result.get("suggestions", []),
            "advisory": task.kind == "soft_judge" and not task.blocking,
            "blocking_result": bool(task.blocking),
            "rubric_ids": list(result.keys()) if task.kind == "quality_gate" else [],
            "scores": result,
            "blockers": result.get("errors", []) if isinstance(result.get("errors"), list) else [],
            "feedback": task.message,
            "retry_count": result.get("retry_count"),
            "final_action": result.get("final_action")
            or ("allow" if task.status in {"success", "skipped"} else "review"),
        },
    )


async def _emit_state_diff_event(
    *,
    trace_recorder: TraceRecorder | None,
    trace_context: TraceContext | None,
    tool_call: ToolCall,
    result: ToolResult,
    before_snapshot: dict[str, Any] | None,
    after_snapshot: dict[str, Any] | None,
    parent_event: Any | None,
    correlation_id: str | None,
) -> None:
    if (
        trace_recorder is None
        or trace_context is None
        or before_snapshot is None
        or after_snapshot is None
    ):
        return
    parent_event_id = parent_event.event_id if parent_event is not None else None
    root_event_id = (
        parent_event.root_event_id or parent_event.event_id
        if parent_event is not None
        else trace_context.root_event_id
    )
    context = _trace_context_for_tool(
        trace_context,
        actor="storage",
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
        correlation_id=correlation_id,
    )
    await trace_recorder.emit_event(
        context,
        event_type="state_diff",
        tool_name=tool_call.name,
        status=result.status,
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
        correlation_id=correlation_id,
        payload=_state_diff_payload(
            before=before_snapshot,
            after=after_snapshot,
            tool_call=tool_call,
            result=result,
        ),
    )


async def execute_tool_batch(
    *,
    tool_calls: list[ToolCall],
    messages: list[Message],
    tool_engine: Any,
    hooks: Any,
    guardrail: Any | None,
    parallel_tool_execution: bool,
    parallel_group_counter: int,
    search_history: Any,
    check_cancelled: Callable[[], None],
    run_after_tool_result_hook: Callable[..., AsyncIterator[LLMChunk]],
    current_progress: IterationProgress,
    plan: Any | None = None,
    trace_recorder: TraceRecorder | None = None,
    trace_context: TraceContext | None = None,
    trace_parent_event_id: str | None = None,
    trace_root_event_id: str | None = None,
    trace_correlation_id: str | None = None,
) -> AsyncIterator[LLMChunk | ToolBatchOutcome]:
    needs_rebuild = False
    saw_state_update = False
    rebuild_result: ToolResult | None = None
    idx = 0
    emitted_indices: set[int] = set()
    progress = current_progress

    while idx < len(tool_calls):
        tc = tool_calls[idx]
        result = pre_execution_skip_result(
            tool_call=tc,
            guardrail=guardrail,
            search_history=search_history,
        )
        if result is None and is_parallel_read_call(
            parallel_tool_execution=parallel_tool_execution,
            tool_engine=tool_engine,
            tool_call=tc,
        ):
            read_batch: list[tuple[int, ToolCall]] = []
            scan_idx = idx
            while scan_idx < len(tool_calls):
                scan_tc = tool_calls[scan_idx]
                if pre_execution_skip_result(
                    tool_call=scan_tc,
                    guardrail=guardrail,
                    search_history=search_history,
                ) is not None or not is_parallel_read_call(
                    parallel_tool_execution=parallel_tool_execution,
                    tool_engine=tool_engine,
                    tool_call=scan_tc,
                ):
                    break
                read_batch.append((scan_idx, scan_tc))
                scan_idx += 1

            parallel_group_counter += 1
            current_group = parallel_group_counter
            call_events = {
                call.id: await _emit_tool_call_event(
                    trace_recorder=trace_recorder,
                    trace_context=trace_context,
                    tool_engine=tool_engine,
                    tool_call=call,
                    parallel_group=current_group,
                    parent_event_id=trace_parent_event_id,
                    root_event_id=trace_root_event_id,
                    correlation_id=trace_correlation_id,
                )
                for _, call in read_batch
            }

            batch_results = await tool_engine.execute_batch(
                [call for _, call in read_batch]
            )
            for (batch_idx, batch_tc), batch_result in zip(
                read_batch,
                batch_results,
            ):
                if batch_result.metadata is None:
                    batch_result.metadata = {}
                batch_result.metadata["parallel_group"] = current_group
                result = validate_tool_output(
                    guardrail=guardrail,
                    tool_call=batch_tc,
                    result=batch_result,
                )
                if batch_tc.name in PLAN_WRITER_TOOL_NAMES and result.status == "success":
                    saw_state_update = True

                if is_parallel_read_call(
                    parallel_tool_execution=parallel_tool_execution,
                    tool_engine=tool_engine,
                    tool_call=batch_tc,
                ):
                    if progress != IterationProgress.TOOLS_WITH_WRITES:
                        progress = IterationProgress.TOOLS_READ_ONLY
                else:
                    progress = IterationProgress.TOOLS_WITH_WRITES

                messages.append(
                    Message(
                        role=Role.TOOL,
                        tool_result=result,
                    )
                )
                emitted_indices.add(batch_idx)

                yield LLMChunk(type=ChunkType.KEEPALIVE)

                await hooks.run(
                    "after_tool_call",
                    tool_name=batch_tc.name,
                    tool_call=batch_tc,
                    result=result,
                    messages=messages,
                )

                result_event = await _emit_tool_result_event(
                    trace_recorder=trace_recorder,
                    trace_context=trace_context,
                    tool_call=batch_tc,
                    result=result,
                    tool_call_event=call_events.get(batch_tc.id),
                    parallel_group=current_group,
                    correlation_id=trace_correlation_id,
                )
                await _emit_validation_events(
                    trace_recorder=trace_recorder,
                    trace_context=trace_context,
                    tool_call=batch_tc,
                    result=result,
                    parent_event=result_event,
                    correlation_id=trace_correlation_id,
                )

                yield LLMChunk(
                    type=ChunkType.TOOL_RESULT,
                    tool_result=result,
                )
                async for hook_chunk in run_after_tool_result_hook(
                    tool_name=batch_tc.name,
                    tool_call=batch_tc,
                    result=result,
                ):
                    await _emit_internal_task_trace(
                        trace_recorder=trace_recorder,
                        trace_context=trace_context,
                        chunk=hook_chunk,
                        parent_event=result_event,
                        correlation_id=trace_correlation_id,
                    )
                    yield hook_chunk

            idx = scan_idx
            continue

        if result is None:
            check_cancelled()
        before_snapshot = (
            _state_snapshot(plan) if _is_writer_tool(tool_engine, tc.name) else None
        )
        tool_call_event = await _emit_tool_call_event(
            trace_recorder=trace_recorder,
            trace_context=trace_context,
            tool_engine=tool_engine,
            tool_call=tc,
            parallel_group=None,
            parent_event_id=trace_parent_event_id,
            root_event_id=trace_root_event_id,
            correlation_id=trace_correlation_id,
        )
        started_at = time.monotonic()
        if result is None:
            result = await tool_engine.execute(tc)
            result = validate_tool_output(
                guardrail=guardrail,
                tool_call=tc,
                result=result,
            )
        if result.metadata is None:
            result.metadata = {}
        result.metadata.setdefault(
            "duration_ms",
            round((time.monotonic() - started_at) * 1000, 1),
        )
        if tc.name in PLAN_WRITER_TOOL_NAMES and result.status == "success":
            saw_state_update = True

        if is_parallel_read_call(
            parallel_tool_execution=parallel_tool_execution,
            tool_engine=tool_engine,
            tool_call=tc,
        ):
            if progress != IterationProgress.TOOLS_WITH_WRITES:
                progress = IterationProgress.TOOLS_READ_ONLY
        else:
            progress = IterationProgress.TOOLS_WITH_WRITES

        messages.append(
            Message(
                role=Role.TOOL,
                tool_result=result,
            )
        )
        emitted_indices.add(idx)

        yield LLMChunk(type=ChunkType.KEEPALIVE)

        await hooks.run(
            "after_tool_call",
            tool_name=tc.name,
            tool_call=tc,
            result=result,
            messages=messages,
        )

        result_event = await _emit_tool_result_event(
            trace_recorder=trace_recorder,
            trace_context=trace_context,
            tool_call=tc,
            result=result,
            tool_call_event=tool_call_event,
            parallel_group=None,
            correlation_id=trace_correlation_id,
        )
        await _emit_validation_events(
            trace_recorder=trace_recorder,
            trace_context=trace_context,
            tool_call=tc,
            result=result,
            parent_event=result_event,
            correlation_id=trace_correlation_id,
        )
        if before_snapshot is not None:
            await _emit_state_diff_event(
                trace_recorder=trace_recorder,
                trace_context=trace_context,
                tool_call=tc,
                result=result,
                before_snapshot=before_snapshot,
                after_snapshot=_state_snapshot(plan),
                parent_event=result_event,
                correlation_id=trace_correlation_id,
            )

        yield LLMChunk(
            type=ChunkType.TOOL_RESULT,
            tool_result=result,
        )
        async for hook_chunk in run_after_tool_result_hook(
            tool_name=tc.name,
            tool_call=tc,
            result=result,
        ):
            await _emit_internal_task_trace(
                trace_recorder=trace_recorder,
                trace_context=trace_context,
                chunk=hook_chunk,
                parent_event=result_event,
                correlation_id=trace_correlation_id,
            )
            yield hook_chunk

        if is_backtrack_result(result):
            rebuild_result = result
            for skipped_idx, skipped_tc in enumerate(
                tool_calls[idx + 1 :],
                start=idx + 1,
            ):
                if skipped_idx in emitted_indices:
                    continue
                yield LLMChunk(
                    type=ChunkType.TOOL_RESULT,
                    tool_result=build_skipped_tool_result(
                        skipped_tc.id,
                        error="Skipped after backtrack",
                        error_code="BACKTRACK_CHANGED",
                        suggestion="The conversation moved to an earlier phase before this tool ran.",
                    ),
                )
            needs_rebuild = True
            break
        idx += 1

    yield ToolBatchOutcome(
        progress=progress,
        saw_state_update=saw_state_update,
        needs_rebuild=needs_rebuild,
        rebuild_result=rebuild_result,
        next_parallel_group_counter=parallel_group_counter,
    )
