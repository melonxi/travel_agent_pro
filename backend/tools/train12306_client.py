# backend/tools/train12306_client.py
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://kyfw.12306.cn"
_INIT_URL = f"{_BASE}/otn/leftTicket/init"
_STATION_JS_URL = f"{_BASE}/otn/resources/js/framework/station_name.js"
_DEFAULT_QUERY_PATHS = ("leftTicket/queryG", "leftTicket/queryZ", "leftTicket/query")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": f"{_INIT_URL}?linktypeid=dc",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

# leftTicket query 返回的 '|' 分隔字段下标(12306 官方 web 端解析约定)
_IDX_TRAIN_NO = 2
_IDX_TRAIN_CODE = 3
_IDX_FROM_TELECODE = 6
_IDX_TO_TELECODE = 7
_IDX_START_TIME = 8
_IDX_ARRIVE_TIME = 9
_IDX_DURATION = 10
_IDX_CAN_BUY = 11

_SEAT_FIELDS = {
    "商务座/特等座": 32,
    "一等座": 31,
    "二等座": 30,
    "高级软卧": 21,
    "软卧/一等卧": 23,
    "动卧": 33,
    "硬卧/二等卧": 28,
    "软座": 24,
    "硬座": 29,
    "无座": 26,
}


class Train12306Error(Exception):
    def __init__(self, message: str, *, code: str = "SERVICE_UNAVAILABLE") -> None:
        super().__init__(message)
        self.code = code


class Train12306Client:
    """Async client for the public 12306 left-ticket query endpoints.

    查询余票不需要登录。会话 cookie 从 init 页获取;查询路径
    (queryG/queryZ/query)会随 12306 改版轮换,从 init 页动态解析并保留兜底列表。
    """

    def __init__(self, timeout: int = 15) -> None:
        self.timeout = timeout
        self._stations: dict[str, dict[str, str]] | None = None
        self._query_path: str | None = None
        self._lock = asyncio.Lock()

    async def _load_stations(self, client: httpx.AsyncClient) -> dict[str, dict[str, str]]:
        if self._stations is not None:
            return self._stations
        resp = await client.get(_STATION_JS_URL, timeout=self.timeout)
        resp.raise_for_status()
        stations: dict[str, dict[str, str]] = {}
        # 格式: @bjb|北京北|VAP|beijingbei|bjb|0@bjd|北京东|BOP|...
        for chunk in resp.text.split("@"):
            parts = chunk.split("|")
            if len(parts) >= 5 and parts[2].isalpha() and len(parts[2]) == 3:
                stations[parts[1]] = {
                    "name": parts[1],
                    "telecode": parts[2],
                    "pinyin": parts[3],
                }
        if not stations:
            raise Train12306Error("12306 station list is empty or format changed")
        self._stations = stations
        return stations

    def resolve_station(self, name: str) -> tuple[str, list[str]]:
        """Resolve a city/station name to a telecode.

        Returns (telecode, alternative_station_names). Exact match wins;
        otherwise the shortest prefix match is treated as the main station.
        """
        stations = self._stations or {}
        name = name.strip()
        if name in stations:
            alts = [
                s
                for s in stations
                if s != name and s.startswith(name)
            ]
            return stations[name]["telecode"], sorted(alts)[:8]
        prefixed = sorted(
            (s for s in stations if s.startswith(name)),
            key=len,
        )
        if prefixed:
            main = prefixed[0]
            return stations[main]["telecode"], prefixed[1:9]
        raise Train12306Error(
            f"未找到车站: {name}",
            code="STATION_NOT_FOUND",
        )

    async def _ensure_session(self, client: httpx.AsyncClient) -> str:
        """Warm up cookies and discover the current query path."""
        resp = await client.get(
            _INIT_URL, params={"linktypeid": "dc"}, timeout=self.timeout
        )
        resp.raise_for_status()
        match = re.search(r"CLeftTicketUrl\s*=\s*'([^']+)'", resp.text)
        if match:
            self._query_path = match.group(1)
        return self._query_path or _DEFAULT_QUERY_PATHS[0]

    async def query_left_tickets(
        self,
        from_station: str,
        to_station: str,
        date: str,
    ) -> dict[str, Any]:
        async with self._lock:
            async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:
                await self._load_stations(client)
                from_code, from_alts = self.resolve_station(from_station)
                to_code, to_alts = self.resolve_station(to_station)
                primary = await self._ensure_session(client)

                paths = [primary] + [
                    p for p in _DEFAULT_QUERY_PATHS if p != primary
                ]
                data = None
                last_error: Exception | None = None
                for path in paths:
                    try:
                        resp = await client.get(
                            f"{_BASE}/otn/{path}",
                            params={
                                "leftTicketDTO.train_date": date,
                                "leftTicketDTO.from_station": from_code,
                                "leftTicketDTO.to_station": to_code,
                                "purpose_codes": "ADULT",
                            },
                            timeout=self.timeout,
                        )
                        resp.raise_for_status()
                        payload = resp.json()
                        candidate = payload.get("data")
                        if isinstance(candidate, dict) and "result" in candidate:
                            data = candidate
                            self._query_path = path
                            break
                    except Exception as exc:  # noqa: BLE001 - try next path
                        last_error = exc
                        logger.warning("12306 query via %s failed: %s", path, exc)
                if data is None:
                    raise Train12306Error(
                        f"12306 查询失败: {last_error}",
                    ) from last_error

        station_map: dict[str, str] = data.get("map", {}) or {}
        trains = []
        for row in data.get("result", []):
            parts = row.split("|")
            if len(parts) <= max(_SEAT_FIELDS.values()):
                continue
            seats = {}
            for label, idx in _SEAT_FIELDS.items():
                value = parts[idx].strip()
                if value and value != "--":
                    seats[label] = value
            trains.append(
                {
                    "train_code": parts[_IDX_TRAIN_CODE],
                    "from_station": station_map.get(
                        parts[_IDX_FROM_TELECODE], parts[_IDX_FROM_TELECODE]
                    ),
                    "to_station": station_map.get(
                        parts[_IDX_TO_TELECODE], parts[_IDX_TO_TELECODE]
                    ),
                    "start_time": parts[_IDX_START_TIME],
                    "arrive_time": parts[_IDX_ARRIVE_TIME],
                    "duration": parts[_IDX_DURATION],
                    "bookable": parts[_IDX_CAN_BUY] == "Y",
                    "seats": seats,
                }
            )
        return {
            "trains": trains,
            "from_alternatives": from_alts,
            "to_alternatives": to_alts,
        }
