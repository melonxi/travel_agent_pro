# backend/tools/quick_travel_search.py
from __future__ import annotations

from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "自然语言旅行搜索，如 '杭州三日游' '法国签证' '上海邮轮'",
        },
    },
    "required": ["query"],
}


def make_quick_travel_search_tool(flyai_client):
    @tool(
        name="quick_travel_search",
        description="""跨品类快速搜索旅行产品（机票、酒店、门票、跟团游、签证等）。
Use when: 用户在阶段 2-3，需要快速了解某个目的地的旅行产品概览和价格范围。
Don't use when: 已确定具体出行方案，应使用专项搜索工具。
返回多品类产品列表，含标题、价格和预订链接。""",
        phases=[2, 3],
        parameters=_PARAMETERS,
    )
    async def quick_travel_search(query: str) -> dict:
        if not flyai_client or not flyai_client.available:
            raise ToolError(
                "FlyAI service unavailable",
                error_code="SERVICE_UNAVAILABLE",
                suggestion="Use search_destinations for destination research instead.",
            )

        raw_list = await flyai_client.fast_search(query=query)

        results = []
        for item in raw_list:
            results.append(
                {
                    "title": item.get("title", ""),
                    "price": item.get("price"),
                    "booking_url": item.get("jumpUrl") or item.get("detailUrl"),
                    "image_url": item.get("picUrl") or item.get("mainPic"),
                }
            )

        return {"results": results, "query": query, "source": "flyai"}

    return quick_travel_search
