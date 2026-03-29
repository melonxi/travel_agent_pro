# backend/tools/get_poi_info.py
from __future__ import annotations

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "景点/POI 搜索关键词，如 '金阁寺' '卢浮宫'",
        },
        "location": {
            "type": "string",
            "description": "限定搜索范围的城市或地区，如 '京都' '巴黎'",
        },
    },
    "required": ["query"],
}


def make_get_poi_info_tool(api_keys: ApiKeysConfig):
    @tool(
        name="get_poi_info",
        description="""获取景点/兴趣点详细信息。
Use when: 用户在阶段 3-5，需要了解某个景点的详情。
Don't use when: 已有该景点的完整信息。
返回景点列表，含名称、地址、评分和位置。""",
        phases=[3, 4, 5],
        parameters=_PARAMETERS,
    )
    async def get_poi_info(query: str, location: str | None = None) -> dict:
        if not api_keys.google_maps:
            raise ToolError(
                "Google Maps API key not configured",
                error_code="NO_API_KEY",
                suggestion="Set GOOGLE_MAPS_API_KEY",
            )

        search_query = query
        if location:
            search_query += f" in {location}"

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={
                    "query": search_query,
                    "key": api_keys.google_maps,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

        pois = []
        for place in data.get("results", [])[:5]:
            pois.append(
                {
                    "name": place.get("name", ""),
                    "formatted_address": place.get("formatted_address", ""),
                    "rating": place.get("rating"),
                    "location": place.get("geometry", {}).get("location", {}),
                    "types": place.get("types", []),
                }
            )

        return {"pois": pois, "source": "google_places", "query": query}

    return get_poi_info
