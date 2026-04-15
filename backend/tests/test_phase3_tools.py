import pytest

from state.models import TravelPlanState
from tools.base import ToolError
from tools.plan_tools.phase3_tools import (
    make_select_skeleton_tool,
    make_set_skeleton_plans_tool,
)


@pytest.fixture
def plan():
    return TravelPlanState(session_id="s1")


@pytest.mark.asyncio
async def test_set_skeleton_plans_rejects_missing_name(plan):
    tool_fn = make_set_skeleton_plans_tool(plan)

    with pytest.raises(ToolError, match="name") as exc_info:
        await tool_fn(plans=[{"id": "plan-a"}])

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", [None, "", "   "])
async def test_set_skeleton_plans_rejects_invalid_id_values(plan, bad_id):
    tool_fn = make_set_skeleton_plans_tool(plan)

    with pytest.raises(ToolError, match="id") as exc_info:
        await tool_fn(plans=[{"id": bad_id, "name": "Valid"}])

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_name", [None, "", "   "])
async def test_set_skeleton_plans_rejects_invalid_name_values(plan, bad_name):
    tool_fn = make_set_skeleton_plans_tool(plan)

    with pytest.raises(ToolError, match="name") as exc_info:
        await tool_fn(plans=[{"id": "plan-a", "name": bad_name}])

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_skeleton_plans_rejects_duplicate_ids(plan):
    tool_fn = make_set_skeleton_plans_tool(plan)

    with pytest.raises(ToolError, match="重复") as exc_info:
        await tool_fn(
            plans=[
                {"id": "dup", "name": "A"},
                {"id": "dup", "name": "B"},
            ]
        )

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_select_skeleton_handles_malformed_legacy_ids(plan):
    plan.skeleton_plans = [{"id": None}, {"id": "valid", "name": "Valid"}]
    tool_fn = make_select_skeleton_tool(plan)

    with pytest.raises(ToolError, match="missing") as exc_info:
        await tool_fn(id="missing")

    assert exc_info.value.error_code == "INVALID_VALUE"
    assert exc_info.value.suggestion == "可选 id: valid"
