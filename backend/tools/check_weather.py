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


def _forecast_entry_payload(entry: dict) -> dict:
    return {
        "temp": entry.get("main", {}).get("temp"),
        "temp_min": entry.get("main", {}).get("temp_min"),
        "temp_max": entry.get("main", {}).get("temp_max"),
        "description": entry.get("weather", [{}])[0].get("description", ""),
        "humidity": entry.get("main", {}).get("humidity"),
        "wind_speed": entry.get("wind", {}).get("speed"),
    }


def make_check_weather_tool(api_keys: ApiKeysConfig):
    @tool(
        name="check_weather",
        description="""查询城市天气预报。
Use when: 用户在阶段 3 或 4，需要了解目的地天气情况。
Don't use when: 已有天气信息或不需要天气数据。
Important: city 参数必须使用英文名称（如 Tokyo 而非 东京），OpenWeather API 不支持中文城市名。
        返回城市天气预报，含温度、天气描述等。""",
        phases=[3, 4],
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
                **_forecast_entry_payload(matched),
                "source": "openweather_forecast",
                "exact_date_available": True,
                "requested_date": date,
                "reference_date": matched.get("dt_txt"),
            }
        else:
            # Keep nearest forecast as reference only. Do not expose its temp as
            # target-date weather; OpenWeather forecast is only a short window.
            first = forecast_list[0] if forecast_list else {}
            forecast = {
                "source": "openweather_nearest_reference",
                "exact_date_available": False,
                "requested_date": date,
                "reference_date": first.get("dt_txt") if first else None,
                "note": "精确日期预报不可用，近期预报仅作参考；请临近出发前再确认",
            }
            if first:
                forecast["reference"] = _forecast_entry_payload(first)

        return {"city": city, "date": date, "forecast": forecast}

    return check_weather_forecast
