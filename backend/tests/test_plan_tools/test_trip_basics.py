from __future__ import annotations

import pytest

from state.models import TravelPlanState
from tools.base import ToolError
from tools.plan_tools.trip_basics import make_update_trip_basics_tool


def _make_plan(phase: int = 1) -> TravelPlanState:
    plan = TravelPlanState(session_id='test-trip-basics')
    plan.phase = phase
    return plan


@pytest.mark.parametrize(
    (
        'field_name',
        'expected_any_of_types',
    ),
    [
        ('destination', {'string', 'object'}),
        ('dates', {'string', 'object'}),
        ('travelers', {'string', 'object', 'integer'}),
        ('budget', {'string', 'object', 'number'}),
        ('departure_city', {'string', 'object'}),
    ],
)
def test_trip_basics_tool_metadata(field_name, expected_any_of_types):
    tool_fn = make_update_trip_basics_tool(_make_plan())

    assert tool_fn.name == 'update_trip_basics'
    assert tool_fn.side_effect == 'write'
    assert tool_fn.human_label == '更新行程基础信息'
    assert tool_fn.phases == [1, 3]
    assert '支持结构化输入和自然语言短语' in tool_fn.description
    assert tool_fn.parameters['type'] == 'object'
    assert set(tool_fn.parameters['properties']) == {
        'destination',
        'dates',
        'travelers',
        'budget',
        'departure_city',
    }
    assert {
        option['type'] for option in tool_fn.parameters['properties'][field_name]['anyOf']
    } == expected_any_of_types


class TestUpdateTripBasics:
    @pytest.mark.asyncio
    async def test_updates_multiple_fields_in_one_call(self):
        plan = _make_plan()
        tool_fn = make_update_trip_basics_tool(plan)

        result = await tool_fn(
            destination={'name': '大阪'},
            dates={'start': '2026-05-01', 'end': '2026-05-05'},
            travelers={'adults': 2, 'children': 1},
            budget=20000,
            departure_city={'city': '上海'},
        )

        assert result == {
            'updated_fields': [
                'destination',
                'dates',
                'travelers',
                'budget',
                'departure_city',
            ],
            'count': 5,
        }
        assert plan.destination == '大阪'
        assert plan.dates is not None
        assert plan.dates.start == '2026-05-01'
        assert plan.dates.end == '2026-05-05'
        assert plan.travelers is not None
        assert plan.travelers.adults == 2
        assert plan.travelers.children == 1
        assert plan.budget is not None
        assert plan.budget.total == 20000.0
        assert plan.trip_brief['departure_city'] == '上海'

    @pytest.mark.asyncio
    async def test_accepts_phrase_inputs_for_supported_fields(self):
        plan = _make_plan()
        tool_fn = make_update_trip_basics_tool(plan)

        result = await tool_fn(
            destination='东京',
            dates='2026-05-01 到 2026-05-05',
            travelers='2人',
            budget='1.5万',
            departure_city='杭州',
        )

        assert result['updated_fields'] == [
            'destination',
            'dates',
            'travelers',
            'budget',
            'departure_city',
        ]
        assert plan.destination == '东京'
        assert plan.dates is not None
        assert plan.dates.start == '2026-05-01'
        assert plan.dates.end == '2026-05-05'
        assert plan.travelers is not None
        assert plan.travelers.adults == 2
        assert plan.budget is not None
        assert plan.budget.total == 15000.0
        assert plan.trip_brief['departure_city'] == '杭州'

    @pytest.mark.asyncio
    async def test_rejects_when_no_fields_are_provided(self):
        tool_fn = make_update_trip_basics_tool(_make_plan())

        with pytest.raises(ToolError, match='至少需要提供一个字段') as exc_info:
            await tool_fn()

        assert exc_info.value.error_code == 'INVALID_VALUE'
        assert 'destination' in exc_info.value.suggestion

    @pytest.mark.asyncio
    async def test_rejects_unparseable_dates(self):
        tool_fn = make_update_trip_basics_tool(_make_plan())

        with pytest.raises(ToolError, match='无法解析日期') as exc_info:
            await tool_fn(dates='下次找时间再说')

        assert exc_info.value.error_code == 'INVALID_VALUE'
        assert 'start' in exc_info.value.suggestion

    @pytest.mark.asyncio
    async def test_rejects_invalid_structured_dates(self):
        tool_fn = make_update_trip_basics_tool(_make_plan())

        with pytest.raises(ToolError, match='无法解析日期') as exc_info:
            await tool_fn(dates={'start': 'foo', 'end': 'bar'})

        assert exc_info.value.error_code == 'INVALID_VALUE'

    @pytest.mark.asyncio
    async def test_rejects_calendar_invalid_date_phrase(self):
        tool_fn = make_update_trip_basics_tool(_make_plan())

        with pytest.raises(ToolError, match='无法解析日期') as exc_info:
            await tool_fn(dates='2026-02-30 到 2026-03-02')

        assert exc_info.value.error_code == 'INVALID_VALUE'

    @pytest.mark.asyncio
    async def test_failed_multi_field_update_is_atomic(self):
        plan = _make_plan()
        tool_fn = make_update_trip_basics_tool(plan)

        with pytest.raises(ToolError, match='无法解析日期') as exc_info:
            await tool_fn(destination='东京', dates='下次找时间再说')

        assert exc_info.value.error_code == 'INVALID_VALUE'
        assert plan.destination is None
        assert plan.dates is None

    @pytest.mark.asyncio
    async def test_rejects_invalid_structured_travelers(self):
        tool_fn = make_update_trip_basics_tool(_make_plan())

        with pytest.raises(ToolError, match='无法解析人数') as exc_info:
            await tool_fn(travelers={'foo': 1})

        assert exc_info.value.error_code == 'INVALID_VALUE'

    @pytest.mark.asyncio
    async def test_rejects_structured_travelers_with_non_numeric_adults(self):
        tool_fn = make_update_trip_basics_tool(_make_plan())

        with pytest.raises(ToolError, match='无法解析人数') as exc_info:
            await tool_fn(travelers={'adults': 'many'})

        assert exc_info.value.error_code == 'INVALID_VALUE'

    @pytest.mark.asyncio
    async def test_rejects_unparseable_travelers(self):
        tool_fn = make_update_trip_basics_tool(_make_plan())

        with pytest.raises(ToolError, match='无法解析人数') as exc_info:
            await tool_fn(travelers='很多人')

        assert exc_info.value.error_code == 'INVALID_VALUE'
        assert 'adults' in exc_info.value.suggestion

    @pytest.mark.asyncio
    async def test_rejects_invalid_structured_budget(self):
        tool_fn = make_update_trip_basics_tool(_make_plan())

        with pytest.raises(ToolError, match='无法解析预算') as exc_info:
            await tool_fn(budget={'foo': 1})

        assert exc_info.value.error_code == 'INVALID_VALUE'

    @pytest.mark.asyncio
    async def test_rejects_structured_budget_with_non_numeric_total(self):
        tool_fn = make_update_trip_basics_tool(_make_plan())

        with pytest.raises(ToolError, match='无法解析预算') as exc_info:
            await tool_fn(budget={'total': 'expensive'})

        assert exc_info.value.error_code == 'INVALID_VALUE'

    @pytest.mark.asyncio
    async def test_rejects_unparseable_budget(self):
        tool_fn = make_update_trip_basics_tool(_make_plan())

        with pytest.raises(ToolError, match='无法解析预算') as exc_info:
            await tool_fn(budget='丰俭由人')

        assert exc_info.value.error_code == 'INVALID_VALUE'
        assert 'total' in exc_info.value.suggestion
