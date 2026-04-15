# backend/tools/update_plan_state.py
from __future__ import annotations

import json
from typing import Any

from state.intake import parse_budget_value, parse_dates_value, parse_travelers_value
from state.models import (
    Accommodation,
    Constraint,
    Preference,
    Travelers,
    TravelPlanState,
)
from state.plan_writers import (
    append_one_day_plan,
    replace_all_daily_plans,
    write_accommodation_options,
    write_alternatives,
    write_candidate_pool,
    write_risks,
    write_shortlist,
    write_skeleton_plans,
    write_transport_options,
    write_trip_brief,
)
from tools.base import ToolError, tool

_UNCOMPARABLE = object()


def _snapshot_field(plan: TravelPlanState, field: str) -> Any:
    """Capture current field value before update, for state diff tracking."""
    if field == "destination":
        return plan.destination if plan.destination else None
    if field == "dates":
        return plan.dates.to_dict() if plan.dates else None
    if field == "travelers":
        return plan.travelers.to_dict() if plan.travelers else None
    if field == "budget":
        return plan.budget.to_dict() if plan.budget else None
    if field == "accommodation":
        return plan.accommodation.to_dict() if plan.accommodation else None
    if field == "selected_skeleton_id":
        return plan.selected_skeleton_id
    if field == "selected_transport":
        return plan.selected_transport
    if field in ("preferences", "constraints", "daily_plans"):
        return len(getattr(plan, field, []))
    return None


_ALLOWED_FIELDS = {
    "destination",
    "dates",
    "trip_brief",
    "candidate_pool",
    "shortlist",
    "skeleton_plans",
    "selected_skeleton_id",
    "transport_options",
    "selected_transport",
    "accommodation_options",
    "travelers",
    "budget",
    "accommodation",
    "risks",
    "alternatives",
    "preferences",
    "constraints",
    "destination_candidates",
    "daily_plans",
    "backtrack",
}

_STRUCTURED_LIST_FIELDS = {
    "skeleton_plans",
    "candidate_pool",
    "shortlist",
    "transport_options",
    "accommodation_options",
    "risks",
    "alternatives",
    "daily_plans",
}


def _stringify_preference_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " · ".join(
            part
            for part in (_stringify_preference_value(item) for item in value)
            if part
        )
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            text = _stringify_preference_value(item)
            if text:
                parts.append(f"{key}: {text}")
        return "；".join(parts)
    return str(value)


def _append_preferences(plan: TravelPlanState, value: Any) -> None:
    if isinstance(value, dict):
        if "key" in value:
            plan.preferences.append(Preference.from_dict(value))
            return
        for key, item in value.items():
            plan.preferences.append(
                Preference(key=str(key), value=_stringify_preference_value(item))
            )
        return
    if isinstance(value, list):
        for item in value:
            _append_preferences(plan, item)
        return
    plan.preferences.append(Preference(key=str(value), value=""))


