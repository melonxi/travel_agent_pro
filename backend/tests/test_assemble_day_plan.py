# backend/tests/test_assemble_day_plan.py
import pytest

from tools.assemble_day_plan import make_assemble_day_plan_tool


@pytest.fixture
def tool_fn():
    return make_assemble_day_plan_tool()


@pytest.mark.asyncio
async def test_assemble_day_plan(tool_fn):
    pois = [
        {"name": "A", "lat": 35.00, "lng": 135.70, "duration_hours": 1.0},
        {"name": "B", "lat": 35.01, "lng": 135.71, "duration_hours": 1.5},
        {"name": "C", "lat": 35.05, "lng": 135.75, "duration_hours": 2.0},
    ]
    result = await tool_fn(pois=pois)
    assert len(result["ordered_pois"]) == 3
    # First POI should remain the starting point
    assert result["ordered_pois"][0]["name"] == "A"
    # B is closer to A than C, so B should be second
    assert result["ordered_pois"][1]["name"] == "B"
    assert result["total_distance_km"] > 0
    assert result["estimated_hours"] > 0


@pytest.mark.asyncio
async def test_empty_pois(tool_fn):
    result = await tool_fn(pois=[])
    assert result["ordered_pois"] == []
    assert result["total_distance_km"] == 0.0
    assert result["estimated_hours"] == 0.0
