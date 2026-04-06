# backend/tests/test_appendix_issues.py
"""
Tests verifying the three design inconsistency issues from Appendix A
of the architecture analysis document, and confirming the fixes work.
"""

import pytest

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from phase.router import PhaseRouter
from state.models import (
    Accommodation,
    Activity,
    DateRange,
    DayPlan,
    Location,
    Preference,
    TravelPlanState,
)
from tools.base import ToolError
from tools.engine import ToolEngine
from tools.update_plan_state import make_update_plan_state_tool


class _PhaseRouter:
    def get_prompt(self, phase: int) -> str:
        return f"phase-{phase}"


class _ContextManager:
    def build_system_message(self, plan, phase_prompt, user_summary=""):
        return Message(role=Role.SYSTEM, content=phase_prompt)

    async def compress_for_transition(self, messages, from_phase, to_phase, llm_factory):
        return "summary"


class _MemoryManager:
    async def load(self, user_id: str):
        return {}

    def generate_summary(self, memory) -> str:
        return ""


# ---------------------------------------------------------------------------
# A.1  PHASE_TOOL_NAMES is dead code / inconsistent with tool phases
# ---------------------------------------------------------------------------


class TestA1PhaseToolNamesInconsistency:
    """A.1: PHASE_TOOL_NAMES was dead code that conflicted with actual tool phases.
    After fix: PHASE_TOOL_NAMES is removed, PhaseRouter.get_tool_names() is removed.
    ToolEngine.get_tools_for_phase() is the single source of truth."""

    def test_get_tools_for_phase_uses_tool_level_phases(self):
        """ToolEngine.get_tools_for_phase() uses the tool's own phases attribute."""
        from tools.base import tool

        @tool(
            name="only_phase3",
            description="test",
            phases=[3],
            parameters={"type": "object", "properties": {}},
        )
        async def only_phase3() -> dict:
            return {}

        engine = ToolEngine()
        engine.register(only_phase3)

        # Runtime filtering: tool should appear in phase 3, not phase 4
        assert len(engine.get_tools_for_phase(3)) == 1
        assert len(engine.get_tools_for_phase(4)) == 0

    def test_phase_tool_names_removed_from_prompts(self):
        """After fix: PHASE_TOOL_NAMES should no longer exist in prompts module."""
        from phase import prompts

        assert not hasattr(prompts, "PHASE_TOOL_NAMES"), (
            "PHASE_TOOL_NAMES should be removed as dead code"
        )

    def test_phase_router_get_tool_names_removed(self):
        """After fix: PhaseRouter should no longer have get_tool_names method."""
        router = PhaseRouter()
        assert not hasattr(router, "get_tool_names"), (
            "get_tool_names should be removed from PhaseRouter"
        )


# ---------------------------------------------------------------------------
# A.2  _ALLOWED_FIELDS missing daily_plans — blocks Phase 5→7 transition
# ---------------------------------------------------------------------------


