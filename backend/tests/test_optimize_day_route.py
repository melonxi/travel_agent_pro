from __future__ import annotations

import pytest

from tools.base import ToolError
from tools.optimize_day_route import make_optimize_day_route_tool


@pytest.fixture
def tool_fn():
    return make_optimize_day_route_tool()


def _poi(name: str, lat: float, lng: float, duration_hours: float = 1.0) -> dict:
    return {
        "name": name,
        "lat": lat,
        "lng": lng,
        "duration_hours": duration_hours,
    }


@pytest.mark.asyncio
async def test_optimize_day_route_orders_pois(tool_fn):
    result = await tool_fn(
        pois=[
            _poi("A", 35.00, 135.70),
            _poi("B", 35.01, 135.71, 1.5),
            _poi("C", 35.05, 135.75, 2.0),
        ],
        day_start_time="09:00",
        day_end_time="21:00",
    )

    assert [poi["name"] for poi in result["ordered_pois"]] == ["A", "B", "C"]
    assert result["estimated_total_distance_km"] > 0
    assert result["estimated_travel_minutes"] > 0
    assert result["estimated_activity_minutes"] == 270
    assert result["estimated_total_minutes"] > result["estimated_activity_minutes"]
    assert result["can_fit_in_day"] is True
    assert result["warnings"] == []
    assert "did not write daily_plans" in result["next_action"]


@pytest.mark.asyncio
async def test_optimize_day_route_is_read_side_effect(tool_fn):
    assert tool_fn.name == "optimize_day_route"
    assert tool_fn.side_effect == "read"
    assert tool_fn.phases == [5]
    assert tool_fn.human_label == "优化单日路线"
    assert tool_fn.parameters["required"] == ["pois"]
    assert tool_fn.parameters["properties"]["transport_mode"]["enum"] == [
        "walking",
        "transit",
        "driving",
    ]


@pytest.mark.asyncio
async def test_optimize_day_route_warns_for_single_poi(tool_fn):
    result = await tool_fn(pois=[_poi("Only", 35.0, 135.7)])

    assert [poi["name"] for poi in result["ordered_pois"]] == ["Only"]
    assert result["estimated_total_distance_km"] == 0.0
    assert result["warnings"] == [
        "Only one POI supplied; route ordering was not needed."
    ]
    assert result["can_fit_in_day"] is True


@pytest.mark.asyncio
async def test_optimize_day_route_requires_coordinates(tool_fn):
    with pytest.raises(ToolError, match="pois\\[0\\].lat") as exc_info:
        await tool_fn(pois=[{"name": "Bad", "lng": 135.7}])

    assert exc_info.value.error_code == "INVALID_VALUE"
    assert "get_poi_info" in exc_info.value.suggestion


@pytest.mark.asyncio
async def test_optimize_day_route_flags_overfull_day(tool_fn):
    result = await tool_fn(
        pois=[
            _poi("A", 35.00, 135.70, 6.0),
            _poi("B", 35.30, 136.20, 6.0),
        ],
        day_start_time="09:00",
        day_end_time="18:00",
    )

    assert result["can_fit_in_day"] is False
    assert any(
        "exceeds available day window" in warning for warning in result["warnings"]
    )
