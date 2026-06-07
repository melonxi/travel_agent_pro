"""
Reproduce iteration-2 bug: LLM calls save_day_plan with tips but no activities.

Verifies two layers of rejection:
1. ToolEngine pre-validation: missing required params → INVALID_ARGUMENTS
2. Tool function validation: activities not a list → INVALID_VALUE
"""

from __future__ import annotations

import pytest

from agent.types import ToolCall
from state.models import DateRange, DayPlan, TravelPlanState
from tools.base import ToolError
from tools.engine import ToolEngine
from tools.plan_tools.daily_plans import make_save_day_plan_tool


def _make_plan(phase: int = 3) -> TravelPlanState:
    plan = TravelPlanState(session_id="test-tips-vs-activities")
    plan.phase = phase
    return plan


_SAMPLE_ACTIVITY = {
    "name": "故宫博物院",
    "location": {"name": "故宫", "lat": 39.916, "lng": 116.397},
    "start_time": "09:00",
    "end_time": "12:00",
    "category": "景点",
    "cost": 60,
}


class TestSaveDayPlanMissingActivities:
    @pytest.mark.asyncio
    async def test_tool_engine_rejects_missing_activities(self):
        """ToolEngine 预校验层：save_day_plan 缺少 activities 必填参数 → INVALID_ARGUMENTS"""
        plan = _make_plan()
        plan.dates = DateRange(start="2026-07-10", end="2026-07-12")
        plan.daily_plans = [
            DayPlan.from_dict(
                {"day": 1, "date": "2026-07-10", "activities": [_SAMPLE_ACTIVITY]}
            ),
            DayPlan.from_dict(
                {"day": 2, "date": "2026-07-11", "activities": [_SAMPLE_ACTIVITY]}
            ),
            DayPlan.from_dict(
                {"day": 3, "date": "2026-07-12", "activities": [_SAMPLE_ACTIVITY]}
            ),
        ]
        tool_fn = make_save_day_plan_tool(plan)
        engine = ToolEngine()
        engine.register(tool_fn)

        call = ToolCall(
            id="tc_tips_only",
            name="save_day_plan",
            arguments={
                "mode": "replace_existing",
                "day": 1,
                "date": "2026-07-10",
                "tips": "到达日。预留充足休整时间：落地→酒店办理入住约1.5h",
            },
        )
        result = await engine.execute(call)

        assert result.status == "error"
        assert result.error_code == "INVALID_ARGUMENTS"
        assert "activities" in result.error

    @pytest.mark.asyncio
    async def test_tool_fn_rejects_non_list_activities(self):
        """函数层：activities 传字符串 → ToolError"""
        plan = _make_plan()
        plan.daily_plans = [
            DayPlan.from_dict(
                {"day": 1, "date": "2026-07-10", "activities": [_SAMPLE_ACTIVITY]}
            ),
        ]
        tool_fn = make_save_day_plan_tool(plan)

        with pytest.raises(ToolError, match="activities") as exc_info:
            await tool_fn(
                mode="replace_existing",
                day=1,
                date="2026-07-10",
                tips="到达日。预留充足休整时间",
                activities="到达日。预留充足休整时间：落地→酒店办理入住约1.5h",
            )
        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_tool_fn_accepts_empty_list_activities(self):
        """函数层：activities 传空列表 → 正常保存（空日程合理）"""
        plan = _make_plan()
        plan.dates = DateRange(start="2026-07-10", end="2026-07-12")
        plan.daily_plans = [
            DayPlan.from_dict(
                {"day": 1, "date": "2026-07-10", "activities": [_SAMPLE_ACTIVITY]}
            ),
        ]
        tool_fn = make_save_day_plan_tool(plan)

        result = await tool_fn(
            mode="replace_existing",
            day=1,
            date="2026-07-10",
            tips="到达日休整",
            activities=[],
        )
        assert result["action"] == "replace_existing"
        assert result["activity_count"] == 0

    @pytest.mark.asyncio
    async def test_parallel_calls_both_missing_activities(self):
        """复现 trace 中 iteration 2 的场景：两个并行 tool_call 都缺 activities。"""
        plan = _make_plan()
        plan.dates = DateRange(start="2026-07-10", end="2026-07-12")
        plan.daily_plans = [
            DayPlan.from_dict(
                {"day": 1, "date": "2026-07-10", "activities": [_SAMPLE_ACTIVITY]}
            ),
            DayPlan.from_dict(
                {"day": 2, "date": "2026-07-11", "activities": [_SAMPLE_ACTIVITY]}
            ),
            DayPlan.from_dict(
                {"day": 3, "date": "2026-07-12", "activities": [_SAMPLE_ACTIVITY]}
            ),
        ]
        tool_fn = make_save_day_plan_tool(plan)
        engine = ToolEngine()
        engine.register(tool_fn)

        calls = [
            ToolCall(
                id="tc_day1",
                name="save_day_plan",
                arguments={
                    "mode": "replace_existing",
                    "day": 1,
                    "date": "2026-07-10",
                    "tips": "到达日。预留充足休整时间：落地→酒店办理入住约1.5h",
                },
            ),
            ToolCall(
                id="tc_day3",
                name="save_day_plan",
                arguments={
                    "mode": "replace_existing",
                    "day": 3,
                    "date": "2026-07-12",
                    "tips": "离开日。精简至4个核心活动，节奏轻松",
                },
            ),
        ]

        results = await engine.execute_batch(calls)
        for result in results:
            assert result.status == "error"
            assert result.error_code == "INVALID_ARGUMENTS"
            assert "activities" in result.error