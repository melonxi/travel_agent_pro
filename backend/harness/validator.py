# backend/harness/validator.py
from __future__ import annotations

import logging
import re
from typing import Any

from harness.feasibility import check_feasibility
from state.models import (
    Budget,
    DateRange,
    TravelPlanState,
    normalize_pace_value,
)

logger = logging.getLogger(__name__)

_LOCK_BUDGET_RATIO = 0.8
_AIRPORT_GROUPS = {
    "haneda": {"羽田", "haneda", "hnd"},
    "narita": {"成田", "narita", "nrt"},
    "kansai": {"关西", "関西", "kansai", "kix"},
    "itami": {"伊丹", "itami", "itm"},
}
_FLIGHT_NO_RE = re.compile(r"\b[A-Z]{1,3}\s?\d{2,4}\b", re.IGNORECASE)


def _time_to_minutes(t: str) -> int | None:
    """Convert 'HH:MM' to minutes since midnight. Returns None on bad format."""
    try:
        if isinstance(t, str):
            match = re.search(r"(?:[01]\d|2[0-3]):[0-5]\d", t)
            if match:
                t = match.group(0)
        h, m = map(int, t.split(":"))
        return h * 60 + m
    except (ValueError, AttributeError):
        return None


def _coerce_budget(value: Any) -> Budget | None:
    if isinstance(value, Budget):
        return value
    if isinstance(value, dict) and "total" in value:
        try:
            return Budget(
                total=float(value["total"]), currency=value.get("currency", "CNY")
            )
        except (TypeError, ValueError):
            return None
    return None


def _coerce_dates(value: Any) -> DateRange | None:
    if isinstance(value, DateRange):
        return value
    if isinstance(value, dict) and value.get("start") and value.get("end"):
        try:
            dates = DateRange(start=str(value["start"]), end=str(value["end"]))
            _ = dates.total_days
            return dates
        except (TypeError, ValueError):
            return None
    return None


def _activity_total_cost(plan: TravelPlanState) -> float:
    return sum(act.cost for day in plan.daily_plans for act in day.activities)


