# backend/tests/test_check_availability.py
import pytest
import respx
from httpx import Response

from config import ApiKeysConfig
from tools.check_availability import make_check_availability_tool


@pytest.fixture
def tool_fn():
    keys = ApiKeysConfig(google_maps="test_key")
    return make_check_availability_tool(keys)


@respx.mock
@pytest.mark.asyncio
async def test_check_availability(tool_fn):
    respx.get("https://maps.googleapis.com/maps/api/place/findplacefromtext/json").mock(
        return_value=Response(
            200, json={"candidates": [{"place_id": "abc123", "name": "Kinkaku-ji"}]}
        )
    )
    respx.get("https://maps.googleapis.com/maps/api/place/details/json").mock(
        return_value=Response(
            200,
            json={
                "result": {
                    "name": "Kinkaku-ji",
                    "opening_hours": {
                        "open_now": True,
                        "weekday_text": ["Monday: 9:00 AM – 5:00 PM"],
                    },
                }
            },
        )
    )
    result = await tool_fn(place_name="金阁寺", date="2024-07-15")
    assert result["place_name"] == "金阁寺"
    assert result["likely_open"] is True
    assert isinstance(result["hours"], list)


@pytest.mark.asyncio
async def test_no_api_key():
    keys = ApiKeysConfig(google_maps="")
    fn = make_check_availability_tool(keys)
    from tools.base import ToolError

    with pytest.raises(ToolError, match="API key"):
        await fn(place_name="test", date="2024-01-01")
