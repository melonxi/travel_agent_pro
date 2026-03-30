# backend/tools/search_travel_services.py
from __future__ import annotations

from tools.base import ToolError, tool

_SERVICE_KEYWORDS: dict[str, str] = {
    "visa": "签证办理",
    "insurance": "旅行保险",
    "sim_card": "境外电话卡",
    "car_rental": "租车自驾",
    "transfer": "接送机",
}

_PARAMETERS = {
    "type": "object",
    "properties": {
        "destination": {
            "type": "string",
            "description": "旅行目的地",
        },
        "service_type": {
            "type": "string",
            "enum": list(_SERVICE_KEYWORDS.keys()),
            "description": "服务类型：visa（签证）、insurance（保险）、sim_card（电话卡）、car_rental（租车）、transfer（接送机）",
        },
    },
    "required": ["destination", "service_type"],
}


def make_search_travel_services_tool(flyai_client):
    @tool(
        name="search_travel_services",
        description="""搜索旅行辅助服务：签证办理、旅行保险、电话卡、租车、接送机。
Use when: 用户在阶段 7，行程已确认，需要推荐实用出行服务。
Don't use when: 行程尚未确定。
返回服务列表，含标题、价格和预订链接。""",
        phases=[7],
        parameters=_PARAMETERS,
    )
    async def search_travel_services(destination: str, service_type: str) -> dict:
        if not flyai_client or not flyai_client.available:
            raise ToolError(
                "FlyAI service unavailable",
                error_code="SERVICE_UNAVAILABLE",
                suggestion="Suggest user search for travel services independently.",
            )

        keyword = _SERVICE_KEYWORDS.get(service_type, service_type)
        query = f"{destination} {keyword}"
        raw_list = await flyai_client.fast_search(query=query)

        services = []
        for item in raw_list:
            services.append(
                {
                    "title": item.get("title", ""),
                    "price": item.get("price"),
                    "booking_url": item.get("jumpUrl") or item.get("detailUrl"),
                    "image_url": item.get("picUrl") or item.get("mainPic"),
                }
            )

        return {
            "services": services,
            "destination": destination,
            "service_type": service_type,
            "source": "flyai",
        }

    return search_travel_services
