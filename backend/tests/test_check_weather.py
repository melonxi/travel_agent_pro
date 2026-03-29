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


@pytest.mark.asyncio
async def test_no_api_key():
    keys = ApiKeysConfig(openweather="")
    fn = make_check_weather_tool(keys)
    from tools.base import ToolError

    with pytest.raises(ToolError, match="API key"):
        await fn(city="test", date="2024-01-01")
