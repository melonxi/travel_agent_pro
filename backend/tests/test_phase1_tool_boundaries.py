from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import respx
from httpx import Request, Response

from phase.prompts import PHASE_PROMPTS
from config import ApiKeysConfig, XhsConfig
from state.models import Accommodation, Budget, DateRange, Travelers, TravelPlanState
from tools.base import ToolError
from tools.check_feasibility import make_check_feasibility_tool
from tools.engine import ToolEngine
from tools.quick_travel_search import make_quick_travel_search_tool
from tools.search_destinations import make_search_destinations_tool
from tools.web_search import make_web_search_tool
from tools.xiaohongshu_search import make_xiaohongshu_search_tool
from tools.plan_tools.trip_basics import make_update_trip_basics_tool
from tools.plan_tools.append_tools import (
    make_set_destination_candidates_tool,
)


def _geocode_result(name: str, types: list[str]) -> dict:
    return {
        "address_components": [{"long_name": name}],
        "formatted_address": f"{name} address",
        "geometry": {"location": {"lat": 35.0, "lng": 139.0}},
        "types": types,
    }


@respx.mock
@pytest.mark.asyncio
async def test_search_destinations_filters_non_destination_types_deduplicates_and_caps_results():
    tool_fn = make_search_destinations_tool(ApiKeysConfig(google_maps="test_key"))

    def geocode_callback(request: Request) -> Response:
        address = request.url.params.get("address", "")
        if address == "日本":
            return Response(
                200,
                json={
                    "status": "OK",
                    "results": [
                        _geocode_result("Tokyo", ["locality", "political"]),
                        _geocode_result("Tokyo Tower", ["tourist_attraction", "establishment"]),
                        _geocode_result("Kyoto", ["locality", "political"]),
                        _geocode_result("Osaka", ["locality", "political"]),
                    ],
                },
            )
        seed_map = {
            "东京": "Tokyo",
            "京都": "Kyoto",
            "大阪": "Osaka",
            "奈良": "Nara",
            "北海道": "Hokkaido",
        }
        if address in seed_map:
            return Response(
                200,
                json={
                    "status": "OK",
                    "results": [_geocode_result(seed_map[address], ["locality", "political"])],
                },
            )
        return Response(200, json={"status": "ZERO_RESULTS", "results": []})

    respx.get("https://maps.googleapis.com/maps/api/geocode/json").mock(
        side_effect=geocode_callback
    )

    result = await tool_fn(query="日本旅游")

    assert [item["name"] for item in result["destinations"]] == [
        "Tokyo",
        "Kyoto",
        "Osaka",
    ]
    assert all("establishment" not in item["place_types"] for item in result["destinations"])
    assert len(result["destinations"]) == 3


def test_search_destinations_is_not_exposed_in_phase1():
    engine = ToolEngine()
    engine.register(make_search_destinations_tool(ApiKeysConfig(google_maps="test_key")))

    phase1_tool_names = [tool["name"] for tool in engine.get_tools_for_phase(1)]
    phase2_tool_names = [tool["name"] for tool in engine.get_tools_for_phase(2)]

    assert "search_destinations" not in phase1_tool_names
    assert "search_destinations" not in phase2_tool_names


@respx.mock
@pytest.mark.asyncio
async def test_check_feasibility_uses_current_weather_only_and_always_returns_feasible():
    tool_fn = make_check_feasibility_tool(ApiKeysConfig(openweather="test_key"))
    route = respx.get("https://api.openweathermap.org/data/2.5/weather").mock(
        return_value=Response(
            200,
            json={
                "main": {"temp": -5, "humidity": 98},
                "weather": [{"description": "extreme blizzard"}],
            },
        )
    )

    result = await tool_fn(destination="Reykjavik", travel_date="2030-12-31")

    assert route.called
    assert "travel_date" not in str(route.calls[0].request.url)
    assert result["travel_date"] == "2030-12-31"
    assert result["weather"]["description"] == "extreme blizzard"
    assert result["visa_info"] == "请自行查询签证要求"
    assert result["feasible"] is True


def test_check_feasibility_is_not_exposed_in_phase1():
    engine = ToolEngine()
    engine.register(make_check_feasibility_tool(ApiKeysConfig(openweather="test_key")))

    phase1_tool_names = [tool["name"] for tool in engine.get_tools_for_phase(1)]
    phase3_tool_names = [tool["name"] for tool in engine.get_tools_for_phase(3)]

    assert "check_feasibility" not in phase1_tool_names
    assert "check_feasibility" not in phase3_tool_names


def test_phase1_prompt_does_not_mention_check_feasibility():
    assert "`check_feasibility`" not in PHASE_PROMPTS[1]


def test_phase1_prompt_does_not_mention_search_destinations():
    assert "`search_destinations`" not in PHASE_PROMPTS[1]


