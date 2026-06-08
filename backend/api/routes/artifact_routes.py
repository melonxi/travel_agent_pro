from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from api.trace import build_trace
from evals.trace_grader import grade_trace_run
from evals.trace_models import TraceEvent


def register_artifact_routes(
    app: FastAPI,
    *,
    sessions: dict[str, dict],
    ensure_storage_ready,
    session_store,
    state_mgr,
    trace_store,
) -> None:
    def _event_from_row(row: dict) -> TraceEvent:
        return TraceEvent(
            event_id=row["event_id"],
            run_id=row["run_id"],
            sequence=int(row["sequence"]),
            event_type=row["event_type"],
            phase=row.get("phase"),
            phase2_step=row.get("phase2_step"),
            iteration=row.get("iteration"),
            tool_name=row.get("tool_name"),
            llm_provider=row.get("llm_provider"),
            llm_model=row.get("llm_model"),
            status=row.get("status"),
            duration_ms=row.get("duration_ms"),
            cost_usd=row.get("cost_usd"),
            payload=json.loads(row["payload_json"] or "{}"),
            created_at=row["created_at"],
        )

    @app.get("/api/sessions/{session_id}/trace")
    async def get_session_trace(session_id: str):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        agent = session.get("agent")
        engine = getattr(agent, "tool_engine", None) if agent else None
        return build_trace(session_id, session, tool_engine=engine)

    @app.get("/api/traces/{run_id}")
    async def get_persisted_trace(run_id: str):
        await ensure_storage_ready()
        run = await trace_store.load_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Trace run not found")
        events = await trace_store.load_events(run_id)
        grades = await trace_store.load_grades(run_id)
        return {"run": run, "events": events, "grades": grades}

    @app.post("/api/traces/{run_id}/grade")
    async def grade_persisted_trace(run_id: str):
        await ensure_storage_ready()
        run = await trace_store.load_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Trace run not found")

        event_rows = await trace_store.load_events(run_id)
        events = [_event_from_row(row) for row in event_rows]
        try:
            final_plan = await state_mgr.load(run["session_id"])
        except (FileNotFoundError, ValueError):
            final_plan = None

        grades = grade_trace_run(
            run_id=run_id,
            events=events,
            final_plan=final_plan,
            run_status=run.get("status"),
        )
        await trace_store.save_grades(run_id, grades)
        return {
            "run_id": run_id,
            "grades": [
                {
                    "rubric_id": grade.rubric_id,
                    "status": grade.status,
                    "score": grade.score,
                    "reason": grade.reason,
                    "evidence_event_ids": grade.evidence_event_ids,
                }
                for grade in grades
            ],
        }

    @app.get("/api/sessions/{session_id}/deliverables/{filename}")
    async def download_deliverable(session_id: str, filename: str):
        await ensure_storage_ready()
        meta = await session_store.load(session_id)
        if meta is None or meta["status"] == "deleted":
            raise HTTPException(status_code=404, detail="Session not found")

        try:
            content = await state_mgr.read_deliverable(session_id, filename)
        except ValueError:
            raise HTTPException(status_code=404, detail="Deliverable not found")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Deliverable not found")

        return Response(
            content=content,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
