# backend/tests/test_tool_engine.py
import pytest

from agent.types import ToolCall, ToolResult
from state.models import TravelPlanState
from tools.base import ToolDef, ToolError, tool
from tools.engine import ToolEngine
from tools.plan_tools import PLAN_WRITER_TOOL_NAMES


@pytest.fixture
def engine():
    @tool(
        name="greet",
        description="Greet someone",
        phases=[1, 2],
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )
    async def greet(name: str) -> dict:
        return {"greeting": f"Hello, {name}!"}

    @tool(
        name="fail_tool",
        description="Always fails",
        phases=[1],
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def fail_tool() -> dict:
        raise ToolError("Something broke", error_code="BROKEN", suggestion="Try again")

    eng = ToolEngine()
    eng.register(greet)
    eng.register(fail_tool)
    return eng


def test_get_tools_for_phase(engine):
    phase1_tools = engine.get_tools_for_phase(1)
    assert len(phase1_tools) == 2
    phase2_tools = engine.get_tools_for_phase(2)
    assert len(phase2_tools) == 1
    assert phase2_tools[0]["name"] == "greet"


def test_get_tools_for_phase3_respects_substep(engine):
    @tool(
        name="search_accommodations",
        description="stay",
        phases=[3],
        parameters={"type": "object", "properties": {}},
    )
    async def search_accommodations() -> dict:
        return {}

    @tool(
        name="web_search",
        description="web",
        phases=[3],
        parameters={"type": "object", "properties": {}},
    )
    async def web_search() -> dict:
        return {}

    @tool(
        name="update_trip_basics",
        description="state",
        phases=[3],
        parameters={"type": "object", "properties": {}},
    )
    async def update_trip_basics() -> dict:
        return {}

    engine.register(search_accommodations)
    engine.register(web_search)
    engine.register(update_trip_basics)

    plan = TravelPlanState(session_id="s1", phase=3, phase3_step="brief")
    brief_tools = {tool["name"] for tool in engine.get_tools_for_phase(3, plan)}
    assert "search_accommodations" not in brief_tools
    assert "web_search" in brief_tools

    plan.phase3_step = "lock"
    lock_tools = {tool["name"] for tool in engine.get_tools_for_phase(3, plan)}
    assert "search_accommodations" in lock_tools


@pytest.mark.asyncio
async def test_execute_success(engine):
    call = ToolCall(id="tc_1", name="greet", arguments={"name": "World"})
    result = await engine.execute(call)
    assert result.status == "success"
    assert result.data["greeting"] == "Hello, World!"
    assert result.tool_call_id == "tc_1"


@pytest.mark.asyncio
async def test_execute_tool_error(engine):
    call = ToolCall(id="tc_2", name="fail_tool", arguments={})
    result = await engine.execute(call)
    assert result.status == "error"
    assert result.error_code == "BROKEN"
    assert result.suggestion == "Try again"


@pytest.mark.asyncio
async def test_execute_unknown_tool(engine):
    call = ToolCall(id="tc_3", name="nonexistent", arguments={})
    result = await engine.execute(call)
    assert result.status == "error"
    assert result.error_code == "UNKNOWN_TOOL"


class TestEnginePhase3NewTools:
    @staticmethod
    def _expected_common_tools() -> set[str]:
        return {
            "update_trip_basics",
            "request_backtrack",
        }

    def test_brief_whitelist_includes_split_plan_tools(self):
        engine = ToolEngine()

        assert engine._phase3_tool_names("brief") == {
            *self._expected_common_tools(),
            "set_trip_brief",
            "add_preferences",
            "add_constraints",
            "web_search",
            "xiaohongshu_search",
            # Forward-looking rescue tools
            "set_candidate_pool",
            "set_shortlist",
        }

    def test_candidate_whitelist_includes_split_plan_tools(self):
        engine = ToolEngine()

        assert engine._phase3_tool_names("candidate") == {
            *self._expected_common_tools(),
            "set_trip_brief",
            "set_candidate_pool",
            "set_shortlist",
            "add_preferences",
            "add_constraints",
            "web_search",
            "xiaohongshu_search",
            "quick_travel_search",
            "get_poi_info",
            # Forward-looking rescue tools
            "set_skeleton_plans",
            "select_skeleton",
        }

    def test_skeleton_whitelist_includes_split_plan_tools(self):
        engine = ToolEngine()

        assert engine._phase3_tool_names("skeleton") == {
            *self._expected_common_tools(),
            "set_skeleton_plans",
            "select_skeleton",
            "set_candidate_pool",
            "set_shortlist",
            "add_preferences",
            "add_constraints",
            "web_search",
            "xiaohongshu_search",
            "quick_travel_search",
            "get_poi_info",
            "calculate_route",
            "assemble_day_plan",
            "check_availability",
        }

    def test_lock_whitelist_includes_split_plan_tools(self):
        engine = ToolEngine()

        assert engine._phase3_tool_names("lock") == {
            *self._expected_common_tools(),
            "set_skeleton_plans",
            "select_skeleton",
            "set_transport_options",
            "select_transport",
            "set_accommodation_options",
            "set_accommodation",
            "set_risks",
            "set_alternatives",
            "add_preferences",
            "add_constraints",
            "web_search",
            "xiaohongshu_search",
            "quick_travel_search",
            "get_poi_info",
            "calculate_route",
            "assemble_day_plan",
            "check_availability",
            "search_flights",
            "search_trains",
            "search_accommodations",
        }

    def test_phase3_builtin_names_include_split_plan_tools(self):
        engine = ToolEngine()
        builtin_names = engine._phase3_builtin_tool_names()

        assert {
            "update_trip_basics",
            "request_backtrack",
            "set_trip_brief",
            "set_candidate_pool",
            "set_shortlist",
            "set_skeleton_plans",
            "select_skeleton",
            "set_transport_options",
            "select_transport",
            "set_accommodation_options",
            "set_accommodation",
            "set_risks",
            "set_alternatives",
            "add_preferences",
            "add_constraints",
        }.issubset(builtin_names)
        assert PLAN_WRITER_TOOL_NAMES.issuperset(
            builtin_names
            & {
                "update_trip_basics",
                "request_backtrack",
                "set_trip_brief",
                "set_candidate_pool",
                "set_shortlist",
                "set_skeleton_plans",
                "select_skeleton",
                "set_transport_options",
                "select_transport",
                "set_accommodation_options",
                "set_accommodation",
                "set_risks",
                "set_alternatives",
                "add_preferences",
                "add_constraints",
            }
        )
