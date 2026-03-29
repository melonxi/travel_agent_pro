# backend/tools/search_flights.py
from __future__ import annotations

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool

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


def make_search_flights_tool(api_keys: ApiKeysConfig):
    @tool(
        name="search_flights",
        description="""搜索航班信息。
Use when: 用户在阶段 3-4，需要查询航班选项。
Don't use when: 航班已预订或不需要飞行。
返回航班列表，含价格、时间和航空公司信息。""",
        phases=[3, 4],
        parameters=_PARAMETERS,
    )
    async def search_flights(
        origin: str, destination: str, date: str, max_results: int = 5
    ) -> dict:
        if not api_keys.amadeus_key:
            raise ToolError(
                "Amadeus API key not configured",
                error_code="NO_API_KEY",
                suggestion="Set AMADEUS_KEY",
            )

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

        flights = []
        for offer in data.get("data", [])[:max_results]:
            flights.append(
                {
                    "id": offer.get("id", ""),
                    "price": offer.get("price", {}).get("total", ""),
                    "currency": offer.get("price", {}).get("currency", ""),
                    "segments": offer.get("itineraries", []),
                }
            )

        return {
            "flights": flights,
            "source": "amadeus",
            "origin": origin,
            "destination": destination,
        }

    return search_flights
