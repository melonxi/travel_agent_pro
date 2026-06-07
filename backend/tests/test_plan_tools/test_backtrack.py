from __future__ import annotations

import pytest

from state.models import Accommodation, DateRange, DayPlan, TravelPlanState
from tools.base import ToolError
from tools.plan_tools.backtrack import make_request_backtrack_tool


def _make_plan(phase: int = 4) -> TravelPlanState:
    plan = TravelPlanState(
        session_id="test-backtrack",
        phase=phase,
        destination="Tokyo",
        dates=DateRange(start="2025-08-01", end="2025-08-05"),
        phase2_step="lock",
        trip_brief={"goal": "城市漫游"},
        candidate_pool=[{"name": "浅草寺"}],
        shortlist=[{"name": "浅草寺"}],
        skeleton_plans=[{"id": "balanced"}],
        selected_skeleton_id="balanced",
        accommodation=Accommodation(area="Shinjuku", hotel="Hotel A"),
        daily_plans=[
            DayPlan(day=1, date="2025-08-01"),
            DayPlan(day=2, date="2025-08-02"),
        ],
    )
    return plan


def test_request_backtrack_tool_metadata():
    tool_fn = make_request_backtrack_tool(_make_plan())

    assert tool_fn.name == "request_backtrack"
    assert (
        tool_fn.description
        == "请求回退到更早的规划阶段。当用户想推翻之前的阶段决策时使用。目标阶段必须小于当前阶段。"
    )
    assert tool_fn.phases == [2, 3, 4]
    assert tool_fn.side_effect == "write"
    assert tool_fn.human_label == "请求回退阶段"
    assert tool_fn.parameters == {
        "type": "object",
        "properties": {
            "to_phase": {
                "type": "integer",
                "description": "要回退到的目标阶段（必须小于当前阶段）",
            },
            "reason": {
                "type": "string",
                "description": "回退原因",
            },
        },
        "required": ["to_phase", "reason"],
    }


@pytest.mark.asyncio
async def test_request_backtrack_success_clears_downstream_and_records_history():
    plan = _make_plan(phase=3)
    tool_fn = make_request_backtrack_tool(plan)

    result = await tool_fn(to_phase=2, reason="用户想换日期")

    assert result == {
        "backtracked": True,
        "from_phase": 3,
        "to_phase": 2,
        "reason": "用户想换日期",
        "next_action": (
            "已回退并重建上下文。若用户本轮已明确授权修复，请继续在目标阶段调用工具完成修复；"
            "若只是提出修改意向，再向用户确认。"
        ),
    }
    assert plan.phase == 2
    assert len(plan.backtrack_history) == 1
    event = plan.backtrack_history[0]
    assert event.from_phase == 3
    assert event.to_phase == 2
    assert event.reason == "用户想换日期"
    assert plan.dates is None
    assert plan.phase2_step == "brief"
    assert plan.trip_brief == {}
    assert plan.candidate_pool == []
    assert plan.shortlist == []
    assert plan.skeleton_plans == []
    assert plan.selected_skeleton_id is None
    assert plan.accommodation is None
    assert plan.daily_plans == []
    assert plan.destination == "Tokyo"


@pytest.mark.asyncio
async def test_request_backtrack_rejects_same_phase():
    plan = _make_plan(phase=2)
    tool_fn = make_request_backtrack_tool(plan)

    with pytest.raises(ToolError, match="只能回退到更早的阶段"):
        await tool_fn(to_phase=2, reason="重新选目的地")


@pytest.mark.asyncio
@pytest.mark.parametrize("to_phase", [4, 2])
async def test_request_backtrack_rejects_non_backward_targets(to_phase: int):
    plan = _make_plan(phase=2)
    tool_fn = make_request_backtrack_tool(plan)

    with pytest.raises(ToolError, match="只能回退到更早的阶段") as exc_info:
        await tool_fn(to_phase=to_phase, reason="test")

    assert exc_info.value.error_code == "INVALID_BACKTRACK"
    assert "目标阶段必须小于当前阶段" in exc_info.value.suggestion


@pytest.mark.asyncio
async def test_request_backtrack_rejects_non_int_phase():
    tool_fn = make_request_backtrack_tool(_make_plan())

    with pytest.raises(ToolError, match="to_phase") as exc_info:
        await tool_fn(to_phase="3", reason="test")

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_request_backtrack_rejects_blank_reason():
    tool_fn = make_request_backtrack_tool(_make_plan())

    with pytest.raises(ToolError, match="reason") as exc_info:
        await tool_fn(to_phase=2, reason="   ")

    assert exc_info.value.error_code == "INVALID_VALUE"
