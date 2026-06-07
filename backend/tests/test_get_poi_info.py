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


_KINKAKU_RESULT = {
    "results": [
        {
            "name": "Kinkaku-ji",
            "formatted_address": "1 Kinkakujicho, Kyoto",
            "rating": 4.6,
            "geometry": {"location": {"lat": 35.04, "lng": 135.73}},
            "types": ["tourist_attraction", "place_of_worship"],
        },
    ]
}


@respx.mock
@pytest.mark.asyncio
async def test_repeated_query_served_from_session_cache():
    route = respx.get(
        "https://maps.googleapis.com/maps/api/place/textsearch/json"
    ).mock(return_value=Response(200, json=_KINKAKU_RESULT))
    fn = make_get_poi_info_tool(ApiKeysConfig(google_maps="test_key"))

    first = await fn(query="金阁寺", location="京都")
    second = await fn(query="金阁寺", location="京都")

    assert second == first
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_cache_keyed_by_query_and_location():
    route = respx.get(
        "https://maps.googleapis.com/maps/api/place/textsearch/json"
    ).mock(return_value=Response(200, json=_KINKAKU_RESULT))
    fn = make_get_poi_info_tool(ApiKeysConfig(google_maps="test_key"))

    await fn(query="金阁寺", location="京都")
    await fn(query="清水寺", location="京都")
    await fn(query="金阁寺", location="大阪")

    assert route.call_count == 3


@respx.mock
@pytest.mark.asyncio
async def test_failures_are_not_cached():
    route = respx.get(
        "https://maps.googleapis.com/maps/api/place/textsearch/json"
    ).mock(
        side_effect=[
            Response(200, json={"results": []}),
            Response(200, json=_KINKAKU_RESULT),
        ]
    )
    fn = make_get_poi_info_tool(ApiKeysConfig(google_maps="test_key"))

    with pytest.raises(ToolError):
        await fn(query="金阁寺", location="京都")
    result = await fn(query="金阁寺", location="京都")

    assert result["pois"][0]["name"] == "Kinkaku-ji"
    assert route.call_count == 2
