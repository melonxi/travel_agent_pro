from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agent.execution.phase_transition import detect_phase_transition
from agent.execution.tool_batches import ToolBatchOutcome
from agent.internal_tasks import InternalTask
from agent.types import ToolResult
from run import IterationProgress


class _Plan:
    def __init__(self, *, phase: int, phase3_step: str | None = None):
        self.phase = phase
        self.phase3_step = phase3_step


@pytest.mark.asyncio
async def test_detects_backtrack_before_router_check():
    plan = _Plan(phase=1)
    router = AsyncMock()
    router.check_and_apply_transition = AsyncMock(return_value=True)
    result = ToolResult(tool_call_id="tc1", status="success")
    batch = ToolBatchOutcome(
        progress=IterationProgress.TOOLS_WITH_WRITES,
        saw_state_update=True,
        needs_rebuild=True,
        rebuild_result=result,
        next_parallel_group_counter=0,
    )

    detection = await detect_phase_transition(
        plan=plan,
        phase_router=router,
        hooks=None,
        batch_outcome=batch,
        phase_before_batch=5,
        phase3_step_before_batch=None,
        current_phase=5,
        drain_internal_task_events=lambda: [],
    )

    assert detection.request is not None
    assert detection.request.reason == "backtrack"
    assert detection.request.from_phase == 5
    assert detection.request.to_phase == 1
    assert detection.request.result is result
    router.check_and_apply_transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_detects_direct_plan_phase_write_before_router_check():
    plan = _Plan(phase=3)
    router = AsyncMock()
    router.check_and_apply_transition = AsyncMock(return_value=True)
    batch = ToolBatchOutcome(
        progress=IterationProgress.TOOLS_WITH_WRITES,
        saw_state_update=True,
        needs_rebuild=False,
        rebuild_result=None,
        next_parallel_group_counter=0,
    )

    detection = await detect_phase_transition(
        plan=plan,
        phase_router=router,
        hooks=None,
        batch_outcome=batch,
        phase_before_batch=1,
        phase3_step_before_batch=None,
        current_phase=1,
        drain_internal_task_events=lambda: [],
    )

    assert detection.request is not None
    assert detection.request.reason == "plan_tool_direct"
    assert detection.request.from_phase == 1
    assert detection.request.to_phase == 3
    router.check_and_apply_transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_detects_router_transition_and_returns_internal_tasks_first():
    plan = _Plan(phase=1)
    task = InternalTask(
        id="quality_gate:1",
        kind="quality_gate",
        label="Quality gate",
        status="success",
    )

    async def _promote(_plan, hooks=None):
        _plan.phase = 3
        return True

    router = AsyncMock()
    router.check_and_apply_transition = AsyncMock(side_effect=_promote)
    batch = ToolBatchOutcome(
        progress=IterationProgress.TOOLS_WITH_WRITES,
        saw_state_update=True,
        needs_rebuild=False,
        rebuild_result=None,
        next_parallel_group_counter=0,
    )

    detection = await detect_phase_transition(
        plan=plan,
        phase_router=router,
        hooks="hooks",
        batch_outcome=batch,
        phase_before_batch=1,
        phase3_step_before_batch=None,
        current_phase=1,
        drain_internal_task_events=lambda: [task],
    )

    assert detection.internal_tasks == [task]
    assert detection.request is not None
    assert detection.request.reason == "check_and_apply_transition"
    assert detection.request.to_phase == 3
    router.check_and_apply_transition.assert_awaited_once_with(plan, hooks="hooks")


@pytest.mark.asyncio
async def test_returns_phase3_step_after_batch_without_phase_transition():
    plan = _Plan(phase=3, phase3_step="candidate")
    router = AsyncMock()
    router.check_and_apply_transition = AsyncMock(return_value=False)
    batch = ToolBatchOutcome(
        progress=IterationProgress.TOOLS_WITH_WRITES,
        saw_state_update=False,
        needs_rebuild=False,
        rebuild_result=None,
        next_parallel_group_counter=0,
    )

    detection = await detect_phase_transition(
        plan=plan,
        phase_router=router,
        hooks=None,
        batch_outcome=batch,
        phase_before_batch=3,
        phase3_step_before_batch="brief",
        current_phase=3,
        drain_internal_task_events=lambda: [],
    )

    assert detection.request is None
    assert detection.internal_tasks == []
    assert detection.phase3_step_after_batch == "candidate"
    router.check_and_apply_transition.assert_not_awaited()
