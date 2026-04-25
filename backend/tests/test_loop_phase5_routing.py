# backend/tests/test_loop_phase5_routing.py
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.phase5.parallel import (
    should_enter_parallel_phase5_at_iteration_boundary,
    should_enter_parallel_phase5_now,
)
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

    def test_named_phase5_guards_share_current_eligibility_rules(self):
        from state.models import TravelPlanState, DateRange, Accommodation

        plan = TravelPlanState(session_id="test-routing-named")
        plan.phase = 5
        plan.dates = DateRange(start="2026-05-01", end="2026-05-02")
        plan.selected_skeleton_id = "plan_A"
        plan.skeleton_plans = [{"id": "plan_A", "days": [{}, {}]}]
        plan.accommodation = Accommodation(area="新宿")
        plan.daily_plans = []

        config = Phase5ParallelConfig(enabled=True)

        assert should_enter_parallel_phase5_now(plan, config) is True
        assert should_enter_parallel_phase5_at_iteration_boundary(plan, config) is True

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


@pytest.mark.asyncio
async def test_parallel_orchestrator_emits_internal_task_lifecycle(monkeypatch):
    from state.models import Accommodation, DateRange, TravelPlanState

    plan = TravelPlanState(session_id="s-phase5", phase=5)
    plan.dates = DateRange(start="2026-05-01", end="2026-05-01")
    plan.selected_skeleton_id = "plan_A"
    plan.skeleton_plans = [{"id": "plan_A", "days": [{}]}]
    plan.accommodation = Accommodation(area="新宿")

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            self.final_dayplans = [{"day": 1, "date": "2026-05-01"}]
            self.final_issues = []

        async def run(self):
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="完成")
            yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr("agent.phase5.orchestrator.Phase5Orchestrator", FakeOrchestrator)

    agent = AgentLoop(
        llm=MagicMock(),
        tool_engine=ToolEngine(),
        hooks=HookManager(),
        plan=plan,
        phase5_parallel_config=Phase5ParallelConfig(enabled=True),
    )

    chunks = [chunk async for chunk in agent._run_parallel_phase5_orchestrator()]
    tasks = [chunk.internal_task for chunk in chunks if chunk.type == ChunkType.INTERNAL_TASK]

    assert any(
        task and task.kind == "phase5_orchestration" and task.status == "pending"
        for task in tasks
    )
    assert any(
        task and task.kind == "phase5_orchestration" and task.status == "success"
        for task in tasks
    )


@pytest.mark.asyncio
async def test_parallel_wrapper_returns_final_dayplans_via_handoff(monkeypatch):
    from agent.phase5.parallel import run_parallel_phase5_orchestrator
    from state.models import Accommodation, DateRange, TravelPlanState

    plan = TravelPlanState(session_id="s-handoff", phase=5)
    plan.dates = DateRange(start="2026-05-01", end="2026-05-01")
    plan.selected_skeleton_id = "plan_A"
    plan.skeleton_plans = [{"id": "plan_A", "days": [{}]}]
    plan.accommodation = Accommodation(area="新宿")

    final_dayplans = [{
        "day": 1,
        "date": "2026-05-01",
        "notes": "ok",
        "activities": [{
            "name": "测试活动",
            "location": {"name": "测试活动", "lat": 35.0, "lng": 139.0},
            "start_time": "09:00",
            "end_time": "10:00",
            "category": "activity",
            "cost": 0,
        }],
    }]

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            self.final_dayplans = final_dayplans
            self.final_issues = []

        async def run(self):
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="并行完成")

    monkeypatch.setattr("agent.phase5.orchestrator.Phase5Orchestrator", FakeOrchestrator)

    handoffs = []
    chunks = [
        chunk
        async for chunk in run_parallel_phase5_orchestrator(
            plan=plan,
            llm=MagicMock(),
            tool_engine=ToolEngine(),
            config=Phase5ParallelConfig(enabled=True),
            on_handoff=handoffs.append,
        )
    ]

    assert len(handoffs) == 1
    assert handoffs[0].dayplans == final_dayplans
    assert handoffs[0].issues == []
    assert plan.daily_plans == []
    assert any(c.type == ChunkType.INTERNAL_TASK for c in chunks)
