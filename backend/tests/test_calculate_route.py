# backend/tests/test_calculate_route.py
import pytest
import respx
from httpx import Response

from config import ApiKeysConfig
from tools.calculate_route import make_calculate_route_tool


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
                "routes": [
                    {
                        "legs": [
                            {
                                "distance": {"text": "5.2 km"},
                                "duration": {"text": "18 mins"},
                                "steps": [
                                    {
                                        "html_instructions": "Head north",
                                        "distance": {"text": "0.3 km"},
                                        "duration": {"text": "2 mins"},
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
    assert len(result["steps"]) == 1
    assert result["mode"] == "transit"


@pytest.mark.asyncio
async def test_no_api_key():
    keys = ApiKeysConfig(google_maps="")
    fn = make_calculate_route_tool(keys)
    from tools.base import ToolError

    with pytest.raises(ToolError, match="API key"):
        await fn(origin_lat=0, origin_lng=0, dest_lat=1, dest_lng=1)
