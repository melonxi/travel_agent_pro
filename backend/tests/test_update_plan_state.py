# backend/tests/test_update_plan_state.py
import pytest

from state.models import TravelPlanState
from tools.update_plan_state import make_update_plan_state_tool


@pytest.fixture
def plan():
    return TravelPlanState(session_id="s1")


@pytest.fixture
def tool_fn(plan):
    return make_update_plan_state_tool(plan)


@pytest.mark.asyncio
async def test_set_destination(tool_fn, plan):
    result = await tool_fn(field="destination", value="Kyoto")
    assert result["updated_field"] == "destination"
    assert plan.destination == "Kyoto"


@pytest.mark.asyncio
async def test_set_dates(tool_fn, plan):
    result = await tool_fn(
        field="dates", value={"start": "2026-04-10", "end": "2026-04-15"}
    )
    assert plan.dates is not None
    assert plan.dates.total_days == 5


@pytest.mark.asyncio
async def test_set_budget(tool_fn, plan):
    result = await tool_fn(field="budget", value={"total": 15000, "currency": "CNY"})
    assert plan.budget.total == 15000


@pytest.mark.asyncio
async def test_add_preference(tool_fn, plan):
    result = await tool_fn(
        field="preferences", value={"key": "pace", "value": "relaxed"}
    )
    assert len(plan.preferences) == 1
    assert plan.preferences[0].key == "pace"


@pytest.mark.asyncio
async def test_add_constraint(tool_fn, plan):
    result = await tool_fn(
        field="constraints", value={"type": "hard", "description": "预算 1 万"}
    )
    assert len(plan.constraints) == 1


@pytest.mark.asyncio
async def test_invalid_field(tool_fn):
    from tools.base import ToolError

    with pytest.raises(ToolError):
        await tool_fn(field="nonexistent", value="x")
