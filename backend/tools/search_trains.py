# backend/tools/search_trains.py
from __future__ import annotations

import logging

from tools.base import ToolError, tool
from tools.train12306_client import Train12306Client, Train12306Error

logger = logging.getLogger(__name__)

_PARAMETERS = {
    "type": "object",
    "properties": {
        "origin": {
            "type": "string",
            "description": "出发城市或车站名,如 '北京' '上海虹桥'",
        },
        "destination": {
            "type": "string",
            "description": "目的地城市或车站名,如 '上海' '杭州'",
        },
        "date": {"type": "string", "description": "出发日期,如 '2026-04-15'"},
        "train_types": {
            "type": "string",
            "description": "车次类型过滤,由 G/D/Z/T/K 组成的字符串,如 'GD' 只看高铁动车。缺省不过滤。",
        },
        "earliest_start": {
            "type": "string",
            "description": "最早出发时间 HH:MM,如 '08:00'。缺省不过滤。",
        },
        "latest_start": {
            "type": "string",
            "description": "最晚出发时间 HH:MM,如 '18:00'。缺省不过滤。",
        },
        "max_results": {
            "type": "integer",
            "description": "最大返回数量,默认 10,自动限制在 1-30。",
        },
    },
    "required": ["origin", "destination", "date"],
}

# 进程级共享:车站码表与查询路径只需解析一次
_shared_client = Train12306Client()


def make_search_trains_tool():
    @tool(
        name="search_trains",
        description="""搜索火车/高铁车次信息(12306 官方实时数据)。
Use when: 用户在阶段 2,需要查询火车或高铁出行方案(国内城市间交通)。
Don't use when: 用户明确要坐飞机,或目的地不通火车。
Important:
  - 返回车次、出发/到达站、时间、历时、可预订状态和各席别余票。
  - 不含票价:确定候选车次后,用 web_search 查询"车次 出发站 到达站 票价"补齐,并提示用户以 12306 为准。
  - 12306 预售期约 15 天,超出预售期的日期查不到车次,应向用户说明并改为给出参考车次。""",
        phases=[2],
        parameters=_PARAMETERS,
        human_label="检索火车",
    )
    async def search_trains(
        origin: str,
        destination: str,
        date: str,
        train_types: str | None = None,
        earliest_start: str | None = None,
        latest_start: str | None = None,
        max_results: int = 10,
    ) -> dict:
        max_results = max(1, min(30, max_results))
        try:
            data = await _shared_client.query_left_tickets(origin, destination, date)
        except Train12306Error as exc:
            if exc.code == "STATION_NOT_FOUND":
                raise ToolError(
                    str(exc),
                    error_code="STATION_NOT_FOUND",
                    suggestion="请使用 12306 收录的城市或车站中文名,如 '北京''上海虹桥'。",
                ) from exc
            raise ToolError(
                f"12306 查询失败: {exc}",
                error_code="SERVICE_UNAVAILABLE",
                suggestion=(
                    "12306 接口暂时不可用或被限流。请稍后重试;"
                    "如持续失败,用 web_search 查询车次和票价作为参考,并提示用户自行核实。"
                ),
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning("12306 search failed: %s", exc)
            raise ToolError(
                f"12306 查询失败: {exc}",
                error_code="SERVICE_UNAVAILABLE",
                suggestion="请稍后重试,或用 web_search 查询车次作为参考。",
            ) from exc

        trains = data["trains"]
        if train_types:
            allowed = {c.upper() for c in train_types}
            trains = [
                t
                for t in trains
                if t["train_code"][:1].upper() in allowed
            ]
        if earliest_start:
            trains = [t for t in trains if t["start_time"] >= earliest_start]
        if latest_start:
            trains = [t for t in trains if t["start_time"] <= latest_start]

        if not trains:
            raise ToolError(
                f"No train results for {origin} → {destination} on {date}",
                error_code="NO_RESULTS",
                suggestion=(
                    "请检查日期是否在 12306 预售期内、放宽车次类型/时间过滤,"
                    "或尝试邻近车站(见返回的备选车站列表)。"
                ),
            )

        return {
            "trains": trains[:max_results],
            "total_found": len(trains),
            "origin": origin,
            "destination": destination,
            "date": date,
            "origin_station_alternatives": data["from_alternatives"],
            "destination_station_alternatives": data["to_alternatives"],
            "source": "12306",
            "note": "票价未包含;确定候选车次后用 web_search 查票价,并提示用户以 12306 为准。",
        }

    return search_trains
