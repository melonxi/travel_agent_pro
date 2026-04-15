# backend/tools/web_search.py
from __future__ import annotations

import httpx
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "需要搜索的实时问题，建议写成完整意图，"
                "如 '东京迪士尼门票价格 2026'、'日本签证最新政策'、"
            ),
        },
        "search_depth": {
            "type": "string",
            "enum": ["basic", "advanced"],
            "description": "搜索深度提示。建议使用 basic 或 advanced。当前实现会原样透传给搜索服务，默认 basic。",
        },
        "max_results": {
            "type": "integer",
            "description": "期望返回的结果数量。当前实现会自动限制在 1 到 10，默认 5。",
        },
    },
    "required": ["query"],
}

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"


def make_web_search_tool(api_keys) -> object:
    tavily_key = api_keys.tavily if api_keys else ""

    @tool(
        name="web_search",
        description="""通用实时网络搜索工具，用于公开信息检索。
Use when:
  - 你需要最新价格、政策变动、开放变化、新闻型更新或通用攻略信息。
  - 现有专项工具不能直接回答，或者你需要补充更通用的外部公开信息。
Important:
  - 当前实现只支持 query、search_depth、max_results 三个输入。
  - 不支持域名白名单、官方站点限定、时间窗口过滤或结构化抽取。
  - max_results 会自动限制在 1 到 10。
        返回 Tavily 的简答和结果列表，包含标题、链接、摘要和分数。对于推荐型 query，它经常能直接给出可用的候选结论。""",
        phases=[1, 3],
        parameters=_PARAMETERS,
        human_label="上网查资料",
    )
    async def web_search(
        query: str,
        search_depth: str = "basic",
        max_results: int = 5,
    ) -> dict:
        if not tavily_key:
            raise ToolError(
                "Tavily API key not configured",
                error_code="MISSING_API_KEY",
                suggestion="Set TAVILY_API_KEY in .env or config.yaml.",
            )

        max_results = max(1, min(10, max_results))

        payload = {
            "api_key": tavily_key,
            "query": query,
            "search_depth": search_depth,
            "max_results": max_results,
            "include_answer": True,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(_TAVILY_SEARCH_URL, json=payload)
            if resp.status_code != 200:
                raise ToolError(
                    f"Tavily API error: {resp.status_code}",
                    error_code="API_ERROR",
                    suggestion="Check TAVILY_API_KEY or try again later.",
                )
            data = resp.json()

        results = []
        for item in data.get("results", []):
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": item.get("content", ""),
                    "score": item.get("score"),
                }
            )

        return {
            "query": query,
            "answer": data.get("answer", ""),
            "results": results,
            "source": "tavily",
        }

    return web_search
