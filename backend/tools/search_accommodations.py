# backend/tools/search_accommodations.py
from __future__ import annotations

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "destination": {
            "type": "string",
            "description": "目的地名称，如 '东京' '巴黎'",
        },
        "check_in": {"type": "string", "description": "入住日期，如 '2024-07-15'"},
        "check_out": {"type": "string", "description": "退房日期，如 '2024-07-20'"},
        "budget_per_night": {"type": "number", "description": "每晚预算（美元）"},
        "area": {"type": "string", "description": "偏好区域，如 '市中心' '海滨'"},
        "requirements": {
            "type": "array",
            "items": {"type": "string"},
            "description": "特殊要求，如 ['含早餐', '有泳池', '可停车']",
        },
    },
    "required": ["destination", "check_in", "check_out"],
}


def make_search_accommodations_tool(api_keys: ApiKeysConfig):
    @tool(
        name="search_accommodations",
        description="""搜索住宿信息。
Use when: 用户在阶段 3-4，需要查询住宿选项。
Don't use when: 住宿已确定。
返回住宿列表，含评分、价格和位置信息。""",
        phases=[3, 4],
        parameters=_PARAMETERS,
    )
    async def search_accommodations(
        destination: str,
        check_in: str,
        check_out: str,
        budget_per_night: float | None = None,
        area: str | None = None,
        requirements: list[str] | None = None,
    ) -> dict:
        if not api_keys.google_maps:
            raise ToolError(
                "Google Maps API key not configured",
                error_code="NO_API_KEY",
                suggestion="Set GOOGLE_MAPS_API_KEY",
            )

        query = f"hotel lodging in {destination}"
        if area:
            query += f" {area}"

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={
                    "query": query,
                    "key": api_keys.google_maps,
                    "type": "lodging",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

        accommodations = []
        for place in data.get("results", [])[:5]:
            accommodations.append(
                {
                    "name": place.get("name", ""),
                    "formatted_address": place.get("formatted_address", ""),
                    "rating": place.get("rating"),
                    "location": place.get("geometry", {}).get("location", {}),
                    "price_level": place.get("price_level"),
                }
            )

        return {
            "accommodations": accommodations,
            "source": "google_places",
            "destination": destination,
            "check_in": check_in,
            "check_out": check_out,
        }

    return search_accommodations
