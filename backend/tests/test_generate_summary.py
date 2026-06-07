# backend/tests/test_generate_summary.py
import pytest

from state.models import TravelPlanState
from tools.base import ToolError
from tools.generate_summary import make_generate_summary_tool


@pytest.fixture
def plan():
    return TravelPlanState(session_id="sess_123456789abc", phase=4)


@pytest.fixture
def tool_fn(plan):
    return make_generate_summary_tool(plan)


def _valid_daily_sections():
    return [{"day": 1, "date": "2025-07-10", "title": "浅草寺", "content": "- 上午浅草寺\n- 下午秋叶原"}]


def _valid_checklist_categories():
    return [
        {"category": "证件与文件", "items": ["护照有效期确认", "签证办理"]},
        {"category": "财务准备", "items": ["日元兑换"]},
    ]


@pytest.mark.asyncio
async def test_generate_summary_returns_dual_markdown(tool_fn):
    result = await tool_fn(
        plan_data={"destination": "东京"},
        title="东京5日旅行计划",
        overview="轻松的东京之旅",
        daily_sections=_valid_daily_sections(),
        checklist_title="东京出发前清单",
        checklist_categories=_valid_checklist_categories(),
    )

    assert "travel_plan_markdown" in result
    assert "checklist_markdown" in result
    assert result["summary"].startswith("已生成并冻结")
    assert "# 东京5日旅行计划" in result["travel_plan_markdown"]
    assert "## 第 1 天" in result["travel_plan_markdown"]
    assert "# 东京出发前清单" in result["checklist_markdown"]


@pytest.mark.asyncio
async def test_generate_summary_rejects_frozen_deliverables(plan):
    plan.deliverables = {
        "travel_plan_md": "travel_plan.md",
        "checklist_md": "checklist.md",
        "generated_at": "2026-04-18T22:30:00+08:00",
    }
    tool_fn = make_generate_summary_tool(plan)

    with pytest.raises(ToolError, match="已冻结"):
        await tool_fn(
            plan_data={"destination": "东京"},
            title="东京5日旅行计划",
            daily_sections=_valid_daily_sections(),
            checklist_title="清单",
            checklist_categories=_valid_checklist_categories(),
        )


@pytest.mark.asyncio
async def test_generate_summary_rejects_empty_title(tool_fn):
    with pytest.raises(ToolError, match="title"):
        await tool_fn(
            plan_data={"destination": "东京"},
            title="",
            daily_sections=_valid_daily_sections(),
            checklist_title="清单",
            checklist_categories=_valid_checklist_categories(),
        )


@pytest.mark.asyncio
async def test_generate_summary_rejects_empty_daily_sections(tool_fn):
    with pytest.raises(ToolError, match="daily_sections"):
        await tool_fn(
            plan_data={"destination": "东京"},
            title="东京5日旅行计划",
            daily_sections=[],
            checklist_title="清单",
            checklist_categories=_valid_checklist_categories(),
        )


@pytest.mark.asyncio
async def test_generate_summary_rejects_empty_checklist_title(tool_fn):
    with pytest.raises(ToolError, match="checklist_title"):
        await tool_fn(
            plan_data={"destination": "东京"},
            title="东京5日旅行计划",
            daily_sections=_valid_daily_sections(),
            checklist_title="",
            checklist_categories=_valid_checklist_categories(),
        )


@pytest.mark.asyncio
async def test_generate_summary_auto_injects_estimation_marker(plan):
    plan.daily_plans = [
        type("DayPlan", (), {
            "day": 1,
            "date": "2025-07-10",
            "activities": [
                type("Activity", (), {
                    "summary": "浅草寺→秋叶原",
                    "transport_estimated": True,
                })(),
            ],
        })(),
    ]
    tool_fn = make_generate_summary_tool(plan)
    result = await tool_fn(
        plan_data={"destination": "东京"},
        title="东京5日旅行计划",
        daily_sections=[
            {"day": 1, "date": "2025-07-10", "title": "浅草寺", "content": "- 浅草寺→秋叶原\n- 浅草寺参观"},
        ],
        checklist_title="东京出发前清单",
        checklist_categories=_valid_checklist_categories(),
    )

    assert "⚠️" in result["travel_plan_markdown"]