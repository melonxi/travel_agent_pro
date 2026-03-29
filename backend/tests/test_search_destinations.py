# backend/tests/test_search_destinations.py
import pytest
import respx
from httpx import Response

from config import ApiKeysConfig
from tools.search_destinations import make_search_destinations_tool


@pytest.fixture
def tool_fn():
    keys = ApiKeysConfig(google_maps="test_key")
    return make_search_destinations_tool(keys)


@respx.mock
@pytest.mark.asyncio
async def test_search_destinations(tool_fn):
    respx.get("https://maps.googleapis.com/maps/api/place/textsearch/json").mock(
        return_value=Response(
            200,
            json={
                "results": [
                    {
                        "name": "Kyoto",
                        "formatted_address": "Kyoto, Japan",
                        "rating": 4.5,
                        "geometry": {"location": {"lat": 35.01, "lng": 135.76}},
                    },
                ]
            },
        )
    )
    result = await tool_fn(query="日本文化")
    assert len(result["destinations"]) == 1
    assert result["destinations"][0]["name"] == "Kyoto"
    assert result["source"] == "google_places"


@pytest.mark.asyncio
async def test_no_api_key():
    keys = ApiKeysConfig(google_maps="")
    fn = make_search_destinations_tool(keys)
    from tools.base import ToolError

    with pytest.raises(ToolError, match="API key"):
        await fn(query="test")
