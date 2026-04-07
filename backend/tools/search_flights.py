# backend/tools/search_flights.py
from __future__ import annotations

import asyncio
import logging

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool
from tools.normalizers import (
    normalize_amadeus_flight,
    normalize_flyai_flight,
    merge_flights,
)

logger = logging.getLogger(__name__)

_PARAMETERS = {
    "type": "object",
    "properties": {
        "origin": {
            "type": "string",
            "description": "出发城市 IATA 代码，如 'PEK' 'SHA'",
        },
        "destination": {
            "type": "string",
            "description": "目的地城市 IATA 代码，如 'NRT' 'DPS'",
        },
        "date": {"type": "string", "description": "出发日期，如 '2024-07-15'"},
        "max_results": {"type": "integer", "description": "最大返回数量", "default": 5},
    },
    "required": ["origin", "destination", "date"],
}

# IATA code → Chinese city name
_IATA_TO_CITY: dict[str, str] = {
    # 北京
    "PEK": "北京", "PKX": "北京",
    # 上海
    "SHA": "上海", "PVG": "上海",
    # 一线 & 新一线
    "CAN": "广州", "SZX": "深圳", "CTU": "成都", "TFU": "成都",
    "HGH": "杭州", "NKG": "南京", "WUH": "武汉", "CKG": "重庆",
    "XIY": "西安", "TSN": "天津", "SHE": "沈阳", "DLC": "大连",
    "TAO": "青岛", "CGO": "郑州", "CSX": "长沙", "KHN": "南昌",
    "FOC": "福州", "XMN": "厦门", "NNG": "南宁", "HAK": "海口",
    "SYX": "三亚", "KWE": "贵阳", "HRB": "哈尔滨", "CGQ": "长春",
    "URC": "乌鲁木齐", "LHW": "兰州", "INC": "银川", "XNN": "西宁",
    "HET": "呼和浩特", "KMG": "昆明", "TNA": "济南", "SJW": "石家庄",
    "HFE": "合肥", "WNZ": "温州", "NTG": "南通", "ZUH": "珠海",
    # 旅游城市
    "DLU": "大理", "LJG": "丽江", "JHG": "西双版纳", "KWL": "桂林",
    "LXA": "拉萨", "JZH": "九寨沟", "TYN": "太原", "DDG": "丹东",
    "WEH": "威海", "YNT": "烟台", "YIH": "宜昌", "ZHA": "湛江",
    "BHY": "北海", "MIG": "绵阳", "YNZ": "盐城", "XUZ": "徐州",
    "LYA": "洛阳", "ENH": "恩施",
    # 港澳台
    "HKG": "香港", "MFM": "澳门", "TPE": "台北", "KHH": "高雄",
    # 日韩
    "NRT": "东京", "HND": "东京", "KIX": "大阪", "ITM": "大阪",
    "NGO": "名古屋", "FUK": "福冈", "CTS": "札幌", "OKA": "冲绳",
    "ICN": "首尔", "GMP": "首尔", "PUS": "釜山", "CJU": "济州岛",
    # 东南亚
    "BKK": "曼谷", "DMK": "曼谷", "SIN": "新加坡", "KUL": "吉隆坡",
    "DPS": "巴厘岛", "CGK": "雅加达", "MNL": "马尼拉", "SGN": "胡志明市",
    "HAN": "河内", "DAD": "岘港", "REP": "暹粒", "PNH": "金边",
    "RGN": "仰光",
    # 其他热门
    "SVO": "莫斯科", "CDG": "巴黎", "LHR": "伦敦", "FRA": "法兰克福",
    "LAX": "洛杉矶", "JFK": "纽约", "SFO": "旧金山", "SYD": "悉尼",
    "MEL": "墨尔本", "AKL": "奥克兰", "DXB": "迪拜", "IST": "伊斯坦布尔",
    "CAI": "开罗", "NBO": "内罗毕",
}


def make_search_flights_tool(api_keys: ApiKeysConfig, flyai_client=None):
    @tool(
        name="search_flights",
        description="""搜索航班信息。
Use when: 用户在阶段 3-4，需要查询航班选项。
Don't use when: 航班已预订或不需要飞行。
返回航班列表，含价格、时间、航空公司信息和预订链接。""",
        phases=[3],
        parameters=_PARAMETERS,
    )
    async def search_flights(
        origin: str, destination: str, date: str, max_results: int = 5
    ) -> dict:
        tasks = []

        # Branch 1: Amadeus
        async def _amadeus():
            if not api_keys.amadeus_key:
                return []
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://test.api.amadeus.com/v2/shopping/flight-offers",
                    json={
                        "originLocationCode": origin,
                        "destinationLocationCode": destination,
                        "departureDate": date,
                        "adults": 1,
                        "max": max_results,
                    },
                    headers={"Authorization": f"Bearer {api_keys.amadeus_key}"},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
            return [
                normalize_amadeus_flight(o) for o in data.get("data", [])[:max_results]
            ]

        tasks.append(_amadeus())

        # Branch 2: FlyAI
        async def _flyai():
            if not flyai_client or not flyai_client.available:
                return []
            origin_city = _IATA_TO_CITY.get(origin.upper(), origin)
            dest_city = _IATA_TO_CITY.get(destination.upper(), destination)
            raw_list = await flyai_client.search_flight(
                origin=origin_city,
                destination=dest_city,
                dep_date=date,
            )
            return [normalize_flyai_flight(r) for r in raw_list]

        tasks.append(_flyai())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        amadeus_results = (
            results[0] if not isinstance(results[0], BaseException) else []
        )
        flyai_results = results[1] if not isinstance(results[1], BaseException) else []

        if isinstance(results[0], BaseException):
            logger.warning("Amadeus search failed: %s", results[0])
        if isinstance(results[1], BaseException):
            logger.warning("FlyAI flight search failed: %s", results[1])

        if not amadeus_results and not flyai_results:
            reasons = []
            if not api_keys.amadeus_key:
                reasons.append("Amadeus API key not configured")
            elif isinstance(results[0], BaseException):
                reasons.append(f"Amadeus error: {results[0]}")
            if not flyai_client or not flyai_client.available:
                reasons.append("FlyAI CLI not available")
            elif isinstance(results[1], BaseException):
                reasons.append(f"FlyAI error: {results[1]}")
            raise ToolError(
                "No flight results from any source"
                + (f" ({'; '.join(reasons)})" if reasons else ""),
                error_code="NO_RESULTS",
                suggestion="Check API keys, install FlyAI CLI, or try different dates/airports",
            )

        merged = merge_flights(amadeus_results, flyai_results)

        return {
            "flights": [f.to_dict() for f in merged],
            "origin": origin,
            "destination": destination,
        }

    return search_flights
