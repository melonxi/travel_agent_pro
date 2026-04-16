from __future__ import annotations

from typing import Any

from state.models import TravelPlanState
from state.plan_writers import (
    append_constraints,
    append_destination_candidate,
    append_preferences,
    replace_destination_candidates,
)
from tools.base import ToolError, tool


def _validate_items_list(items: Any, field_name: str) -> list[Any]:
    if not isinstance(items, list):
        raise ToolError(
            f"{field_name} 必须是 list，收到 {type(items).__name__}",
            error_code="INVALID_VALUE",
            suggestion=f"请传入 {field_name}: list",
        )
    return items


def _validate_string_or_object_items(items: list[Any], field_name: str) -> None:
    for index, item in enumerate(items):
        if not isinstance(item, (str, dict)):
            raise ToolError(
                f"{field_name}[{index}] 必须是 string 或 object，收到 {type(item).__name__}",
                error_code="INVALID_VALUE",
                suggestion=f"{field_name}[{index}] 应为字符串或 JSON 对象",
            )


def _validate_object_items(items: list[Any], field_name: str) -> None:
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ToolError(
                f"{field_name}[{index}] 必须是 object，收到 {type(item).__name__}",
                error_code="INVALID_VALUE",
                suggestion=f"{field_name}[{index}] 应为 JSON 对象",
            )


_PREFERENCES_PARAMS = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "anyOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "value": {},
                        },
                    },
                ]
            },
            "description": "偏好列表，支持字符串、{key, value} 对象，或会展开为多条偏好的键值映射对象",
        }
    },
    "required": ["items"],
}

_CONSTRAINTS_PARAMS = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "anyOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "description": {"type": "string"},
                        },
                    },
                ]
            },
            "description": "约束列表，支持字符串或 {type, description} 对象",
        }
    },
    "required": ["items"],
}

_DESTINATION_CANDIDATE_PARAMS = {
    "type": "object",
    "properties": {
        "item": {
            "type": "object",
            "description": "单个目的地候选对象",
        }
    },
    "required": ["item"],
}

_SET_DESTINATION_CANDIDATES_PARAMS = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": "object"},
            "description": "完整目的地候选列表",
        }
    },
    "required": ["items"],
}


def make_add_preferences_tool(plan: TravelPlanState):
    @tool(
        name="add_preferences",
        description="记录用户偏好。追加到现有偏好列表，不会覆盖已有条目。",
        phases=[1, 3, 5],
        parameters=_PREFERENCES_PARAMS,
        side_effect="write",
        human_label="记录用户偏好",
    )
    async def add_preferences(items: list) -> dict:
        items = _validate_items_list(items, "items")
        _validate_string_or_object_items(items, "items")
        previous_count = len(plan.preferences)
        append_preferences(plan, items)
        added_count = len(plan.preferences) - previous_count
        return {
            "updated_field": "preferences",
            "added_count": added_count,
            "total_count": len(plan.preferences),
            "previous_count": previous_count,
        }

    return add_preferences


def make_add_constraints_tool(plan: TravelPlanState):
    @tool(
        name="add_constraints",
        description="记录用户约束条件。追加到现有约束列表，不会覆盖已有条目。",
        phases=[1, 3, 5],
        parameters=_CONSTRAINTS_PARAMS,
        side_effect="write",
        human_label="记录用户约束",
    )
    async def add_constraints(items: list) -> dict:
        items = _validate_items_list(items, "items")
        _validate_string_or_object_items(items, "items")
        previous_count = len(plan.constraints)
        append_constraints(plan, items)
        return {
            "updated_field": "constraints",
            "added_count": len(items),
            "total_count": len(plan.constraints),
            "previous_count": previous_count,
        }

    return add_constraints


def make_add_destination_candidate_tool(plan: TravelPlanState):
    @tool(
        name="add_destination_candidate",
        description="追加一个目的地候选到列表末尾。",
        phases=[1],
        parameters=_DESTINATION_CANDIDATE_PARAMS,
        side_effect="write",
        human_label="追加目的地候选",
    )
    async def add_destination_candidate(item: dict) -> dict:
        if not isinstance(item, dict):
            raise ToolError(
                f"item 必须是 dict，收到 {type(item).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传入单个 JSON 对象",
            )
        previous_count = len(plan.destination_candidates)
        append_destination_candidate(plan, item)
        return {
            "updated_field": "destination_candidates",
            "action": "append",
            "total_count": len(plan.destination_candidates),
            "previous_count": previous_count,
        }

    return add_destination_candidate


def make_set_destination_candidates_tool(plan: TravelPlanState):
    @tool(
        name="set_destination_candidates",
        description="整体替换目的地候选列表。",
        phases=[1],
        parameters=_SET_DESTINATION_CANDIDATES_PARAMS,
        side_effect="write",
        human_label="整体替换候选列表",
    )
    async def set_destination_candidates(items: list) -> dict:
        items = _validate_items_list(items, "items")
        _validate_object_items(items, "items")
        previous_count = len(plan.destination_candidates)
        replace_destination_candidates(plan, items)
        return {
            "updated_field": "destination_candidates",
            "action": "replace",
            "total_count": len(plan.destination_candidates),
            "previous_count": previous_count,
        }

    return set_destination_candidates
