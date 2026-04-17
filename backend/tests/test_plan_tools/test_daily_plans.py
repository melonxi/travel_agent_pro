from __future__ import annotations

import pytest

from state.models import DateRange, DayPlan, TravelPlanState
from tools.base import ToolError
from tools.plan_tools.daily_plans import (
    make_append_day_plan_tool,
    make_replace_daily_plans_tool,
)


def _make_plan(phase: int = 5) -> TravelPlanState:
    plan = TravelPlanState(session_id="test-daily")
    plan.phase = phase
    return plan


def _sample_activity() -> dict:
    return {
        "name": "故宫博物院",
        "location": {"name": "故宫", "lat": 39.916, "lng": 116.397},
        "start_time": "09:00",
        "end_time": "12:00",
        "category": "景点",
        "cost": 60,
    }


def _activity(name: str, start: str, end: str, cost: float = 0) -> dict:
    return {
        "name": name,
        "location": {"name": name, "lat": 30.0, "lng": 104.0},
        "start_time": start,
        "end_time": end,
        "category": "activity",
        "cost": cost,
    }


@pytest.mark.parametrize(
    (
        "factory",
        "expected_name",
        "expected_label",
        "expected_required",
        "expected_properties",
    ),
    [
        (
            make_append_day_plan_tool,
            "append_day_plan",
            "追加一天行程",
            ["day", "date", "activities"],
            {"day", "date", "activities"},
        ),
        (
            make_replace_daily_plans_tool,
            "replace_daily_plans",
            "整体替换逐日行程",
            ["days"],
            {"days"},
        ),
    ],
)
def test_daily_plan_tool_metadata(
    factory,
    expected_name,
    expected_label,
    expected_required,
    expected_properties,
):
    tool_fn = factory(_make_plan())

    assert tool_fn.name == expected_name
    assert tool_fn.side_effect == "write"
    assert tool_fn.human_label == expected_label
    assert tool_fn.phases == [5]
    assert tool_fn.parameters["type"] == "object"
    assert tool_fn.parameters["required"] == expected_required
    assert set(tool_fn.parameters["properties"]) == expected_properties


