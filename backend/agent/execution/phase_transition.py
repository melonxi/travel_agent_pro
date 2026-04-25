from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent.execution.tool_batches import ToolBatchOutcome
from agent.internal_tasks import InternalTask
from agent.types import ToolResult


@dataclass(frozen=True)
class PhaseTransitionRequest:
    from_phase: int
    to_phase: int
    from_step: Any
    reason: str
    result: ToolResult


@dataclass(frozen=True)
class PhaseTransitionDetection:
    request: PhaseTransitionRequest | None
    internal_tasks: list[InternalTask]
    phase3_step_after_batch: Any


async def detect_phase_transition(
    *,
    plan: Any | None,
    phase_router: Any | None,
    hooks: Any | None,
    batch_outcome: ToolBatchOutcome,
    phase_before_batch: int,
    phase3_step_before_batch: Any,
    current_phase: int,
    drain_internal_task_events: Callable[[], list[InternalTask]],
) -> PhaseTransitionDetection:
    """Detect exactly one phase transition after a completed tool batch."""

    if batch_outcome.needs_rebuild:
        phase_after_batch = plan.phase if plan is not None else current_phase
        return PhaseTransitionDetection(
            request=PhaseTransitionRequest(
                from_phase=phase_before_batch,
                to_phase=phase_after_batch,
                from_step=phase3_step_before_batch,
                reason="backtrack",
                result=batch_outcome.rebuild_result
                or ToolResult(tool_call_id="", status="success"),
            ),
            internal_tasks=[],
            phase3_step_after_batch=getattr(plan, "phase3_step", None)
            if plan is not None
            else None,
        )

    phase_after_batch = plan.phase if plan is not None else current_phase
    if phase_after_batch != phase_before_batch:
        return PhaseTransitionDetection(
            request=PhaseTransitionRequest(
                from_phase=phase_before_batch,
                to_phase=phase_after_batch,
                from_step=phase3_step_before_batch,
                reason="plan_tool_direct",
                result=ToolResult(tool_call_id="", status="success"),
            ),
            internal_tasks=[],
            phase3_step_after_batch=getattr(plan, "phase3_step", None)
            if plan is not None
            else None,
        )

    internal_tasks: list[InternalTask] = []
    if batch_outcome.saw_state_update and phase_router is not None and plan is not None:
        phase_changed = await phase_router.check_and_apply_transition(
            plan,
            hooks=hooks,
        )
        internal_tasks = drain_internal_task_events()
        phase_after_batch = plan.phase
        if phase_changed:
            return PhaseTransitionDetection(
                request=PhaseTransitionRequest(
                    from_phase=phase_before_batch,
                    to_phase=phase_after_batch,
                    from_step=phase3_step_before_batch,
                    reason="check_and_apply_transition",
                    result=ToolResult(tool_call_id="", status="success"),
                ),
                internal_tasks=internal_tasks,
                phase3_step_after_batch=getattr(plan, "phase3_step", None),
            )

    return PhaseTransitionDetection(
        request=None,
        internal_tasks=internal_tasks,
        phase3_step_after_batch=getattr(plan, "phase3_step", None)
        if plan is not None
        else None,
    )
