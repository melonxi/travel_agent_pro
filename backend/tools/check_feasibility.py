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
            "description": "要做轻量可行性补充的目的地名称，建议传城市或地区名，如 '东京' '巴厘岛'。",
        },
        "travel_date": {
            "type": "string",
            "description": "用户计划出行日期，如 '2024-07-15'。当前实现会原样回显该字段，但不会据此查询未来天气。",
        },
    },
    "required": ["destination", "travel_date"],
}


def make_check_feasibility_tool(api_keys: ApiKeysConfig):
    @tool(
        name="check_feasibility",
        description="""对已明确目的地做轻量可行性补充。
Use when:
  - 目的地已经明确，且你只需要一个快速 sanity check。
  - 你想补一个基础天气参考，帮助用户判断是否值得继续考虑这个目的地。
Don't use when:
  - 你需要基于具体出行日期的未来天气判断。
  - 你需要真实签证政策、季节风险、节假日拥挤度或更严肃的可行性评估。
Important:
  - 当前实现调用的是当前天气接口，不是按 travel_date 查询未来天气。
  - visa_info 当前是固定提示。
  - feasible 当前固定返回 true，更适合做轻量参考而不是最终结论。
返回目的地、传入日期、天气摘要和基础可行性字段。""",
        phases=[],
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