class TestA2DailyPlansBlocked:
    """A.2: update_plan_state rejects 'daily_plans' because it is not in
    _ALLOWED_FIELDS. This blocks the phase 5→7 transition."""

    @pytest.fixture
    def plan_at_phase5(self):
        """A plan that is in phase 5 (has destination, dates, accommodation)."""
        return TravelPlanState(
            session_id="test-p5",
            phase=5,
            destination="Tokyo",
            dates=DateRange(start="2026-05-01", end="2026-05-04"),
            accommodation=Accommodation(area="Shinjuku"),
            preferences=[Preference(key="pace", value="relaxed")],
        )

    @pytest.mark.asyncio
    async def test_daily_plans_update_succeeds(self, plan_at_phase5):
        """After fix: update_plan_state should accept 'daily_plans' field."""
        tool_fn = make_update_plan_state_tool(plan_at_phase5)
        day1 = {
            "day": 1,
            "date": "2026-05-01",
            "activities": [
                {
                    "name": "Meiji Shrine",
                    "location": {
                        "lat": 35.6764,
                        "lng": 139.6993,
                        "name": "Meiji Shrine",
                    },
                    "start_time": "09:00",
                    "end_time": "11:00",
                    "category": "shrine",
                    "cost": 0,
                }
            ],
            "notes": "Day 1 itinerary",
        }

        result = await tool_fn(field="daily_plans", value=day1)
        assert result["updated_field"] == "daily_plans"
        assert len(plan_at_phase5.daily_plans) == 1
        assert plan_at_phase5.daily_plans[0].day == 1

    @pytest.mark.asyncio
    async def test_daily_plans_append_multiple(self, plan_at_phase5):
        """Appending multiple day plans one by one should work."""
        tool_fn = make_update_plan_state_tool(plan_at_phase5)

        for i in range(1, 4):
            day_data = {
                "day": i,
                "date": f"2026-05-0{i}",
                "activities": [],
                "notes": f"Day {i}",
            }
            await tool_fn(field="daily_plans", value=day_data)

        assert len(plan_at_phase5.daily_plans) == 3

    @pytest.mark.asyncio
    async def test_phase5_to_7_transition_after_daily_plans(self, plan_at_phase5):
        """With daily_plans writable, phase 5→7 transition should work."""
        tool_fn = make_update_plan_state_tool(plan_at_phase5)
        router = PhaseRouter()

        # The plan has 3 total days (May 1-4, total_days = 3)
        for i in range(1, 4):
            await tool_fn(
                field="daily_plans",
                value={"day": i, "date": f"2026-05-0{i}", "activities": []},
            )

        # Now daily_plans count (3) >= dates.total_days (3)
        assert len(plan_at_phase5.daily_plans) >= plan_at_phase5.dates.total_days
        changed = router.check_and_apply_transition(plan_at_phase5)
        assert changed is True
        assert plan_at_phase5.phase == 7

    @pytest.mark.asyncio
    async def test_daily_plans_list_replaces_all(self, plan_at_phase5):
        """Passing a list replaces all daily_plans at once."""
        tool_fn = make_update_plan_state_tool(plan_at_phase5)
        plans_list = [
            {"day": 1, "date": "2026-05-01", "activities": []},
            {"day": 2, "date": "2026-05-02", "activities": []},
            {"day": 3, "date": "2026-05-03", "activities": []},
        ]
        result = await tool_fn(field="daily_plans", value=plans_list)
        assert len(plan_at_phase5.daily_plans) == 3


# ---------------------------------------------------------------------------
# A.3  max_retries parameter unused — hardcoded range(20)
# ---------------------------------------------------------------------------


class TestA3MaxRetriesUnused:
    """A.3: AgentLoop.max_retries is stored but run() uses hardcoded range(20)."""

    @pytest.mark.asyncio
    async def test_max_retries_controls_loop_iterations(self):
        """After fix: the safety loop should use self.max_retries, not range(20)."""
        call_count = 0

        async def infinite_tool_calls(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Always return a tool call → forces loop to iterate
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id=f"tc_{call_count}",
                    name="echo",
                    arguments={"msg": "hi"},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

        from tools.base import tool as tool_decorator

        @tool_decorator(
            name="echo",
            description="Echo",
            phases=[1],
            parameters={
                "type": "object",
                "properties": {"msg": {"type": "string"}},
            },
        )
        async def echo(msg: str = "") -> dict:
            return {"echo": msg}

        engine = ToolEngine()
        engine.register(echo)

        mock_llm = type("MockLLM", (), {"chat": infinite_tool_calls})()
        hooks = HookManager()

        # Set max_retries to 5 — loop should stop at 5, not 20
        agent = AgentLoop(
            llm=mock_llm,
            tool_engine=engine,
            hooks=hooks,
            max_retries=5,
            phase_router=_PhaseRouter(),
            context_manager=_ContextManager(),
            plan=TravelPlanState(session_id="s1", phase=1),
            llm_factory=lambda: None,
            memory_mgr=_MemoryManager(),
            user_id="appendix-user",
        )

        messages = [Message(role=Role.USER, content="loop")]
        chunks = []
        async for chunk in agent.run(messages, phase=1):
            chunks.append(chunk)

        # The loop should have stopped after max_retries (5) iterations
        assert call_count == 5, (
            f"Expected loop to stop at max_retries=5, but LLM was called {call_count} times"
        )
