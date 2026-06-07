# backend/tests/test_calculate_route.py
from datetime import datetime

import pytest
import respx
from httpx import Response

from config import ApiKeysConfig
from tools.calculate_route import make_calculate_route_tool
from tools.base import ToolError


@pytest.fixture
def tool_fn():
    keys = ApiKeysConfig(google_maps="test_key")
    return make_calculate_route_tool(keys)


@respx.mock
@pytest.mark.asyncio
async def test_calculate_route(tool_fn):
    respx.get("https://maps.googleapis.com/maps/api/directions/json").mock(
        return_value=Response(
            200,
            json={
                "status": "OK",
                "routes": [
                    {
                        "legs": [
                            {
                                "distance": {"text": "5.2 km"},
                                "duration": {"text": "18 mins", "value": 1080},
                                "steps": [
                                    {
                                        "html_instructions": "Head north",
                                        "distance": {"text": "0.3 km", "value": 300},
                                        "duration": {"text": "2 mins", "value": 120},
                                    },
                                ],
                            }
                        ]
                    }
                ]
            },
        )
    )
    result = await tool_fn(
        origin_lat=35.01, origin_lng=135.76, dest_lat=35.04, dest_lng=135.73
    )
    assert result["distance"] == "5.2 km"
    assert result["duration"] == "18 mins"
    assert result["duration_min"] == 18
    assert len(result["steps"]) == 1
    assert result["steps"][0]["duration_min"] == 2
    assert result["mode"] == "transit"
    assert result["requested_mode"] == "transit"
    assert result["fallback_used"] is False
    assert result["google_status"] == "OK"
    assert result["route_available"] is True


@respx.mock
@pytest.mark.asyncio
async def test_calculate_route_sends_departure_time(tool_fn):
    respx.get("https://maps.googleapis.com/maps/api/directions/json").mock(
        return_value=Response(
            200,
            json={
                "status": "OK",
                "routes": [
                    {
                        "legs": [
                            {
                                "distance": {"text": "5.2 km", "value": 5200},
                                "duration": {"text": "18 mins", "value": 1080},
                                "steps": [],
                            }
                        ]
                    }
                ],
            },
        )
    )

    await tool_fn(
        origin_lat=35.01,
        origin_lng=135.76,
        dest_lat=35.04,
        dest_lng=135.73,
        departure_time=1_800_000_000,
    )

    params = respx.calls[0].request.url.params
    assert params["departure_time"] == "1800000000"


@respx.mock
@pytest.mark.asyncio
async def test_calculate_route_accepts_iso_departure_time(tool_fn):
    respx.get("https://maps.googleapis.com/maps/api/directions/json").mock(
        return_value=Response(
            200,
            json={
                "status": "OK",
                "routes": [
                    {
                        "legs": [
                            {
                                "distance": {"text": "5.2 km", "value": 5200},
                                "duration": {"text": "18 mins", "value": 1080},
                                "steps": [],
                            }
                        ]
                    }
                ],
            },
        )
    )

    iso_time = "2026-05-01T09:00:00+09:00"
    await tool_fn(
        origin_lat=35.01,
        origin_lng=135.76,
        dest_lat=35.04,
        dest_lng=135.73,
        departure_time=iso_time,
    )

    expected = str(int(datetime.fromisoformat(iso_time).timestamp()))
    params = respx.calls[0].request.url.params
    assert params["departure_time"] == expected


@respx.mock
@pytest.mark.asyncio
async def test_calculate_route_zero_results_is_not_success(tool_fn):
    respx.get("https://maps.googleapis.com/maps/api/directions/json").mock(
        return_value=Response(200, json={"status": "ZERO_RESULTS", "routes": []})
    )

    with pytest.raises(ToolError) as exc_info:
        await tool_fn(
            origin_lat=35.01,
            origin_lng=135.76,
            dest_lat=35.04,
            dest_lng=135.73,
        )

    assert exc_info.value.error_code == "NO_ROUTE"
    assert "已自动尝试" in exc_info.value.suggestion


@respx.mock
@pytest.mark.asyncio
async def test_calculate_route_zero_results_short_distance_falls_back_to_walking(tool_fn):
    respx.get("https://maps.googleapis.com/maps/api/directions/json").mock(
        side_effect=[
            Response(200, json={"status": "ZERO_RESULTS", "routes": []}),
            Response(
                200,
                json={
                    "status": "OK",
                    "routes": [
                        {
                            "legs": [
                                {
                                    "distance": {"text": "120 m", "value": 120},
                                    "duration": {"text": "2 mins", "value": 120},
                                    "steps": [],
                                }
                            ]
                        }
                    ],
                },
            ),
        ]
    )

    result = await tool_fn(
        origin_lat=35.0000,
        origin_lng=139.0000,
        dest_lat=35.0005,
        dest_lng=139.0005,
        mode="transit",
    )

    assert len(respx.calls) == 2
    assert respx.calls[0].request.url.params["mode"] == "transit"
    assert respx.calls[1].request.url.params["mode"] == "walking"
    assert result["mode"] == "walking"
    assert result["requested_mode"] == "transit"
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "transit_zero_results_short_distance"


@respx.mock
@pytest.mark.asyncio
async def test_calculate_route_zero_results_long_distance_does_not_fallback(tool_fn):
    respx.get("https://maps.googleapis.com/maps/api/directions/json").mock(
        return_value=Response(200, json={"status": "ZERO_RESULTS", "routes": []})
    )

    with pytest.raises(ToolError) as exc_info:
        await tool_fn(
            origin_lat=35.01,
            origin_lng=135.76,
            dest_lat=35.40,
            dest_lng=139.73,
            mode="transit",
        )

    assert len(respx.calls) == 1
    assert exc_info.value.error_code == "NO_ROUTE"


@respx.mock
@pytest.mark.asyncio
async def test_calculate_route_request_denied_has_actionable_error(tool_fn):
    respx.get("https://maps.googleapis.com/maps/api/directions/json").mock(
        return_value=Response(
            200,
            json={
                "status": "REQUEST_DENIED",
                "error_message": "This API project is not authorized.",
                "routes": [],
            },
        )
    )

    with pytest.raises(ToolError) as exc_info:
        await tool_fn(
            origin_lat=35.01,
            origin_lng=135.76,
            dest_lat=35.04,
            dest_lng=135.73,
        )

    assert exc_info.value.error_code == "ROUTE_REQUEST_DENIED"
    assert "Directions API" in exc_info.value.suggestion


@pytest.mark.asyncio
async def test_calculate_route_rejects_unknown_mode(tool_fn):
    with pytest.raises(ToolError) as exc_info:
        await tool_fn(
            origin_lat=35.01,
            origin_lng=135.76,
            dest_lat=35.04,
            dest_lng=135.73,
            mode="scooter",
        )

    assert exc_info.value.error_code == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_no_api_key():
    keys = ApiKeysConfig(google_maps="")
    fn = make_calculate_route_tool(keys)

    with pytest.raises(ToolError, match="API key"):
        await fn(origin_lat=0, origin_lng=0, dest_lat=1, dest_lng=1)
