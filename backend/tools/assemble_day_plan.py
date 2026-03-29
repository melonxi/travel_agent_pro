# backend/tools/assemble_day_plan.py
from __future__ import annotations

import math

from tools.base import tool

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
            },
            "description": "景点列表，包含名称、坐标和预计游览时长",
        },
        "start_time": {
            "type": "string",
            "description": "开始时间，如 '09:00'",
            "default": "09:00",
        },
        "end_time": {
            "type": "string",
            "description": "结束时间，如 '21:00'",
            "default": "21:00",
        },
        "max_walk_km": {
            "type": "number",
            "description": "最大步行距离（公里）",
            "default": 10.0,
        },
    },
    "required": ["pois"],
}


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate the great-circle distance between two points in km."""
    r = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def make_assemble_day_plan_tool():
    @tool(
        name="assemble_day_plan",
        description="""组装单日行程计划。
Use when: 用户在阶段 4-5，需要将多个景点排列成合理的日程。
Don't use when: 行程已排好或只有一个景点。
使用贪心算法按地理临近度排序，返回排序后的景点和总距离。""",
        phases=[4, 5],
        parameters=_PARAMETERS,
    )
    async def assemble_day_plan(
        pois: list[dict],
        start_time: str = "09:00",
        end_time: str = "21:00",
        max_walk_km: float = 10.0,
    ) -> dict:
        if not pois:
            return {
                "ordered_pois": [],
                "total_distance_km": 0.0,
                "estimated_hours": 0.0,
            }

        # Greedy nearest-neighbor ordering
        remaining = list(pois)
        ordered = [remaining.pop(0)]
        total_distance = 0.0

        while remaining:
            last = ordered[-1]
            best_idx = 0
            best_dist = float("inf")
            for i, poi in enumerate(remaining):
                d = _haversine_km(last["lat"], last["lng"], poi["lat"], poi["lng"])
                if d < best_dist:
                    best_dist = d
                    best_idx = i
            total_distance += best_dist
            ordered.append(remaining.pop(best_idx))

        estimated_hours = sum(p.get("duration_hours", 1.0) for p in ordered)
        # Add travel time estimate: ~15 min per km walking
        estimated_hours += total_distance * 0.25

        return {
            "ordered_pois": ordered,
            "total_distance_km": round(total_distance, 2),
            "estimated_hours": round(estimated_hours, 1),
        }

    return assemble_day_plan
