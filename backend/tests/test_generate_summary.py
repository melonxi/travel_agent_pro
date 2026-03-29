# backend/tests/test_generate_summary.py
import pytest

from tools.generate_summary import make_generate_summary_tool


@pytest.fixture
def tool_fn():
    return make_generate_summary_tool()


@pytest.mark.asyncio
async def test_generate_summary(tool_fn):
    plan_data = {
        "destination": "东京",
        "days": [
            {"activities": [{"name": "浅草寺"}, {"name": "天空树"}]},
            {"activities": [{"name": "富士山一日游"}]},
            {"activities": [{"name": "秋叶原"}, {"name": "银座"}]},
        ],
        "budget": {
            "flights": 3000,
            "hotels": 4500,
            "activities": 2000,
            "food": 1500,
        },
    }
    result = await tool_fn(plan_data=plan_data)
    assert result["total_days"] == 3
    assert result["total_budget"] == 11000
    assert "东京" in result["summary"]
    assert "第1天" in result["summary"]
    assert "浅草寺" in result["summary"]


@pytest.mark.asyncio
async def test_empty_plan(tool_fn):
    plan_data = {"destination": "未定", "days": [], "budget": {}}
    result = await tool_fn(plan_data=plan_data)
    assert result["total_days"] == 0
    assert result["total_budget"] == 0
    assert "未定" in result["summary"]
