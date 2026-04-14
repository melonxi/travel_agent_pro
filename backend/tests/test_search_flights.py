# backend/tests/test_search_flights.py
import pytest
import respx
from httpx import Response

from config import ApiKeysConfig
from tools.search_flights import make_search_flights_tool


@pytest.fixture
def tool_fn():
    keys = ApiKeysConfig(amadeus_key="test_key", amadeus_secret="test_secret")
    return make_search_flights_tool(keys)


@respx.mock
@pytest.mark.asyncio
async def test_search_flights(tool_fn):
    respx.post("https://test.api.amadeus.com/v1/security/oauth2/token").mock(
        return_value=Response(200, json={"access_token": "test_access_token"})
    )
    respx.post("https://test.api.amadeus.com/v2/shopping/flight-offers").mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {
                        "id": "1",
                        "price": {"total": "3500.00", "currency": "CNY"},
                        "itineraries": [{"duration": "PT3H30M"}],
                    },
                ]
            },
        )
    )
    result = await tool_fn(origin="PEK", destination="NRT", date="2024-07-15")
    assert len(result["flights"]) == 1
    assert result["flights"][0]["price"] == 3500.0
    assert result["flights"][0]["source"] == "amadeus"
    assert result["origin"] == "PEK"


@pytest.mark.asyncio
async def test_no_api_key():
    keys = ApiKeysConfig(amadeus_key="")
    fn = make_search_flights_tool(keys)
    from tools.base import ToolError

    with pytest.raises(ToolError, match="API key"):
        await fn(origin="PEK", destination="NRT", date="2024-07-15")


@respx.mock
@pytest.mark.asyncio
async def test_search_flights_uses_amadeus_oauth_token():
    keys = ApiKeysConfig(amadeus_key="client_id", amadeus_secret="client_secret")
    fn = make_search_flights_tool(keys)

    token_route = respx.post(
        "https://test.api.amadeus.com/v1/security/oauth2/token"
    ).mock(return_value=Response(200, json={"access_token": "oauth_token"}))
    offers_route = respx.post(
        "https://test.api.amadeus.com/v2/shopping/flight-offers"
    ).mock(return_value=Response(200, json={"data": []}))

    from tools.base import ToolError

    with pytest.raises(ToolError, match="No flight results"):
        await fn(origin="PEK", destination="NRT", date="2024-07-15")

    assert token_route.called
    assert offers_route.called
    token_request = token_route.calls[0].request
    assert token_request.headers["content-type"].startswith(
        "application/x-www-form-urlencoded"
    )
    assert b"grant_type=client_credentials" in token_request.content
    assert b"client_id=client_id" in token_request.content
    assert b"client_secret=client_secret" in token_request.content
    offers_request = offers_route.calls[0].request
    assert offers_request.headers["authorization"] == "Bearer oauth_token"


@pytest.mark.asyncio
async def test_surfaces_flyai_quota_error_when_no_other_source_available():
    from tools.base import ToolError

    class StubFlyAIClient:
        available = True

        async def search_flight(self, **kwargs):
            raise RuntimeError("Trial limit reached. Please configure FLYAI_API_KEY")

    keys = ApiKeysConfig(amadeus_key="")
    fn = make_search_flights_tool(keys, StubFlyAIClient())

    with pytest.raises(ToolError, match="Trial limit reached"):
        await fn(origin="PEK", destination="NRT", date="2024-07-15")
