# backend/tests/test_ai_travel_search.py
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from tools.base import ToolError
from tools.ai_travel_search import make_ai_travel_search_tool


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
async def test_ai_travel_search_success(mock_flyai_client):
    mock_flyai_client.ai_search.return_value = (
        "推荐您五一去杭州，3天行程安排如下：Day1 西湖…"
    )

    tool_fn = make_ai_travel_search_tool(mock_flyai_client)
    result = await tool_fn(query="五一去杭州玩三天，预算人均2000")

    assert result["source"] == "flyai_ai_search"
    assert "杭州" in result["answer"]
    assert result["query"] == "五一去杭州玩三天，预算人均2000"
    mock_flyai_client.ai_search.assert_called_once_with(
        query="五一去杭州玩三天，预算人均2000"
    )


@pytest.mark.asyncio
async def test_ai_travel_search_empty(mock_flyai_client):
    mock_flyai_client.ai_search.return_value = ""

    tool_fn = make_ai_travel_search_tool(mock_flyai_client)
    with pytest.raises(ToolError, match="No AI search results"):
        await tool_fn(query="some query")


@pytest.mark.asyncio
async def test_ai_travel_search_unavailable(mock_flyai_unavailable):
    tool_fn = make_ai_travel_search_tool(mock_flyai_unavailable)
    with pytest.raises(ToolError, match="unavailable"):
        await tool_fn(query="五一去杭州")
