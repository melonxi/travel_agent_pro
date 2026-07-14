# backend/tests/test_search_trains.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tools.base import ToolError
from tools.search_trains import make_search_trains_tool
from tools.train12306_client import Train12306Client, Train12306Error


QUERY_RESULT = {
    "trains": [
        {
            "train_code": "G11",
            "from_station": "北京南",
            "to_station": "上海虹桥",
            "start_time": "08:00",
            "arrive_time": "12:28",
            "duration": "04:28",
            "bookable": True,
            "seats": {"二等座": "有", "一等座": "12"},
        },
        {
            "train_code": "D717",
            "from_station": "北京",
            "to_station": "上海",
            "start_time": "19:36",
            "arrive_time": "07:22",
            "duration": "11:46",
            "bookable": True,
            "seats": {"软卧/一等卧": "有"},
        },
        {
            "train_code": "T109",
            "from_station": "北京",
            "to_station": "上海",
            "start_time": "20:05",
            "arrive_time": "10:50",
            "duration": "14:45",
            "bookable": False,
            "seats": {},
        },
    ],
    "from_alternatives": ["北京南", "北京西"],
    "to_alternatives": ["上海虹桥"],
}


def _patched_query(result=None, error: Exception | None = None):
    mock = AsyncMock()
    if error is not None:
        mock.side_effect = error
    else:
        mock.return_value = result
    return patch.object(Train12306Client, "query_left_tickets", mock), mock


@pytest.mark.asyncio
async def test_search_trains_success():
    patcher, mock = _patched_query(QUERY_RESULT)
    with patcher:
        tool_fn = make_search_trains_tool()
        result = await tool_fn(origin="北京", destination="上海", date="2026-04-15")

    mock.assert_awaited_once_with("北京", "上海", "2026-04-15")
    assert result["source"] == "12306"
    assert result["total_found"] == 3
    train = result["trains"][0]
    assert train["train_code"] == "G11"
    assert train["from_station"] == "北京南"
    assert train["seats"]["二等座"] == "有"
    assert result["origin_station_alternatives"] == ["北京南", "北京西"]


@pytest.mark.asyncio
async def test_search_trains_train_type_filter():
    patcher, _ = _patched_query(QUERY_RESULT)
    with patcher:
        tool_fn = make_search_trains_tool()
        result = await tool_fn(
            origin="北京", destination="上海", date="2026-04-15", train_types="GD"
        )

    codes = [t["train_code"] for t in result["trains"]]
    assert codes == ["G11", "D717"]


@pytest.mark.asyncio
async def test_search_trains_time_window_filter():
    patcher, _ = _patched_query(QUERY_RESULT)
    with patcher:
        tool_fn = make_search_trains_tool()
        result = await tool_fn(
            origin="北京",
            destination="上海",
            date="2026-04-15",
            earliest_start="19:00",
            latest_start="20:00",
        )

    codes = [t["train_code"] for t in result["trains"]]
    assert codes == ["D717"]


@pytest.mark.asyncio
async def test_search_trains_no_results():
    patcher, _ = _patched_query(
        {"trains": [], "from_alternatives": [], "to_alternatives": []}
    )
    with patcher:
        tool_fn = make_search_trains_tool()
        with pytest.raises(ToolError, match="No train results"):
            await tool_fn(origin="北京", destination="拉萨", date="2026-04-15")


@pytest.mark.asyncio
async def test_search_trains_station_not_found():
    patcher, _ = _patched_query(
        error=Train12306Error("未找到车站: 不存在站", code="STATION_NOT_FOUND")
    )
    with patcher:
        tool_fn = make_search_trains_tool()
        with pytest.raises(ToolError) as exc_info:
            await tool_fn(origin="不存在站", destination="上海", date="2026-04-15")

    assert exc_info.value.error_code == "STATION_NOT_FOUND"


@pytest.mark.asyncio
async def test_search_trains_service_unavailable():
    patcher, _ = _patched_query(error=Train12306Error("12306 查询失败: timeout"))
    with patcher:
        tool_fn = make_search_trains_tool()
        with pytest.raises(ToolError) as exc_info:
            await tool_fn(origin="北京", destination="上海", date="2026-04-15")

    error = exc_info.value
    assert error.error_code == "SERVICE_UNAVAILABLE"
    assert "web_search" in (error.suggestion or "")