def _coerce_jsonish(value: Any, *, field: str | None = None) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text and text[0] in "[{" and text[-1] in "]}":
            try:
                return _coerce_jsonish(json.loads(text), field=field)
            except json.JSONDecodeError:
                if field in _STRUCTURED_LIST_FIELDS:
                    raise ToolError(
                        f"{field} 必须传原生 list[object]，不要传无法解析的字符串",
                        error_code="INVALID_VALUE",
                        suggestion=f"{field} 请直接传 native list[object]，不要传 string",
                    )
                return value
        return value
    if isinstance(value, list):
        return [_coerce_jsonish(item, field=field) for item in value]
    if isinstance(value, dict):
        return {key: _coerce_jsonish(item, field=field) for key, item in value.items()}
    return value


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
    if field == "selected_skeleton_id":
        return str(value)
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
    if field == "selected_skeleton_id":
        return plan.selected_skeleton_id
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
                "阶段 3 新增：trip_brief、candidate_pool、shortlist、skeleton_plans、selected_skeleton_id、transport_options、selected_transport、accommodation_options、risks、alternatives。"
                "注意：phase3_step 由系统自动推导，不支持通过本工具写入。"
            ),
        },
        "value": {
            "description": (
                "字段的新值，格式取决于 field。"
                'destination 建议传纯字符串；dates 建议传 {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}；'
                'travelers 建议传结构化人数或可解析短语；budget 建议传数字、金额字符串或 {"total": number, "currency": "..."}；'
                "preferences/constraints 为追加写入；destination_candidates 传单个对象会追加，传列表会整体替换；"
                "trip_brief 建议传 dict 并做增量合并；candidate_pool/shortlist/skeleton_plans/transport_options/accommodation_options/risks/alternatives 传 list[object] 可整体替换、传单个 object 会追加；"
                "selected_skeleton_id 建议传字符串（必须精确匹配 skeleton_plans 中某项的 id 字段）；selected_transport 建议传 dict；"
                'daily_plans 传单个 dict 追加一天（形如 {"day":1,"date":"2026-05-01","activities":[...]}），'
                '传 list[object] 整体替换全部天数；结构化列表字段若传 JSON 字符串，必须是合法 JSON；'
                '每个 activity 必须是 dict，且 location 必须是 {"name":..,"lat":..,"lng":..} dict，'
                'start_time/end_time 必须是 "HH:MM" 字符串，category 必须提供，cost 必须是数字；'
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
  - 你在 phase 3 里需要把旅行画像、候选池、骨架方案、已选骨架、交通候选、风险和备选项写入状态，方便后续局部重规划。
  - 用户要推翻之前的阶段结论，回到更早阶段重新规划。回退时使用 field="backtrack"，value={"to_phase": 目标阶段, "reason": "回退原因"}。
Don't use when:
  - 只是做分析、比较、推荐，但用户并没有给出新的明确决策。
  - 你只是想把自己推荐出的候选、默认偏好或分析结论写进状态。
Important:
  - 这是状态写入工具，不负责分析。
  - 对 dates、budget、travelers 这类字段，优先传明确结构化值；如果传入不可解析的值，当前实现可能会把已有字段覆盖为空值。""",
        phases=[1, 3, 5, 7],
        parameters=_PARAMETERS,
        side_effect="write",
        human_label="更新旅行计划",
    )
    async def update_plan_state(field: str, value: Any) -> dict:
        if field not in _ALLOWED_FIELDS:
            raise ToolError(
                f"不支持的字段: {field}",
                error_code="INVALID_FIELD",
                suggestion=f"可用字段: {', '.join(sorted(_ALLOWED_FIELDS))}",
            )

        if field == "backtrack":
            value = _coerce_jsonish(value, field=field)
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

        previous_value = _snapshot_field(plan, field)
        value = _coerce_jsonish(value, field=field)

        if field in _STRUCTURED_LIST_FIELDS:
            if isinstance(value, str):
                raise ToolError(
                    f"{field} 必须是 list[object]，不要传 string",
                    error_code="INVALID_VALUE",
                    suggestion=f"{field} 请直接传 native list[object]；如果是追加单项，请直接传 dict",
                )
            if isinstance(value, list):
                for index, item in enumerate(value):
                    if not isinstance(item, dict):
                        raise ToolError(
                            f"{field}[{index}] 必须是 object，实际收到 {type(item).__name__}",
                            error_code="INVALID_VALUE",
                            suggestion=f"{field} 必须传 list[object]",
                        )

        if field == "destination":
            if isinstance(value, dict):
                plan.destination = str(value.get("name", value))
            else:
                plan.destination = str(value)
        elif field == "dates":
            plan.dates = parse_dates_value(value)
        elif field == "trip_brief":
            if not isinstance(value, dict):
                raise ToolError(
                    "trip_brief 的值必须是 dict",
                    error_code="INVALID_VALUE",
                    suggestion='示例: {"goal": "慢旅行", "pace": "relaxed"}',
                )
            write_trip_brief(plan, value)
        elif field == "candidate_pool":
            if isinstance(value, list):
                write_candidate_pool(plan, value)
            else:
                write_candidate_pool(plan, [*plan.candidate_pool, value])
        elif field == "shortlist":
            if isinstance(value, list):
                write_shortlist(plan, value)
            else:
                write_shortlist(plan, [*plan.shortlist, value])
        elif field == "skeleton_plans":
            if isinstance(value, list):
                write_skeleton_plans(plan, value)
            else:
                write_skeleton_plans(plan, [*plan.skeleton_plans, value])
        elif field == "selected_skeleton_id":
            plan.selected_skeleton_id = str(value)
        elif field == "transport_options":
            if isinstance(value, list):
                write_transport_options(plan, value)
            else:
                write_transport_options(plan, [*plan.transport_options, value])
        elif field == "selected_transport":
            if isinstance(value, dict):
                plan.selected_transport = value
            else:
                plan.selected_transport = {"summary": str(value)}
        elif field == "accommodation_options":
            if isinstance(value, list):
                write_accommodation_options(plan, value)
            else:
                write_accommodation_options(plan, [*plan.accommodation_options, value])
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
                hotel = (
                    value.get("hotel") or value.get("hotel_name") or value.get("name")
                )
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
            _append_preferences(plan, value)
        elif field == "constraints":
            if isinstance(value, dict):
                constraint_type = str(value.get("type", "soft"))
                description = str(
                    value.get("description") or value.get("summary") or value
                )
                plan.constraints.append(
                    Constraint(type=constraint_type, description=description)
                )
            else:
                plan.constraints.append(Constraint(type="soft", description=str(value)))
        elif field == "risks":
            if isinstance(value, list):
                write_risks(plan, value)
            else:
                write_risks(plan, [*plan.risks, value])
        elif field == "alternatives":
            if isinstance(value, list):
                write_alternatives(plan, value)
            else:
                write_alternatives(plan, [*plan.alternatives, value])
        elif field == "destination_candidates":
            if isinstance(value, list):
                plan.destination_candidates = value
            else:
                plan.destination_candidates.append(value)
        elif field == "daily_plans":
            if isinstance(value, list):
                replace_all_daily_plans(plan, value)
            elif isinstance(value, dict):
                append_one_day_plan(plan, value)
            else:
                raise ToolError(
                    "daily_plans 的值必须是 dict（单日）或 list[dict]（多日）",
                    error_code="INVALID_VALUE",
                    suggestion='示例: {"day": 1, "date": "2026-05-01", "activities": [...]}',
                )

        return {
            "updated_field": field,
            "new_value": str(value)[:200],
            "previous_value": previous_value,
        }

    return update_plan_state
