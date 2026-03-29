# backend/tools/check_feasibility.py
from __future__ import annotations

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "destination": {
            "type": "string",
            "description": "目的地名称，如 '东京' '巴厘岛'",
        },
        "travel_date": {
            "type": "string",
            "description": "计划出行日期，如 '2024-07-15'",
        },
    },
    "required": ["destination", "travel_date"],
}


def make_check_feasibility_tool(api_keys: ApiKeysConfig):
    @tool(
        name="check_feasibility",
        description="""检查旅行目的地的可行性，包括天气和基本信息。
Use when: 用户在阶段 2，需要评估目的地是否适合出行。
Don't use when: 已完成可行性分析。
返回天气信息、签证提示和可行性评估。""",
        phases=[2],
        parameters=_PARAMETERS,
    )
    async def check_travel_feasibility(destination: str, travel_date: str) -> dict:
        if not api_keys.openweather:
            raise ToolError(
                "OpenWeather API key not configured",
                error_code="NO_API_KEY",
                suggestion="Set OPENWEATHER_API_KEY",
            )

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={
                    "q": destination,
                    "appid": api_keys.openweather,
                    "units": "metric",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

        weather = {
            "temp": data.get("main", {}).get("temp"),
            "description": data.get("weather", [{}])[0].get("description", ""),
            "humidity": data.get("main", {}).get("humidity"),
        }

        return {
            "destination": destination,
            "travel_date": travel_date,
            "visa_info": "请自行查询签证要求",
            "weather": weather,
            "feasible": True,
        }

    return check_travel_feasibility
