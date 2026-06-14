from __future__ import annotations

import logging

from storage.trace_redaction import stable_content_hash

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
            session.pop("_pending_phase4_deliverables_trace", None)
            return False
        if decision.get("status") != "approved" and not force:
            return False
    elif not force:
        return False

    result_data = pending.get("result_data")
    if not isinstance(result_data, dict):
        session.pop("_pending_phase4_deliverables", None)
        session.pop("_phase4_deliverables_quality", None)
        session.pop("_pending_phase4_deliverables_trace", None)
        return False

    await deps.persist_phase4_deliverables(plan, result_data)
    trace_recorder = session.get("_trace_recorder")
    trace_context = session.get("_trace_context")
    trace_links = session.get("_pending_phase4_deliverables_trace")
    if trace_recorder is not None and trace_context is not None:
        parent_event_id = None
        root_event_id = None
        if isinstance(trace_links, dict):
            parent_event_id = trace_links.get("draft_event_id") or trace_links.get(
                "tool_result_event_id"
            )
            root_event_id = trace_links.get("root_event_id") or parent_event_id
        final_artifacts = []
        for name, content in (
            ("travel_plan_md", str(result_data.get("travel_plan_markdown") or "")),
            ("checklist_md", str(result_data.get("checklist_markdown") or "")),
        ):
            artifact = await trace_recorder.attach_artifact(
                trace_context,
                event_id=None,
                kind="deliverable_final",
                content=content,
                content_type="text/markdown",
            )
            if artifact is not None:
                final_artifacts.append(
                    {
                        "name": name,
                        "artifact_id": artifact.artifact_id,
                        "content_hash": artifact.content_hash,
                        "redaction_status": artifact.redaction_status,
                    }
                )
        await trace_recorder.emit_event(
            trace_context,
            event_type="deliverable_finalize",
            tool_name="generate_summary",
            status="success",
            actor="storage",
            parent_event_id=parent_event_id,
            root_event_id=root_event_id,
            payload={
                "tool_call_id": pending.get("tool_call_id"),
                "tool_result_event_id": pending.get("tool_result_trace_event_id"),
                "draft_event_id": parent_event_id,
                "final_artifact_paths": dict(plan.deliverables or {}),
                "final_artifacts": final_artifacts,
                "final_state_hash": stable_content_hash(plan.to_dict()),
                "travel_plan_markdown_hash": stable_content_hash(
                    str(result_data.get("travel_plan_markdown") or "")
                ),
                "checklist_markdown_hash": stable_content_hash(
                    str(result_data.get("checklist_markdown") or "")
                ),
                "quality_decision": decision if isinstance(decision, dict) else None,
            },
        )
    session.pop("_pending_phase4_deliverables", None)
    session.pop("_phase4_deliverables_quality", None)
    session.pop("_pending_phase4_deliverables_trace", None)
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
