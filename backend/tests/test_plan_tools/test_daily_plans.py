from __future__ import annotations

import pytest

from state.models import DateRange, DayPlan, TravelPlanState
from tools.base import ToolError
from tools.plan_tools.daily_plans import (
    make_replace_all_day_plans_tool,
    make_save_day_plan_tool,
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
            make_save_day_plan_tool,
            "save_day_plan",
            "保存单日行程",
            ["mode", "day", "date", "activities"],
            {"mode", "day", "date", "activities", "notes"},
        ),
        (
            make_replace_all_day_plans_tool,
            "replace_all_day_plans",
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


class TestSaveDayPlan:
    @pytest.mark.asyncio
    async def test_save_day_plan_create_adds_day(self):
        plan = _make_plan()
        plan.dates = DateRange(start="2026-05-01", end="2026-05-04")
        tool_fn = make_save_day_plan_tool(plan)

        result = await tool_fn(
            mode="create",
            day=1,
            date="2026-05-01",
            activities=[_sample_activity()],
        )

        assert result == {
            "updated_field": "daily_plans",
            "action": "create",
            "day": 1,
            "date": "2026-05-01",
            "activity_count": 1,
            "covered_days": [1],
            "missing_days": [2, 3, 4],
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
    async def test_save_day_plan_create_accepts_optional_notes(self):
        plan = _make_plan()
        tool_fn = make_save_day_plan_tool(plan)

        await tool_fn(
            mode="create",
            day=1,
            date="2026-05-01",
            notes="第一天以城市漫步和美食为主",
            activities=[_sample_activity()],
        )

        assert plan.daily_plans[0].notes == "第一天以城市漫步和美食为主"

    @pytest.mark.asyncio
    async def test_save_day_plan_create_rejects_existing_day(self):
        plan = _make_plan()
        plan.daily_plans = [
            DayPlan.from_dict({"day": 1, "date": "2026-05-01", "activities": []})
        ]
        tool_fn = make_save_day_plan_tool(plan)

        with pytest.raises(ToolError, match="day=1 already exists") as exc_info:
            await tool_fn(
                mode="create",
                day=1,
                date="2026-05-01",
                activities=[_sample_activity()],
            )

        assert exc_info.value.error_code == "DAY_ALREADY_EXISTS"
        assert 'mode="replace_existing"' in exc_info.value.suggestion

    @pytest.mark.asyncio
    async def test_save_day_plan_replace_existing_updates_only_that_day(self):
        plan = _make_plan()
        plan.daily_plans = [
            DayPlan.from_dict(
                {
                    "day": 1,
                    "date": "2026-05-01",
                    "activities": [_activity("旧A", "09:00", "10:00")],
                }
            ),
            DayPlan.from_dict(
                {
                    "day": 2,
                    "date": "2026-05-02",
                    "activities": [_activity("旧B", "09:00", "10:00")],
                }
            ),
        ]
        tool_fn = make_save_day_plan_tool(plan)

        result = await tool_fn(
            mode="replace_existing",
            day=1,
            date="2026-05-01",
            activities=[_activity("新A", "10:00", "12:00")],
        )

        assert result["action"] == "replace_existing"
        assert result["covered_days"] == [1, 2]
        assert result["missing_days"] == []
        assert [day.day for day in plan.daily_plans] == [1, 2]
        assert plan.daily_plans[0].activities[0].name == "新A"
        assert plan.daily_plans[1].activities[0].name == "旧B"

    @pytest.mark.asyncio
    async def test_save_day_plan_replace_existing_rejects_missing_day(self):
        plan = _make_plan()
        tool_fn = make_save_day_plan_tool(plan)

        with pytest.raises(ToolError, match="day=1 does not exist") as exc_info:
            await tool_fn(
                mode="replace_existing",
                day=1,
                date="2026-05-01",
                activities=[_sample_activity()],
            )

        assert exc_info.value.error_code == "DAY_NOT_FOUND"
        assert 'mode="create"' in exc_info.value.suggestion

    @pytest.mark.asyncio
    async def test_save_day_plan_rejects_non_integer_day(self):
        tool_fn = make_save_day_plan_tool(_make_plan())

        with pytest.raises(ToolError, match="day") as exc_info:
            await tool_fn(
                mode="create",
                day="1",
                date="2026-05-01",
                activities=[_sample_activity()],
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_save_day_plan_validates_date_format(self):
        tool_fn = make_save_day_plan_tool(_make_plan())

        with pytest.raises(ToolError, match="date") as exc_info:
            await tool_fn(
                mode="create", day=1, date="5月1日", activities=[_sample_activity()]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_save_day_plan_rejects_non_string_date(self):
        tool_fn = make_save_day_plan_tool(_make_plan())

        with pytest.raises(ToolError, match="date") as exc_info:
            await tool_fn(
                mode="create", day=1, date=20260501, activities=[_sample_activity()]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_save_day_plan_rejects_non_list_activities(self):
        tool_fn = make_save_day_plan_tool(_make_plan())

        with pytest.raises(ToolError, match="activities") as exc_info:
            await tool_fn(
                mode="create", day=1, date="2026-05-01", activities="not a list"
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_save_day_plan_rejects_activity_missing_required_field(self):
        tool_fn = make_save_day_plan_tool(_make_plan())
        bad_activity = _sample_activity()
        bad_activity.pop("cost")

        with pytest.raises(ToolError, match="cost") as exc_info:
            await tool_fn(
                mode="create", day=1, date="2026-05-01", activities=[bad_activity]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_save_day_plan_rejects_non_numeric_cost(self):
        tool_fn = make_save_day_plan_tool(_make_plan())
        bad_activity = _sample_activity()
        bad_activity["cost"] = "free"

        with pytest.raises(ToolError, match="cost") as exc_info:
            await tool_fn(
                mode="create", day=1, date="2026-05-01", activities=[bad_activity]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_save_day_plan_rejects_day_beyond_trip_length(self):
        plan = _make_plan()
        plan.dates = DateRange(start="2026-05-01", end="2026-05-04")
        tool_fn = make_save_day_plan_tool(plan)

        with pytest.raises(ToolError, match="day") as exc_info:
            await tool_fn(
                mode="create", day=5, date="2026-05-05", activities=[_sample_activity()]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_day", [0, -1, True])
    async def test_save_day_plan_rejects_non_positive_day(self, bad_day):
        tool_fn = make_save_day_plan_tool(_make_plan())

        with pytest.raises(ToolError, match="day") as exc_info:
            await tool_fn(
                mode="create",
                day=bad_day,
                date="2026-05-01",
                activities=[_sample_activity()],
            )

        assert exc_info.value.error_code == "INVALID_VALUE"


class TestReplaceAllDayPlans:
    @pytest.mark.asyncio
    async def test_replace_all_day_plans_success(self):
        plan = _make_plan()
        plan.dates = DateRange(start="2026-05-01", end="2026-05-02")
        plan.daily_plans = []
        tool_fn = make_replace_all_day_plans_tool(plan)
        days = [
            {"day": 1, "date": "2026-05-01", "activities": [_sample_activity()]},
            {"day": 2, "date": "2026-05-02", "activities": [_sample_activity()]},
        ]

        result = await tool_fn(days=days)

        assert result == {
            "updated_field": "daily_plans",
            "action": "replace_all",
            "total_days": 2,
            "previous_days": 0,
            "covered_days": [1, 2],
            "missing_days": [],
            "conflicts": [],
            "has_severe_conflicts": False,
        }
        assert len(plan.daily_plans) == 2
        assert [day.day for day in plan.daily_plans] == [1, 2]

    @pytest.mark.asyncio
    async def test_replace_all_day_plans_requires_complete_coverage(self):
        plan = _make_plan()
        plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
        tool_fn = make_replace_all_day_plans_tool(plan)

        with pytest.raises(ToolError, match="missing days: 2, 3") as exc_info:
            await tool_fn(
                days=[
                    {
                        "day": 1,
                        "date": "2026-05-01",
                        "activities": [_sample_activity()],
                    },
                ]
            )

        assert exc_info.value.error_code == "INCOMPLETE_DAILY_PLANS"
        assert "save_day_plan" in exc_info.value.suggestion

    @pytest.mark.asyncio
    async def test_replace_all_day_plans_rejects_non_list(self):
        tool_fn = make_replace_all_day_plans_tool(_make_plan())

        with pytest.raises(ToolError, match="days") as exc_info:
            await tool_fn(days="not a list")

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_all_day_plans_rejects_non_dict_entries(self):
        tool_fn = make_replace_all_day_plans_tool(_make_plan())

        with pytest.raises(ToolError, match=r"days\[0\]") as exc_info:
            await tool_fn(days=["not a dict"])

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_all_day_plans_rejects_missing_required_fields(self):
        tool_fn = make_replace_all_day_plans_tool(_make_plan())

        with pytest.raises(ToolError, match="day") as exc_info:
            await tool_fn(
                days=[{"date": "2026-05-01", "activities": [_sample_activity()]}]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_all_day_plans_rejects_non_integer_day(self):
        tool_fn = make_replace_all_day_plans_tool(_make_plan())

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
    async def test_replace_all_day_plans_rejects_bad_date_format(self):
        tool_fn = make_replace_all_day_plans_tool(_make_plan())

        with pytest.raises(ToolError, match="date") as exc_info:
            await tool_fn(
                days=[{"day": 1, "date": "5月1日", "activities": [_sample_activity()]}]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_all_day_plans_rejects_non_string_date(self):
        tool_fn = make_replace_all_day_plans_tool(_make_plan())

        with pytest.raises(ToolError, match="date") as exc_info:
            await tool_fn(
                days=[{"day": 1, "date": 20260501, "activities": [_sample_activity()]}]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_all_day_plans_rejects_activity_missing_required_field(self):
        tool_fn = make_replace_all_day_plans_tool(_make_plan())
        bad_activity = _sample_activity()
        bad_activity.pop("location")

        with pytest.raises(ToolError, match="location") as exc_info:
            await tool_fn(
                days=[{"day": 1, "date": "2026-05-01", "activities": [bad_activity]}]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_all_day_plans_rejects_invalid_location_shape(self):
        tool_fn = make_replace_all_day_plans_tool(_make_plan())
        bad_activity = _sample_activity()
        bad_activity["location"] = "故宫"

        with pytest.raises(ToolError, match="location") as exc_info:
            await tool_fn(
                days=[{"day": 1, "date": "2026-05-01", "activities": [bad_activity]}]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_all_day_plans_rejects_invalid_time_format(self):
        tool_fn = make_replace_all_day_plans_tool(_make_plan())
        bad_activity = _sample_activity()
        bad_activity["start_time"] = "9am"

        with pytest.raises(ToolError, match="start_time") as exc_info:
            await tool_fn(
                days=[{"day": 1, "date": "2026-05-01", "activities": [bad_activity]}]
            )

        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_replace_all_day_plans_rejects_duplicate_day_numbers(self):
        tool_fn = make_replace_all_day_plans_tool(_make_plan())

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
    async def test_replace_all_day_plans_rejects_day_beyond_trip_length(self):
        plan = _make_plan()
        plan.dates = DateRange(start="2026-05-01", end="2026-05-04")
        tool_fn = make_replace_all_day_plans_tool(plan)

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
    async def test_replace_all_day_plans_rejects_non_positive_day(self, bad_day):
        tool_fn = make_replace_all_day_plans_tool(_make_plan())

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
    async def test_save_day_plan_returns_conflicts_on_time_overlap(self):
        """save_day_plan 写入有时间冲突的活动时，返回 conflicts 字段。"""
        plan = _make_plan()
        plan.dates = DateRange(start="2026-05-01", end="2026-05-02")
        tool_fn = make_save_day_plan_tool(plan)
        result = await tool_fn(
            mode="create",
            day=1,
            date="2026-05-01",
            activities=[
                _activity("A", "09:00", "12:00"),
                _activity("B", "11:00", "13:00"),  # 冲突：12:00 > 11:00
            ],
        )
        assert result["action"] == "create"
        assert "conflicts" in result
        assert len(result["conflicts"]) > 0
        assert result["has_severe_conflicts"] is True

    @pytest.mark.asyncio
    async def test_save_day_plan_no_conflicts_when_valid(self):
        """save_day_plan 写入无冲突的活动时，conflicts 为空。"""
        plan = _make_plan()
        plan.dates = DateRange(start="2026-05-01", end="2026-05-02")
        tool_fn = make_save_day_plan_tool(plan)
        result = await tool_fn(
            mode="create",
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
    async def test_replace_all_day_plans_returns_conflicts(self):
        """replace_all_day_plans 写入后也返回冲突信息。"""
        plan = _make_plan()
        plan.dates = DateRange(start="2026-05-01", end="2026-05-01")
        tool_fn = make_replace_all_day_plans_tool(plan)
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
