# backend/tools/search_destinations.py
from __future__ import annotations

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "搜索关键词，如 '海岛度假' '日本文化'",
        },
        "preferences": {
            "type": "array",
            "items": {"type": "string"},
            "description": "用户偏好标签，如 ['美食', '文化', '海滩']",
        },
    },
    "required": ["query"],
}


def make_search_destinations_tool(api_keys: ApiKeysConfig):
    @tool(
        name="search_destinations",
        description="""搜索匹配用户意愿的旅行目的地。
Use when: 用户在阶段 2，需要目的地推荐或对比。
Don't use when: 目的地已确定。
返回 2-5 个目的地候选，含基本信息和匹配度说明。""",
        phases=[2],
        parameters=_PARAMETERS,
    )
    async def search_destinations(
        query: str, preferences: list[str] | None = None
    ) -> dict:
        if not api_keys.google_maps:
            raise ToolError(
                "Google Maps API key not configured",
                error_code="NO_API_KEY",
                suggestion="Set GOOGLE_MAPS_API_KEY",
            )

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={
                    "query": f"{query} travel destination",
                    "key": api_keys.google_maps,
                    "type": "locality",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for place in data.get("results", [])[:5]:
            results.append(
                {
                    "name": place.get("name", ""),
                    "formatted_address": place.get("formatted_address", ""),
                    "rating": place.get("rating"),
                    "location": place.get("geometry", {}).get("location", {}),
                }
            )

        return {"destinations": results, "source": "google_places", "query": query}

    return search_destinations
