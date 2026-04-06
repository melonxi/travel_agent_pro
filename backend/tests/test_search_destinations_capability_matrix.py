from __future__ import annotations

import pytest
import respx
from httpx import Request, Response

from config import ApiKeysConfig
from tools.search_destinations import make_search_destinations_tool


def _result(name: str, formatted_address: str, types: list[str]) -> dict:
    return {
        "address_components": [{"long_name": name}],
        "formatted_address": formatted_address,
        "geometry": {"location": {"lat": 35.0, "lng": 139.0}},
        "types": types,
    }


@pytest.fixture
def tool_fn():
    return make_search_destinations_tool(ApiKeysConfig(google_maps="test_key"))


@respx.mock
@pytest.mark.asyncio
async def test_search_destinations_supplements_direct_results_only_when_fewer_than_three(tool_fn):
    def geocode_callback(request: Request) -> Response:
        address = request.url.params.get("address", "")
        if address == "日本文化":
            return Response(
                200,
                json={
                    "status": "OK",
                    "results": [
                        _result("Kyoto", "Kyoto, Japan", ["locality", "political"]),
                    ],
                },
            )
        seed_map = {
            "东京": "Tokyo",
            "京都": "Kyoto",
            "大阪": "Osaka",
        }
        if address in seed_map:
            return Response(
                200,
                json={
                    "status": "OK",
                    "results": [
                        _result(seed_map[address], f"{seed_map[address]}, Japan", ["locality", "political"])
                    ],
                },
            )
        return Response(200, json={"status": "ZERO_RESULTS", "results": []})

    respx.get("https://maps.googleapis.com/maps/api/geocode/json").mock(
        side_effect=geocode_callback
    )

    result = await tool_fn(query="日本文化")

    assert [item["name"] for item in result["destinations"]] == [
        "Kyoto",
        "Tokyo",
        "Osaka",
    ]
    assert "culture_history" in result["matched_themes"]


@respx.mock
@pytest.mark.asyncio
async def test_search_destinations_may_return_sublocality_or_natural_feature_not_just_city(tool_fn):
    def geocode_callback(request: Request) -> Response:
        address = request.url.params.get("address", "")
        if address == "山里散心":
            return Response(
                200,
                json={
                    "status": "OK",
                    "results": [
                        _result("Arashiyama", "Ukyo Ward, Kyoto, Japan", ["sublocality", "political"]),
                        _result("Mount Fuji", "Japan", ["natural_feature", "establishment"]),
                    ],
                },
            )
        return Response(200, json={"status": "ZERO_RESULTS", "results": []})

    respx.get("https://maps.googleapis.com/maps/api/geocode/json").mock(
        side_effect=geocode_callback
    )

    result = await tool_fn(query="山里散心")

    assert [item["name"] for item in result["destinations"]] == ["Arashiyama", "Mount Fuji"]
    assert result["destinations"][0]["place_types"] == ["sublocality", "political"]
    assert result["destinations"][1]["place_types"] == ["natural_feature", "establishment"]


@respx.mock
@pytest.mark.asyncio
async def test_search_destinations_preferences_trigger_seed_expansion_but_do_not_hard_filter_results(tool_fn):
    def geocode_callback(request: Request) -> Response:
        address = request.url.params.get("address", "")
        if address == "日本":
            return Response(
                200,
                json={
                    "status": "OK",
                    "results": [
                        _result("Tokyo", "Tokyo, Japan", ["locality", "political"]),
                        _result("Kyoto", "Kyoto, Japan", ["locality", "political"]),
                        _result("Osaka", "Osaka, Japan", ["locality", "political"]),
                    ],
                },
            )
        return Response(200, json={"status": "ZERO_RESULTS", "results": []})

    respx.get("https://maps.googleapis.com/maps/api/geocode/json").mock(
        side_effect=geocode_callback
    )

    result = await tool_fn(query="日本", preferences=["海景", "放松"])

    assert [item["name"] for item in result["destinations"]] == ["Tokyo", "Kyoto", "Osaka"]
    assert "beach_relax" in result["matched_themes"]
    assert result["candidate_seeds"][:5] == ["东京", "京都", "大阪", "奈良", "北海道"]


@respx.mock
@pytest.mark.asyncio
async def test_search_destinations_relies_on_fixed_seed_catalog_for_abstract_queries(tool_fn):
    respx.get("https://maps.googleapis.com/maps/api/geocode/json").mock(
        return_value=Response(200, json={"status": "ZERO_RESULTS", "results": []})
    )

    from tools.base import ToolError

    with pytest.raises(ToolError, match="No destination candidates found"):
        await tool_fn(query="免签海岛亲子")
