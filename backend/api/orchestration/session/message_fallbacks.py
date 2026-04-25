from __future__ import annotations

from datetime import date

from phase.router import PhaseRouter
from state.intake import extract_trip_facts
from state.models import TravelPlanState


def _should_replace_dates_with_message_dates(
    current_dates,
    message_dates,
    *,
    today: date,
) -> bool:
    if message_dates is None:
        return False
    if current_dates is None:
        return True

    try:
        current_start = date.fromisoformat(current_dates.start)
        message_start = date.fromisoformat(message_dates.start)
    except ValueError:
        return False

    return current_start < today <= message_start


async def _apply_message_fallbacks(
    plan: TravelPlanState,
    message: str,
    phase_router: PhaseRouter,
    *,
    today: date | None = None,
) -> None:
    today = today or date.today()
    facts = extract_trip_facts(message, today=today)
    changed = False

    destination = facts.get("destination")
    if destination and not plan.destination:
        plan.destination = destination
        changed = True

    budget = facts.get("budget")
    if budget and not plan.budget:
        plan.budget = budget
        changed = True

    travelers = facts.get("travelers")
    if travelers and not plan.travelers:
        plan.travelers = travelers
        changed = True

    message_dates = facts.get("dates")
    if _should_replace_dates_with_message_dates(
        plan.dates,
        message_dates,
        today=today,
    ):
        plan.dates = message_dates
        changed = True

    if changed:
        await phase_router.check_and_apply_transition(plan)
