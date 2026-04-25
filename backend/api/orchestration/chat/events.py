from __future__ import annotations

import json

from llm.types import ChunkType


def event_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def passthrough_chunk_event(chunk) -> str | dict | None:
    if chunk.type.value == "keepalive":
        return {"comment": "ping"}
    if chunk.type == ChunkType.CONTEXT_COMPRESSION:
        return event_json(
            {
                "type": "context_compression",
                "compression_info": chunk.compression_info,
            }
        )
    if chunk.type == ChunkType.PHASE_TRANSITION and chunk.phase_info is not None:
        return event_json({"type": "phase_transition", **chunk.phase_info})
    if chunk.type == ChunkType.AGENT_STATUS and chunk.agent_status is not None:
        return event_json({"type": "agent_status", **chunk.agent_status})
    if chunk.type == ChunkType.INTERNAL_TASK and chunk.internal_task is not None:
        return event_json(
            {
                "type": "internal_task",
                "task": chunk.internal_task.to_dict(),
            }
        )
    return None


def chunk_event_data(chunk, tool_call_names: dict[str, str], tool_call_args: dict[str, dict]):
    event_type = (
        "tool_call"
        if chunk.tool_call and chunk.type.value == "tool_call_start"
        else "tool_result"
        if chunk.tool_result and chunk.type.value == "tool_result"
        else chunk.type.value
    )
    event_data = {"type": event_type}
    content_delta = ""
    if chunk.content:
        content_delta = chunk.content
        event_data["content"] = chunk.content
    if chunk.tool_call:
        tool_call_names[chunk.tool_call.id] = chunk.tool_call.name
        tool_call_args[chunk.tool_call.id] = chunk.tool_call.arguments or {}
        event_data["tool_call"] = {
            "id": chunk.tool_call.id,
            "name": chunk.tool_call.name,
            "arguments": chunk.tool_call.arguments,
            "human_label": chunk.tool_call.human_label,
        }
    if chunk.tool_result:
        event_data["tool_result"] = {
            "tool_call_id": chunk.tool_result.tool_call_id,
            "status": chunk.tool_result.status,
            "data": chunk.tool_result.data,
            "error": chunk.tool_result.error,
            "error_code": chunk.tool_result.error_code,
            "suggestion": chunk.tool_result.suggestion,
        }
    return event_data, content_delta


def apply_pending_tool_stats(session: dict) -> None:
    stats = session.get("stats")
    if not (stats and stats.tool_calls):
        return
    pending_state_changes = session.pop("_pending_state_changes", None)
    if pending_state_changes is not None:
        stats.tool_calls[-1].state_changes = pending_state_changes
    pending_validation_errors = session.pop("_pending_validation_errors", None)
    if pending_validation_errors is not None:
        stats.tool_calls[-1].validation_errors = pending_validation_errors
    pending_judge_scores = session.pop("_pending_judge_scores", None)
    if pending_judge_scores is not None:
        stats.tool_calls[-1].judge_scores = pending_judge_scores


def done_event(run) -> str:
    return event_json(
        {
            "type": "done",
            "run_id": run.run_id,
            "run_status": run.status,
        }
    )
