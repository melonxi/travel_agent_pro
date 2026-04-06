# backend/tools/search_trains.py
from __future__ import annotations

import logging

from tools.base import ToolError, tool
from tools.normalizers import normalize_flyai_train

logger = logging.getLogger(__name__)

_PARAMETERS = {
    "type": "object",
    "properties": {
        "origin": {
            "type": "string",
            "description": "出发城市或车站名，如 '北京' '上海虹桥'",
        },
        "destination": {
            "type": "string",
            "description": "目的地城市或车站名，如 '上海' '杭州'",
        },
        "date": {"type": "string", "description": "出发日期，如 '2026-04-15'"},
        "seat_class": {
            "type": "string",
            "description": "席别：second class / first class / business class / hard sleeper / soft sleeper",
        },
        "journey_type": {
            "type": "integer",
            "description": "1=直达, 2=中转",
        },
        "sort_type": {
            "type": "integer",
            "description": "排序：1=价格降序 2=推荐 3=价格升序 4=耗时升序 5=耗时降序 6=出发早→晚 7=出发晚→早 8=直达优先",
        },
        "max_price": {
            "type": "number",
            "description": "最高票价（元）",
        },
    },
    "required": ["origin", "destination", "date"],
}


def make_search_trains_tool(flyai_client):
    @tool(
        name="search_trains",
        description="""搜索火车/高铁车次信息。
Use when: 用户在阶段 3-4，需要查询火车或高铁出行方案（国内城市间交通）。
Don't use when: 用户明确要坐飞机，或目的地不通火车。
返回车次列表，含车次号、出发/到达站、时间、票价、席别和预订链接。""",
        phases=[3, 4],
        parameters=_PARAMETERS,
    )
    async def search_trains(
        origin: str,
        destination: str,
        date: str,
        seat_class: str | None = None,
        journey_type: int | None = None,
        sort_type: int | None = None,
        max_price: float | None = None,
    ) -> dict:
        if not flyai_client or not flyai_client.available:
            raise ToolError(
                "FlyAI service unavailable — cannot search trains",
                error_code="SERVICE_UNAVAILABLE",
                suggestion="Train search requires flyai CLI. Install with: npm i -g @fly-ai/flyai-cli",
            )

        kwargs: dict = {"destination": destination, "dep_date": date}
        if seat_class:
            kwargs["seat_class_name"] = seat_class
        if journey_type is not None:
            kwargs["journey_type"] = journey_type
        if sort_type is not None:
            kwargs["sort_type"] = sort_type
        if max_price is not None:
            kwargs["max_price"] = max_price

        raw_list = await flyai_client.search_train(origin=origin, **kwargs)

        if not raw_list:
            raise ToolError(
                f"No train results for {origin} → {destination} on {date}",
                error_code="NO_RESULTS",
                suggestion="Try different dates, cities, or remove filters",
            )

        trains = [normalize_flyai_train(r) for r in raw_list]

        return {
            "trains": [t.to_dict() for t in trains],
            "origin": origin,
            "destination": destination,
            "date": date,
        }

    return search_trains
