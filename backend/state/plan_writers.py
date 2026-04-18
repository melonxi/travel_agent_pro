"""Thin write layer for TravelPlanState mutations.

Every plan-writing operation is a pure function: take plan + data, mutate plan.
Type validation lives in the tool wrappers (ToolError); this layer performs
defensive assertions that should never fire in production.

All plan-writing tools call these shared functions, ensuring identical
write behavior across split tools.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from state.intake import (
    parse_budget_value,
    parse_dates_value,
    parse_travelers_value,
)
from state.models import (
    Accommodation,
    Constraint,
    DayPlan,
    Preference,
    TravelPlanState,
)


# ---------------------------------------------------------------------------
# Category A: high-risk structured writes
# ---------------------------------------------------------------------------


def write_skeleton_plans(plan: TravelPlanState, plans: list[dict]) -> None:
    """Replace skeleton_plans wholesale."""
    assert isinstance(plans, list), f"Expected list, got {type(plans).__name__}"
    plan.skeleton_plans = plans


def write_selected_skeleton_id(plan: TravelPlanState, skeleton_id: str) -> None:
    """Lock a skeleton plan by ID."""
    assert isinstance(skeleton_id, str), (
        f"Expected str, got {type(skeleton_id).__name__}"
    )
    plan.selected_skeleton_id = skeleton_id


def clear_selected_skeleton_id(plan: TravelPlanState) -> None:
    """Clear any previously selected skeleton plan."""
    plan.selected_skeleton_id = None


def write_candidate_pool(plan: TravelPlanState, pool: list[dict]) -> None:
    assert isinstance(pool, list), f"Expected list, got {type(pool).__name__}"
    plan.candidate_pool = pool


def write_shortlist(plan: TravelPlanState, items: list[dict]) -> None:
    assert isinstance(items, list), f"Expected list, got {type(items).__name__}"
    plan.shortlist = items


def write_transport_options(plan: TravelPlanState, options: list[dict]) -> None:
    assert isinstance(options, list), f"Expected list, got {type(options).__name__}"
    plan.transport_options = options


def write_selected_transport(plan: TravelPlanState, choice: dict) -> None:
    assert isinstance(choice, dict), f"Expected dict, got {type(choice).__name__}"
    plan.selected_transport = choice


def write_accommodation_options(plan: TravelPlanState, options: list[dict]) -> None:
    assert isinstance(options, list), f"Expected list, got {type(options).__name__}"
    plan.accommodation_options = options


def write_accommodation(
    plan: TravelPlanState, area: str, hotel: str | None = None
) -> None:
    assert isinstance(area, str), f"Expected str for area, got {type(area).__name__}"
    plan.accommodation = Accommodation(area=area, hotel=hotel)


def write_risks(plan: TravelPlanState, risks: list[dict]) -> None:
    assert isinstance(risks, list), f"Expected list, got {type(risks).__name__}"
    plan.risks = risks


def write_alternatives(plan: TravelPlanState, alternatives: list[dict]) -> None:
    assert isinstance(alternatives, list), (
        f"Expected list, got {type(alternatives).__name__}"
    )
    plan.alternatives = alternatives


def write_trip_brief(plan: TravelPlanState, fields: dict) -> None:
    """Merge fields into existing trip_brief (incremental update)."""
    assert isinstance(fields, dict), f"Expected dict, got {type(fields).__name__}"
    plan.trip_brief.update(fields)


# ---------------------------------------------------------------------------
# Category A: daily plans
# ---------------------------------------------------------------------------


def _sort_daily_plans(plan: TravelPlanState) -> None:
    plan.daily_plans.sort(key=lambda day: day.day)


def append_one_day_plan(plan: TravelPlanState, day_dict: dict) -> None:
    """Append a single day to daily_plans."""
    assert isinstance(day_dict, dict), f"Expected dict, got {type(day_dict).__name__}"
    plan.daily_plans.append(DayPlan.from_dict(day_dict))
    _sort_daily_plans(plan)


def replace_all_daily_plans(plan: TravelPlanState, days: list[dict]) -> None:
    """Replace the entire daily_plans list."""
    assert isinstance(days, list), f"Expected list, got {type(days).__name__}"
    plan.daily_plans = [DayPlan.from_dict(day) for day in days]
    _sort_daily_plans(plan)


def replace_one_day_plan(plan: TravelPlanState, day_dict: dict) -> None:
    """Replace an existing day in daily_plans."""
    assert isinstance(day_dict, dict), f"Expected dict, got {type(day_dict).__name__}"
    day_number = day_dict["day"]
    plan.daily_plans = [
        DayPlan.from_dict(day_dict) if existing.day == day_number else existing
        for existing in plan.daily_plans
    ]
    _sort_daily_plans(plan)


# ---------------------------------------------------------------------------
# Category B: phrase-tolerant basic writes
# ---------------------------------------------------------------------------


def write_destination(plan: TravelPlanState, value: Any) -> None:
    if isinstance(value, dict):
        plan.destination = str(value.get("name", value))
    else:
        plan.destination = str(value)


def write_dates(plan: TravelPlanState, value: Any) -> None:
    plan.dates = parse_dates_value(value)


def write_travelers(plan: TravelPlanState, value: Any) -> None:
    plan.travelers = parse_travelers_value(value)


def write_budget(plan: TravelPlanState, value: Any) -> None:
    plan.budget = parse_budget_value(value)


def write_departure_city(plan: TravelPlanState, value: Any) -> None:
    if isinstance(value, dict):
        city = (
            value.get("name")
            or value.get("city")
            or value.get("departure_city")
            or value.get("from")
        )
        plan.trip_brief["departure_city"] = str(city or value)
        return
    plan.trip_brief["departure_city"] = str(value)


# ---------------------------------------------------------------------------
# Category C: append-semantics
# ---------------------------------------------------------------------------


def _stringify_preference_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " · ".join(
            part
            for part in (_stringify_preference_value(item) for item in value)
            if part
        )
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            text = _stringify_preference_value(item)
            if text:
                parts.append(f"{key}: {text}")
        return "；".join(parts)
    return str(value)


def _normalize_append_items(items: Any) -> list[Any]:
    if isinstance(items, list):
        return items
    if isinstance(items, (dict, str)) or not isinstance(items, Iterable):
        return [items]
    raise AssertionError(
        f"Expected appendable item or list, got {type(items).__name__}"
    )


def append_preferences(plan: TravelPlanState, items: Any) -> None:
    """Append one or more preferences."""
    for item in _normalize_append_items(items):
        if isinstance(item, dict):
            if "key" in item:
                plan.preferences.append(Preference.from_dict(item))
            else:
                for key, value in item.items():
                    plan.preferences.append(
                        Preference(
                            key=str(key),
                            value=_stringify_preference_value(value),
                        )
                    )
        elif isinstance(item, str):
            plan.preferences.append(Preference(key=item, value=""))
        else:
            plan.preferences.append(Preference(key=str(item), value=""))


def append_constraints(plan: TravelPlanState, items: Any) -> None:
    """Append one or more constraints."""
    for item in _normalize_append_items(items):
        if isinstance(item, dict):
            constraint_type = str(item.get("type", "soft"))
            description = str(item.get("description") or item.get("summary") or item)
            plan.constraints.append(
                Constraint(type=constraint_type, description=description)
            )
        else:
            plan.constraints.append(Constraint(type="soft", description=str(item)))


# ---------------------------------------------------------------------------
# Category D: standalone action
# ---------------------------------------------------------------------------


def execute_backtrack(
    plan: TravelPlanState,
    to_phase: int,
    reason: str,
) -> dict:
    """Execute a phase backtrack. Returns result dict for tool response."""
    from phase.backtrack import BacktrackService

    if to_phase == 2:
        to_phase = 1
    if to_phase >= plan.phase:
        raise ValueError(
            f"只能回退到更早的阶段，当前阶段: {plan.phase}，目标: {to_phase}"
        )
    from_phase = plan.phase
    service = BacktrackService()
    service.execute(plan, to_phase, reason, snapshot_path="")
    return {
        "backtracked": True,
        "from_phase": from_phase,
        "to_phase": to_phase,
        "reason": reason,
        "next_action": "请向用户确认回退结果，不要继续调用其他工具",
    }
