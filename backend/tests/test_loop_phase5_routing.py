# backend/tests/test_loop_phase5_routing.py
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role, ToolCall
from config import Phase5ParallelConfig
from llm.types import ChunkType, LLMChunk
from tools.base import tool
from tools.engine import ToolEngine


class TestPhase5Routing:
    def test_should_use_parallel_when_enabled(self):
        """Phase 5 + 并行启用 + daily_plans 为空 → 应使用并行模式。"""
        from state.models import TravelPlanState, DateRange, Accommodation

        plan = TravelPlanState(session_id="test-routing")
        plan.phase = 5
        plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
        plan.selected_skeleton_id = "plan_A"
        plan.skeleton_plans = [{"id": "plan_A", "days": [{}, {}, {}]}]
        plan.accommodation = Accommodation(area="新宿")
        plan.daily_plans = []

        config = Phase5ParallelConfig(enabled=True)
        assert AgentLoop.should_use_parallel_phase5(plan, config) is True

    def test_should_not_use_parallel_when_disabled(self):
        from state.models import TravelPlanState, DateRange, Accommodation

        plan = TravelPlanState(session_id="test-routing")
        plan.phase = 5
        plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
        plan.selected_skeleton_id = "plan_A"
        plan.skeleton_plans = [{"id": "plan_A", "days": [{}, {}, {}]}]
        plan.accommodation = Accommodation(area="新宿")
        plan.daily_plans = []

        config = Phase5ParallelConfig(enabled=False)
        assert AgentLoop.should_use_parallel_phase5(plan, config) is False

    def test_should_not_use_parallel_when_plans_exist(self):
        """daily_plans 已有数据 → 用户在修改，用串行模式。"""
        from state.models import TravelPlanState, DateRange, Accommodation, DayPlan

        plan = TravelPlanState(session_id="test-routing")
        plan.phase = 5
        plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
        plan.selected_skeleton_id = "plan_A"
        plan.skeleton_plans = [{"id": "plan_A", "days": [{}, {}, {}]}]
        plan.accommodation = Accommodation(area="新宿")
        plan.daily_plans = [DayPlan(day=1, date="2026-05-01")]

        config = Phase5ParallelConfig(enabled=True)
        assert AgentLoop.should_use_parallel_phase5(plan, config) is False

    def test_should_not_use_parallel_when_not_phase5(self):
        from state.models import TravelPlanState

        plan = TravelPlanState(session_id="test-routing")
        plan.phase = 3

        config = Phase5ParallelConfig(enabled=True)
        assert AgentLoop.should_use_parallel_phase5(plan, config) is False

    def test_should_not_use_parallel_when_plan_is_none(self):
        config = Phase5ParallelConfig(enabled=True)
        assert AgentLoop.should_use_parallel_phase5(None, config) is False

    def test_should_not_use_parallel_when_config_is_none(self):
        from state.models import TravelPlanState

        plan = TravelPlanState(session_id="test-routing")
        plan.phase = 5
        assert AgentLoop.should_use_parallel_phase5(plan, None) is False


class _PromotingRouter:
    """Test double: any tool call in phase 3 promotes plan to phase 5."""

    def get_prompt(self, phase: int) -> str:
        return f"phase-{phase}-prompt"

    def get_prompt_for_plan(self, plan) -> str:
        return f"phase-{plan.phase}-prompt"

    async def check_and_apply_transition(self, plan, hooks=None) -> bool:
        if plan.phase == 3:
            plan.phase = 5
            return True
        return False


class _StubContextManager:
    def build_system_message(
        self, plan, phase_prompt, memory_context="", available_tools=None
    ):
        return Message(role=Role.SYSTEM, content=f"phase={plan.phase}")

    def build_phase_handoff_note(self, *, plan, from_phase, to_phase) -> str:
        return f"handoff {from_phase}->{to_phase}"


class _StubMemoryManager:
    async def generate_context(self, user_id, plan):
        return "", [], 0, 0, 0


@pytest.mark.asyncio
async def test_parallel_orchestrator_fires_after_final_iteration_phase_promotion():
    """Boundary: the single allowed iteration promotes phase 3→5 via a write tool.
    The loop must still route to the parallel orchestrator rather than emitting
    the safety-limit fallback text.
    """
    from state.models import (
        Accommodation,
        DateRange,
        TravelPlanState,
    )

    plan = TravelPlanState(session_id="s-boundary", phase=3)
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.selected_skeleton_id = "plan_A"
    plan.skeleton_plans = [{"id": "plan_A", "days": [{}, {}, {}]}]
    plan.accommodation = Accommodation(area="新宿")

    @tool(
        name="set_accommodation",
        description="Test write tool (reuses plan-writer name so loop treats it as state update).",
        phases=[3, 5],
        parameters={"type": "object", "properties": {}, "required": []},
        side_effect="write",
    )
    async def noop_write() -> dict:
        return {"ok": True}

    engine = ToolEngine()
    engine.register(noop_write)

    llm = AsyncMock()

    async def chat(*args, **kwargs):
        yield LLMChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call=ToolCall(id="tc1", name="set_accommodation", arguments={}),
        )
        yield LLMChunk(type=ChunkType.DONE)

    llm.chat = chat

    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=1,
        phase_router=_PromotingRouter(),
        context_manager=_StubContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=_StubMemoryManager(),
        user_id="u",
        phase5_parallel_config=Phase5ParallelConfig(enabled=True),
    )

    orchestrator_fired = False

    async def _fake_orchestrator():
        nonlocal orchestrator_fired
        orchestrator_fired = True
        yield LLMChunk(type=ChunkType.DONE)

    agent._run_parallel_phase5_orchestrator = _fake_orchestrator

    chunks = [
        chunk
        async for chunk in agent.run(
            [Message(role=Role.USER, content="go")], phase=3
        )
    ]

    assert orchestrator_fired, "parallel orchestrator must fire on boundary iteration"
    assert not any(
        c.type == ChunkType.TEXT_DELTA and "达到最大循环次数" in (c.content or "")
        for c in chunks
    ), "safety-limit fallback must not be emitted when orchestrator can still run"
