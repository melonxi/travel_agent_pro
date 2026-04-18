from __future__ import annotations

import re
from typing import Any

from harness.validator import validate_day_conflicts
from state.models import TravelPlanState
from state.plan_writers import (
    append_one_day_plan,
    replace_all_daily_plans,
    replace_one_day_plan,
)
from tools.base import ToolError, tool

_REQUIRED_ACTIVITY_FIELDS = {
    "name",
    "location",
    "start_time",
    "end_time",
    "category",
    "cost",
}
_TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")

_SAVE_DAY_PLAN_PARAMETERS = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["create", "replace_existing"],
            "description": "create 新增一天；replace_existing 替换已存在的一天",
        },
        "day": {"type": "integer", "description": "第几天（从1开始）"},
        "date": {"type": "string", "description": "日期，格式 YYYY-MM-DD"},
        "notes": {"type": "string", "description": "当天的补充说明，可选"},
        "activities": {
            "type": "array",
            "items": {"type": "object"},
            "description": "活动列表，每个必须包含 name, location, start_time, end_time, category, cost",
        },
    },
    "required": ["mode", "day", "date", "activities"],
}

_REPLACE_ALL_DAY_PLANS_PARAMETERS = {
    "type": "object",
    "properties": {
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "day": {"type": "integer"},
                    "date": {"type": "string", "description": "格式 YYYY-MM-DD"},
                    "notes": {"type": "string", "description": "当天的补充说明，可选"},
                    "activities": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["day", "date", "activities"],
            },
            "description": "完整的逐日行程列表，每天包含 day, date, activities",
        }
    },
    "required": ["days"],
}


def _validate_day(day: Any, field_name: str) -> None:
    if isinstance(day, bool) or not isinstance(day, int) or day < 1:
        raise ToolError(
            f"{field_name} 必须是从 1 开始的正整数，收到 {type(day).__name__}: {day!r}",
            error_code="INVALID_VALUE",
            suggestion="day 应为从 1 开始的正整数，如 1、2、3",
        )


def _validate_day_in_range(plan: TravelPlanState, day: int, field_name: str) -> None:
    if plan.dates is not None and day > plan.dates.total_days:
        raise ToolError(
            f"{field_name} 超出行程总天数 {plan.dates.total_days}: {day}",
            error_code="INVALID_VALUE",
            suggestion=f"day 应介于 1 到 {plan.dates.total_days} 之间",
        )


def _validate_unique_day_numbers(day_numbers: list[int], field_name: str) -> None:
    seen: set[int] = set()
    for day in day_numbers:
        if day in seen:
            raise ToolError(
                f"{field_name} 出现重复 day={day}",
                error_code="INVALID_VALUE",
                suggestion="每天的 day 编号必须唯一",
            )
        seen.add(day)


def _validate_date_format(date: str) -> None:
    if not isinstance(date, str):
        raise ToolError(
            f"date 必须是字符串，收到 {type(date).__name__}",
            error_code="INVALID_VALUE",
            suggestion='示例: "2026-05-01"',
        )
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ToolError(
            f"date 格式错误: {date!r}，需要 YYYY-MM-DD",
            error_code="INVALID_VALUE",
            suggestion='示例: "2026-05-01"',
        )


def _validate_activities(activities: Any) -> None:
    if not isinstance(activities, list):
        raise ToolError(
            f"activities 必须是 list，收到 {type(activities).__name__}",
            error_code="INVALID_VALUE",
            suggestion=(
                "activities 应为 list[dict]，每个 dict 包含 name, location, "
                "start_time, end_time, category, cost"
            ),
        )
    for index, activity in enumerate(activities):
        if not isinstance(activity, dict):
            raise ToolError(
                f"activities[{index}] 必须是 dict，收到 {type(activity).__name__}",
                error_code="INVALID_VALUE",
                suggestion="每个 activity 必须是 dict",
            )
        missing = _REQUIRED_ACTIVITY_FIELDS - set(activity.keys())
        if missing:
            raise ToolError(
                f"activities[{index}] 缺少必填字段: {', '.join(sorted(missing))}",
                error_code="INVALID_VALUE",
                suggestion=(
                    "每个 activity 必须包含: "
                    f"{', '.join(sorted(_REQUIRED_ACTIVITY_FIELDS))}"
                ),
            )
        location = activity["location"]
        if not isinstance(location, dict):
            raise ToolError(
                f"activities[{index}].location 必须是 dict，收到 {type(location).__name__}",
                error_code="INVALID_VALUE",
                suggestion="location 必须包含 name, lat, lng",
            )
        missing_location = {"name", "lat", "lng"} - set(location.keys())
        if missing_location:
            raise ToolError(
                f"activities[{index}].location 缺少必填字段: {', '.join(sorted(missing_location))}",
                error_code="INVALID_VALUE",
                suggestion="location 必须包含 name, lat, lng",
            )
        if not isinstance(location["name"], str) or not location["name"].strip():
            raise ToolError(
                f"activities[{index}].location.name 必须是非空字符串",
                error_code="INVALID_VALUE",
                suggestion="location.name 应为地点名称字符串",
            )
        for axis in ("lat", "lng"):
            if isinstance(location[axis], bool) or not isinstance(
                location[axis], (int, float)
            ):
                raise ToolError(
                    f"activities[{index}].location.{axis} 必须是数字",
                    error_code="INVALID_VALUE",
                    suggestion=f"location.{axis} 应为坐标数值",
                )
        for field in ("start_time", "end_time"):
            value = activity[field]
            if not isinstance(value, str) or not _TIME_PATTERN.fullmatch(value):
                raise ToolError(
                    f"activities[{index}].{field} 格式错误: {value!r}，需要 HH:MM",
                    error_code="INVALID_VALUE",
                    suggestion=f"{field} 应为 24 小时制时间，如 09:00",
                )
        cost = activity["cost"]
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise ToolError(
                f"activities[{index}].cost 必须是数字",
                error_code="INVALID_VALUE",
                suggestion="cost 应为数字，如 60 或 60.0",
            )


