# backend/tests/test_get_poi_info.py
import pytest
import respx
from httpx import Response

from config import ApiKeysConfig
from tools.base import ToolError
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
    assert result["pois"][0]["source"] == "google"


@pytest.mark.asyncio
async def test_no_api_key():
    keys = ApiKeysConfig(google_maps="")
    fn = make_get_poi_info_tool(keys)

    with pytest.raises(ToolError, match="API key"):
        await fn(query="test")


class _FailingFlyAIClient:
    available = True

    async def search_poi(self, city_name: str, keyword: str):
        raise RuntimeError("Trial limit reached. Please configure FLYAI_API_KEY")


@respx.mock
@pytest.mark.asyncio
async def test_returns_flyai_error_detail_when_google_empty():
    respx.get("https://maps.googleapis.com/maps/api/place/textsearch/json").mock(
        return_value=Response(200, json={"results": []})
    )
    keys = ApiKeysConfig(google_maps="test_key")
    fn = make_get_poi_info_tool(keys, flyai_client=_FailingFlyAIClient())

    with pytest.raises(ToolError, match="Trial limit reached"):
        await fn(query="金阁寺", location="京都")


@respx.mock
@pytest.mark.asyncio
async def test_returns_google_results_when_flyai_fails():
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
    keys = ApiKeysConfig(google_maps="test_key")
    fn = make_get_poi_info_tool(keys, flyai_client=_FailingFlyAIClient())

    result = await fn(query="金阁寺", location="京都")

    assert len(result["pois"]) == 1
    assert result["pois"][0]["name"] == "Kinkaku-ji"
    assert result["pois"][0]["source"] == "google"


@pytest.mark.asyncio
async def test_reports_google_key_and_flyai_error_when_google_disabled():
    keys = ApiKeysConfig(google_maps="")
    fn = make_get_poi_info_tool(keys, flyai_client=_FailingFlyAIClient())

    with pytest.raises(ToolError) as exc_info:
        await fn(query="金阁寺", location="京都")

    message = str(exc_info.value)
    assert "Google Maps API key not configured" in message
    assert "Trial limit reached" in message
