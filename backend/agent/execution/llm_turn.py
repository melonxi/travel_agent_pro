from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from agent.internal_tasks import InternalTask
from agent.narration import compute_narration
from agent.types import Message, Role, ToolCall
from llm.types import ChunkType, LLMChunk
from run import IterationProgress


@dataclass
class LlmTurnOutcome:
    text_chunks: list[str]
    tool_calls: list[ToolCall]
    progress: IterationProgress
    next_iteration_idx: int
    previous_phase3_step: str | None


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
    previous_phase3_step: str | None,
    check_cancelled: Callable[[], None],
    update_progress: Callable[[IterationProgress], None],
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

    while compression_events:
        info = compression_events.pop(0)
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

    next_previous_phase3_step = previous_phase3_step
    if reflection is not None and plan is not None:
        reflection_msg = reflection.check_and_inject(
            messages,
            plan,
            previous_phase3_step,
        )
        if reflection_msg:
            messages.append(Message(role=Role.SYSTEM, content=reflection_msg))
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
        next_previous_phase3_step = getattr(plan, "phase3_step", None)

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

    tool_calls: list[ToolCall] = []
    text_chunks: list[str] = []
    progress = IterationProgress.NO_OUTPUT

    async for chunk in llm.chat(messages, **chat_kwargs):
        check_cancelled()
        if chunk.type == ChunkType.TEXT_DELTA:
            if progress == IterationProgress.NO_OUTPUT:
                progress = IterationProgress.PARTIAL_TEXT
                update_progress(progress)
            text_chunks.append(chunk.content or "")
            yield chunk
        elif chunk.type == ChunkType.USAGE:
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

    yield LlmTurnOutcome(
        text_chunks=text_chunks,
        tool_calls=tool_calls,
        progress=progress,
        next_iteration_idx=next_iteration_idx,
        previous_phase3_step=next_previous_phase3_step,
    )
