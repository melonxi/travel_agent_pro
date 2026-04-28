from __future__ import annotations

import logging
import time

from api.orchestration.chat.events import done_event, event_json

logger = logging.getLogger(__name__)


async def finalize_agent_run(
    *,
    deps,
    session,
    plan,
    messages,
    run,
    phase_before_run: int,
):
    if run.status == "running":
        run.status = "completed"
        run.finished_at = time.time()

    await deps.state_mgr.save(plan)
    # Source of truth for DB persistence is runtime_view (the `messages` arg /
    # session["messages"]). All main-path appends mutate runtime_view; the
    # on_phase_rebuild callback also flushes runtime_view tagged with from_phase
    # before resetting it. session["history_messages"] is an informational
    # observation track only — using it here would silently drop any append
    # site that didn't go through append_dual_track. See Task 6.5 fix.
    new_count = await deps.persist_messages(
        plan.session_id,
        messages,
        phase=plan.phase,
        phase3_step=plan.phase3_step,
        persisted_count=session.get("persisted_count", 0),
    )
    session["persisted_count"] = new_count
    await deps.session_store.update(
        plan.session_id,
        phase=plan.phase,
        title=deps.generate_title(plan),
        last_run_id=run.run_id,
        last_run_status=run.status,
        last_run_error=run.error_code,
    )
    if plan.phase != phase_before_run:
        await deps.archive_store.save_snapshot(
            plan.session_id,
            plan.phase,
            event_json(plan.to_dict()),
        )
    if plan.phase == 7:
        await deps.archive_store.save(
            plan.session_id,
            event_json(plan.to_dict()),
            summary=deps.generate_title(plan),
        )
        await deps.session_store.update(plan.session_id, status="archived")
        if deps.config.memory.enabled:
            try:
                await deps.append_archived_trip_episode_once(
                    user_id=session["user_id"],
                    session_id=plan.session_id,
                    plan=plan,
                )
            except Exception:
                pass

    yield event_json({"type": "state_update", "plan": plan.to_dict()})
    if run.status in {"completed", "cancelled"}:
        yield done_event(run)


async def persist_run_safely(*, deps, session, plan, messages, run) -> None:
    try:
        if run.status == "running":
            run.status = "cancelled"
            run.finished_at = time.time()
        await deps.state_mgr.save(plan)
        # Same rationale as finalize_agent_run: persist runtime_view, not
        # history_view (see Task 6.5 fix).
        new_count = await deps.persist_messages(
            plan.session_id,
            messages,
            phase=plan.phase,
            phase3_step=plan.phase3_step,
            persisted_count=session.get("persisted_count", 0),
        )
        session["persisted_count"] = new_count
        await deps.session_store.update(
            plan.session_id,
            phase=plan.phase,
            title=deps.generate_title(plan),
            last_run_id=run.run_id,
            last_run_status=run.status,
            last_run_error=run.error_code,
        )
    except Exception:
        logger.warning(
            "保底持久化失败 session=%s",
            plan.session_id,
            exc_info=True,
        )