# ---------------------------------------------------------------------------
# Coverage helpers
# ---------------------------------------------------------------------------


def _existing_day_numbers(plan: TravelPlanState) -> list[int]:
    return sorted(day.day for day in plan.daily_plans)


def _missing_day_numbers(plan: TravelPlanState) -> list[int]:
    if plan.dates is None:
        return []
    covered = set(_existing_day_numbers(plan))
    return [
        day_number
        for day_number in range(1, plan.dates.total_days + 1)
        if day_number not in covered
    ]


def _validate_mode(mode: Any) -> str:
    if mode not in {"create", "replace_existing"}:
        raise ToolError(
            f"mode 必须是 create 或 replace_existing，收到 {mode!r}",
            error_code="INVALID_VALUE",
            suggestion='新增一天用 mode="create"；修改已有天用 mode="replace_existing"',
        )
    return str(mode)


def _day_exists(plan: TravelPlanState, day: int) -> bool:
    return any(existing_day.day == day for existing_day in plan.daily_plans)


def _validate_complete_day_coverage(
    plan: TravelPlanState, day_numbers: list[int]
) -> None:
    if plan.dates is None:
        return
    expected = set(range(1, plan.dates.total_days + 1))
    actual = set(day_numbers)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise ToolError(
            f"replace_all_day_plans requires complete coverage; missing days: {', '.join(str(day) for day in missing)}",
            error_code="INCOMPLETE_DAILY_PLANS",
            suggestion=(
                '如果只是补少数天，请用 save_day_plan(mode="create", day=缺失天数, date=对应日期, activities=活动列表)。'
                "如果要全量替换，请传入覆盖 1 到 total_days 的完整 days 列表。"
            ),
        )
    if extra:
        raise ToolError(
            f"days 超出行程总天数 {plan.dates.total_days}: {', '.join(str(day) for day in extra)}",
            error_code="INVALID_VALUE",
            suggestion=f"day 应介于 1 到 {plan.dates.total_days} 之间",
        )


# ---------------------------------------------------------------------------
# New tool factories
# ---------------------------------------------------------------------------


def make_save_day_plan_tool(plan: TravelPlanState):
    @tool(
        name="save_day_plan",
        description=(
            "保存 Phase 5 的单日行程。"
            'Use when: 新增一天用 mode="create"；修改已有某天或修复该天冲突用 mode="replace_existing"。'
            "Don't use when: 需要一次性替换所有天，改用 replace_all_day_plans。"
            "写入后会返回 covered_days/missing_days/conflicts，严重冲突必须先修复。"
        ),
        phases=[5],
        parameters=_SAVE_DAY_PLAN_PARAMETERS,
        side_effect="write",
        human_label="保存单日行程",
    )
    async def save_day_plan(
        mode: str,
        day: int,
        date: str,
        activities: list,
        notes: str = "",
    ) -> dict:
        mode = _validate_mode(mode)
        _validate_day(day, "day")
        _validate_day_in_range(plan, day, "day")
        _validate_date_format(date)
        _validate_activities(activities)

        exists = _day_exists(plan, day)
        if mode == "create" and exists:
            raise ToolError(
                f"day={day} already exists",
                error_code="DAY_ALREADY_EXISTS",
                suggestion=f'Use save_day_plan(mode="replace_existing", day={day}, date=<date>, activities=<activities>) to modify this day.',
            )
        if mode == "replace_existing" and not exists:
            raise ToolError(
                f"day={day} does not exist",
                error_code="DAY_NOT_FOUND",
                suggestion=f'Use save_day_plan(mode="create", day={day}, date=<date>, activities=<activities>) to add this day.',
            )

        previous_count = len(plan.daily_plans)
        payload = {
            "day": day,
            "date": date,
            "notes": str(notes or ""),
            "activities": activities,
        }
        if mode == "create":
            append_one_day_plan(plan, payload)
        else:
            replace_one_day_plan(plan, payload)

        conflict_info = validate_day_conflicts(plan, [day])
        return {
            "updated_field": "daily_plans",
            "action": mode,
            "day": day,
            "date": date,
            "activity_count": len(activities),
            "covered_days": _existing_day_numbers(plan),
            "missing_days": _missing_day_numbers(plan),
            "total_days": len(plan.daily_plans),
            "previous_days": previous_count,
            **conflict_info,
        }

    return save_day_plan


