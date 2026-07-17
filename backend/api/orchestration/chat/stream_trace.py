from __future__ import annotations

from api.orchestration.chat.finalization import persist_run_safely
from api.orchestration.chat.trace_persistence import persist_trace_run_safely
from storage.trace_redaction import stable_content_hash


async def emit_deliverable_draft_trace(
    *,
    session,
    plan,
    agent,
    tool_result,
    result_data: dict,
) -> None:
    trace_recorder = getattr(agent, "trace_recorder", None)
    trace_context = getattr(agent, "trace_context", None)
    if trace_recorder is None or trace_context is None:
        return
    metadata = tool_result.metadata if isinstance(tool_result.metadata, dict) else {}
    parent_event_id = metadata.get("trace_event_id")
    root_event_id = metadata.get("trace_parent_event_id") or parent_event_id
    travel_plan_markdown = str(result_data.get("travel_plan_markdown") or "")
    checklist_markdown = str(result_data.get("checklist_markdown") or "")
    draft_artifacts = []
    for name, content in (
        ("travel_plan_markdown", travel_plan_markdown),
        ("checklist_markdown", checklist_markdown),
    ):
        artifact = await trace_recorder.attach_artifact(
            trace_context,
            event_id=None,
            kind="deliverable_draft",
            content=content,
            content_type="text/markdown",
        )
        if artifact is not None:
            draft_artifacts.append(
                {
                    "name": name,
                    "artifact_id": artifact.artifact_id,
                    "content_hash": artifact.content_hash,
                    "redaction_status": artifact.redaction_status,
                }
            )
    event = await trace_recorder.emit_event(
        trace_context,
        event_type="deliverable_draft",
        tool_name="generate_summary",
        status="success",
        actor="main_agent",
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
        payload={
            "tool_call_id": tool_result.tool_call_id,
            "source_state_hash": stable_content_hash(plan.to_dict()),
            "travel_plan_markdown_hash": stable_content_hash(travel_plan_markdown),
            "checklist_markdown_hash": stable_content_hash(checklist_markdown),
            "draft_artifacts": draft_artifacts,
            "quality_decision": session.get("_phase4_deliverables_quality"),
        },
    )
    if event is not None:
        session["_pending_phase4_deliverables_trace"] = {
            "draft_event_id": event.event_id,
            "tool_result_event_id": parent_event_id,
            "root_event_id": root_event_id,
            "travel_plan_markdown_hash": stable_content_hash(travel_plan_markdown),
            "checklist_markdown_hash": stable_content_hash(checklist_markdown),
        }


async def emit_deliverable_gap_trace(*, plan, agent) -> None:
    """Make a Phase 4 run without frozen files explicitly observable."""
    if plan.phase < 4 or plan.deliverables:
        return
    trace_recorder = getattr(agent, "trace_recorder", None)
    trace_context = getattr(agent, "trace_context", None)
    if trace_recorder is None or trace_context is None:
        return
    await trace_recorder.emit_event(
        trace_context,
        event_type="deliverable_gap",
        status="warning",
        actor="main_agent",
        payload={
            "phase": plan.phase,
            "reason": "run_ended_without_frozen_phase4_deliverables",
        },
    )


async def finalize_stream_trace_and_persistence(
    *,
    deps,
    session,
    plan,
    messages,
    agent,
    run,
) -> None:
    trace_recorder = getattr(agent, "trace_recorder", None)
    trace_context = getattr(agent, "trace_context", None)
    if trace_recorder is not None and trace_context is not None:
        await emit_deliverable_gap_trace(plan=plan, agent=agent)
        final_snapshot = plan.to_dict()
        snapshot_event = await trace_recorder.emit_event(
            trace_context,
            event_type="state_snapshot",
            status="success",
            actor="storage",
            payload={
                "snapshot_scope": "run_end",
                "state_hash": stable_content_hash(final_snapshot),
                "phase": plan.phase,
                "phase2_step": getattr(plan, "phase2_step", None),
            },
        )
        if snapshot_event is not None:
            await trace_recorder.attach_artifact(
                trace_context,
                event_id=snapshot_event.event_id,
                kind="state_snapshot",
                content=final_snapshot,
            )
        # persist_trace_run_safely (below) is the authoritative writer of the
        # trace_runs summary (status + run-scoped token/cost/duration totals).
        # Emit only the run_end event here to avoid a redundant summary write
        # that would momentarily set totals to zero.
        await trace_recorder.end_run(
            trace_context,
            status=run.status,
            final_phase=plan.phase,
            final_phase2_step=getattr(plan, "phase2_step", None),
            update_summary=False,
        )
    await persist_run_safely(
        deps=deps,
        session=session,
        plan=plan,
        messages=messages,
        run=run,
    )
    tool_side_effects = (
        deps.tool_side_effects() if deps.tool_side_effects is not None else {}
    )
    await persist_trace_run_safely(
        trace_store=deps.trace_store,
        session=session,
        plan=plan,
        run=run,
        tool_side_effects=tool_side_effects,
    )
