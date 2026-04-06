# backend/tests/test_web_search.py
import pytest
import respx
from httpx import Response

from config import ApiKeysConfig
from tools.base import ToolError
from tools.web_search import make_web_search_tool


@pytest.fixture
def tool_fn():
    keys = ApiKeysConfig(tavily="test_tavily_key")
    return make_web_search_tool(keys)


@respx.mock
@pytest.mark.asyncio
async def test_web_search_basic(tool_fn):
    respx.post("https://api.tavily.com/search").mock(
        return_value=Response(
            200,
            json={
                "answer": "东京迪士尼门票约 7900 日元。",
                "results": [
                    {
                        "title": "东京迪士尼票价",
                        "url": "https://example.com/disney",
                        "content": "成人一日票 7900 日元",
                        "score": 0.95,
                    }
                ],
            },
        )
    )
    result = await tool_fn(query="东京迪士尼门票价格")
    assert result["query"] == "东京迪士尼门票价格"
    assert result["answer"] == "东京迪士尼门票约 7900 日元。"
    assert len(result["results"]) == 1
    assert result["results"][0]["title"] == "东京迪士尼票价"
    assert result["source"] == "tavily"


@respx.mock
@pytest.mark.asyncio
async def test_web_search_sends_correct_payload(tool_fn):
    route = respx.post("https://api.tavily.com/search").mock(
        return_value=Response(200, json={"answer": "", "results": []})
    )
    await tool_fn(query="日本签证", search_depth="advanced", max_results=3)
    assert route.called
    body = route.calls[0].request.read()
    import json

    payload = json.loads(body)
    assert payload["query"] == "日本签证"
    assert payload["search_depth"] == "advanced"
    assert payload["max_results"] == 3
    assert payload["include_answer"] is True


@respx.mock
@pytest.mark.asyncio
async def test_web_search_api_error(tool_fn):
    respx.post("https://api.tavily.com/search").mock(
        return_value=Response(401, json={"error": "Unauthorized"})
    )
    with pytest.raises(ToolError, match="Tavily API error"):
        await tool_fn(query="test")


@pytest.mark.asyncio
async def test_web_search_no_api_key():
    keys = ApiKeysConfig(tavily="")
    fn = make_web_search_tool(keys)
    with pytest.raises(ToolError, match="Tavily API key not configured"):
        await fn(query="test")


@respx.mock
@pytest.mark.asyncio
async def test_web_search_empty_results(tool_fn):
    respx.post("https://api.tavily.com/search").mock(
        return_value=Response(200, json={"answer": "未找到结果", "results": []})
    )
    result = await tool_fn(query="不存在的搜索词")
    assert result["results"] == []
    assert result["answer"] == "未找到结果"
