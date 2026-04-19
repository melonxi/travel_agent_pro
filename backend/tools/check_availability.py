# backend/tools/check_availability.py
from __future__ import annotations

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "place_name": {
            "type": "string",
            "description": "地点名称，如 '金阁寺' '卢浮宫'",
        },
        "date": {"type": "string", "description": "查询日期，如 '2024-07-15'"},
    },
    "required": ["place_name", "date"],
}


def make_check_availability_tool(api_keys: ApiKeysConfig):
    @tool(
        name="check_availability",
        description="""查询地点在指定日期是否开放。
Use when: 用户在阶段 4-5，需要确认景点的开放状态。
Don't use when: 已知开放时间或不需要确认。
        返回开放状态和营业时间。""",
        phases=[3],
        parameters=_PARAMETERS,
        human_label="查景点可用性",
    )
    async def check_availability(place_name: str, date: str) -> dict:
        if not api_keys.google_maps:
            raise ToolError(
                "Google Maps API key not configured",
                error_code="NO_API_KEY",
                suggestion="Set GOOGLE_MAPS_API_KEY",
            )

        # Step 1: Find the place
        async with httpx.AsyncClient() as client:
            find_resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
                params={
                    "input": place_name,
                    "inputtype": "textquery",
                    "fields": "place_id,name",
                    "key": api_keys.google_maps,
                },
                timeout=10,
            )
            find_resp.raise_for_status()
            find_data = find_resp.json()

        candidates = find_data.get("candidates", [])
        if not candidates:
            return {
                "place_name": place_name,
                "date": date,
                "likely_open": False,
                "hours": "未找到该地点",
            }

        place_id = candidates[0].get("place_id", "")

        # Step 2: Get place details for opening hours
        async with httpx.AsyncClient() as client:
            detail_resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/details/json",
                params={
                    "place_id": place_id,
                    "fields": "opening_hours,name",
                    "key": api_keys.google_maps,
                },
                timeout=10,
            )
            detail_resp.raise_for_status()
            detail_data = detail_resp.json()

        result = detail_data.get("result", {})
        opening_hours = result.get("opening_hours", {})
        is_open = opening_hours.get("open_now", False)
        periods = opening_hours.get("weekday_text", [])

        return {
            "place_name": place_name,
            "date": date,
            "likely_open": is_open,
            "hours": periods if periods else "营业时间未知",
        }

    return check_availability
