from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
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
        payload = json.loads(row.get("payload_json") or "{}")
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
            payload=payload,
            created_at=row.get("created_at") or "",
            session_id=row.get("session_id"),
            trip_id=row.get("trip_id"),
            context_epoch=row.get("context_epoch"),
            parent_event_id=row.get("parent_event_id"),
            root_event_id=row.get("root_event_id"),
            correlation_id=row.get("correlation_id"),
            actor=row.get("actor"),
            started_at=row.get("started_at"),
            ended_at=row.get("ended_at"),
            payload_schema_version=row.get("payload_schema_version"),
        )

    def _artifact_content(row: dict) -> str | None:
        storage_path = row.get("storage_path")
        if not storage_path:
            return None
        content_type = str(row.get("content_type") or "")
        if not (
            content_type.startswith("text/")
            or content_type == "application/json"
        ):
            return None
        db_path = Path(getattr(trace_store._db, "_db_path", ""))
        base_dir = db_path.parent if str(db_path) != ":memory:" else Path.cwd()
        path = Path(storage_path)
        if not path.is_absolute():
            path = base_dir / path
        try:
            resolved_base = base_dir.resolve()
            resolved_path = path.resolve()
            if resolved_base not in resolved_path.parents and resolved_path != resolved_base:
                return None
            if resolved_path.stat().st_size > 1_000_000:
                return None
            return resolved_path.read_text(encoding="utf-8")
        except Exception:
            return None

    def _serialize_artifact(row: dict, *, include_content: bool = False) -> dict:
        data = {
            "artifact_id": row.get("artifact_id"),
            "run_id": row.get("run_id"),
            "event_id": row.get("event_id"),
            "kind": row.get("kind"),
            "content_type": row.get("content_type"),
            "content_hash": row.get("content_hash"),
            "redaction_status": row.get("redaction_status"),
            "storage_path": row.get("storage_path"),
            "size_bytes": row.get("size_bytes"),
            "created_at": row.get("created_at"),
        }
        if include_content:
            data["content"] = _artifact_content(row)
        return data

    def _serialize_event(
        row: dict,
        *,
        artifacts_by_event: dict[str, list[dict]],
        include_content: bool = False,
    ) -> dict:
        payload = json.loads(row.get("payload_json") or "{}")
        event_id = row.get("event_id")
        return {
            "event_id": event_id,
            "run_id": row.get("run_id"),
            "sequence": row.get("sequence"),
            "event_type": row.get("event_type"),
            "phase": row.get("phase"),
            "phase2_step": row.get("phase2_step"),
            "iteration": row.get("iteration"),
            "tool_name": row.get("tool_name"),
            "llm_provider": row.get("llm_provider"),
            "llm_model": row.get("llm_model"),
            "status": row.get("status"),
            "duration_ms": row.get("duration_ms"),
            "cost_usd": row.get("cost_usd"),
            "payload": payload,
            "payload_json": row.get("payload_json"),
            "created_at": row.get("created_at"),
            "session_id": row.get("session_id"),
            "trip_id": row.get("trip_id"),
            "context_epoch": row.get("context_epoch"),
            "parent_event_id": row.get("parent_event_id"),
            "root_event_id": row.get("root_event_id"),
            "correlation_id": row.get("correlation_id"),
            "actor": row.get("actor"),
            "started_at": row.get("started_at"),
            "ended_at": row.get("ended_at"),
            "payload_schema_version": row.get("payload_schema_version"),
            "artifacts": [
                _serialize_artifact(artifact, include_content=include_content)
                for artifact in artifacts_by_event.get(event_id, [])
            ],
        }

    @app.get("/api/sessions/{session_id}/trace")
    async def get_session_trace(session_id: str):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        agent = session.get("agent")
        engine = getattr(agent, "tool_engine", None) if agent else None
        return build_trace(session_id, session, tool_engine=engine)

    @app.get("/api/traces/{run_id}")
    async def get_persisted_trace(
        run_id: str,
        include_artifact_content: bool = Query(False),
    ):
        await ensure_storage_ready()
        run = await trace_store.load_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Trace run not found")
        events = await trace_store.load_events(run_id)
        artifacts = await trace_store.load_artifact_metadata(run_id)
        artifacts_by_event: dict[str, list[dict]] = {}
        for artifact in artifacts:
            event_id = artifact.get("event_id")
            if event_id:
                artifacts_by_event.setdefault(event_id, []).append(artifact)
        grades = await trace_store.load_grades(run_id)
        return {
            "run": run,
            "events": [
                _serialize_event(
                    row,
                    artifacts_by_event=artifacts_by_event,
                    include_content=include_artifact_content,
                )
                for row in events
            ],
            "artifacts": [
                _serialize_artifact(
                    row,
                    include_content=include_artifact_content,
                )
                for row in artifacts
            ],
            "grades": grades,
        }

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
