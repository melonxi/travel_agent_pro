# backend/tests/test_get_poi_info.py
import pytest
import respx
from httpx import Response

from config import ApiKeysConfig
from tools.get_poi_info import make_get_poi_info_tool


@pytest.fixture
def tool_fn():
    keys = ApiKeysConfig(google_maps="test_key")
    return make_get_poi_info_tool(keys)


@respx.mock
@pytest.mark.asyncio
async def test_get_poi_info(tool_fn):
    respx.get("https://maps.googleapis.com/maps/api/place/textsearch/json").mock(
        return_value=Response(
            200,
            json={
                "results": [
                    {
                        "name": "Kinkaku-ji",
                        "formatted_address": "1 Kinkakujicho, Kyoto",
                        "rating": 4.6,
                        "geometry": {"location": {"lat": 35.04, "lng": 135.73}},
                        "types": ["tourist_attraction", "place_of_worship"],
                    },
                ]
            },
        )
    )
    result = await tool_fn(query="金阁寺", location="京都")
    assert len(result["pois"]) == 1
    assert result["pois"][0]["name"] == "Kinkaku-ji"
    assert result["source"] == "google_places"


@pytest.mark.asyncio
async def test_no_api_key():
    keys = ApiKeysConfig(google_maps="")
    fn = make_get_poi_info_tool(keys)
    from tools.base import ToolError

    with pytest.raises(ToolError, match="API key"):
        await fn(query="test")
