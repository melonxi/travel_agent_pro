# backend/tests/test_search_flights.py
from unittest.mock import AsyncMock

import pytest

from config import ApiKeysConfig
from tools.base import ToolError
from tools.search_flights import make_search_flights_tool


@pytest.fixture
def flyai_client():
    client = AsyncMock()
    client.available = True
    client.search_flight.return_value = [
        {
            "adultPrice": "¥3500.0",
            "journeys": [
                {
                    "journeyType": "直达",
                    "segments": [
                        {
                            "marketingTransportName": "中国国际航空",
                            "marketingTransportNo": "CA1234",
                            "depCityName": "北京",
                            "arrCityName": "东京",
                            "depDateTime": "2024-07-15 08:00:00",
                            "arrDateTime": "2024-07-15 12:30:00",
                            "duration": "210分钟",
                            "seatClassName": "经济舱",
                        }
                    ],
                }
            ],
            "jumpUrl": "https://fliggy.com/f/1",
        }
    ]
    return client


@pytest.mark.asyncio
async def test_search_flights(flyai_client):
    keys = ApiKeysConfig()
    tool_fn = make_search_flights_tool(keys, flyai_client)
    result = await tool_fn(origin="PEK", destination="NRT", date="2024-07-15")
    assert len(result["flights"]) == 1
    assert result["flights"][0]["source"] == "flyai"
    assert result["origin"] == "PEK"
    flyai_client.search_flight.assert_awaited_once()
    call_kwargs = flyai_client.search_flight.await_args.kwargs
    assert call_kwargs["origin"] == "北京"
    assert call_kwargs["destination"] == "东京"


@pytest.mark.asyncio
async def test_no_flyai_client():
    keys = ApiKeysConfig()
    fn = make_search_flights_tool(keys, None)

    with pytest.raises(ToolError, match="FlyAI"):
        await fn(origin="PEK", destination="NRT", date="2024-07-15")


@pytest.mark.asyncio
async def test_flyai_unavailable():
    client = AsyncMock()
    client.available = False
    fn = make_search_flights_tool(ApiKeysConfig(), client)

    with pytest.raises(ToolError, match="FlyAI"):
        await fn(origin="PEK", destination="NRT", date="2024-07-15")


@pytest.mark.asyncio
async def test_surfaces_flyai_quota_error_when_no_other_source_available():
    class StubFlyAIClient:
        available = True

        async def search_flight(self, **kwargs):
            raise RuntimeError("Trial limit reached. Please configure FLYAI_API_KEY")

    fn = make_search_flights_tool(ApiKeysConfig(), StubFlyAIClient())

    with pytest.raises(ToolError, match="Trial limit reached"):
        await fn(origin="PEK", destination="NRT", date="2024-07-15")
