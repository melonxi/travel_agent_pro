# backend/tools/quick_travel_search.py
from __future__ import annotations

from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "单个自然语言旅行搜索词，建议把目的地和意图写在一起，"
                "如 '杭州三日游'、'法国签证'、'东京 旅行产品'。"
                "当前没有单独的日期、预算、品类过滤参数。"
            ),
        },
    },
    "required": ["query"],
}


def make_quick_travel_search_tool(flyai_client):
    @tool(
        name="quick_travel_search",
        description="""对一个自然语言旅行需求做跨品类快速扫面，返回混合旅行产品卡片。
Use when:
  - 你想快速感知某个目的地或主题的大致产品形态、价格带和供给面。
  - 阶段 1 里需要粗略判断“这个地方大概卖什么、贵不贵、产品多不多”。
Don't use when:
  - 你需要结构化查询航班、酒店、景点详情。
  - 你需要按日期、预算、位置等条件做精确筛选。
Important:
  - 返回结果可能是门票、酒店、签证、跟团游等跨品类混合列表。
  - 这不是结构化预订搜索器，更适合快速扫面而不是精确决策。
        返回标题、价格、预订链接和图片链接。""",
        phases=[1, 3],
        parameters=_PARAMETERS,
        human_label="快速查行程价格",
    )
    async def quick_travel_search(query: str) -> dict:
        if not flyai_client or not flyai_client.available:
            raise ToolError(
                "FlyAI service unavailable",
                error_code="SERVICE_UNAVAILABLE",
                suggestion="Use web_search or xiaohongshu_search for destination research instead.",
            )

        try:
            raw_list = await flyai_client.fast_search(query=query)
        except RuntimeError as exc:
            raise ToolError(
                str(exc),
                error_code="SERVICE_UNAVAILABLE",
                suggestion="Check FlyAI CLI quota/auth status or retry later.",
            ) from exc

        results = []
        for item in raw_list:
            payload = item.get("info") if isinstance(item.get("info"), dict) else item
            results.append(
                {
                    "title": payload.get("title", ""),
                    "price": payload.get("price"),
                    "booking_url": payload.get("jumpUrl") or payload.get("detailUrl"),
                    "image_url": payload.get("picUrl") or payload.get("mainPic"),
                }
            )

        return {"results": results, "query": query, "source": "flyai"}

    return quick_travel_search
