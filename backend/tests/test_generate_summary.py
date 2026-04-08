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


@pytest.mark.asyncio
async def test_generate_summary_tolerates_days_as_int(tool_fn):
    """LLM 有时会把 days 传成整数（它其实想表达 total_days）。必须不崩。"""
    plan_data = {
        "destination": "涠洲岛",
        "days": 4,
        "budget": {"flights": 1000, "hotels": 1200, "activities": 800, "food": 500},
    }
    result = await tool_fn(plan_data=plan_data)
    assert result["total_days"] == 4
    assert result["total_budget"] == 3500
    assert "涠洲岛" in result["summary"]


@pytest.mark.asyncio
async def test_generate_summary_accepts_daily_plans_alias(tool_fn):
    """LLM 常用 daily_plans 替代 days。"""
    plan_data = {
        "destination": "京都",
        "daily_plans": [
            {"activities": [{"name": "金阁寺"}]},
            {"activities": [{"name": "伏见稻荷"}]},
        ],
        "budget": {"total": 8000},
    }
    result = await tool_fn(plan_data=plan_data)
    assert result["total_days"] == 2
    assert result["total_budget"] == 8000
    assert "金阁寺" in result["summary"]


@pytest.mark.asyncio
async def test_generate_summary_tolerates_numeric_budget(tool_fn):
    """LLM 偶尔把 budget 直接传数字。"""
    plan_data = {
        "destination": "大阪",
        "days": [{"activities": []}],
        "budget": 6800,
    }
    result = await tool_fn(plan_data=plan_data)
    assert result["total_days"] == 1
    assert result["total_budget"] == 6800


@pytest.mark.asyncio
async def test_generate_summary_tolerates_total_days_only(tool_fn):
    """没有 days 明细，只有 total_days 时仍应工作。"""
    plan_data = {"destination": "札幌", "total_days": 5, "budget": {}}
    result = await tool_fn(plan_data=plan_data)
    assert result["total_days"] == 5
    assert "札幌" in result["summary"]


@pytest.mark.asyncio
async def test_generate_summary_tolerates_string_activities(tool_fn):
    """activities 里有非 dict 元素时不要崩，跳过即可。"""
    plan_data = {
        "destination": "上海",
        "days": [
            {"activities": ["外滩", {"name": "迪士尼"}]},
        ],
        "budget": {},
    }
    result = await tool_fn(plan_data=plan_data)
    assert result["total_days"] == 1
    assert "迪士尼" in result["summary"]


@pytest.mark.asyncio
async def test_generate_summary_tolerates_non_dict_plan(tool_fn):
    """极端情况：plan_data 本身就不是 dict。"""
    result = await tool_fn(plan_data=None)
    assert result["total_days"] == 0
    assert result["total_budget"] == 0
