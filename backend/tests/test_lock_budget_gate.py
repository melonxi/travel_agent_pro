from __future__ import annotations

from harness.validator import validate_lock_budget
from state.models import Accommodation, Budget, DateRange, TravelPlanState


def _plan(
    *,
    budget: float | None = 10_000,
    transport=None,
    accommodation=None,
) -> TravelPlanState:
    return TravelPlanState(
        session_id="s1",
        dates=DateRange(start="2026-05-01", end="2026-05-05"),
        budget=Budget(total=budget) if budget is not None else None,
        selected_transport=transport,
        accommodation=accommodation,
    )


def test_lock_budget_allows_sixty_percent() -> None:
    plan = _plan(
        transport={"segments": [{"price": 3_000}]},
        accommodation=Accommodation(area="新宿", hotel="A"),
    )
    plan.accommodation_options = [{"name": "A", "price_per_night": 750}]

    assert validate_lock_budget(plan) == []


def test_lock_budget_warns_above_eighty_percent() -> None:
    plan = _plan(
        transport={"segments": [{"price": 5_000}]},
        accommodation=Accommodation(area="新宿", hotel="A"),
    )
    plan.accommodation_options = [{"name": "A", "price_per_night": 875}]

    errors = validate_lock_budget(plan)

    assert any("85%" in error and "仅剩" in error for error in errors)


def test_lock_budget_errors_above_total_budget() -> None:
    plan = _plan(
        transport={"segments": [{"price": 7_000}]},
        accommodation=Accommodation(area="新宿", hotel="A"),
    )
    plan.accommodation_options = [{"name": "A", "price_per_night": 1_000}]

    errors = validate_lock_budget(plan)

    assert any("超过预算" in error for error in errors)


def test_lock_budget_skips_without_budget() -> None:
    plan = _plan(budget=None, transport={"price": 9_000})

    assert validate_lock_budget(plan) == []


def test_lock_budget_skips_without_locked_items() -> None:
    plan = _plan()

    assert validate_lock_budget(plan) == []


def test_lock_budget_warns_for_transport_only() -> None:
    plan = _plan(transport={"price": 8_000})

    errors = validate_lock_budget(plan)

    assert any("80%" in error for error in errors)
