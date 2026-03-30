# backend/tests/test_search_accommodations.py
import pytest
import respx
from httpx import Response

from config import ApiKeysConfig
from tools.search_accommodations import make_search_accommodations_tool


@pytest.fixture
def tool_fn():
    keys = ApiKeysConfig(google_maps="test_key")
    return make_search_accommodations_tool(keys)


@respx.mock
@pytest.mark.asyncio
async def test_search_accommodations(tool_fn):
    respx.get("https://maps.googleapis.com/maps/api/place/textsearch/json").mock(
        return_value=Response(
            200,
            json={
                "results": [
                    {
                        "name": "Tokyo Hotel",
                        "formatted_address": "Shinjuku, Tokyo",
                        "rating": 4.2,
                        "geometry": {"location": {"lat": 35.69, "lng": 139.70}},
                        "price_level": 3,
                    },
                ]
            },
        )
    )
    result = await tool_fn(
        destination="东京", check_in="2024-07-15", check_out="2024-07-20"
    )
    assert len(result["accommodations"]) == 1
    assert result["accommodations"][0]["name"] == "Tokyo Hotel"
    assert result["accommodations"][0]["source"] == "google"


@pytest.mark.asyncio
async def test_no_api_key():
    keys = ApiKeysConfig(google_maps="")
    fn = make_search_accommodations_tool(keys)
    from tools.base import ToolError

    with pytest.raises(ToolError, match="API key"):
        await fn(destination="test", check_in="2024-07-15", check_out="2024-07-20")
