# backend/tests/test_flyai_new_tools.py
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from tools.base import ToolError


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
async def test_quick_travel_search_normal(mock_flyai_client):
    from tools.quick_travel_search import make_quick_travel_search_tool

    mock_flyai_client.fast_search.return_value = [
        {
            "info": {
                "title": "杭州3日游",
                "price": "1500",
                "jumpUrl": "https://fliggy.com/1",
                "picUrl": "https://img.example.com/1.jpg",
            }
        },
        {
            "info": {
                "title": "西湖门票",
                "price": "0",
                "jumpUrl": "https://fliggy.com/2",
            }
        },
    ]

    tool_fn = make_quick_travel_search_tool(mock_flyai_client)
    result = await tool_fn(query="杭州三日游")
    assert len(result["results"]) == 2
    assert result["results"][0]["title"] == "杭州3日游"
    assert result["results"][0]["booking_url"] == "https://fliggy.com/1"
    assert result["results"][0]["image_url"] == "https://img.example.com/1.jpg"


@pytest.mark.asyncio
async def test_search_travel_services_visa(mock_flyai_client):
    from tools.search_travel_services import make_search_travel_services_tool

    mock_flyai_client.fast_search.return_value = [
        {
            "title": "日本签证代办",
            "price": "299",
            "jumpUrl": "https://fliggy.com/visa/1",
        },
    ]

    tool_fn = make_search_travel_services_tool(mock_flyai_client)
    result = await tool_fn(destination="日本", service_type="visa")
    assert len(result["services"]) == 1
    mock_flyai_client.fast_search.assert_called_once()
    # Verify the query contains visa-related keyword
    call_args = mock_flyai_client.fast_search.call_args
    assert "签证" in call_args.kwargs.get(
        "query", call_args.args[0] if call_args.args else ""
    )


@pytest.mark.asyncio
async def test_quick_travel_search_unavailable(mock_flyai_unavailable):
    from tools.quick_travel_search import make_quick_travel_search_tool

    tool_fn = make_quick_travel_search_tool(mock_flyai_unavailable)
    with pytest.raises(ToolError, match="unavailable"):
        await tool_fn(query="杭州三日游")


@pytest.mark.asyncio
async def test_quick_travel_search_propagates_flyai_runtime_error(mock_flyai_client):
    from tools.quick_travel_search import make_quick_travel_search_tool

    mock_flyai_client.fast_search.side_effect = RuntimeError(
        "Trial limit reached. Please configure FLYAI_API_KEY"
    )

    tool_fn = make_quick_travel_search_tool(mock_flyai_client)

    with pytest.raises(ToolError) as exc_info:
        await tool_fn(query="杭州三日游")

    assert "Trial limit reached" in str(exc_info.value)
    assert exc_info.value.error_code == "SERVICE_UNAVAILABLE"
    assert (
        exc_info.value.suggestion == "Check FlyAI CLI quota/auth status or retry later."
    )
