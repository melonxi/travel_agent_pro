from __future__ import annotations

from collections.abc import Callable

from state.models import TravelPlanState


async def persist_phase4_deliverables(
    plan: TravelPlanState,
    result_data: dict,
    *,
    state_mgr,
    now_iso: Callable[[], str],
) -> None:
    # 冻结改为版本化：已有交付物时允许重新生成（如 backtrack 4→3 后重排），
    # 新版本覆盖文件并递增 version，而不是 once-only 直接 raise。
    previous_version = 0
    if plan.deliverables:
        try:
            previous_version = int(plan.deliverables.get("version", 1))
        except (TypeError, ValueError):
            previous_version = 1

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
        "version": str(previous_version + 1),
    }
