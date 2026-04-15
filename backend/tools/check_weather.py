# backend/tools/check_weather.py
from __future__ import annotations

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "city": {
            "type": "string",
            "description": "城市英文名称（必须用英文），如 'Tokyo' 'Paris' 'Beijing'",
        },
        "date": {"type": "string", "description": "查询日期，如 '2024-07-15'"},
    },
    "required": ["city", "date"],
}


def make_check_weather_tool(api_keys: ApiKeysConfig):
    @tool(
        name="check_weather",
        description="""查询城市天气预报。
Use when: 用户在阶段 5 或 7，需要了解目的地天气情况。
Don't use when: 已有天气信息或不需要天气数据。
Important: city 参数必须使用英文名称（如 Tokyo 而非 东京），OpenWeather API 不支持中文城市名。
        返回城市天气预报，含温度、天气描述等。""",
        phases=[5, 7],
        parameters=_PARAMETERS,
        human_label="查天气",
    )
    async def check_weather_forecast(city: str, date: str) -> dict:
        if not api_keys.openweather:
            raise ToolError(
                "OpenWeather API key not configured",
                error_code="NO_API_KEY",
                suggestion="Set OPENWEATHER_API_KEY",
            )

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.openweathermap.org/data/2.5/forecast",
                params={
                    "q": city,
                    "appid": api_keys.openweather,
                    "units": "metric",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

        # Find the closest forecast entry to the requested date
        forecast_list = data.get("list", [])
        matched = None
        for entry in forecast_list:
            if entry.get("dt_txt", "").startswith(date):
                matched = entry
                break

        if matched:
            forecast = {
                "temp": matched.get("main", {}).get("temp"),
                "temp_min": matched.get("main", {}).get("temp_min"),
                "temp_max": matched.get("main", {}).get("temp_max"),
                "description": matched.get("weather", [{}])[0].get("description", ""),
                "humidity": matched.get("main", {}).get("humidity"),
                "wind_speed": matched.get("wind", {}).get("speed"),
            }
        else:
            # Return first available entry as general reference
            first = forecast_list[0] if forecast_list else {}
            forecast = {
                "temp": first.get("main", {}).get("temp"),
                "description": first.get("weather", [{}])[0].get("description", ""),
                "note": "精确日期预报不可用，返回最近预报作为参考",
            }

        return {"city": city, "date": date, "forecast": forecast}

    return check_weather_forecast
