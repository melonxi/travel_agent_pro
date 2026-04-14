# backend/tests/test_search_trains.py
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from tools.base import ToolError
from tools.search_trains import make_search_trains_tool


FLYAI_TRAIN_RESPONSE = [
    {
        "adultPrice": "¥553.0",
        "journeys": [
            {
                "journeyType": "直达",
                "segments": [
                    {
                        "depCityName": "北京",
                        "depStationName": "北京南",
                        "depDateTime": "2026-04-15 08:00:00",
                        "arrCityName": "上海",
                        "arrStationName": "上海虹桥",
                        "arrDateTime": "2026-04-15 12:28:00",
                        "duration": "268分钟",
                        "transportType": "火车",
                        "marketingTransportNo": "G11",
                        "seatClassName": "二等座",
                    }
                ],
                "totalDuration": "268分钟",
            }
        ],
        "jumpUrl": "https://fliggy.com/t/123",
    }
]


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


@pytest.mark.asyncio
async def test_search_trains_success(mock_flyai_client):
    mock_flyai_client.search_train.return_value = FLYAI_TRAIN_RESPONSE

    tool_fn = make_search_trains_tool(mock_flyai_client)
    result = await tool_fn(origin="北京", destination="上海", date="2026-04-15")

    assert len(result["trains"]) == 1
    train = result["trains"][0]
    assert train["train_no"] == "G11"
    assert train["origin"] == "北京"
    assert train["origin_station"] == "北京南"
    assert train["destination"] == "上海"
    assert train["destination_station"] == "上海虹桥"
    assert train["duration_min"] == 268
    assert train["price"] == 553.0
    assert train["seat_class"] == "二等座"
    assert train["stops"] == 0
    assert train["booking_url"] == "https://fliggy.com/t/123"
    assert train["source"] == "flyai"


@pytest.mark.asyncio
async def test_search_trains_with_filters(mock_flyai_client):
    mock_flyai_client.search_train.return_value = FLYAI_TRAIN_RESPONSE

    tool_fn = make_search_trains_tool(mock_flyai_client)
    await tool_fn(
        origin="北京",
        destination="上海",
        date="2026-04-15",
        seat_class="second class",
        journey_type=1,
        sort_type=3,
        max_price=600,
    )

    mock_flyai_client.search_train.assert_called_once()
    call_kwargs = mock_flyai_client.search_train.call_args.kwargs
    assert call_kwargs["origin"] == "北京"
    assert call_kwargs["seat_class_name"] == "second class"
    assert call_kwargs["journey_type"] == 1
    assert call_kwargs["sort_type"] == 3
    assert call_kwargs["max_price"] == 600


@pytest.mark.asyncio
async def test_search_trains_no_results(mock_flyai_client):
    mock_flyai_client.search_train.return_value = []

    tool_fn = make_search_trains_tool(mock_flyai_client)
    with pytest.raises(ToolError, match="No train results"):
        await tool_fn(origin="北京", destination="拉萨", date="2026-04-15")


@pytest.mark.asyncio
async def test_search_trains_unavailable(mock_flyai_unavailable):
    tool_fn = make_search_trains_tool(mock_flyai_unavailable)
    with pytest.raises(ToolError, match="unavailable"):
        await tool_fn(origin="北京", destination="上海", date="2026-04-15")


@pytest.mark.asyncio
async def test_search_trains_propagates_flyai_runtime_error(mock_flyai_client):
    mock_flyai_client.search_train.side_effect = RuntimeError(
        "Trial limit reached. Please configure FLYAI_API_KEY"
    )

    tool_fn = make_search_trains_tool(mock_flyai_client)

    with pytest.raises(ToolError) as exc_info:
        await tool_fn(origin="北京", destination="上海", date="2026-04-15")

    error = exc_info.value
    assert "Trial limit reached" in str(error)
    assert error.error_code == "SERVICE_UNAVAILABLE"
    assert error.suggestion == "Check FlyAI CLI quota/auth status or retry later."
