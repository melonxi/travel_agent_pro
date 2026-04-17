from __future__ import annotations

import pytest

from state.models import Preference, TravelPlanState
from tools.base import ToolError
from tools.plan_tools.append_tools import (
    make_add_constraints_tool,
    make_add_preferences_tool,
)


def _make_plan(phase: int = 1) -> TravelPlanState:
    plan = TravelPlanState(session_id="test-append-tools")
    plan.phase = phase
    return plan


@pytest.mark.parametrize(
    (
        "factory",
        "expected_name",
        "expected_description",
        "expected_label",
        "expected_phases",
        "expected_required",
        "expected_property",
    ),
    [
        (
            make_add_preferences_tool,
            "add_preferences",
            "记录用户偏好。追加到现有偏好列表，不会覆盖已有条目。",
            "记录用户偏好",
            [1, 3, 5],
            ["items"],
            "items",
        ),
        (
            make_add_constraints_tool,
            "add_constraints",
            "记录用户约束条件。追加到现有约束列表，不会覆盖已有条目。",
            "记录用户约束",
            [1, 3, 5],
            ["items"],
            "items",
        ),
    ],
)
def test_append_tool_metadata(
    factory,
    expected_name,
    expected_description,
    expected_label,
    expected_phases,
    expected_required,
    expected_property,
):
    tool_fn = factory(_make_plan())

    assert tool_fn.name == expected_name
    assert tool_fn.description == expected_description
    assert tool_fn.side_effect == "write"
    assert tool_fn.human_label == expected_label
    assert tool_fn.phases == expected_phases
    assert tool_fn.parameters["type"] == "object"
    assert tool_fn.parameters["required"] == expected_required
    assert expected_property in tool_fn.parameters["properties"]


class TestAddPreferencesTool:
    def test_schema_accepts_string_and_object_items(self):
        tool_fn = make_add_preferences_tool(_make_plan())

        item_schema = tool_fn.parameters["properties"]["items"]["items"]
        assert {option["type"] for option in item_schema["anyOf"]} == {
            "string",
            "object",
        }

    @pytest.mark.asyncio
    async def test_appends_string_items_and_dict_items(self):
        plan = _make_plan()
        tool_fn = make_add_preferences_tool(plan)

        result = await tool_fn(items=["美食", {"key": "pace", "value": "慢节奏"}])

        assert result == {
            "updated_field": "preferences",
            "added_count": 2,
            "total_count": 2,
            "previous_count": 0,
        }
        assert [item.key for item in plan.preferences] == ["美食", "pace"]
        assert plan.preferences[1].value == "慢节奏"

    @pytest.mark.asyncio
    async def test_appends_to_existing_preferences(self):
        plan = _make_plan()
        plan.preferences.append(Preference(key="old", value="x"))
        tool_fn = make_add_preferences_tool(plan)

        result = await tool_fn(items=[{"key": "new", "value": "y"}])

        assert result["previous_count"] == 1
        assert result["total_count"] == 2
        assert [item.key for item in plan.preferences] == ["old", "new"]

    @pytest.mark.asyncio
    async def test_accepts_loose_dict_and_reports_real_added_count(self):
        plan = _make_plan()
        tool_fn = make_add_preferences_tool(plan)

        result = await tool_fn(items=[{"不去": ["迪士尼"], "节奏": "不想太赶"}])

        assert result == {
            "updated_field": "preferences",
            "added_count": 2,
            "total_count": 2,
            "previous_count": 0,
        }
        assert [item.key for item in plan.preferences] == ["不去", "节奏"]
        assert plan.preferences[0].value == "迪士尼"
        assert plan.preferences[1].value == "不想太赶"

    @pytest.mark.asyncio
    async def test_rejects_non_list_items(self):
        tool_fn = make_add_preferences_tool(_make_plan())

        with pytest.raises(ToolError, match="items") as exc_info:
            await tool_fn(items="美食")

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_rejects_non_string_or_object_item(self):
        tool_fn = make_add_preferences_tool(_make_plan())

        with pytest.raises(ToolError, match=r"items\[0\]") as exc_info:
            await tool_fn(items=[1])

        assert exc_info.value.error_code == "INVALID_VALUE"


class TestAddConstraintsTool:
    def test_schema_accepts_string_and_object_items(self):
        tool_fn = make_add_constraints_tool(_make_plan())

        item_schema = tool_fn.parameters["properties"]["items"]["items"]
        assert {option["type"] for option in item_schema["anyOf"]} == {
            "string",
            "object",
        }

    @pytest.mark.asyncio
    async def test_appends_string_items_and_dict_items(self):
        plan = _make_plan()
        tool_fn = make_add_constraints_tool(plan)

        result = await tool_fn(
            items=["不赶早班机", {"type": "hard", "description": "不坐红眼航班"}]
        )

        assert result == {
            "updated_field": "constraints",
            "added_count": 2,
            "total_count": 2,
            "previous_count": 0,
        }
        assert [item.description for item in plan.constraints] == [
            "不赶早班机",
            "不坐红眼航班",
        ]
        assert plan.constraints[1].type == "hard"

    @pytest.mark.asyncio
    async def test_rejects_non_list_items(self):
        tool_fn = make_add_constraints_tool(_make_plan())

        with pytest.raises(ToolError, match="items") as exc_info:
            await tool_fn(items="不赶早班机")

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_rejects_non_string_or_object_item(self):
        tool_fn = make_add_constraints_tool(_make_plan())

        with pytest.raises(ToolError, match=r"items\[0\]") as exc_info:
            await tool_fn(items=[1])

        assert exc_info.value.error_code == "INVALID_VALUE"
