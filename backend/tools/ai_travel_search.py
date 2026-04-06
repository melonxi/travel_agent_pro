# backend/tools/ai_travel_search.py
from __future__ import annotations

from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "自然语言旅行需求，可包含目的地、日期、预算、人数、偏好等复杂约束。"
                "例如 '五一去杭州玩三天，预算人均2000，想住西湖附近'。"
            ),
        },
    },
    "required": ["query"],
}


def make_ai_travel_search_tool(flyai_client):
    @tool(
        name="ai_travel_search",
        description="""AI 语义旅行搜索，理解复杂自然语言意图并返回综合旅行建议。
Use when:
  - 用户描述了一个复杂的旅行需求（目的地+日期+预算+偏好的组合）。
  - 阶段 1 需要做整体旅行方案探索，或阶段 3 需要对比多个维度的选项。
Don't use when:
  - 用户只需要查具体航班、酒店、火车等单一品类 → 用专门工具。
  - 只是简单关键词搜索 → 用 quick_travel_search。
返回 AI 生成的综合旅行建议文本。""",
        phases=[1, 3],
        parameters=_PARAMETERS,
    )
    async def ai_travel_search(query: str) -> dict:
        if not flyai_client or not flyai_client.available:
            raise ToolError(
                "FlyAI service unavailable — cannot perform AI search",
                error_code="SERVICE_UNAVAILABLE",
                suggestion="AI travel search requires flyai CLI. Install with: npm i -g @fly-ai/flyai-cli",
            )

        result = await flyai_client.ai_search(query=query)

        if not result:
            raise ToolError(
                f"No AI search results for: {query}",
                error_code="NO_RESULTS",
                suggestion="Try rephrasing your query with more details",
            )

        return {
            "answer": result,
            "query": query,
            "source": "flyai_ai_search",
        }

    return ai_travel_search
