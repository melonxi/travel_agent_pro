# backend/tools/calculate_route.py
from __future__ import annotations

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "origin_lat": {"type": "number", "description": "起点纬度"},
        "origin_lng": {"type": "number", "description": "起点经度"},
        "dest_lat": {"type": "number", "description": "终点纬度"},
        "dest_lng": {"type": "number", "description": "终点经度"},
        "mode": {
            "type": "string",
            "description": "出行方式: driving, walking, bicycling, transit",
            "default": "transit",
        },
    },
    "required": ["origin_lat", "origin_lng", "dest_lat", "dest_lng"],
}


def make_calculate_route_tool(api_keys: ApiKeysConfig):
    @tool(
        name="calculate_route",
        description="""计算两点之间的路线。
Use when: 用户在阶段 4-5，需要计算景点之间的路线和时间。
Don't use when: 不需要路线规划。
        返回距离、时长和路线步骤。""",
        phases=[3, 5],
        parameters=_PARAMETERS,
        human_label="规划路线",
    )
    async def calculate_route(
        origin_lat: float,
        origin_lng: float,
        dest_lat: float,
        dest_lng: float,
        mode: str = "transit",
    ) -> dict:
        if not api_keys.google_maps:
            raise ToolError(
                "Google Maps API key not configured",
                error_code="NO_API_KEY",
                suggestion="Set GOOGLE_MAPS_API_KEY",
            )

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/directions/json",
                params={
                    "origin": f"{origin_lat},{origin_lng}",
                    "destination": f"{dest_lat},{dest_lng}",
                    "mode": mode,
                    "key": api_keys.google_maps,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

        routes = data.get("routes", [])
        if not routes:
            return {"distance": "", "duration": "", "steps": [], "mode": mode}

        leg = routes[0].get("legs", [{}])[0]
        steps = []
        for step in leg.get("steps", []):
            steps.append(
                {
                    "instruction": step.get("html_instructions", ""),
                    "distance": step.get("distance", {}).get("text", ""),
                    "duration": step.get("duration", {}).get("text", ""),
                }
            )

        return {
            "distance": leg.get("distance", {}).get("text", ""),
            "duration": leg.get("duration", {}).get("text", ""),
            "steps": steps,
            "mode": mode,
        }

    return calculate_route
