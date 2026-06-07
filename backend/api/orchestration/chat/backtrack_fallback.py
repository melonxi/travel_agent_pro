from __future__ import annotations

from api.orchestration.chat.events import event_json
from api.orchestration.chat.finalization import persist_unflushed_messages


async def maybe_apply_backtrack_fallback(
    *,
    deps,
    session,
    plan,
    messages,
    run,
    user_message: str | None,
    phase_before_run: int,
):
    if user_message is None or plan.phase != phase_before_run:
        return

    backtrack_target = deps.detect_backtrack(user_message, plan)
    if backtrack_target is None:
        return

    reason = f"fallback回退：{user_message[:50]}"
    tool_call_id = f"fallback.request_backtrack:{plan.version}"
    yield event_json(
        {
            "type": "tool_call",
            "tool_call": {
                "id": tool_call_id,
                "name": "request_backtrack",
                "arguments": {
                    "to_phase": backtrack_target,
                    "reason": reason,
                },
                "human_label": "回退到之前阶段",
            },
        }
    )
    snapshot_path = await deps.state_mgr.save_snapshot(plan)
    from_phase = plan.phase
    await persist_unflushed_messages(
        deps=deps,
        session=session,
        plan=plan,
        messages=messages,
        phase=from_phase,
        phase2_step=getattr(plan, "phase2_step", None),
        run_id=run.run_id,
        trip_id=getattr(plan, "trip_id", None),
    )
    deps.phase_router.prepare_backtrack(
        plan,
        backtrack_target,
        reason,
        snapshot_path,
    )
    await deps.state_mgr.clear_deliverables(plan.session_id)
    await deps.rotate_trip_on_reset_backtrack(
        user_id=session["user_id"],
        plan=plan,
        to_phase=backtrack_target,
        reason_text=user_message,
    )
    session["needs_rebuild"] = True
    yield event_json(
        {
            "type": "tool_result",
            "tool_result": {
                "tool_call_id": tool_call_id,
                "status": "success",
                "data": {
                    "backtracked": True,
                    "from_phase": from_phase,
                    "to_phase": backtrack_target,
                    "reason": reason,
                    "next_action": "请向用户确认回退结果，不要继续调用其他工具",
                },
                "error": None,
                "error_code": None,
                "suggestion": None,
            },
        }
    )
    deps.schedule_memory_event(
        user_id=session["user_id"],
        session_id=plan.session_id,
        event_type="reject",
        object_type="phase_output",
        object_payload={
            "from_phase": from_phase,
            "to_phase": backtrack_target,
            "reason": reason,
        },
        reason_text=reason,
    )
