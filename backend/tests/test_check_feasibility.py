# backend/tests/test_check_feasibility.py
import pytest
import respx
from httpx import Response

from config import ApiKeysConfig
from tools.check_feasibility import make_check_feasibility_tool


@pytest.fixture
def tool_fn():
    keys = ApiKeysConfig(openweather="test_key")
    return make_check_feasibility_tool(keys)


@respx.mock
@pytest.mark.asyncio
async def test_check_feasibility(tool_fn):
    respx.get("https://api.openweathermap.org/data/2.5/weather").mock(
        return_value=Response(
            200,
            json={
                "main": {"temp": 28.5, "humidity": 75},
                "weather": [{"description": "scattered clouds"}],
            },
        )
    )
    result = await tool_fn(destination="东京", travel_date="2024-07-15")
    assert result["destination"] == "东京"
    assert result["feasible"] is True
    assert result["weather"]["temp"] == 28.5
    assert result["visa_info"] == "请自行查询签证要求"


@pytest.mark.asyncio
async def test_no_api_key():
    keys = ApiKeysConfig(openweather="")
    fn = make_check_feasibility_tool(keys)
    from tools.base import ToolError

    with pytest.raises(ToolError, match="API key"):
        await fn(destination="test", travel_date="2024-01-01")