def make_replace_all_day_plans_tool(plan: TravelPlanState):
    @tool(
        name="replace_all_day_plans",
        description=(
            "整体替换 Phase 5 的所有逐日行程。"
            "Use when: 用户要求一次性完整版、全局重排、或跨多天结构需要整体替换。"
            "Don't use when: 只新增或修改一天，改用 save_day_plan。"
            "days 必须覆盖完整 1..total_days。"
        ),
        phases=[5],
        parameters=_REPLACE_ALL_DAY_PLANS_PARAMETERS,
        side_effect="write",
        human_label="整体替换逐日行程",
    )
    async def replace_all_day_plans(days: list) -> dict:
        if not isinstance(days, list):
            raise ToolError(
                f"days 必须是 list，收到 {type(days).__name__}",
                error_code="INVALID_VALUE",
                suggestion="days 应为 list[dict]，每个 dict 包含 day, date, activities",
            )
        for index, day_payload in enumerate(days):
            if not isinstance(day_payload, dict):
                raise ToolError(
                    f"days[{index}] 必须是 dict，收到 {type(day_payload).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每天的数据必须是 dict，包含 day, date, activities",
                )
            missing = {"day", "date", "activities"} - set(day_payload.keys())
            if missing:
                raise ToolError(
                    f"days[{index}] 缺少必填字段: {', '.join(sorted(missing))}",
                    error_code="INVALID_VALUE",
                    suggestion="每天必须包含 day, date, activities",
                )
            _validate_day(day_payload["day"], f"days[{index}].day")
            _validate_day_in_range(plan, day_payload["day"], f"days[{index}].day")
            _validate_date_format(day_payload["date"])
            _validate_activities(day_payload["activities"])

        day_numbers = [day_payload["day"] for day_payload in days]
        _validate_unique_day_numbers(day_numbers, "days")
        _validate_complete_day_coverage(plan, day_numbers)
        previous_count = len(plan.daily_plans)
        replace_all_daily_plans(plan, days)
        conflict_info = validate_day_conflicts(plan, day_numbers)
        return {
            "updated_field": "daily_plans",
            "action": "replace_all",
            "total_days": len(plan.daily_plans),
            "previous_days": previous_count,
            "covered_days": _existing_day_numbers(plan),
            "missing_days": _missing_day_numbers(plan),
            **conflict_info,
        }

    return replace_all_day_plans


# ---------------------------------------------------------------------------
# Legacy factories (hidden from Phase 5, kept for backward compatibility)
# ---------------------------------------------------------------------------


def make_append_day_plan_tool(plan: TravelPlanState):
    @tool(
        name="append_day_plan",
        description="追加一天的行程计划。传入天数编号、日期和活动列表。",
        phases=[],
        parameters=_SAVE_DAY_PLAN_PARAMETERS,
        side_effect="write",
        human_label="追加一天行程（legacy）",
    )
    async def append_day_plan(
        day: int,
        date: str,
        activities: list,
        notes: str = "",
        **kwargs: Any,
    ) -> dict:
        return await make_save_day_plan_tool(plan)._fn(
            mode="create",
            day=day,
            date=date,
            activities=activities,
            notes=notes,
        )

    return append_day_plan


def make_replace_daily_plans_tool(plan: TravelPlanState):
    @tool(
        name="replace_daily_plans",
        description="整体替换所有逐日行程。传入完整的天数列表。",
        phases=[],
        parameters=_REPLACE_ALL_DAY_PLANS_PARAMETERS,
        side_effect="write",
        human_label="整体替换逐日行程（legacy）",
    )
    async def replace_daily_plans(days: list) -> dict:
        return await make_replace_all_day_plans_tool(plan)._fn(days=days)

    return replace_daily_plans