class TestAppendDayPlan:
    @pytest.mark.asyncio
    async def test_append_day_plan_success(self):
        plan = _make_plan()
        tool_fn = make_append_day_plan_tool(plan)

        result = await tool_fn(
            day=1, date="2026-05-01", activities=[_sample_activity()]
        )

        assert result == {
            "updated_field": "daily_plans",
            "action": "append",
            "day": 1,
            "date": "2026-05-01",
            "activity_count": 1,
            "total_days": 1,
            "previous_days": 0,
            "conflicts": [],
            "has_severe_conflicts": False,
        }
        assert len(plan.daily_plans) == 1
        assert plan.daily_plans[0].day == 1
        assert plan.daily_plans[0].date == "2026-05-01"
        assert len(plan.daily_plans[0].activities) == 1

    @pytest.mark.asyncio
    async def test_append_day_plan_rejects_non_integer_day(self):
        tool_fn = make_append_day_plan_tool(_make_plan())

        with pytest.raises(ToolError, match="day") as exc_info:
            await tool_fn(day="1", date="2026-05-01", activities=[_sample_activity()])

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_append_day_plan_validates_date_format(self):
        tool_fn = make_append_day_plan_tool(_make_plan())

        with pytest.raises(ToolError, match="date") as exc_info:
            await tool_fn(day=1, date="5月1日", activities=[_sample_activity()])

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_append_day_plan_rejects_non_string_date(self):
        tool_fn = make_append_day_plan_tool(_make_plan())

        with pytest.raises(ToolError, match="date") as exc_info:
            await tool_fn(day=1, date=20260501, activities=[_sample_activity()])

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_append_day_plan_rejects_non_list_activities(self):
        tool_fn = make_append_day_plan_tool(_make_plan())

        with pytest.raises(ToolError, match="activities") as exc_info:
            await tool_fn(day=1, date="2026-05-01", activities="not a list")

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_append_day_plan_rejects_activity_missing_required_field(self):
        tool_fn = make_append_day_plan_tool(_make_plan())
        bad_activity = _sample_activity()
        bad_activity.pop("cost")

        with pytest.raises(ToolError, match="cost") as exc_info:
            await tool_fn(day=1, date="2026-05-01", activities=[bad_activity])

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_append_day_plan_rejects_non_numeric_cost(self):
        tool_fn = make_append_day_plan_tool(_make_plan())
        bad_activity = _sample_activity()
        bad_activity["cost"] = "free"

        with pytest.raises(ToolError, match="cost") as exc_info:
            await tool_fn(day=1, date="2026-05-01", activities=[bad_activity])

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_append_day_plan_rejects_duplicate_day_number(self):
        plan = _make_plan()
        plan.daily_plans = [
            DayPlan.from_dict({"day": 1, "date": "2026-05-01", "activities": []})
        ]
        tool_fn = make_append_day_plan_tool(plan)

        with pytest.raises(ToolError, match="day") as exc_info:
            await tool_fn(day=1, date="2026-05-01", activities=[_sample_activity()])

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_append_day_plan_rejects_day_beyond_trip_length(self):
        plan = _make_plan()
        plan.dates = DateRange(start="2026-05-01", end="2026-05-04")
        tool_fn = make_append_day_plan_tool(plan)

        with pytest.raises(ToolError, match="day") as exc_info:
            await tool_fn(day=5, date="2026-05-05", activities=[_sample_activity()])

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_day", [0, -1, True])
    async def test_append_day_plan_rejects_non_positive_day(self, bad_day):
        tool_fn = make_append_day_plan_tool(_make_plan())

        with pytest.raises(ToolError, match="day") as exc_info:
            await tool_fn(
                day=bad_day, date="2026-05-01", activities=[_sample_activity()]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"


class TestReplaceDailyPlans:
    @pytest.mark.asyncio
    async def test_replace_daily_plans_success(self):
        plan = _make_plan()
        plan.daily_plans = []
        tool_fn = make_replace_daily_plans_tool(plan)
        days = [
            {"day": 1, "date": "2026-05-01", "activities": [_sample_activity()]},
            {"day": 2, "date": "2026-05-02", "activities": [_sample_activity()]},
        ]

        result = await tool_fn(days=days)

        assert result == {
            "updated_field": "daily_plans",
            "action": "replace",
            "total_days": 2,
            "previous_days": 0,
            "conflicts": [],
            "has_severe_conflicts": False,
        }
        assert len(plan.daily_plans) == 2
        assert [day.day for day in plan.daily_plans] == [1, 2]

    @pytest.mark.asyncio
    async def test_replace_daily_plans_rejects_non_list(self):
        tool_fn = make_replace_daily_plans_tool(_make_plan())

        with pytest.raises(ToolError, match="days") as exc_info:
            await tool_fn(days="not a list")

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_daily_plans_rejects_non_dict_entries(self):
        tool_fn = make_replace_daily_plans_tool(_make_plan())

        with pytest.raises(ToolError, match=r"days\[0\]") as exc_info:
            await tool_fn(days=["not a dict"])

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_daily_plans_rejects_missing_required_fields(self):
        tool_fn = make_replace_daily_plans_tool(_make_plan())

        with pytest.raises(ToolError, match="day") as exc_info:
            await tool_fn(
                days=[{"date": "2026-05-01", "activities": [_sample_activity()]}]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_daily_plans_rejects_non_integer_day(self):
        tool_fn = make_replace_daily_plans_tool(_make_plan())

        with pytest.raises(ToolError, match="day") as exc_info:
            await tool_fn(
                days=[
                    {
                        "day": "1",
                        "date": "2026-05-01",
                        "activities": [_sample_activity()],
                    }
                ]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_daily_plans_rejects_bad_date_format(self):
        tool_fn = make_replace_daily_plans_tool(_make_plan())

        with pytest.raises(ToolError, match="date") as exc_info:
            await tool_fn(
                days=[{"day": 1, "date": "5月1日", "activities": [_sample_activity()]}]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_daily_plans_rejects_non_string_date(self):
        tool_fn = make_replace_daily_plans_tool(_make_plan())

        with pytest.raises(ToolError, match="date") as exc_info:
            await tool_fn(
                days=[{"day": 1, "date": 20260501, "activities": [_sample_activity()]}]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_daily_plans_rejects_activity_missing_required_field(self):
        tool_fn = make_replace_daily_plans_tool(_make_plan())
        bad_activity = _sample_activity()
        bad_activity.pop("location")

        with pytest.raises(ToolError, match="location") as exc_info:
            await tool_fn(
                days=[{"day": 1, "date": "2026-05-01", "activities": [bad_activity]}]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_daily_plans_rejects_invalid_location_shape(self):
        tool_fn = make_replace_daily_plans_tool(_make_plan())
        bad_activity = _sample_activity()
        bad_activity["location"] = "故宫"

        with pytest.raises(ToolError, match="location") as exc_info:
            await tool_fn(
                days=[{"day": 1, "date": "2026-05-01", "activities": [bad_activity]}]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_daily_plans_rejects_invalid_time_format(self):
        tool_fn = make_replace_daily_plans_tool(_make_plan())
        bad_activity = _sample_activity()
        bad_activity["start_time"] = "9am"

        with pytest.raises(ToolError, match="start_time") as exc_info:
            await tool_fn(
                days=[{"day": 1, "date": "2026-05-01", "activities": [bad_activity]}]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_daily_plans_rejects_duplicate_day_numbers(self):
        tool_fn = make_replace_daily_plans_tool(_make_plan())

        with pytest.raises(ToolError, match="day") as exc_info:
            await tool_fn(
                days=[
                    {
                        "day": 1,
                        "date": "2026-05-01",
                        "activities": [_sample_activity()],
                    },
                    {
                        "day": 1,
                        "date": "2026-05-02",
                        "activities": [_sample_activity()],
                    },
                ]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_daily_plans_rejects_day_beyond_trip_length(self):
        plan = _make_plan()
        plan.dates = DateRange(start="2026-05-01", end="2026-05-04")
        tool_fn = make_replace_daily_plans_tool(plan)

        with pytest.raises(ToolError, match="day") as exc_info:
            await tool_fn(
                days=[
                    {
                        "day": 1,
                        "date": "2026-05-01",
                        "activities": [_sample_activity()],
                    },
                    {
                        "day": 2,
                        "date": "2026-05-02",
                        "activities": [_sample_activity()],
                    },
                    {
                        "day": 5,
                        "date": "2026-05-04",
                        "activities": [_sample_activity()],
                    },
                ]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_day", [0, -1, True])
    async def test_replace_daily_plans_rejects_non_positive_day(self, bad_day):
        tool_fn = make_replace_daily_plans_tool(_make_plan())

        with pytest.raises(ToolError, match="day") as exc_info:
            await tool_fn(
                days=[
                    {
                        "day": bad_day,
                        "date": "2026-05-01",
                        "activities": [_sample_activity()],
                    }
                ]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"


class TestConflictDetection:
    @pytest.mark.asyncio
    async def test_append_day_plan_returns_conflicts_on_time_overlap(self):
        """append_day_plan 写入有时间冲突的活动时，返回 conflicts 字段。"""
        plan = _make_plan()
        plan.dates = DateRange(start="2026-05-01", end="2026-05-02")
        tool_fn = make_append_day_plan_tool(plan)
        result = await tool_fn(
            day=1,
            date="2026-05-01",
            activities=[
                _activity("A", "09:00", "12:00"),
                _activity("B", "11:00", "13:00"),  # 冲突：12:00 > 11:00
            ],
        )
        assert result["action"] == "append"
        assert "conflicts" in result
        assert len(result["conflicts"]) > 0
        assert result["has_severe_conflicts"] is True

    @pytest.mark.asyncio
    async def test_append_day_plan_no_conflicts_when_valid(self):
        """append_day_plan 写入无冲突的活动时，conflicts 为空。"""
        plan = _make_plan()
        plan.dates = DateRange(start="2026-05-01", end="2026-05-02")
        tool_fn = make_append_day_plan_tool(plan)
        result = await tool_fn(
            day=1,
            date="2026-05-01",
            activities=[
                _activity("A", "09:00", "10:00"),
                _activity("B", "11:00", "12:00"),
            ],
        )
        assert result["conflicts"] == []
        assert result["has_severe_conflicts"] is False

    @pytest.mark.asyncio
    async def test_replace_daily_plans_returns_conflicts(self):
        """replace_daily_plans 写入后也返回冲突信息。"""
        plan = _make_plan()
        plan.dates = DateRange(start="2026-05-01", end="2026-05-02")
        tool_fn = make_replace_daily_plans_tool(plan)
        result = await tool_fn(
            days=[
                {
                    "day": 1,
                    "date": "2026-05-01",
                    "activities": [
                        _activity("A", "09:00", "12:00"),
                        _activity("B", "11:00", "13:00"),  # 冲突
                    ],
                },
            ],
        )
        assert "conflicts" in result
        assert len(result["conflicts"]) > 0
        assert result["has_severe_conflicts"] is True
