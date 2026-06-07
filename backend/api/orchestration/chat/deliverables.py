from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def has_frozen_phase4_deliverables(plan: object) -> bool:
    return bool(
        getattr(plan, "phase", None) == 4
        and getattr(plan, "deliverables", None)
    )


async def finalize_pending_phase4_deliverables(
    *,
    deps,
    session,
    plan,
    force: bool = False,
) -> bool:
    pending = session.get("_pending_phase4_deliverables")
    if not isinstance(pending, dict):
        return False

    decision = session.get("_phase4_deliverables_quality")
    if isinstance(decision, dict):
        if (
            decision.get("tool_call_id")
            and decision.get("tool_call_id") != pending.get("tool_call_id")
        ):
            return False
        if decision.get("status") == "blocked":
            session.pop("_pending_phase4_deliverables", None)
            session.pop("_phase4_deliverables_quality", None)
            return False
        if decision.get("status") != "approved" and not force:
            return False
    elif not force:
        return False

    result_data = pending.get("result_data")
    if not isinstance(result_data, dict):
        session.pop("_pending_phase4_deliverables", None)
        session.pop("_phase4_deliverables_quality", None)
        return False

    await deps.persist_phase4_deliverables(plan, result_data)
    session.pop("_pending_phase4_deliverables", None)
    session.pop("_phase4_deliverables_quality", None)
    await deps.state_mgr.save(plan)
    try:
        await deps.session_store.update(
            plan.session_id,
            phase=plan.phase,
            title=deps.generate_title(plan),
        )
    except Exception:
        logger.warning(
            "Phase 4 交付物 session meta 更新失败 session=%s",
            plan.session_id,
            exc_info=True,
        )
    return True
