# backend/harness/validator.py
from __future__ import annotations

import logging
from typing import Any

from harness.feasibility import check_feasibility
from state.models import Budget, DateRange, TravelPlanState

logger = logging.getLogger(__name__)

_LOCK_BUDGET_RATIO = 0.8


def _time_to_minutes(t: str) -> int | None:
    """Convert 'HH:MM' to minutes since midnight. Returns None on bad format."""
    try:
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


def validate_hard_constraints(plan: TravelPlanState) -> list[str]:
    errors: list[str] = []

    # Time conflict check
    errors.extend(_validate_time_conflicts(plan))

    # Budget check
    if plan.budget and plan.daily_plans:
        total_cost = _activity_total_cost(plan)
        if total_cost > plan.budget.total:
            errors.append(f"总费用 ¥{total_cost:.0f} 超出预算 ¥{plan.budget.total:.0f}")

    # Day count check
    if plan.dates and plan.daily_plans:
        allowed_days = plan.dates.total_days
        actual_days = len(plan.daily_plans)
        if actual_days > allowed_days:
            errors.append(
                f"天数超限：规划了 {actual_days} 天行程，但只有 {allowed_days} 天可用"
            )

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
