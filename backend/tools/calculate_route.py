# backend/tools/calculate_route.py
from __future__ import annotations

from datetime import datetime
from math import atan2, cos, radians, sin, sqrt
from typing import Any

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
        "departure_time": {
            "description": "transit 出发时间，可传 Unix timestamp、'now' 或 ISO datetime 字符串",
            "oneOf": [
                {"type": "integer"},
                {"type": "number"},
                {"type": "string"},
            ],
        },
    },
    "required": ["origin_lat", "origin_lng", "dest_lat", "dest_lng"],
}

_VALID_MODES = {"driving", "walking", "bicycling", "transit"}
_WALKING_FALLBACK_MAX_METERS = 1_500
_DRIVING_FALLBACK_MAX_METERS = 200_000

_STATUS_ERROR_MAP = {
    "ZERO_RESULTS": (
        "NO_ROUTE",
        "未找到可用路线。已自动尝试其他出行方式，仍无结果。请使用保守估算并在 notes 标注路线数据不可用。",
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


def _haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6_371_000
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    )
    return radius * 2 * atan2(sqrt(a), sqrt(1 - a))


def _normalize_departure_time(value: Any) -> str | int | None:
    if value in (None, ""):
        return None
    if value == "now":
        return "now"
    if isinstance(value, bool):
        raise ToolError(
            "departure_time must be a timestamp, 'now', or ISO datetime string",
            error_code="INVALID_ARGUMENTS",
            suggestion="departure_time 可传 Unix timestamp、'now' 或 ISO datetime 字符串。",
        )
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).timestamp())
        except ValueError as exc:
            raise ToolError(
                "Invalid departure_time",
                error_code="INVALID_ARGUMENTS",
                suggestion="departure_time 字符串必须是 'now' 或 ISO datetime。",
            ) from exc
    raise ToolError(
        "Invalid departure_time type",
        error_code="INVALID_ARGUMENTS",
        suggestion="departure_time 可传 Unix timestamp、'now' 或 ISO datetime 字符串。",
    )


def _build_request_params(
    *,
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    mode: str,
    key: str,
    departure_time: str | int | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "origin": f"{origin_lat},{origin_lng}",
        "destination": f"{dest_lat},{dest_lng}",
        "mode": mode,
        "key": key,
    }
    if mode == "transit" and departure_time is not None:
        params["departure_time"] = departure_time
    return params


async def _fetch_directions(
    client: httpx.AsyncClient,
    *,
    params: dict[str, Any],
) -> dict[str, Any]:
    resp = await client.get(
        "https://maps.googleapis.com/maps/api/directions/json",
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _route_result_from_data(
    data: dict[str, Any],
    *,
    mode: str,
    requested_mode: str,
    fallback_used: bool,
    fallback_reason: str | None,
) -> dict:
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
        "requested_mode": requested_mode,
        "mode": mode,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "google_status": google_status,
        "route_available": True,
    }


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
        departure_time: int | float | str | None = None,
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

        normalized_departure_time = _normalize_departure_time(departure_time)
        requested_mode = mode

        async with httpx.AsyncClient() as client:
            params = _build_request_params(
                origin_lat=origin_lat,
                origin_lng=origin_lng,
                dest_lat=dest_lat,
                dest_lng=dest_lng,
                mode=mode,
                key=api_keys.google_maps,
                departure_time=normalized_departure_time,
            )
            data = await _fetch_directions(client, params=params)

            google_status = str(data.get("status") or "")
            distance_m = _haversine_meters(origin_lat, origin_lng, dest_lat, dest_lng)

            if mode == "transit" and google_status == "ZERO_RESULTS":
                if distance_m <= _WALKING_FALLBACK_MAX_METERS:
                    walking_params = _build_request_params(
                        origin_lat=origin_lat,
                        origin_lng=origin_lng,
                        dest_lat=dest_lat,
                        dest_lng=dest_lng,
                        mode="walking",
                        key=api_keys.google_maps,
                        departure_time=None,
                    )
                    walking_data = await _fetch_directions(client, params=walking_params)
                    walking_status = str(walking_data.get("status") or "")
                    if walking_status == "OK" and walking_data.get("routes"):
                        return _route_result_from_data(
                            walking_data,
                            mode="walking",
                            requested_mode=requested_mode,
                            fallback_used=True,
                            fallback_reason="transit_zero_results_short_distance",
                        )
                if distance_m <= _DRIVING_FALLBACK_MAX_METERS:
                    driving_params = _build_request_params(
                        origin_lat=origin_lat,
                        origin_lng=origin_lng,
                        dest_lat=dest_lat,
                        dest_lng=dest_lng,
                        mode="driving",
                        key=api_keys.google_maps,
                        departure_time=None,
                    )
                    driving_data = await _fetch_directions(client, params=driving_params)
                    driving_status = str(driving_data.get("status") or "")
                    if driving_status == "OK" and driving_data.get("routes"):
                        return _route_result_from_data(
                            driving_data,
                            mode="driving",
                            requested_mode=requested_mode,
                            fallback_used=True,
                            fallback_reason="transit_zero_results_fallback_driving",
                        )

        return _route_result_from_data(
            data,
            mode=mode,
            requested_mode=requested_mode,
            fallback_used=False,
            fallback_reason=None,
        )

    return calculate_route
