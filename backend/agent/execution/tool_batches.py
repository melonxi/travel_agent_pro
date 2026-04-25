from __future__ import annotations

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
from tools.plan_tools import PLAN_WRITER_TOOL_NAMES


@dataclass
class ToolBatchOutcome:
    progress: IterationProgress
    saw_state_update: bool
    needs_rebuild: bool
    rebuild_result: ToolResult | None
    next_parallel_group_counter: int


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
                    yield hook_chunk

            idx = scan_idx
            continue

        if result is None:
            check_cancelled()
            result = await tool_engine.execute(tc)
            result = validate_tool_output(
                guardrail=guardrail,
                tool_call=tc,
                result=result,
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
