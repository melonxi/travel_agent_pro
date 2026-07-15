# backend/tools/check_availability.py
from __future__ import annotations

from datetime import date as date_cls

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "place_name": {
            "type": "string",
            "description": "地点名称，如 '金阁寺' '卢浮宫'",
        },
        "date": {"type": "string", "description": "查询日期，如 '2024-07-15'"},
    },
    "required": ["place_name", "date"],
}

# Google weekday_text 顺序为 Monday..Sunday；date.weekday() 0=Monday
_WEEKDAY_PREFIXES = (
    ("monday", "星期一"),
    ("tuesday", "星期二"),
    ("wednesday", "星期三"),
    ("thursday", "星期四"),
    ("friday", "星期五"),
    ("saturday", "星期六"),
    ("sunday", "星期日"),
)

_CLOSED_MARKERS = ("closed", "休息", "闭馆", "歇业")


def _weekday_entry(weekday_text: list, target: date_cls) -> str | None:
    """从 weekday_text 中找到目标日期对应星期几的营业时间行。"""
    en_prefix, zh_prefix = _WEEKDAY_PREFIXES[target.weekday()]
    for line in weekday_text:
        if not isinstance(line, str):
            continue
        lowered = line.lower()
        if lowered.startswith(en_prefix) or line.startswith(zh_prefix):
            return line
    return None


def make_check_availability_tool(api_keys: ApiKeysConfig):
    @tool(
        name="check_availability",
        description="""查询地点在指定日期（按星期几的常规营业时间）是否开放。
Use when: Phase 2 skeleton/lock 或 Phase 3 需要确认景点的开放状态。
Don't use when: 已知开放时间或不需要确认。
返回该日期星期几对应的常规营业时间判断。
⚠️ 局限：基于每周常规营业时间，无法判断节假日/临时闭馆；
关键景点（必去项）请再用 web_search 验证目标日期是否有特殊闭馆安排。""",
        phases=[2, 3],
        parameters=_PARAMETERS,
        human_label="查景点可用性",
    )
    async def check_availability(place_name: str, date: str) -> dict:
        if not api_keys.google_maps:
            raise ToolError(
                "Google Maps API key not configured",
                error_code="NO_API_KEY",
                suggestion="Set GOOGLE_MAPS_API_KEY",
            )

        try:
            target_date = date_cls.fromisoformat(date)
        except ValueError:
            raise ToolError(
                f"date 格式无效: {date!r}",
                error_code="INVALID_VALUE",
                suggestion="请用 YYYY-MM-DD 格式，如 2026-07-15",
            )

        # Step 1: Find the place
        async with httpx.AsyncClient() as client:
            find_resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
                params={
                    "input": place_name,
                    "inputtype": "textquery",
                    "fields": "place_id,name",
                    "key": api_keys.google_maps,
                },
                timeout=10,
            )
            find_resp.raise_for_status()
            find_data = find_resp.json()

        candidates = find_data.get("candidates", [])
        if not candidates:
            return {
                "place_name": place_name,
                "date": date,
                "open_on_date": None,
                "hours": "未找到该地点",
                "note": "未找到该地点，请用 web_search 核实名称或开放信息",
            }

        place_id = candidates[0].get("place_id", "")

        # Step 2: Get place details for opening hours
        async with httpx.AsyncClient() as client:
            detail_resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/details/json",
                params={
                    "place_id": place_id,
                    "fields": "opening_hours,name",
                    "key": api_keys.google_maps,
                },
                timeout=10,
            )
            detail_resp.raise_for_status()
            detail_data = detail_resp.json()

        result = detail_data.get("result", {})
        opening_hours = result.get("opening_hours", {})
        weekday_text = opening_hours.get("weekday_text", [])

        # 用目标日期的星期几对照每周常规营业时间做真实判断，
        # 而不是回传查询时刻的 open_now（那与目标日期无关）。
        day_entry = _weekday_entry(weekday_text, target_date)
        open_on_date: bool | None = None
        if day_entry is not None:
            lowered = day_entry.lower()
            open_on_date = not any(m in lowered for m in _CLOSED_MARKERS)

        return {
            "place_name": place_name,
            "date": date,
            "open_on_date": open_on_date,
            "hours_on_date": day_entry or "该日营业时间未知",
            "hours": weekday_text if weekday_text else "营业时间未知",
            "open_now_at_query_time": opening_hours.get("open_now"),
            "note": (
                "基于每周常规营业时间判断，无法覆盖节假日/临时闭馆；"
                "必去景点请再用 web_search 验证该日期的特殊安排。"
            ),
        }

    return check_availability
