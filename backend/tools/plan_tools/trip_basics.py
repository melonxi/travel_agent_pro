from __future__ import annotations

from datetime import date as dt_date
from numbers import Real

from state.intake import parse_budget_value, parse_dates_value, parse_travelers_value
from state.models import TravelPlanState
from state.plan_writers import (
    write_budget,
    write_dates,
    write_departure_city,
    write_destination,
    write_travelers,
)
from tools.base import ToolError, tool

_PARAMETERS = {
    'type': 'object',
    'properties': {
        'destination': {
            'description': '目的地名称',
            'anyOf': [{'type': 'string'}, {'type': 'object'}],
        },
        'dates': {
            'description': '出行日期。可传 {start, end} 或可解析短语',
            'anyOf': [{'type': 'string'}, {'type': 'object'}],
        },
        'travelers': {
            'description': '同行人数。可传 {adults, children?}、短语或整数',
            'anyOf': [{'type': 'string'}, {'type': 'object'}, {'type': 'integer'}],
        },
        'budget': {
            'description': '预算。可传 {total, currency?}、短语或数字',
            'anyOf': [{'type': 'string'}, {'type': 'object'}, {'type': 'number'}],
        },
        'departure_city': {
            'description': '出发城市。可传字符串或含 city/name 的对象',
            'anyOf': [{'type': 'string'}, {'type': 'object'}],
        },
    },
}


def _validated_dates_or_error(value: str | dict) -> None:
    try:
        parsed = parse_dates_value(value)
    except (KeyError, TypeError, ValueError):
        parsed = None
    if parsed is None:
        raise ToolError(
            f'无法解析日期: {value!r}',
            error_code='INVALID_VALUE',
            suggestion='请传入 {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} 或可解析短语',
        )
    try:
        dt_date.fromisoformat(parsed.start)
        dt_date.fromisoformat(parsed.end)
    except (TypeError, ValueError):
        raise ToolError(
            f'无法解析日期: {value!r}',
            error_code='INVALID_VALUE',
            suggestion='请传入 {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} 或可解析短语',
        )


def _validated_travelers_or_error(value: str | dict | int) -> None:
    if isinstance(value, dict) and 'adults' not in value:
        raise ToolError(
            f'无法解析人数: {value!r}',
            error_code='INVALID_VALUE',
            suggestion='请传入 {"adults": 2} 或 "2人" 等可解析格式',
        )
    try:
        parsed = parse_travelers_value(value)
    except (KeyError, TypeError, ValueError):
        parsed = None
    if parsed is None:
        raise ToolError(
            f'无法解析人数: {value!r}',
            error_code='INVALID_VALUE',
            suggestion='请传入 {"adults": 2} 或 "2人" 等可解析格式',
        )
    if (
        not isinstance(parsed.adults, int)
        or isinstance(parsed.adults, bool)
        or parsed.adults < 1
        or not isinstance(parsed.children, int)
        or isinstance(parsed.children, bool)
        or parsed.children < 0
    ):
        raise ToolError(
            f'无法解析人数: {value!r}',
            error_code='INVALID_VALUE',
            suggestion='请传入 {"adults": 2} 或 "2人" 等可解析格式',
        )


def _validated_budget_or_error(value: str | dict | float | int) -> None:
    try:
        parsed = parse_budget_value(value)
    except (KeyError, TypeError, ValueError):
        parsed = None
    if parsed is None:
        raise ToolError(
            f'无法解析预算: {value!r}',
            error_code='INVALID_VALUE',
            suggestion='请传入 {"total": 10000} 或 "1万" 或数字',
        )
    if not isinstance(parsed.total, Real) or isinstance(parsed.total, bool):
        raise ToolError(
            f'无法解析预算: {value!r}',
            error_code='INVALID_VALUE',
            suggestion='请传入 {"total": 10000} 或 "1万" 或数字',
        )


def make_update_trip_basics_tool(plan: TravelPlanState):
    @tool(
        name='update_trip_basics',
        description=(
            '更新行程基础信息（目的地、日期、人数、预算、出发城市）。'
            '每个字段均可选，只传需要更新的字段。'
            '支持结构化输入和自然语言短语。'
        ),
        phases=[1, 3],
        parameters=_PARAMETERS,
        side_effect='write',
        human_label='更新行程基础信息',
    )
    async def update_trip_basics(
        destination: str | dict | None = None,
        dates: str | dict | None = None,
        travelers: str | dict | int | None = None,
        budget: str | dict | float | int | None = None,
        departure_city: str | dict | None = None,
    ) -> dict:
        if all(
            value is None
            for value in (destination, dates, travelers, budget, departure_city)
        ):
            raise ToolError(
                '至少需要提供一个字段进行更新',
                error_code='INVALID_VALUE',
                suggestion='可更新字段: destination, dates, travelers, budget, departure_city',
            )

        if dates is not None:
            _validated_dates_or_error(dates)

        if travelers is not None:
            _validated_travelers_or_error(travelers)

        if budget is not None:
            _validated_budget_or_error(budget)

        updated_fields: list[str] = []

        if destination is not None:
            write_destination(plan, destination)
            updated_fields.append('destination')

        if dates is not None:
            write_dates(plan, dates)
            updated_fields.append('dates')

        if travelers is not None:
            write_travelers(plan, travelers)
            updated_fields.append('travelers')

        if budget is not None:
            write_budget(plan, budget)
            updated_fields.append('budget')

        if departure_city is not None:
            write_departure_city(plan, departure_city)
            updated_fields.append('departure_city')

        return {
            'updated_fields': updated_fields,
            'count': len(updated_fields),
        }

    return update_trip_basics