def _numeric_price(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("¥", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return 0.0
    return 0.0


def _selected_transport_cost(plan: TravelPlanState) -> float:
    transport = plan.selected_transport
    if not isinstance(transport, dict):
        return 0.0

    segments = transport.get("segments")
    if isinstance(segments, list):
        return sum(
            _numeric_price(segment.get("price"))
            for segment in segments
            if isinstance(segment, dict)
        )

    return _numeric_price(transport.get("price"))


def _trip_nights(plan: TravelPlanState) -> int:
    if not plan.dates:
        return 1
    try:
        return max(plan.dates.total_days, 1)
    except ValueError:
        return 1


def _selected_accommodation_nightly_price(plan: TravelPlanState) -> float:
    if not plan.accommodation:
        return 0.0

    selected_names = {
        value
        for value in (plan.accommodation.hotel, plan.accommodation.area)
        if isinstance(value, str) and value
    }
    for option in plan.accommodation_options:
        if not isinstance(option, dict):
            continue
        option_names = {
            value
            for value in (
                option.get("name"),
                option.get("hotel"),
                option.get("hotel_name"),
                option.get("location"),
                option.get("area"),
            )
            if isinstance(value, str) and value
        }
        if selected_names and selected_names.isdisjoint(option_names):
            continue
        price = _numeric_price(
            option.get("price_per_night")
            or option.get("nightly_price")
            or option.get("price")
        )
        if price > 0:
            return price

    return 0.0


def _validate_time_conflicts(plan: TravelPlanState) -> list[str]:
    errors: list[str] = []
    for day in plan.daily_plans:
        acts = day.activities
        for i in range(1, len(acts)):
            prev = acts[i - 1]
            curr = acts[i]
            prev_end = _time_to_minutes(prev.end_time)
            curr_start = _time_to_minutes(curr.start_time)
            if prev_end is None or curr_start is None:
                logger.warning(
                    "Day %s: skipping time check for %s→%s (bad time format)",
                    day.day,
                    prev.name,
                    curr.name,
                )
                continue
            travel = curr.transport_duration_min

            if prev_end + travel > curr_start:
                gap = curr_start - prev_end
                errors.append(
                    f"Day {day.day}: {prev.name}→{curr.name} "
                    f"时间冲突（{prev.name} {prev.end_time} 结束，"
                    f"交通需 {travel}min，但 {curr.name} {curr.start_time} 开始，"
                    f"间隔仅 {gap}min）"
                )

    return errors


def _activity_text(day: Any) -> str:
    parts = [getattr(day, "tips", "") or ""]
    for activity in getattr(day, "activities", []) or []:
        parts.extend(
            [
                getattr(activity, "name", "") or "",
                getattr(getattr(activity, "location", None), "name", "") or "",
                getattr(activity, "transport_from_prev", "") or "",
                getattr(activity, "notes", "") or "",
            ]
        )
    return " ".join(str(part) for part in parts if part)


def _transport_direction_payloads(
    transport: dict[str, Any],
    direction: str,
) -> list[dict[str, Any]]:
    keys = ("outbound", "going") if direction == "outbound" else ("return", "inbound")
    for key in keys:
        nested = transport.get(key)
        if isinstance(nested, dict):
            return [nested]
    segments = transport.get("segments")
    if isinstance(segments, list):
        return [
            segment
            for segment in segments
            if isinstance(segment, dict) and segment.get("direction") == direction
        ]
    return [transport]


def _flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value)
    return str(value or "")


def _airport_groups_in_text(text: str) -> set[str]:
    lowered = text.lower()
    groups: set[str] = set()
    for group, aliases in _AIRPORT_GROUPS.items():
        if any(alias.lower() in lowered for alias in aliases):
            groups.add(group)
    return groups


def _flight_numbers_in_text(text: str) -> set[str]:
    return {
        match.group(0).replace(" ", "").upper()
        for match in _FLIGHT_NO_RE.finditer(text)
    }


def _validate_transport_day_alignment(plan: TravelPlanState) -> list[str]:
    transport = plan.selected_transport
    if not isinstance(transport, dict) or not plan.daily_plans:
        return []

    errors: list[str] = []
    sorted_days = sorted(plan.daily_plans, key=lambda day: day.day)
    checks = [
        ("outbound", sorted_days[0], "到达日"),
        ("return", sorted_days[-1], "离开日"),
    ]
    for direction, day, label in checks:
        payload_text = _flatten_text(
            _transport_direction_payloads(transport, direction)
        )
        expected_airports = _airport_groups_in_text(payload_text)
        expected_flights = _flight_numbers_in_text(payload_text)
        if not expected_airports and not expected_flights:
            continue

        day_text = _activity_text(day)
        actual_airports = _airport_groups_in_text(day_text)
        actual_flights = _flight_numbers_in_text(day_text)
        conflicting_airports = actual_airports - expected_airports
        conflicting_flights = actual_flights - expected_flights
        if expected_airports and conflicting_airports:
            errors.append(
                f"{label}机场与已锁定交通不一致：锁定交通指向 "
                f"{sorted(expected_airports)}，但 Day {day.day} 写了 "
                f"{sorted(actual_airports)}"
            )
        if expected_flights and conflicting_flights:
            errors.append(
                f"{label}航班号与已锁定交通不一致：锁定交通为 "
                f"{sorted(expected_flights)}，但 Day {day.day} 写了 "
                f"{sorted(actual_flights)}"
            )
    return errors


def _transport_time(transport: dict[str, Any], direction: str) -> int | None:
    payloads = _transport_direction_payloads(transport, direction)
    if not payloads:
        return None
    payload = payloads[0]
    if direction == "outbound":
        return _time_to_minutes(
            payload.get("arrival_time", "")
            or payload.get("arr_time", "")
            or payload.get("arrival", "")
        )
    return _time_to_minutes(
        payload.get("departure_time", "")
        or payload.get("dep_time", "")
        or payload.get("departure", "")
    )


def _validate_transport_time_buffers(plan: TravelPlanState) -> list[str]:
    transport = plan.selected_transport
    if not isinstance(transport, dict) or not plan.daily_plans:
        return []

    errors: list[str] = []
    sorted_days = sorted(plan.daily_plans, key=lambda day: day.day)
    arrival_min = _transport_time(transport, "outbound")
    if arrival_min is not None:
        first_day = sorted_days[0]
        if first_day.activities:
            first_start = _time_to_minutes(first_day.activities[0].start_time)
            if first_start is not None and first_start < arrival_min + 120:
                errors.append(
                    f"Day {first_day.day}: 首活动开始时间过早，距到达不足 2 小时"
                )

    departure_min = _transport_time(transport, "return")
    if departure_min is not None:
        last_day = sorted_days[-1]
        if last_day.activities:
            last_end = _time_to_minutes(last_day.activities[-1].end_time)
            if last_end is not None and last_end > departure_min - 180:
                errors.append(
                    f"Day {last_day.day}: 末活动结束过晚，距离开不足 3 小时"
                )
    return errors


def _explicit_pace(plan: TravelPlanState) -> str | None:
    brief_pace = normalize_pace_value((plan.trip_brief or {}).get("pace"))
    if brief_pace:
        return brief_pace
    for preference in plan.preferences:
        if preference.key == "pace":
            normalized = normalize_pace_value(preference.value)
            if normalized:
                return normalized
    for constraint in plan.constraints:
        normalized = normalize_pace_value(constraint.description)
        if normalized:
            return normalized
    return None


def _validate_pace_constraints(plan: TravelPlanState) -> list[str]:
    pace = _explicit_pace(plan)
    if not pace:
        return []
    limits = {"relaxed": 3, "balanced": 4, "intensive": 5}
    max_activities = limits[pace]
    errors: list[str] = []
    for day in plan.daily_plans:
        activity_count = len(day.activities)
        if activity_count > max_activities:
            errors.append(
                f"Day {day.day}: {activity_count} 个活动超出 "
                f"{pace} 节奏上限 {max_activities}"
            )
    return errors


def validate_hard_constraints(plan: TravelPlanState) -> list[str]:
    errors: list[str] = []

    # Time conflict check
    errors.extend(_validate_time_conflicts(plan))

    # Budget check（P0-6：全口径预算 = 活动 + 已锁交通 + 已锁住宿）
    if plan.budget and plan.daily_plans:
        activity_cost = _activity_total_cost(plan)
        transport_cost = _selected_transport_cost(plan)
        accommodation_cost = (
            _selected_accommodation_nightly_price(plan) * _trip_nights(plan)
            if plan.accommodation
            else 0.0
        )
        total_cost = activity_cost + transport_cost + accommodation_cost
        if total_cost > plan.budget.total:
            over = total_cost - plan.budget.total
            errors.append(
                f"总费用 ¥{total_cost:.0f}（活动 ¥{activity_cost:.0f}"
                f" + 交通 ¥{transport_cost:.0f} + 住宿 ¥{accommodation_cost:.0f}）"
                f"超出预算 ¥{plan.budget.total:.0f}，超支 ¥{over:.0f}"
            )

    # Day count check
    if plan.dates and plan.daily_plans:
        allowed_days = plan.dates.total_days
        actual_days = len(plan.daily_plans)
        if actual_days > allowed_days:
            errors.append(
                f"天数超限：规划了 {actual_days} 天行程，但只有 {allowed_days} 天可用"
            )
        expected_days = set(range(1, allowed_days + 1))
        actual_day_numbers = {day.day for day in plan.daily_plans}
        if actual_days >= allowed_days and (
            len(actual_day_numbers) != actual_days
            or actual_day_numbers != expected_days
        ):
            errors.append(
                "天数覆盖不完整：daily_plans 必须唯一覆盖 "
                f"1 到 {allowed_days}，当前为 {sorted(actual_day_numbers)}"
            )

    errors.extend(_validate_transport_day_alignment(plan))
    errors.extend(_validate_transport_time_buffers(plan))
    errors.extend(_validate_pace_constraints(plan))

    return errors


def validate_incremental(
    plan: TravelPlanState,
    field: str,
    value: Any,
) -> list[str]:
    if field == "budget":
        budget = _coerce_budget(value) or plan.budget
        if not budget:
            return []
        if budget.total <= 0:
            return ["budget.total 不能为负数或零"]
        if plan.daily_plans:
            total_cost = _activity_total_cost(plan)
            if total_cost > budget.total:
                return [f"总费用 ¥{total_cost:.0f} 超出预算 ¥{budget.total:.0f}"]
        return []

    if field == "dates":
        dates = _coerce_dates(value) or plan.dates
        if not dates:
            return []
        errors: list[str] = []
        if dates.total_days < 1:
            errors.append("旅行天数必须至少 1 天")
        if plan.destination:
            budget_total = int(plan.budget.total) if plan.budget else None
            result = check_feasibility(plan.destination, budget_total, dates.total_days)
            errors.extend(result.reasons)
        return errors

    if field == "daily_plans":
        return _validate_time_conflicts(plan)

    return []


def validate_lock_budget(plan: TravelPlanState) -> list[str]:
    if not plan.budget or plan.budget.total <= 0:
        return []

    transport_cost = _selected_transport_cost(plan)
    accommodation_cost = (
        _selected_accommodation_nightly_price(plan) * _trip_nights(plan)
        if plan.accommodation
        else 0.0
    )
    locked_total = transport_cost + accommodation_cost
    if locked_total <= 0:
        return []

    ratio = locked_total / plan.budget.total
    percent = round(ratio * 100)
    remaining = plan.budget.total - locked_total

    if ratio > 1:
        return [f"交通+住宿已占预算的 {percent}%，超过预算 ¥{abs(remaining):.0f}"]

    if ratio >= _LOCK_BUDGET_RATIO:
        return [f"交通+住宿已占预算的 {percent}%，仅剩 ¥{remaining:.0f} 用于活动和餐饮"]

    return []


def validate_day_conflicts(plan: TravelPlanState, day_numbers: list[int]) -> dict:
    """检查指定天数的时间冲突。

    Returns:
        {"conflicts": list[str], "has_severe_conflicts": bool}
        严重冲突定义：相邻活动间隔为负（前一个结束+交通 > 后一个开始）。
    """
    all_errors = _validate_time_conflicts(plan)
    day_set = set(day_numbers)
    relevant = [e for e in all_errors if any(f"Day {d}:" in e for d in day_set)]
    return {
        "conflicts": relevant,
        "has_severe_conflicts": len(relevant) > 0,
    }
