"""Tests for upgraded skeleton day schema validation."""
import pytest

from state.models import TravelPlanState
from tools.base import ToolError


def _make_plan() -> TravelPlanState:
    plan = TravelPlanState(session_id="test-schema")
    plan.phase = 3
    return plan


def _make_tool(plan):
    from tools.plan_tools.phase3_tools import make_set_skeleton_plans_tool
    return make_set_skeleton_plans_tool(plan)


@pytest.mark.asyncio
async def test_valid_skeleton_with_new_fields():
    plan = _make_plan()
    tool = _make_tool(plan)
    result = await tool(plans=[{
        "id": "plan_a",
        "name": "平衡版",
        "days": [
            {
                "area_cluster": ["浅草", "上野"],
                "theme": "传统文化",
                "locked_pois": ["浅草寺"],
                "candidate_pois": ["仲见世商店街", "上野公園"],
                "core_activities": ["寺庙参观", "公园散步"],
                "fatigue_level": "medium",
                "budget_level": "medium",
            },
        ],
        "tradeoffs": {"kept": "传统", "dropped": "购物"},
    }])
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_missing_area_cluster_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="area_cluster"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                {
                    "theme": "传统文化",
                    "locked_pois": ["浅草寺"],
                    "candidate_pois": ["上野公園"],
                },
            ],
        }])


@pytest.mark.asyncio
async def test_missing_locked_pois_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="locked_pois"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["浅草"],
                    "candidate_pois": ["上野公園"],
                },
            ],
        }])


@pytest.mark.asyncio
async def test_missing_candidate_pois_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="candidate_pois"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["浅草"],
                    "locked_pois": ["浅草寺"],
                },
            ],
        }])


@pytest.mark.asyncio
async def test_cross_day_locked_poi_duplicate_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="浅草寺.*locked.*唯一"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["浅草"],
                    "locked_pois": ["浅草寺"],
                    "candidate_pois": ["仲见世"],
                },
                {
                    "area_cluster": ["上野"],
                    "locked_pois": ["浅草寺"],  # duplicate lock!
                    "candidate_pois": ["上野公園"],
                },
            ],
        }])


@pytest.mark.asyncio
async def test_empty_locked_pois_is_valid():
    plan = _make_plan()
    tool = _make_tool(plan)
    result = await tool(plans=[{
        "id": "plan_a",
        "name": "平衡版",
        "days": [
            {
                "area_cluster": ["浅草"],
                "locked_pois": [],
                "candidate_pois": ["浅草寺", "仲见世"],
            },
        ],
    }])
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_empty_days_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="days.*不能为空"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [],
        }])
