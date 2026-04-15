# backend/tools/get_poi_info.py
from __future__ import annotations

import asyncio
import logging

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool
from tools.normalizers import normalize_google_poi, normalize_flyai_poi, merge_pois

logger = logging.getLogger(__name__)

_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "景点/POI 搜索关键词，如 '金阁寺' '卢浮宫'",
        },
        "location": {
            "type": "string",
            "description": "限定搜索范围的城市或地区，如 '京都' '巴黎'",
        },
    },
    "required": ["query"],
}


def make_get_poi_info_tool(api_keys: ApiKeysConfig, flyai_client=None):
    @tool(
        name="get_poi_info",
        description="""获取景点/兴趣点详细信息。
Use when: 用户在阶段 3-5，需要了解某个景点的详情。
Don't use when: 已有该景点的完整信息。
        返回景点列表，含名称、地址、评分、门票价格和位置。""",
        phases=[3, 5],
        parameters=_PARAMETERS,
        human_label="查 POI 详情",
    )
    async def get_poi_info(query: str, location: str | None = None) -> dict:
        tasks = []

        # Branch 1: Google Places
        async def _google():
            if not api_keys.google_maps:
                return []
            search_query = query
            if location:
                search_query += f" in {location}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://maps.googleapis.com/maps/api/place/textsearch/json",
                    params={
                        "query": search_query,
                        "key": api_keys.google_maps,
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
            return [normalize_google_poi(p) for p in data.get("results", [])[:5]]

        tasks.append(_google())

        # Branch 2: FlyAI
        async def _flyai():
            if not flyai_client or not flyai_client.available:
                return []
            city = location or ""
            raw_list = await flyai_client.search_poi(
                city_name=city,
                keyword=query,
            )
            return [normalize_flyai_poi(r) for r in raw_list]

        tasks.append(_flyai())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        google_results = results[0] if not isinstance(results[0], BaseException) else []
        flyai_results = results[1] if not isinstance(results[1], BaseException) else []
        google_error = results[0] if isinstance(results[0], BaseException) else None
        flyai_error = results[1] if isinstance(results[1], BaseException) else None

        if google_error:
            logger.warning("Google POI search failed: %s", google_error)
        if flyai_error:
            logger.warning("FlyAI POI search failed: %s", flyai_error)

        if not google_results and not flyai_results:
            if not api_keys.google_maps:
                message = "Google Maps API key not configured"
                if flyai_error:
                    message = f"{message}. FlyAI: {flyai_error}"
                elif not flyai_client:
                    message = f"{message}. FlyAI: not configured"
                elif not flyai_client.available:
                    message = f"{message}. FlyAI: unavailable"
                else:
                    message = f"{message}. FlyAI: no results"
                raise ToolError(
                    message,
                    error_code="NO_API_KEY",
                    suggestion="Set GOOGLE_MAPS_API_KEY",
                )

            failure_reasons = []
            if google_error:
                failure_reasons.append(f"Google: {google_error}")
            if not google_error:
                failure_reasons.append("Google: no results")
            if flyai_error:
                failure_reasons.append(f"FlyAI: {flyai_error}")
            elif flyai_client and flyai_client.available:
                failure_reasons.append("FlyAI: no results")

            message = "No POI results from any source"
            if failure_reasons:
                message = f"{message}. Reasons: {'; '.join(failure_reasons)}"

            raise ToolError(
                message,
                error_code="NO_RESULTS",
                suggestion="Try a different search query",
            )

        merged = merge_pois(google_results, flyai_results)

        return {"pois": [p.to_dict() for p in merged], "query": query}

    return get_poi_info
