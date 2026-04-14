# backend/tests/test_tool_fusion.py
from __future__ import annotations

import pytest
import respx
from httpx import Response
from unittest.mock import AsyncMock, patch

from config import ApiKeysConfig


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def api_keys():
    return ApiKeysConfig(
        amadeus_key="test_key",
        amadeus_secret="test_secret",
        google_maps="test_maps_key",
    )


@pytest.fixture
def mock_flyai_client():
    client = AsyncMock()
    client.available = True
    return client


@pytest.fixture
def mock_flyai_unavailable():
    client = AsyncMock()
    client.available = False
    return client


# ── search_flights fusion ─────────────────────────────────────────

AMADEUS_RESPONSE = {
    "data": [
        {
            "id": "1",
            "price": {"total": "3500.00", "currency": "CNY"},
            "itineraries": [
                {
                    "duration": "PT3H30M",
                    "segments": [
                        {
                            "carrierCode": "CA",
                            "number": "1234",
                            "departure": {
                                "iataCode": "PEK",
                                "at": "2026-07-15T08:00:00",
                            },
                            "arrival": {"iataCode": "NRT", "at": "2026-07-15T12:30:00"},
                        }
                    ],
                }
            ],
        }
    ]
}


@respx.mock
@pytest.mark.asyncio
async def test_flights_both_succeed(api_keys, mock_flyai_client):
    from tools.search_flights import make_search_flights_tool

    respx.post("https://test.api.amadeus.com/v1/security/oauth2/token").mock(
        return_value=Response(200, json={"access_token": "test_access_token"})
    )
    respx.post("https://test.api.amadeus.com/v2/shopping/flight-offers").mock(
        return_value=Response(200, json=AMADEUS_RESPONSE)
    )
    mock_flyai_client.search_flight.return_value = [
        {
            "adultPrice": "¥2800.0",
            "journeys": [
                {
                    "journeyType": "直达",
                    "segments": [
                        {
                            "marketingTransportName": "东方航空",
                            "marketingTransportNo": "MU5101",
                            "depCityName": "上海",
                            "arrCityName": "东京",
                            "depDateTime": "2026-07-15 10:00:00",
                            "arrDateTime": "2026-07-15 14:00:00",
                            "duration": "240分钟",
                            "seatClassName": "经济舱",
                        }
                    ],
                }
            ],
            "jumpUrl": "https://fliggy.com/f/123",
        }
    ]

    tool_fn = make_search_flights_tool(api_keys, mock_flyai_client)
    result = await tool_fn(origin="PEK", destination="NRT", date="2026-07-15")
    # Should have results from both sources (different flight_no)
    assert len(result["flights"]) == 2


@respx.mock
@pytest.mark.asyncio
async def test_flights_flyai_fails(api_keys, mock_flyai_client):
    from tools.search_flights import make_search_flights_tool

    respx.post("https://test.api.amadeus.com/v1/security/oauth2/token").mock(
        return_value=Response(200, json={"access_token": "test_access_token"})
    )
    respx.post("https://test.api.amadeus.com/v2/shopping/flight-offers").mock(
        return_value=Response(200, json=AMADEUS_RESPONSE)
    )
    mock_flyai_client.search_flight.side_effect = Exception("network error")

    tool_fn = make_search_flights_tool(api_keys, mock_flyai_client)
    result = await tool_fn(origin="PEK", destination="NRT", date="2026-07-15")
    # Amadeus results only
    assert len(result["flights"]) >= 1
    assert any(f["source"] == "amadeus" for f in result["flights"])


@respx.mock
@pytest.mark.asyncio
async def test_flights_flyai_disabled(api_keys, mock_flyai_unavailable):
    from tools.search_flights import make_search_flights_tool

    respx.post("https://test.api.amadeus.com/v1/security/oauth2/token").mock(
        return_value=Response(200, json={"access_token": "test_access_token"})
    )
    respx.post("https://test.api.amadeus.com/v2/shopping/flight-offers").mock(
        return_value=Response(200, json=AMADEUS_RESPONSE)
    )

    tool_fn = make_search_flights_tool(api_keys, mock_flyai_unavailable)
    result = await tool_fn(origin="PEK", destination="NRT", date="2026-07-15")
    assert len(result["flights"]) >= 1
    # FlyAI should not have been called
    mock_flyai_unavailable.search_flight.assert_not_called()


# ── search_accommodations fusion ──────────────────────────────────

GOOGLE_PLACES_RESPONSE = {
    "results": [
        {
            "name": "Park Hyatt Tokyo",
            "formatted_address": "3-7-1 Nishi Shinjuku",
            "rating": 4.6,
            "geometry": {"location": {"lat": 35.69, "lng": 139.69}},
            "price_level": 4,
        }
    ]
}


@respx.mock
@pytest.mark.asyncio
async def test_accommodations_both_succeed(api_keys, mock_flyai_client):
    from tools.search_accommodations import make_search_accommodations_tool

    respx.get("https://maps.googleapis.com/maps/api/place/textsearch/json").mock(
        return_value=Response(200, json=GOOGLE_PLACES_RESPONSE)
    )
    mock_flyai_client.search_hotel.return_value = [
        {
            "name": "全季酒店",
            "address": "东京新宿",
            "score": "4.2",
            "detailUrl": "https://fliggy.com/h/456",
            "price": "¥500",
            "star": "经济型",
            "latitude": "35.69",
            "longitude": "139.70",
        }
    ]

    tool_fn = make_search_accommodations_tool(api_keys, mock_flyai_client)
    result = await tool_fn(
        destination="东京", check_in="2026-07-15", check_out="2026-07-20"
    )
    assert len(result["accommodations"]) == 2  # different names → not merged


# ── get_poi_info fusion ───────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_poi_both_succeed(api_keys, mock_flyai_client):
    from tools.get_poi_info import make_get_poi_info_tool

    respx.get("https://maps.googleapis.com/maps/api/place/textsearch/json").mock(
        return_value=Response(
            200,
            json={
                "results": [
                    {
                        "name": "Fushimi Inari Shrine",
                        "formatted_address": "68 Fukakusa, Kyoto",
                        "rating": 4.6,
                        "geometry": {"location": {"lat": 34.97, "lng": 135.77}},
                        "types": ["place_of_worship"],
                    }
                ]
            },
        )
    )
    mock_flyai_client.search_poi.return_value = [
        {
            "name": "伏见稻荷大社",
            "address": "京都市伏見区",
            "score": "4.8",
            "category": "神社寺院",
            "freePoiStatus": True,
            "ticketInfo": {"price": None},
            "jumpUrl": "https://fliggy.com/p/789",
        }
    ]

    tool_fn = make_get_poi_info_tool(api_keys, mock_flyai_client)
    result = await tool_fn(query="伏见稻荷", location="京都")
    assert len(result["pois"]) >= 1
