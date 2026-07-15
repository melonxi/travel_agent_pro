"""Tests for upgraded skeleton day schema validation."""
import pytest

from state.models import TravelPlanState
from tools.base import ToolError


def _make_plan() -> TravelPlanState:
    plan = TravelPlanState(session_id="test-schema")
    plan.phase = 2
    return plan


def _make_tool(plan):
    from tools.plan_tools.phase2_tools import make_set_skeleton_plans_tool
    return make_set_skeleton_plans_tool(plan)


def _day(
    *,
    area="浅草",
    locked=None,
    candidates=None,
    date_role="full_day",
    core=None,
    **extra,
):
    day = {
        "area_cluster": [area] if isinstance(area, str) else list(area),
        "locked_pois": [] if locked is None else list(locked),
        "candidate_pois": (
            ["仲见世"] if candidates is None else list(candidates)
        ),
        "date_role": date_role,
    }
    if core is not None:
        day["core_activities"] = list(core)
    day.update(extra)
    return day


@pytest.mark.asyncio
async def test_valid_skeleton_with_new_fields():
    plan = _make_plan()
    tool = _make_tool(plan)
    result = await tool(plans=[{
        "id": "plan_a",
        "name": "平衡版",
        "days": [
            _day(
                area=["浅草", "上野"],
                locked=["浅草寺"],
                candidates=["仲见世商店街", "上野公園"],
                date_role="full_day",
                core=["寺庙参观", "公园散步"],
                theme="传统文化",
                fatigue_level="medium",
                budget_level="medium",
            ),
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
                    "date_role": "full_day",
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
                    "date_role": "full_day",
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
                    "date_role": "full_day",
                },
            ],
        }])


@pytest.mark.asyncio
async def test_missing_date_role_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="date_role"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["浅草"],
                    "locked_pois": ["浅草寺"],
                    "candidate_pois": ["仲见世"],
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
                _day(
                    area="浅草",
                    locked=["浅草寺"],
                    candidates=["仲见世"],
                    date_role="arrival_day",
                ),
                _day(
                    area="上野",
                    locked=["浅草寺"],  # duplicate lock!
                    candidates=["上野公園"],
                    date_role="departure_day",
                ),
            ],
        }])


@pytest.mark.asyncio
async def test_cross_day_candidate_poi_duplicate_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="上野公園"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                _day(
                    area="浅草",
                    locked=["浅草寺"],
                    candidates=["上野公園", "仲见世商店街"],
                    date_role="arrival_day",
                ),
                _day(
                    area="上野",
                    locked=["东京塔"],
                    candidates=["上野公園", "不忍池"],
                    date_role="departure_day",
                ),
            ],
        }])


@pytest.mark.asyncio
async def test_cross_day_locked_candidate_conflict_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="浅草寺"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                _day(
                    area="浅草",
                    locked=["浅草寺"],
                    candidates=["仲见世商店街"],
                    date_role="arrival_day",
                ),
                _day(
                    area="上野",
                    locked=["东京塔"],
                    candidates=["浅草寺", "上野公園"],
                    date_role="departure_day",
                ),
            ],
        }])


@pytest.mark.asyncio
async def test_cross_day_candidate_locked_conflict_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="浅草寺"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                _day(
                    area="浅草",
                    locked=["仲见世商店街"],
                    candidates=["浅草寺", "上野公園"],
                    date_role="arrival_day",
                ),
                _day(
                    area="上野",
                    locked=["浅草寺"],
                    candidates=["不忍池", "东京塔"],
                    date_role="departure_day",
                ),
            ],
        }])


@pytest.mark.asyncio
async def test_same_day_locked_candidate_conflict_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="浅草寺"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                _day(
                    area="浅草",
                    locked=["浅草寺"],
                    candidates=["浅草寺", "仲见世商店街"],
                    date_role="full_day",
                ),
            ],
        }])


@pytest.mark.asyncio
async def test_same_day_candidate_duplicate_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="仲见世商店街"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                _day(
                    area="浅草",
                    locked=[],
                    candidates=["仲见世商店街", "上野公園", "仲见世商店街"],
                    date_role="full_day",
                ),
            ],
        }])


@pytest.mark.asyncio
async def test_same_day_locked_duplicate_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="浅草寺"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                _day(
                    area="浅草",
                    locked=["浅草寺", "浅草寺"],
                    candidates=["仲见世商店街"],
                    date_role="full_day",
                ),
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
            _day(
                area="浅草",
                locked=[],
                candidates=["浅草寺", "仲见世"],
                date_role="full_day",
            ),
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


@pytest.mark.asyncio
async def test_multi_day_requires_arrival_and_departure_roles():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="arrival_day"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                _day(
                    area="浅草",
                    locked=["浅草寺"],
                    candidates=["仲见世"],
                    date_role="full_day",
                ),
                _day(
                    area="上野",
                    locked=["东京塔"],
                    candidates=["上野公園"],
                    date_role="departure_day",
                ),
            ],
        }])


@pytest.mark.asyncio
async def test_arrival_day_core_activities_limit():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="core_activities.*arrival_day"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                _day(
                    area="浅草",
                    locked=["浅草寺"],
                    candidates=["仲见世"],
                    date_role="arrival_day",
                    core=["A", "B", "C"],
                ),
                _day(
                    area="上野",
                    locked=["东京塔"],
                    candidates=["上野公園"],
                    date_role="departure_day",
                ),
            ],
        }])


@pytest.mark.asyncio
async def test_valid_multi_day_with_roles():
    plan = _make_plan()
    tool = _make_tool(plan)
    result = await tool(plans=[{
        "id": "plan_a",
        "name": "平衡版",
        "days": [
            _day(
                area="浅草",
                locked=["浅草寺"],
                candidates=["仲见世"],
                date_role="arrival_day",
                core=["寺庙"],
            ),
            _day(
                area="新宿",
                locked=["明治神宫"],
                candidates=["代代木公园"],
                date_role="full_day",
                core=["神社", "公园", "购物"],
            ),
            _day(
                area="上野",
                locked=["东京塔"],
                candidates=["上野公園"],
                date_role="departure_day",
                core=["展望"],
            ),
        ],
    }])
    assert result["count"] == 1
