# backend/tools/search_accommodations.py
from __future__ import annotations

import asyncio
import logging

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool
from tools.normalizers import (
    normalize_google_accommodation,
    normalize_flyai_hotel,
    merge_accommodations,
)

logger = logging.getLogger(__name__)

_PARAMETERS = {
    "type": "object",
    "properties": {
        "destination": {
            "type": "string",
            "description": "目的地名称，如 '东京' '巴黎'",
        },
        "check_in": {"type": "string", "description": "入住日期，如 '2024-07-15'"},
        "check_out": {"type": "string", "description": "退房日期，如 '2024-07-20'"},
        "budget_per_night": {"type": "number", "description": "每晚预算（美元）"},
        "area": {"type": "string", "description": "偏好区域，如 '市中心' '海滨'"},
        "requirements": {
            "type": "array",
            "items": {"type": "string"},
            "description": "特殊要求，如 ['含早餐', '有泳池', '可停车']",
        },
    },
    "required": ["destination", "check_in", "check_out"],
}


def make_search_accommodations_tool(api_keys: ApiKeysConfig, flyai_client=None):
    @tool(
        name="search_accommodations",
        description="""搜索住宿信息。
Use when: 用户在阶段 3-4，需要查询住宿选项。
Don't use when: 住宿已确定。
返回住宿列表，含评分、价格、位置信息和预订链接。""",
        phases=[3],
        parameters=_PARAMETERS,
    )
    async def search_accommodations(
        destination: str,
        check_in: str,
        check_out: str,
        budget_per_night: float | None = None,
        area: str | None = None,
        requirements: list[str] | None = None,
    ) -> dict:
        tasks = []

        # Branch 1: Google Places
        async def _google():
            if not api_keys.google_maps:
                return []
            query = f"hotel lodging in {destination}"
            if area:
                query += f" {area}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://maps.googleapis.com/maps/api/place/textsearch/json",
                    params={
                        "query": query,
                        "key": api_keys.google_maps,
                        "type": "lodging",
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
            return [
                normalize_google_accommodation(p) for p in data.get("results", [])[:5]
            ]

        tasks.append(_google())

        # Branch 2: FlyAI
        async def _flyai():
            if not flyai_client or not flyai_client.available:
                return []
            kwargs = {"check_in_date": check_in, "check_out_date": check_out}
            if budget_per_night:
                kwargs["max_price"] = str(int(budget_per_night))
            raw_list = await flyai_client.search_hotel(
                dest_name=destination,
                **kwargs,
            )
            return [normalize_flyai_hotel(r) for r in raw_list]

        tasks.append(_flyai())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        google_results = results[0] if not isinstance(results[0], BaseException) else []
        flyai_results = results[1] if not isinstance(results[1], BaseException) else []

        if isinstance(results[0], BaseException):
            logger.warning("Google accommodation search failed: %s", results[0])
        if isinstance(results[1], BaseException):
            logger.warning("FlyAI hotel search failed: %s", results[1])

        if not google_results and not flyai_results:
            if not api_keys.google_maps:
                raise ToolError(
                    "Google Maps API key not configured",
                    error_code="NO_API_KEY",
                    suggestion="Set GOOGLE_MAPS_API_KEY",
                )
            raise ToolError(
                "No accommodation results from any source",
                error_code="NO_RESULTS",
                suggestion="Try different dates or destination",
            )

        merged = merge_accommodations(google_results, flyai_results)

        return {
            "accommodations": [a.to_dict() for a in merged],
            "destination": destination,
            "check_in": check_in,
            "check_out": check_out,
        }

    return search_accommodations
