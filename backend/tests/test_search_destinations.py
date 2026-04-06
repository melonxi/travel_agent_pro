# backend/tests/test_search_destinations.py
import pytest
import respx
from httpx import Request, Response

from config import ApiKeysConfig
from tools.base import ToolError
from tools.search_destinations import make_search_destinations_tool


@pytest.fixture
def tool_fn():
    keys = ApiKeysConfig(google_maps="test_key")
    return make_search_destinations_tool(keys)


def test_search_destinations_not_exposed_to_any_phase():
    keys = ApiKeysConfig(google_maps="test_key")
    tool_def = make_search_destinations_tool(keys)
    assert 1 not in tool_def.phases
    assert 2 not in tool_def.phases
    assert tool_def.phases == []


def _geocode_payload(name: str, formatted_address: str, types: list[str]) -> dict:
    return {
        "status": "OK",
        "results": [
            {
                "address_components": [{"long_name": name}],
                "formatted_address": formatted_address,
                "geometry": {"location": {"lat": 35.0, "lng": 139.0}},
                "types": types,
            }
        ],
    }


@respx.mock
@pytest.mark.asyncio
async def test_search_destinations_direct_place_lookup(tool_fn):
    def geocode_callback(request: Request) -> Response:
        address = request.url.params.get("address", "")
        if address == "东京":
            return Response(
                200,
                json=_geocode_payload("Tokyo", "Tokyo, Japan", ["locality", "political"]),
            )
        return Response(200, json={"status": "ZERO_RESULTS", "results": []})

    respx.get("https://maps.googleapis.com/maps/api/geocode/json").mock(
        side_effect=geocode_callback
    )

    result = await tool_fn(query="东京旅游")
    assert len(result["destinations"]) == 1
    assert result["destinations"][0]["name"] == "Tokyo"
    assert result["source"] == "google_geocoding"
    assert result["normalized_query"] == "东京"


@respx.mock
@pytest.mark.asyncio
async def test_search_destinations_falls_back_to_theme_seeds(tool_fn):
    def geocode_callback(request: Request) -> Response:
        address = request.url.params.get("address", "")
        if address == "海边放松":
            return Response(200, json={"status": "ZERO_RESULTS", "results": []})
        if address == "三亚":
            return Response(
                200,
                json=_geocode_payload("Sanya", "Sanya, Hainan, China", ["locality", "political"]),
            )
        if address == "冲绳":
            return Response(
                200,
                json=_geocode_payload("Okinawa", "Okinawa, Japan", ["administrative_area_level_1", "political"]),
            )
        return Response(200, json={"status": "ZERO_RESULTS", "results": []})

    respx.get("https://maps.googleapis.com/maps/api/geocode/json").mock(
        side_effect=geocode_callback
    )

    result = await tool_fn(query="海边放松")

    assert [item["name"] for item in result["destinations"][:2]] == ["Sanya", "Okinawa"]
    assert "beach_relax" in result["matched_themes"]
    assert result["candidate_seeds"][:2] == ["三亚", "冲绳"]


@respx.mock
@pytest.mark.asyncio
async def test_search_destinations_uses_preferences_to_expand_abstract_query(tool_fn):
    def geocode_callback(request: Request) -> Response:
        address = request.url.params.get("address", "")
        if address == "想轻松一点":
            return Response(200, json={"status": "ZERO_RESULTS", "results": []})
        if address == "三亚":
            return Response(
                200,
                json=_geocode_payload("Sanya", "Sanya, Hainan, China", ["locality", "political"]),
            )
        return Response(200, json={"status": "ZERO_RESULTS", "results": []})

    respx.get("https://maps.googleapis.com/maps/api/geocode/json").mock(
        side_effect=geocode_callback
    )

    result = await tool_fn(query="想轻松一点", preferences=["海景", "放松"])

    assert result["destinations"][0]["name"] == "Sanya"
    assert "beach_relax" in result["matched_themes"]


@respx.mock
@pytest.mark.asyncio
async def test_search_destinations_raises_when_no_candidates(tool_fn):
    respx.get("https://maps.googleapis.com/maps/api/geocode/json").mock(
        return_value=Response(200, json={"status": "ZERO_RESULTS", "results": []})
    )

    with pytest.raises(ToolError, match="No destination candidates found"):
        await tool_fn(query="火星潜水")


@pytest.mark.asyncio
async def test_no_api_key():
    keys = ApiKeysConfig(google_maps="")
    fn = make_search_destinations_tool(keys)

    with pytest.raises(ToolError, match="API key"):
        await fn(query="test")
