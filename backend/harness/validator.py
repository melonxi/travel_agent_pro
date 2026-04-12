# backend/harness/validator.py
from __future__ import annotations

import logging
from state.models import TravelPlanState

logger = logging.getLogger(__name__)


def _time_to_minutes(t: str) -> int | None:
    """Convert 'HH:MM' to minutes since midnight. Returns None on bad format."""
    try:
        h, m = map(int, t.split(":"))
        return h * 60 + m
    except (ValueError, AttributeError):
        return None


def validate_hard_constraints(plan: TravelPlanState) -> list[str]:
    errors: list[str] = []

    # Time conflict check
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
                    day.day, prev.name, curr.name,
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

    # Budget check
    if plan.budget and plan.daily_plans:
        total_cost = sum(act.cost for day in plan.daily_plans for act in day.activities)
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
