from __future__ import annotations

from state.models import TravelPlanState
from tools.engine import ToolEngine
from tools.plan_tools import make_all_plan_tools
from tests.helpers.register_plan_tools import register_all_plan_tools


EXPECTED_TOOL_NAMES = [
    "add_constraints",
    "add_preferences",
    "request_backtrack",
    "save_day_plan",
    "replace_all_day_plans",
    "select_skeleton",
    "select_transport",
    "set_accommodation_options",
    "set_accommodation",
    "set_alternatives",
    "set_candidate_pool",
    "set_risks",
    "set_shortlist",
    "set_skeleton_plans",
    "set_transport_options",
    "set_trip_brief",
    "update_trip_basics",
]


def _make_plan() -> TravelPlanState:
    return TravelPlanState(session_id="test-plan-tools")


def test_make_all_plan_tools_returns_expected_tools():
    tools = make_all_plan_tools(_make_plan())

    assert len(tools) == 17
    assert [tool.name for tool in tools] == EXPECTED_TOOL_NAMES
    assert len({tool.name for tool in tools}) == 17
    assert {tool.name for tool in tools} == set(EXPECTED_TOOL_NAMES)
    assert all(tool.human_label for tool in tools)
    assert all(tool.side_effect == "write" for tool in tools)
    assert all(tool.phases for tool in tools)
    assert all(callable(tool) for tool in tools)


def test_register_all_plan_tools_registers_each_tool():
    engine = ToolEngine()
    plan = _make_plan()

    register_all_plan_tools(engine, plan)

    assert {name for name in EXPECTED_TOOL_NAMES if engine.get_tool(name)} == set(
        EXPECTED_TOOL_NAMES
    )
