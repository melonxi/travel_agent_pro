from __future__ import annotations

import logging
import time

from api.orchestration.chat.events import done_event, event_json

logger = logging.getLogger(__name__)


async def persist_unflushed_messages(
    *,
    deps,
    session,
    plan,
    messages,
    phase: int,
    phase3_step: str | None,
    run_id: str | None,
    trip_id: str | None,
    context_epoch: int | None = None,
    rebuild_reason: str | None = None,
) -> None:
    next_history_seq = int(session.get("next_history_seq", 0))
    active_context_epoch = (
        int(session.get("current_context_epoch", 0))
        if context_epoch is None
        else context_epoch
    )
    active_rebuild_reason = (
        session.pop("_next_rebuild_reason", None)
        if rebuild_reason is None
        else rebuild_reason
    )
    next_history_seq = await deps.persist_messages(
        plan.session_id,
        messages,
        phase=phase,
        phase3_step=phase3_step,
        run_id=run_id,
        trip_id=trip_id,
        next_history_seq=next_history_seq,
        context_epoch=active_context_epoch,
        rebuild_reason=active_rebuild_reason,
    )
    session["next_history_seq"] = next_history_seq


def make_context_rebuild_callback(*, deps, session, plan, run):
    async def _advance_context_epoch(
        *,
        messages,
        from_phase,
        from_phase3_step,
        to_phase,
        to_phase3_step,
        rebuild_reason,
    ):
        old_epoch = int(session.get("current_context_epoch", 0))
        await persist_unflushed_messages(
            deps=deps,
            session=session,
            plan=plan,
            messages=messages,
            phase=from_phase,
            phase3_step=from_phase3_step,
            run_id=run.run_id,
            trip_id=getattr(plan, "trip_id", None),
            context_epoch=old_epoch,
        )
        session["current_context_epoch"] = old_epoch + 1
        session["_next_rebuild_reason"] = rebuild_reason

    return _advance_context_epoch


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
    await persist_unflushed_messages(
        deps=deps,
        session=session,
        plan=plan,
        messages=messages,
        phase=plan.phase,
        phase3_step=getattr(plan, "phase3_step", None),
        run_id=run.run_id,
        trip_id=getattr(plan, "trip_id", None),
    )
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
        await persist_unflushed_messages(
            deps=deps,
            session=session,
            plan=plan,
            messages=messages,
            phase=plan.phase,
            phase3_step=getattr(plan, "phase3_step", None),
            run_id=run.run_id,
            trip_id=getattr(plan, "trip_id", None),
        )
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
