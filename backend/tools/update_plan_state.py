# backend/tools/update_plan_state.py
from __future__ import annotations

from typing import Any

from state.intake import parse_budget_value, parse_dates_value
from state.models import (
    Accommodation,
    Constraint,
    Preference,
    Travelers,
    TravelPlanState,
)
from tools.base import ToolError, tool

_ALLOWED_FIELDS = {
    "destination",
    "dates",
    "travelers",
    "budget",
    "accommodation",
    "preferences",
    "constraints",
    "destination_candidates",
}

_PARAMETERS = {
    "type": "object",
    "properties": {
        "field": {
            "type": "string",
            "description": f"要更新的字段名。可选值：{', '.join(sorted(_ALLOWED_FIELDS))}",
        },
        "value": {
            "description": "字段的新值。格式取决于字段类型。",
        },
    },
    "required": ["field", "value"],
}


def make_update_plan_state_tool(plan: TravelPlanState):
    """Create an update_plan_state tool bound to a specific plan instance."""

    @tool(
        name="update_plan_state",
        description="""更新旅行规划状态。
Use when: 用户提供了新的信息需要记录到规划中（目的地、日期、预算、偏好等）。
Don't use when: 只是闲聊或询问信息，没有新的决策需要记录。""",
        phases=[1, 2, 3, 4, 5, 7],
        parameters=_PARAMETERS,
    )
    async def update_plan_state(field: str, value: Any) -> dict:
        if field not in _ALLOWED_FIELDS:
            raise ToolError(
                f"不支持的字段: {field}",
                error_code="INVALID_FIELD",
                suggestion=f"可用字段: {', '.join(sorted(_ALLOWED_FIELDS))}",
            )

        if field == "destination":
            plan.destination = str(value)
        elif field == "dates":
            plan.dates = parse_dates_value(value)
        elif field == "travelers":
            plan.travelers = (
                Travelers.from_dict(value) if isinstance(value, dict) else None
            )
        elif field == "budget":
            plan.budget = parse_budget_value(value)
        elif field == "accommodation":
            plan.accommodation = (
                Accommodation.from_dict(value) if isinstance(value, dict) else None
            )
        elif field == "preferences":
            plan.preferences.append(
                Preference.from_dict(value)
                if isinstance(value, dict)
                else Preference(key=str(value), value="")
            )
        elif field == "constraints":
            plan.constraints.append(
                Constraint.from_dict(value)
                if isinstance(value, dict)
                else Constraint(type="soft", description=str(value))
            )
        elif field == "destination_candidates":
            if isinstance(value, list):
                plan.destination_candidates = value
            else:
                plan.destination_candidates.append(value)

        return {"updated_field": field, "new_value": str(value)[:200]}

    return update_plan_state
