import pytest

from state.models import DayPlan, TravelPlanState
from tools.base import ToolError
from tools.update_plan_state import make_update_plan_state_tool


STRUCTURED_LIST_FIELDS = (
    "skeleton_plans",
    "candidate_pool",
    "shortlist",
    "transport_options",
    "accommodation_options",
    "risks",
    "alternatives",
    "destination_candidates",
    "daily_plans",
)


@pytest.fixture
def plan():
    return TravelPlanState(session_id="strict")


@pytest.fixture
def tool_fn(plan):
    return make_update_plan_state_tool(plan)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", STRUCTURED_LIST_FIELDS)
async def test_structured_list_fields_reject_plain_strings(tool_fn, field):
    with pytest.raises(ToolError) as excinfo:
        await tool_fn(field=field, value="just a string")

    assert excinfo.value.error_code == "INVALID_VALUE"
    assert field in str(excinfo.value)
    assert "list[object]" in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", STRUCTURED_LIST_FIELDS)
async def test_structured_list_fields_reject_non_dict_list_items(tool_fn, field):
    with pytest.raises(ToolError) as excinfo:
        await tool_fn(field=field, value=[{"ok": True}, "bad-item"])

    assert excinfo.value.error_code == "INVALID_VALUE"
    assert "[1]" in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", STRUCTURED_LIST_FIELDS)
async def test_structured_list_fields_reject_scalar_values(tool_fn, field):
    with pytest.raises(ToolError) as excinfo:
        await tool_fn(field=field, value=123)

    assert excinfo.value.error_code == "INVALID_VALUE"
    assert field in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", STRUCTURED_LIST_FIELDS)
async def test_structured_list_fields_reject_malformed_json_strings(tool_fn, field):
    with pytest.raises(ToolError) as excinfo:
        await tool_fn(field=field, value='[{"broken": true}')

    assert excinfo.value.error_code == "INVALID_VALUE"
    assert "native list[object]" in excinfo.value.suggestion


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("skeleton_plans", [{"id": "balanced", "title": "平衡版"}], "balanced"),
        ("candidate_pool", [{"name": "浅草寺"}], "浅草寺"),
        ("shortlist", [{"name": "清水寺"}], "清水寺"),
        ("transport_options", [{"summary": "新干线"}], "新干线"),
        ("accommodation_options", [{"name": "新宿酒店"}], "新宿酒店"),
        ("risks", [{"type": "weather"}], "weather"),
        ("alternatives", [{"name": "晴天版"}], "晴天版"),
        ("destination_candidates", [{"name": "东京"}], "东京"),
        ("daily_plans", [{"day": 2, "date": "2026-05-02", "activities": []}], 2),
    ],
)
async def test_structured_list_fields_accept_valid_lists(tool_fn, plan, field, value, expected):
    result = await tool_fn(field=field, value=value)

    assert result["updated_field"] == field
    assert getattr(plan, field)[0]
    if field == "daily_plans":
        assert isinstance(plan.daily_plans[0], DayPlan)
        assert plan.daily_plans[0].day == expected
    else:
        assert expected in str(getattr(plan, field)[0])


@pytest.mark.asyncio
async def test_valid_json_string_for_skeleton_plans_still_works(tool_fn, plan):
    await tool_fn(
        field="skeleton_plans",
        value='[{"id":"balanced","title":"平衡版"}]',
    )

    assert plan.skeleton_plans == [{"id": "balanced", "title": "平衡版"}]


@pytest.mark.asyncio
async def test_candidate_pool_single_dict_still_appends(tool_fn, plan):
    plan.candidate_pool = [{"name": "东京塔"}]

    await tool_fn(field="candidate_pool", value={"name": "浅草寺"})

    assert plan.candidate_pool == [{"name": "东京塔"}, {"name": "浅草寺"}]


@pytest.mark.asyncio
async def test_candidate_pool_single_dict_with_nested_broken_json_string_still_appends(
    tool_fn, plan
):
    await tool_fn(
        field="candidate_pool",
        value={"name": "浅草寺", "note": "{broken}"},
    )

    assert plan.candidate_pool == [{"name": "浅草寺", "note": "{broken}"}]


@pytest.mark.asyncio
async def test_basic_fields_remain_phrase_tolerant(tool_fn, plan):
    await tool_fn(field="destination", value="Kyoto")
    await tool_fn(field="dates", value="五一假期，3天")
    await tool_fn(field="travelers", value="2个大人")
    await tool_fn(field="budget", value="1万元")

    assert plan.destination == "Kyoto"
    assert plan.dates is not None
    assert plan.travelers is not None
    assert plan.travelers.adults == 2
    assert plan.budget is not None
    assert plan.budget.total == 10000


@pytest.mark.asyncio
async def test_daily_plans_single_dict_still_appends(tool_fn, plan):
    await tool_fn(
        field="daily_plans",
        value={"day": 1, "date": "2026-05-01", "activities": []},
    )

    assert len(plan.daily_plans) == 1
    assert isinstance(plan.daily_plans[0], DayPlan)
    assert plan.daily_plans[0].day == 1


@pytest.mark.asyncio
async def test_daily_plans_valid_list_still_works(tool_fn, plan):
    await tool_fn(
        field="daily_plans",
        value=[{"day": 2, "date": "2026-05-02", "activities": []}],
    )

    assert len(plan.daily_plans) == 1
    assert isinstance(plan.daily_plans[0], DayPlan)
    assert plan.daily_plans[0].day == 2
