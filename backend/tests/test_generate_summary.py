# backend/tests/test_generate_summary.py
import pytest

from state.models import TravelPlanState
from tools.base import ToolError
from tools.generate_summary import make_generate_summary_tool


@pytest.fixture
def plan():
    return TravelPlanState(session_id="sess_123456789abc", phase=7)


@pytest.fixture
def tool_fn(plan):
    return make_generate_summary_tool(plan)


@pytest.mark.asyncio
async def test_generate_summary_returns_dual_markdown(tool_fn):
    result = await tool_fn(
        plan_data={"destination": "东京"},
        travel_plan_markdown="# 东京 5 日旅行计划\n\n## 第 1 天\n- 浅草寺\n",
        checklist_markdown="# 东京出发前清单\n\n- [ ] 护照\n",
    )

    assert "travel_plan_markdown" in result
    assert "checklist_markdown" in result
    assert result["summary"].startswith("已生成并冻结")


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
            travel_plan_markdown="# 东京\n\n## 第 1 天\n- 浅草寺\n",
            checklist_markdown="# 清单\n\n- [ ] 护照\n",
        )


@pytest.mark.asyncio
async def test_generate_summary_rejects_invalid_markdown_structure(tool_fn):
    with pytest.raises(ToolError, match="travel_plan_markdown"):
        await tool_fn(
            plan_data={"destination": "东京"},
            travel_plan_markdown="东京自由行",
            checklist_markdown="# 清单\n\n- [ ] 护照\n",
        )
