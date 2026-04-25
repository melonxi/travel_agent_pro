from __future__ import annotations

from collections.abc import Callable

from state.models import TravelPlanState


async def persist_phase7_deliverables(
    plan: TravelPlanState,
    result_data: dict,
    *,
    state_mgr,
    now_iso: Callable[[], str],
) -> None:
    if plan.deliverables:
        raise RuntimeError("deliverables already frozen")

    travel_md = str(result_data["travel_plan_markdown"])
    checklist_md = str(result_data["checklist_markdown"])

    try:
        await state_mgr.save_deliverable(plan.session_id, "travel_plan.md", travel_md)
        await state_mgr.save_deliverable(
            plan.session_id,
            "checklist.md",
            checklist_md,
        )
    except Exception:
        await state_mgr.clear_deliverables(plan.session_id)
        raise

    plan.deliverables = {
        "travel_plan_md": "travel_plan.md",
        "checklist_md": "checklist.md",
        "generated_at": now_iso(),
    }
