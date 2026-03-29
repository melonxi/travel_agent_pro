# backend/tests/test_search_flights.py
import pytest
import respx
from httpx import Response

from config import ApiKeysConfig
from tools.search_flights import make_search_flights_tool


@pytest.fixture
def tool_fn():
    keys = ApiKeysConfig(amadeus_key="test_key")
    return make_search_flights_tool(keys)


@respx.mock
@pytest.mark.asyncio
async def test_search_flights(tool_fn):
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
    assert result["flights"][0]["price"] == "3500.00"
    assert result["source"] == "amadeus"
    assert result["origin"] == "PEK"


@pytest.mark.asyncio
async def test_no_api_key():
    keys = ApiKeysConfig(amadeus_key="")
    fn = make_search_flights_tool(keys)
    from tools.base import ToolError

    with pytest.raises(ToolError, match="API key"):
        await fn(origin="PEK", destination="NRT", date="2024-07-15")
