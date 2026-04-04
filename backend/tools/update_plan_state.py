# backend/tools/update_plan_state.py
from __future__ import annotations

from typing import Any

from state.intake import parse_budget_value, parse_dates_value
from state.models import (
    Accommodation,
    Constraint,
    DayPlan,
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
    "daily_plans",
    "backtrack",
}

_PARAMETERS = {
    "type": "object",
    "properties": {
        "field": {
            "type": "string",
            "description": f"要更新的字段名。可选值：{', '.join(sorted(_ALLOWED_FIELDS))}",
        },
        "value": {
            "description": '字段的新值。格式取决于字段类型。当 field 为 "backtrack" 时，value 应为 {"to_phase": int, "reason": str}。',
        },
    },
    "required": ["field", "value"],
}


def make_update_plan_state_tool(plan: TravelPlanState):
    """Create an update_plan_state tool bound to a specific plan instance."""

    @tool(
        name="update_plan_state",
        description="""更新旅行规划状态，或触发阶段回退。
Use when:
  - 用户提供了新的信息需要记录到规划中（目的地、日期、预算、偏好等）。
  - 用户想要修改之前的决定，需要回退到更早的规划阶段。回退时使用 field="backtrack"，value={"to_phase": 目标阶段, "reason": "回退原因"}。
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

        if field == "backtrack":
            if not isinstance(value, dict) or "to_phase" not in value:
                raise ToolError(
                    "backtrack 的 value 必须包含 to_phase 字段",
                    error_code="INVALID_VALUE",
                    suggestion='示例: {"to_phase": 3, "reason": "用户想换目的地"}',
                )
            to_phase = int(value["to_phase"])
            reason = str(value.get("reason", "用户请求回退"))
            if to_phase >= plan.phase:
                raise ToolError(
                    f"只能回退到更早的阶段，当前阶段: {plan.phase}，目标: {to_phase}",
                    error_code="INVALID_BACKTRACK",
                    suggestion=f"目标阶段必须小于当前阶段 {plan.phase}",
                )
            from_phase = plan.phase
            from phase.backtrack import BacktrackService

            service = BacktrackService()
            service.execute(plan, to_phase, reason, snapshot_path="")
            return {
                "backtracked": True,
                "from_phase": from_phase,
                "to_phase": to_phase,
                "reason": reason,
                "next_action": "请向用户确认回退结果，不要继续调用其他工具",
            }

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
        elif field == "daily_plans":
            if isinstance(value, list):
                plan.daily_plans = [
                    DayPlan.from_dict(v) if isinstance(v, dict) else v for v in value
                ]
            elif isinstance(value, dict):
                plan.daily_plans.append(DayPlan.from_dict(value))
            else:
                raise ToolError(
                    "daily_plans 的值必须是 dict（单日）或 list[dict]（多日）",
                    error_code="INVALID_VALUE",
                    suggestion='示例: {"day": 1, "date": "2026-05-01", "activities": [...]}',
                )

        return {"updated_field": field, "new_value": str(value)[:200]}

    return update_plan_state
