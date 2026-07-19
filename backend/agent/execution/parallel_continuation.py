"""Parallel Phase 3 sub-flow boundary → same-run continuation decision.

The orchestrator owns a complete Phase 3 sub-flow and therefore emits DONE.
When its commit advances the plan to Phase 4 while deliverables are still
missing, that DONE is only the sub-flow boundary: the same agent run must
continue iterating until Phase 4 produces the deliverables.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from agent.types import Message
from llm.types import ChunkType, LLMChunk


@dataclass(frozen=True)
class ParallelPhase3Continuation:
    """Decision after the parallel sub-flow: finish the run or keep looping."""

    run_finished: bool
    current_phase: int
    tools: list[dict] | None = None


async def run_parallel_phase3_and_decide(
    loop: Any,
    *,
    messages: list[Message],
    original_user_message: Message,
    fallback_phase: int,
) -> AsyncIterator[LLMChunk | ParallelPhase3Continuation]:
    """Run the orchestrator, then yield a ParallelPhase3Continuation last."""
    async for chunk in loop._run_parallel_phase3_orchestrator(
        messages=messages,
        original_user_message=original_user_message,
    ):
        # Suppress the orchestrator's DONE: it marks the sub-flow boundary,
        # not necessarily the end of the whole agent run.
        if chunk.type != ChunkType.DONE:
            yield chunk

    plan = loop.plan
    current_phase = plan.phase if plan is not None else fallback_phase
    if current_phase != 4 or getattr(plan, "deliverables", None):
        yield LLMChunk(type=ChunkType.DONE)
        yield ParallelPhase3Continuation(
            run_finished=True,
            current_phase=current_phase,
        )
        return
    yield ParallelPhase3Continuation(
        run_finished=False,
        current_phase=current_phase,
        tools=loop.tool_engine.get_tools_for_phase(current_phase, plan),
    )
