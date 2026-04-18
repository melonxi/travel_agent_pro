from __future__ import annotations

import math
import re
from typing import Any

from tools.base import ToolError, tool

_TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_MODE_SPEED_KMH = {
    "walking": 4.0,
    "transit": 18.0,
    "driving": 25.0,
}

_LOCATION_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "lat": {"type": "number"},
        "lng": {"type": "number"},
    },
    "required": ["name", "lat", "lng"],
}

_PARAMETERS = {
    "type": "object",
    "properties": {
        "pois": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "lat": {"type": "number"},
                    "lng": {"type": "number"},
                    "duration_hours": {"type": "number"},
                },
                "required": ["name", "lat", "lng"],
            },
            "description": "单日候选 POI 列表。每项必须包含 name, lat, lng；duration_hours 缺省按 1 小时估算。",
        },
        "start_location": {
            **_LOCATION_SCHEMA,
            "description": "可选起点，例如酒店。存在时纳入移动估算，但不写入 ordered_pois。",
        },
        "end_location": {
            **_LOCATION_SCHEMA,
            "description": "可选终点，例如酒店。存在时纳入移动估算，但不写入 ordered_pois。",
        },
        "day_start_time": {
            "type": "string",
            "description": "当天可开始时间，HH:MM，默认 09:00。",
            "default": "09:00",
        },
        "day_end_time": {
            "type": "string",
            "description": "当天可结束时间，HH:MM，默认 21:00。",
            "default": "21:00",
        },
        "transport_mode": {
            "type": "string",
            "enum": ["walking", "transit", "driving"],
            "description": "粗略移动估算方式，默认 transit。",
            "default": "transit",
        },
    },
    "required": ["pois"],
}


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_km = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _validate_location(item: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ToolError(
            f"{field_name} must be an object",
            error_code="INVALID_VALUE",
            suggestion=f"{field_name} should include name, lat, lng. Use get_poi_info first if coordinates are missing.",
        )
    for key in ("name", "lat", "lng"):
        if key not in item:
            raise ToolError(
                f"{field_name}.{key} is required",
                error_code="INVALID_VALUE",
                suggestion=f"{field_name} should include name, lat, lng. Use get_poi_info first if coordinates are missing.",
            )
    if not isinstance(item["name"], str) or not item["name"].strip():
        raise ToolError(
            f"{field_name}.name must be a non-empty string",
            error_code="INVALID_VALUE",
            suggestion="Use a readable POI name.",
        )
    for axis in ("lat", "lng"):
        if isinstance(item[axis], bool) or not isinstance(item[axis], (int, float)):
            raise ToolError(
                f"{field_name}.{axis} must be a number",
                error_code="INVALID_VALUE",
                suggestion=f"{field_name} should include numeric coordinates. Use get_poi_info first if coordinates are missing.",
            )
    return dict(item)


def _parse_minutes(value: str, field_name: str) -> int:
    if not isinstance(value, str) or not _TIME_PATTERN.fullmatch(value):
        raise ToolError(
            f"{field_name} must use HH:MM format",
            error_code="INVALID_VALUE",
            suggestion=f'{field_name} should look like "09:00".',
        )
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _distance_between(a: dict[str, Any], b: dict[str, Any]) -> float:
    return _haversine_km(
        float(a["lat"]), float(a["lng"]), float(b["lat"]), float(b["lng"])
    )


def _order_pois(
    pois: list[dict[str, Any]],
    start_location: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], float]:
    remaining = [dict(poi) for poi in pois]
    if not remaining:
        return [], 0.0
    current = start_location or remaining.pop(0)
    ordered: list[dict[str, Any]] = [] if start_location else [dict(current)]
    total_distance = 0.0
    while remaining:
        best_index = 0
        best_distance = float("inf")
        for index, poi in enumerate(remaining):
            distance = _distance_between(current, poi)
            if distance < best_distance:
                best_index = index
                best_distance = distance
        total_distance += best_distance
        current = remaining.pop(best_index)
        ordered.append(dict(current))
    return ordered, total_distance


def make_optimize_day_route_tool():
    @tool(
        name="optimize_day_route",
        description=(
            "Optimize the order of POIs for one Phase 5 day. "
            "Use when arranging 2+ same-day POIs before saving a DayPlan. "
            "Do not use as a state write: this tool does not modify daily_plans. "
            "After choosing the schedule, persist it with save_day_plan or replace_all_day_plans."
        ),
        phases=[5],
        parameters=_PARAMETERS,
        side_effect="read",
        human_label="优化单日路线",
    )
    async def optimize_day_route(
        pois: list[dict],
        start_location: dict | None = None,
        end_location: dict | None = None,
        day_start_time: str = "09:00",
        day_end_time: str = "21:00",
        transport_mode: str = "transit",
    ) -> dict:
        if not isinstance(pois, list):
            raise ToolError(
                "pois must be a list",
                error_code="INVALID_VALUE",
                suggestion="Pass pois as list[object]. Use get_poi_info first if coordinates are missing.",
            )
        mode = transport_mode or "transit"
        if mode not in _MODE_SPEED_KMH:
            raise ToolError(
                f"transport_mode must be one of walking, transit, driving; got {mode!r}",
                error_code="INVALID_VALUE",
                suggestion='Use transport_mode="transit" unless the day is explicitly walking or driving focused.',
            )
        start_minutes = _parse_minutes(day_start_time, "day_start_time")
        end_minutes = _parse_minutes(day_end_time, "day_end_time")
        if end_minutes <= start_minutes:
            raise ToolError(
                "day_end_time must be later than day_start_time",
                error_code="INVALID_VALUE",
                suggestion="Use a same-day window such as 09:00 to 21:00.",
            )

        normalized_pois = [
            _validate_location(poi, f"pois[{index}]") for index, poi in enumerate(pois)
        ]
        start = (
            _validate_location(start_location, "start_location")
            if start_location
            else None
        )
        end = _validate_location(end_location, "end_location") if end_location else None

        warnings: list[str] = []
        if len(normalized_pois) < 2:
            ordered = normalized_pois
            total_distance = 0.0
            if len(normalized_pois) == 1:
                warnings.append("Only one POI supplied; route ordering was not needed.")
        else:
            ordered, total_distance = _order_pois(normalized_pois, start)
        if end and ordered:
            total_distance += _distance_between(ordered[-1], end)

        speed = _MODE_SPEED_KMH[mode]
        travel_minutes = int(round((total_distance / speed) * 60))
        activity_minutes = int(
            round(
                sum(
                    float(poi.get("duration_hours", 1.0))
                    if not isinstance(poi.get("duration_hours", 1.0), bool)
                    else 1.0
                    for poi in ordered
                )
                * 60
            )
        )
        total_minutes = travel_minutes + activity_minutes
        available_minutes = end_minutes - start_minutes
        can_fit = total_minutes <= available_minutes
        if not can_fit:
            warnings.append(
                f"Estimated {total_minutes}min exceeds available day window of {available_minutes}min."
            )

        return {
            "ordered_pois": ordered,
            "estimated_total_distance_km": round(total_distance, 2),
            "estimated_travel_minutes": travel_minutes,
            "estimated_activity_minutes": activity_minutes,
            "estimated_total_minutes": total_minutes,
            "can_fit_in_day": can_fit,
            "warnings": warnings,
            "next_action": "Use save_day_plan to persist the selected schedule. This tool did not write daily_plans.",
        }

    return optimize_day_route
