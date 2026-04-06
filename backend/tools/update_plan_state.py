# backend/tools/update_plan_state.py
from __future__ import annotations

from typing import Any

from state.intake import parse_budget_value, parse_dates_value, parse_travelers_value
from state.models import (
    Accommodation,
    Constraint,
    DayPlan,
    Preference,
    Travelers,
    TravelPlanState,
)
from tools.base import ToolError, tool

_UNCOMPARABLE = object()

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


def is_redundant_update_plan_state(
    plan: TravelPlanState,
    *,
    field: str,
    value: Any,
) -> bool:
    incoming = _normalize_comparable_value(field, value)
    current = _current_comparable_value(plan, field)
    if incoming is _UNCOMPARABLE or current is _UNCOMPARABLE:
        return False
    return incoming == current


def _normalize_comparable_value(field: str, value: Any) -> Any:
    if field == "destination":
        if isinstance(value, dict):
            return str(value.get("name", value))
        return str(value)
    if field == "dates":
        parsed = parse_dates_value(value)
        return parsed.to_dict() if parsed else _UNCOMPARABLE
    if field == "travelers":
        parsed = parse_travelers_value(value)
        return parsed.to_dict() if parsed else _UNCOMPARABLE
    if field == "budget":
        parsed = parse_budget_value(value)
        return parsed.to_dict() if parsed else _UNCOMPARABLE
    if field == "accommodation":
        if isinstance(value, dict):
            area = (
                value.get("area")
                or value.get("location")
                or value.get("district")
                or value.get("neighborhood")
                or value.get("address")
            )
            hotel = value.get("hotel") or value.get("hotel_name") or value.get("name")
            if not area and not hotel:
                return _UNCOMPARABLE
            return {"area": str(area or hotel), "hotel": str(hotel) if hotel else None}
        if isinstance(value, str):
            return {"area": value, "hotel": None}
        return _UNCOMPARABLE
    return _UNCOMPARABLE


def _current_comparable_value(plan: TravelPlanState, field: str) -> Any:
    if field == "destination":
        return plan.destination
    if field == "dates":
        return plan.dates.to_dict() if plan.dates else None
    if field == "travelers":
        return plan.travelers.to_dict() if plan.travelers else None
    if field == "budget":
        return plan.budget.to_dict() if plan.budget else None
    if field == "accommodation":
        return plan.accommodation.to_dict() if plan.accommodation else None
    return _UNCOMPARABLE

_PARAMETERS = {
    "type": "object",
    "properties": {
        "field": {
            "type": "string",
            "description": (
                "要更新的字段名。可选值："
                f"{', '.join(sorted(_ALLOWED_FIELDS))}。"
                "阶段 1 常用：destination、dates、travelers、budget、preferences、constraints、destination_candidates、backtrack。"
            ),
        },
        "value": {
            "description": (
                '字段的新值，格式取决于 field。'
                'destination 建议传纯字符串；dates 建议传 {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}；'
                'travelers 建议传结构化人数或可解析短语；budget 建议传数字、金额字符串或 {"total": number, "currency": "..."}；'
                'preferences/constraints 为追加写入；destination_candidates 传单个对象会追加，传列表会整体替换；'
                '当 field 为 "backtrack" 时，value 必须为 {"to_phase": int, "reason": str}。'
            ),
        },
    },
    "required": ["field", "value"],
}


def make_update_plan_state_tool(plan: TravelPlanState):
    """Create an update_plan_state tool bound to a specific plan instance."""

    @tool(
        name="update_plan_state",
        description="""写入旅行规划状态，或触发阶段回退。
Use when:
  - 用户已经明确表达了新的决策，需要把目的地、日期、人数、预算、偏好、约束、候选地等写入当前 plan。
  - 用户要推翻之前的阶段结论，回到更早阶段重新规划。回退时使用 field="backtrack"，value={"to_phase": 目标阶段, "reason": "回退原因"}。
Don't use when:
  - 只是做分析、比较、推荐，但用户并没有给出新的明确决策。
  - 你只是想把自己推荐出的候选、默认偏好或分析结论写进状态。
Important:
  - 这是状态写入工具，不负责分析。
  - 对 dates、budget、travelers 这类字段，优先传明确结构化值；如果传入不可解析的值，当前实现可能会把已有字段覆盖为空值。""",
        phases=[1, 3, 5, 7],
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
            if to_phase == 2:
                to_phase = 1
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
            if isinstance(value, dict):
                plan.destination = str(value.get("name", value))
            else:
                plan.destination = str(value)
        elif field == "dates":
            plan.dates = parse_dates_value(value)
        elif field == "travelers":
            plan.travelers = parse_travelers_value(value)
        elif field == "budget":
            plan.budget = parse_budget_value(value)
        elif field == "accommodation":
            if isinstance(value, dict):
                area = (
                    value.get("area")
                    or value.get("location")
                    or value.get("district")
                    or value.get("neighborhood")
                    or value.get("address")
                )
                hotel = value.get("hotel") or value.get("hotel_name") or value.get("name")
                if not area and not hotel:
                    raise ToolError(
                        "accommodation 的值缺少 area/location 或 hotel/hotel_name",
                        error_code="INVALID_VALUE",
                        suggestion='示例: {"area": "新宿", "hotel": "Hyatt Regency Tokyo"}',
                    )
                plan.accommodation = Accommodation(
                    area=str(area or hotel),
                    hotel=str(hotel) if hotel else None,
                )
            elif isinstance(value, str):
                plan.accommodation = Accommodation(area=value)
            else:
                raise ToolError(
                    "accommodation 的值必须是 dict 或 string",
                    error_code="INVALID_VALUE",
                    suggestion='示例: {"area": "新宿", "hotel": "Hyatt Regency Tokyo"}',
                )
        elif field == "preferences":
            if isinstance(value, dict):
                plan.preferences.append(Preference.from_dict(value))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        plan.preferences.append(Preference.from_dict(item))
                    else:
                        plan.preferences.append(Preference(key=str(item), value=""))
            else:
                plan.preferences.append(Preference(key=str(value), value=""))
        elif field == "constraints":
            if isinstance(value, dict):
                constraint_type = str(value.get("type", "soft"))
                description = str(
                    value.get("description")
                    or value.get("summary")
                    or value
                )
                plan.constraints.append(
                    Constraint(type=constraint_type, description=description)
                )
            else:
                plan.constraints.append(
                    Constraint(type="soft", description=str(value))
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
