# backend/tests/test_update_plan_state.py
from datetime import date, timedelta

import pytest

from state.intake import extract_trip_facts
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
async def test_set_dates_from_natural_language_string(tool_fn, plan):
    await tool_fn(field="dates", value="五一假期，3天")

    expected_start = date.today().replace(month=5, day=1)
    if expected_start < date.today():
        expected_start = expected_start.replace(year=expected_start.year + 1)

    assert plan.dates is not None
    assert plan.dates.start == expected_start.isoformat()
    assert plan.dates.end == (expected_start + timedelta(days=3)).isoformat()


@pytest.mark.asyncio
async def test_set_budget(tool_fn, plan):
    result = await tool_fn(field="budget", value={"total": 15000, "currency": "CNY"})
    assert plan.budget.total == 15000


@pytest.mark.asyncio
async def test_set_budget_from_string(tool_fn, plan):
    await tool_fn(field="budget", value="1万元")

    assert plan.budget is not None
    assert plan.budget.total == 10000
    assert plan.budget.currency == "CNY"


@pytest.mark.asyncio
async def test_set_budget_from_number(tool_fn, plan):
    await tool_fn(field="budget", value=20000)

    assert plan.budget is not None
    assert plan.budget.total == 20000
    assert plan.budget.currency == "CNY"


@pytest.mark.asyncio
async def test_set_travelers_from_string(tool_fn, plan):
    await tool_fn(field="travelers", value="2个大人")

    assert plan.travelers is not None
    assert plan.travelers.adults == 2
    assert plan.travelers.children == 0


def test_extract_trip_facts_ignores_negated_destination_without_replacement():
    facts = extract_trip_facts("不想去京都了，换个目的地")

    assert "destination" not in facts


@pytest.mark.asyncio
async def test_set_accommodation_accepts_alias_fields(tool_fn, plan):
    await tool_fn(
        field="accommodation",
        value={
            "hotel_name": "Hyatt Regency Tokyo",
            "location": "西新宿",
            "address": "东京都新宿区西新宿2-7-2",
        },
    )

    assert plan.accommodation is not None
    assert plan.accommodation.area == "西新宿"
    assert plan.accommodation.hotel == "Hyatt Regency Tokyo"


@pytest.mark.asyncio
async def test_add_preference(tool_fn, plan):
    result = await tool_fn(
        field="preferences", value={"key": "pace", "value": "relaxed"}
    )
    assert len(plan.preferences) == 1
    assert plan.preferences[0].key == "pace"


@pytest.mark.asyncio
async def test_add_preference_accepts_loose_dict(tool_fn, plan):
    await tool_fn(
        field="preferences",
        value={"不去": ["迪士尼"], "节奏": "不想太赶", "住宿区域": ["新宿", "涩谷"]},
    )

    assert [pref.key for pref in plan.preferences] == ["不去", "节奏", "住宿区域"]
    assert plan.preferences[0].value == "迪士尼"
    assert plan.preferences[2].value == "新宿 · 涩谷"


@pytest.mark.asyncio
async def test_add_constraint(tool_fn, plan):
    result = await tool_fn(
        field="constraints", value={"type": "hard", "description": "预算 1 万"}
    )
    assert len(plan.constraints) == 1


@pytest.mark.asyncio
async def test_add_constraint_accepts_loose_dict(tool_fn, plan):
    await tool_fn(
        field="constraints", value={"duration_days": 5, "season": "五一假期"}
    )

    assert len(plan.constraints) == 1
    assert plan.constraints[0].type == "soft"
    assert "duration_days" in plan.constraints[0].description


@pytest.mark.asyncio
async def test_phase3_structured_fields_accept_json_strings(tool_fn, plan):
    await tool_fn(
        field="trip_brief",
        value='{"goal":"慢旅行","pace":"relaxed"}',
    )
    await tool_fn(
        field="candidate_pool",
        value='[{"name":"浅草寺","area":"浅草","theme":"传统文化"}]',
    )
    await tool_fn(
        field="skeleton_plans",
        value='[{"id":"balanced","title":"平衡版"}]',
    )
    await tool_fn(
        field="preferences",
        value='["轻松","美食"]',
    )

    assert plan.trip_brief == {"goal": "慢旅行", "pace": "relaxed"}
    assert plan.candidate_pool == [{"name": "浅草寺", "area": "浅草", "theme": "传统文化"}]
    assert plan.skeleton_plans == [{"id": "balanced", "title": "平衡版"}]
    assert [pref.key for pref in plan.preferences] == ["轻松", "美食"]


@pytest.mark.asyncio
async def test_invalid_field(tool_fn):
    from tools.base import ToolError

    with pytest.raises(ToolError):
        await tool_fn(field="nonexistent", value="x")