@pytest.mark.asyncio
async def test_quick_travel_search_uses_info_payload_when_present_and_top_level_fallback():
    flyai_client = AsyncMock()
    flyai_client.available = True
    flyai_client.fast_search.return_value = [
        {
            "info": {
                "title": "杭州3日游",
                "price": "1500",
                "jumpUrl": "https://fliggy.com/pkg",
                "picUrl": "https://img.example.com/pkg.jpg",
            }
        },
        {
            "title": "日本签证",
            "price": "299",
            "detailUrl": "https://fliggy.com/visa",
            "mainPic": "https://img.example.com/visa.jpg",
        },
    ]

    tool_fn = make_quick_travel_search_tool(flyai_client)
    result = await tool_fn(query="杭州三日游")

    assert result["results"] == [
        {
            "title": "杭州3日游",
            "price": "1500",
            "booking_url": "https://fliggy.com/pkg",
            "image_url": "https://img.example.com/pkg.jpg",
        },
        {
            "title": "日本签证",
            "price": "299",
            "booking_url": "https://fliggy.com/visa",
            "image_url": "https://img.example.com/visa.jpg",
        },
    ]
    flyai_client.fast_search.assert_awaited_once_with(query="杭州三日游")


@respx.mock
@pytest.mark.asyncio
async def test_web_search_clamps_max_results_and_does_not_send_other_filters():
    tool_fn = make_web_search_tool(ApiKeysConfig(tavily="test_tavily_key"))
    route = respx.post("https://api.tavily.com/search").mock(
        return_value=Response(200, json={"answer": "", "results": []})
    )

    await tool_fn(query="日本签证最新政策", search_depth="basic", max_results=99)

    assert route.called
    import json

    payload = json.loads(route.calls[0].request.read().decode("utf-8"))
    assert payload["max_results"] == 10
    assert payload["include_answer"] is True
    assert "include_domains" not in payload
    assert "days" not in payload


@pytest.mark.asyncio
async def test_web_search_runtime_does_not_validate_search_depth_value():
    tool_fn = make_web_search_tool(ApiKeysConfig(tavily="test_tavily_key"))

    called = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"answer": "", "results": []}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            called["payload"] = json
            return _FakeResponse()

    import tools.web_search as web_search_module

    original = web_search_module.httpx.AsyncClient
    web_search_module.httpx.AsyncClient = _FakeClient
    try:
        await tool_fn(query="test", search_depth="surprise")
    finally:
        web_search_module.httpx.AsyncClient = original

    assert called["payload"]["search_depth"] == "surprise"


@pytest.mark.asyncio
async def test_xiaohongshu_search_enforces_min_page_and_extracts_token_from_url():
    xhs_client = SimpleNamespace(
        search_notes=AsyncMock(return_value={"items": [], "has_more": False}),
        read_note=AsyncMock(),
        get_comments=AsyncMock(
            return_value={
                "comments": [],
                "has_more": False,
                "cursor": "",
                "total_fetched": 0,
                "pages_fetched": 1,
            }
        ),
    )
    tool_fn = make_xiaohongshu_search_tool(xhs_client=xhs_client)

    await tool_fn(operation="search_notes", keyword="东京 citywalk", page=0)
    xhs_client.search_notes.assert_awaited_once_with(
        keyword="东京 citywalk",
        sort="general",
        note_type="all",
        page=1,
    )

    await tool_fn(
        operation="get_comments",
        note_ref="https://www.xiaohongshu.com/explore/note_1?xsec_token=abc123",
    )
    xhs_client.get_comments.assert_awaited_once_with(
        note_ref="https://www.xiaohongshu.com/explore/note_1?xsec_token=abc123",
        cursor="",
        xsec_token="abc123",
        fetch_all=False,
    )


@pytest.mark.asyncio
async def test_xiaohongshu_search_disabled_short_circuits_before_calling_client():
    xhs_client = SimpleNamespace(
        search_notes=AsyncMock(),
        read_note=AsyncMock(),
        get_comments=AsyncMock(),
    )
    tool_fn = make_xiaohongshu_search_tool(
        xhs_config=XhsConfig(enabled=False),
        xhs_client=xhs_client,
    )

    with pytest.raises(ToolError, match="disabled"):
        await tool_fn(operation="search_notes", keyword="京都")

    xhs_client.search_notes.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_trip_basics_invalid_dates_budget_and_travelers_reset_fields_to_none():
    plan = TravelPlanState(
        session_id="s1",
        dates=DateRange(start="2026-05-01", end="2026-05-05"),
        budget=Budget(total=5000, currency="CNY"),
        travelers=Travelers(adults=2, children=1),
    )
    tool_fn = make_update_trip_basics_tool(plan)

    await tool_fn(dates="尽快出发")
    await tool_fn(budget="丰俭由人")
    await tool_fn(travelers="一家人")

    assert plan.dates is None
    assert plan.budget is None
    assert plan.travelers is None


@pytest.mark.asyncio
async def test_destination_candidates_append_or_replace():
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        destination="Kyoto",
        destination_candidates=[{"name": "Kyoto"}],
        dates=DateRange(start="2026-05-01", end="2026-05-04"),
        accommodation=Accommodation(area="祇園"),
    )
    tool_fn = make_set_destination_candidates_tool(plan)

    # Replace with single candidate
    await tool_fn(candidates=[{"name": "Osaka"}])
    assert plan.destination_candidates == [{"name": "Osaka"}]

    # Replace with multiple candidates
    await tool_fn(candidates=[{"name": "Tokyo"}, {"name": "Kyoto"}])
    assert plan.destination_candidates == [{"name": "Tokyo"}, {"name": "Kyoto"}]
