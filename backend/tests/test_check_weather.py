# backend/tests/test_check_weather.py
import pytest
import respx
from httpx import Response

from config import ApiKeysConfig
from tools.check_weather import make_check_weather_tool


@pytest.fixture
def tool_fn():
    keys = ApiKeysConfig(openweather="test_key")
    return make_check_weather_tool(keys)


@respx.mock
@pytest.mark.asyncio
async def test_check_weather(tool_fn):
    respx.get("https://api.openweathermap.org/data/2.5/forecast").mock(
        return_value=Response(
            200,
            json={
                "list": [
                    {
                        "dt_txt": "2024-07-15 12:00:00",
                        "main": {
                            "temp": 30.2,
                            "temp_min": 27.0,
                            "temp_max": 33.0,
                            "humidity": 65,
                        },
                        "weather": [{"description": "clear sky"}],
                        "wind": {"speed": 3.5},
                    },
                ]
            },
        )
    )
    result = await tool_fn(city="东京", date="2024-07-15")
    assert result["city"] == "东京"
    assert result["date"] == "2024-07-15"
    assert result["forecast"]["temp"] == 30.2
    assert result["forecast"]["description"] == "clear sky"
    assert result["forecast"]["source"] == "openweather_forecast"
    assert result["forecast"]["exact_date_available"] is True
    assert result["forecast"]["requested_date"] == "2024-07-15"
    assert result["forecast"]["reference_date"] == "2024-07-15 12:00:00"


@respx.mock
@pytest.mark.asyncio
async def test_check_weather_unmatched_date_is_reference_only(tool_fn):
    respx.get("https://api.openweathermap.org/data/2.5/forecast").mock(
        return_value=Response(
            200,
            json={
                "list": [
                    {
                        "dt_txt": "2024-06-01 12:00:00",
                        "main": {
                            "temp": 18.2,
                            "temp_min": 17.0,
                            "temp_max": 20.0,
                            "humidity": 70,
                        },
                        "weather": [{"description": "light rain"}],
                        "wind": {"speed": 2.5},
                    },
                ]
            },
        )
    )

    result = await tool_fn(city="Tokyo", date="2024-07-15")
    forecast = result["forecast"]

    assert forecast["source"] == "openweather_nearest_reference"
    assert forecast["exact_date_available"] is False
    assert forecast["requested_date"] == "2024-07-15"
    assert forecast["reference_date"] == "2024-06-01 12:00:00"
    assert "精确日期预报不可用" in forecast["note"]
    assert "temp" not in forecast
    assert forecast["reference"]["temp"] == 18.2
    assert forecast["reference"]["description"] == "light rain"


@pytest.mark.asyncio
async def test_no_api_key():
    keys = ApiKeysConfig(openweather="")
    fn = make_check_weather_tool(keys)
    from tools.base import ToolError

    with pytest.raises(ToolError, match="API key"):
        await fn(city="test", date="2024-01-01")
