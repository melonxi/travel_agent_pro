# backend/tools/calculate_route.py
from __future__ import annotations

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "origin_lat": {"type": "number", "description": "起点纬度"},
        "origin_lng": {"type": "number", "description": "起点经度"},
        "dest_lat": {"type": "number", "description": "终点纬度"},
        "dest_lng": {"type": "number", "description": "终点经度"},
        "mode": {
            "type": "string",
            "description": "出行方式: driving, walking, bicycling, transit",
            "enum": ["driving", "walking", "bicycling", "transit"],
            "default": "transit",
        },
    },
    "required": ["origin_lat", "origin_lng", "dest_lat", "dest_lng"],
}

_VALID_MODES = {"driving", "walking", "bicycling", "transit"}

_STATUS_ERROR_MAP = {
    "ZERO_RESULTS": (
        "NO_ROUTE",
        "未找到可用路线。若当前是 transit，可尝试 walking/driving，或使用保守估算并在 notes 标注路线数据不可用。",
    ),
    "NOT_FOUND": (
        "ROUTE_POINT_NOT_FOUND",
        "起点或终点无法被 Google Maps 识别。请先用 get_poi_info 获取更可靠的坐标。",
    ),
    "MAX_ROUTE_LENGTH_EXCEEDED": (
        "ROUTE_TOO_LONG",
        "路线距离超出 Directions API 可计算范围。请拆分为更短的路段。",
    ),
    "INVALID_REQUEST": (
        "INVALID_ROUTE_REQUEST",
        "路线请求参数无效。请检查坐标和出行方式。",
    ),
    "REQUEST_DENIED": (
        "ROUTE_REQUEST_DENIED",
        "Google Directions API 拒绝请求。请检查 API key、权限和 Directions API 是否启用。",
    ),
    "OVER_DAILY_LIMIT": (
        "ROUTE_QUOTA_EXCEEDED",
        "Google Directions API 配额或计费限制已触发。请稍后重试或切换估算策略。",
    ),
    "OVER_QUERY_LIMIT": (
        "ROUTE_QUOTA_EXCEEDED",
        "Google Directions API 查询频率超限。请减少重复路线查询或稍后重试。",
    ),
    "UNKNOWN_ERROR": (
        "ROUTE_TEMPORARY_ERROR",
        "Google Directions API 临时错误。请稍后重试；不要围绕同一路线反复查询。",
    ),
}


def _raise_for_google_status(status: str, error_message: str | None = None) -> None:
    if status == "OK":
        return
    error_code, suggestion = _STATUS_ERROR_MAP.get(
        status,
        (
            "ROUTE_API_ERROR",
            "Directions API 返回未知状态。请不要把本次结果作为路线证据。",
        ),
    )
    detail = f"Google Directions API returned {status}"
    if error_message:
        detail = f"{detail}: {error_message}"
    raise ToolError(detail, error_code=error_code, suggestion=suggestion)


def _duration_minutes(duration: dict) -> int | None:
    value = duration.get("value")
    if isinstance(value, (int, float)):
        return max(0, round(float(value) / 60))
    return None


def make_calculate_route_tool(api_keys: ApiKeysConfig):
    @tool(
        name="calculate_route",
        description="""计算两点之间的路线。
Use when: Phase 2 skeleton/lock 或 Phase 3 需要计算景点之间的路线和时间。
Don't use when: 不需要路线规划。
        返回距离、时长和路线步骤。""",
        phases=[2, 3],
        parameters=_PARAMETERS,
        human_label="规划路线",
    )
    async def calculate_route(
        origin_lat: float,
        origin_lng: float,
        dest_lat: float,
        dest_lng: float,
        mode: str = "transit",
    ) -> dict:
        if mode not in _VALID_MODES:
            raise ToolError(
                f"Unsupported route mode: {mode}",
                error_code="INVALID_ARGUMENTS",
                suggestion="mode 必须是 driving、walking、bicycling 或 transit。",
            )
        if not api_keys.google_maps:
            raise ToolError(
                "Google Maps API key not configured",
                error_code="NO_API_KEY",
                suggestion="Set GOOGLE_MAPS_API_KEY",
            )

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/directions/json",
                params={
                    "origin": f"{origin_lat},{origin_lng}",
                    "destination": f"{dest_lat},{dest_lng}",
                    "mode": mode,
                    "key": api_keys.google_maps,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

        google_status = str(data.get("status") or "")
        _raise_for_google_status(google_status, data.get("error_message"))

        routes = data.get("routes", [])
        if not routes:
            raise ToolError(
                "Google Directions API returned OK but no routes",
                error_code="NO_ROUTE",
                suggestion=(
                    "本次没有可用路线数据。不要把空路线当作成功证据；"
                    "可尝试其他 mode，或基于常识做保守估算并标注未验证。"
                ),
            )

        leg = routes[0].get("legs", [{}])[0]
        distance = leg.get("distance", {})
        duration = leg.get("duration", {})
        steps = []
        for step in leg.get("steps", []):
            step_distance = step.get("distance", {})
            step_duration = step.get("duration", {})
            steps.append(
                {
                    "instruction": step.get("html_instructions", ""),
                    "distance": step_distance.get("text", ""),
                    "distance_meters": step_distance.get("value"),
                    "duration": step_duration.get("text", ""),
                    "duration_min": _duration_minutes(step_duration),
                }
            )

        return {
            "distance": distance.get("text", ""),
            "distance_meters": distance.get("value"),
            "duration": duration.get("text", ""),
            "duration_min": _duration_minutes(duration),
            "steps": steps,
            "mode": mode,
            "google_status": google_status,
            "route_available": True,
        }

    return calculate_route
