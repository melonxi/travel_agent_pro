from __future__ import annotations

import re
from typing import Any

from harness.validator import validate_day_conflicts
from state.models import TravelPlanState
from state.plan_writers import append_one_day_plan, replace_all_daily_plans
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

_APPEND_DAY_PLAN_PARAMETERS = {
    "type": "object",
    "properties": {
        "day": {"type": "integer", "description": "第几天（从1开始）"},
        "date": {"type": "string", "description": "日期，格式 YYYY-MM-DD"},
        "activities": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "活动列表，每个必须包含 name, location, start_time, "
                "end_time, category, cost"
            ),
        },
    },
    "required": ["day", "date", "activities"],
}

_REPLACE_DAILY_PLANS_PARAMETERS = {
    "type": "object",
    "properties": {
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "day": {"type": "integer"},
                    "date": {"type": "string", "description": "格式 YYYY-MM-DD"},
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


def make_append_day_plan_tool(plan: TravelPlanState):
    @tool(
        name="append_day_plan",
        description="追加一天的行程计划。传入天数编号、日期和活动列表。",
        phases=[5],
        parameters=_APPEND_DAY_PLAN_PARAMETERS,
        side_effect="write",
        human_label="追加一天行程",
    )
    async def append_day_plan(day: int, date: str, activities: list) -> dict:
        _validate_day(day, "day")
        _validate_day_in_range(plan, day, "day")
        _validate_unique_day_numbers(
            [existing_day.day for existing_day in plan.daily_plans] + [day],
            "daily_plans",
        )
        _validate_date_format(date)
        _validate_activities(activities)

        previous_count = len(plan.daily_plans)
        append_one_day_plan(
            plan,
            {"day": day, "date": date, "activities": activities},
        )
        conflict_info = validate_day_conflicts(plan, [day])
        return {
            "updated_field": "daily_plans",
            "action": "append",
            "day": day,
            "date": date,
            "activity_count": len(activities),
            "total_days": len(plan.daily_plans),
            "previous_days": previous_count,
            **conflict_info,
        }

    return append_day_plan


def make_replace_daily_plans_tool(plan: TravelPlanState):
    @tool(
        name="replace_daily_plans",
        description="整体替换所有逐日行程。传入完整的天数列表。",
        phases=[5],
        parameters=_REPLACE_DAILY_PLANS_PARAMETERS,
        side_effect="write",
        human_label="整体替换逐日行程",
    )
    async def replace_daily_plans(days: list) -> dict:
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

        _validate_unique_day_numbers(
            [day_payload["day"] for day_payload in days],
            "days",
        )
        previous_count = len(plan.daily_plans)
        replace_all_daily_plans(plan, days)
        replaced_days = [dp["day"] for dp in days]
        conflict_info = validate_day_conflicts(plan, replaced_days)
        return {
            "updated_field": "daily_plans",
            "action": "replace",
            "total_days": len(plan.daily_plans),
            "previous_days": previous_count,
            **conflict_info,
        }

    return replace_daily_plans
