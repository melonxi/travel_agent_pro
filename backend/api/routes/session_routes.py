from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from telemetry.stats import SessionStats


def register_session_routes(
    app: FastAPI,
    *,
    sessions: dict[str, dict],
    ensure_storage_ready,
    restore_session,
    build_agent,
    generate_title,
    state_mgr,
    phase_router,
    session_store,
    message_store,
    archive_store,
    reflection_cache,
    quality_gate_retries,
) -> None:
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/sessions")
    async def create_session():
        await ensure_storage_ready()
        plan = await state_mgr.create_session()
        compression_events: list[dict] = []
        session: dict = {
            "plan": plan,
            "messages": [],
            "agent": None,
            "needs_rebuild": False,
            "user_id": "default_user",
            "compression_events": compression_events,
            "stats": SessionStats(),
            "_pending_system_notes": [],
            "persisted_count": 0,
        }
        session["agent"] = build_agent(
            plan,
            "default_user",
            session=session,
            compression_events=compression_events,
        )
        sessions[plan.session_id] = session
        await session_store.create(plan.session_id, "default_user")
        return {"session_id": plan.session_id, "phase": plan.phase}

    @app.get("/api/sessions")
    async def list_sessions():
        await ensure_storage_ready()
        rows = await session_store.list_sessions()
        return [
            {
                "session_id": row["session_id"],
                "title": row["title"],
                "phase": row["phase"],
                "status": row["status"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    @app.get("/api/plan/{session_id}")
    async def get_plan(session_id: str):
        await ensure_storage_ready()
        session = sessions.get(session_id)
        if not session:
            restored = await restore_session(session_id)
            if restored is not None:
                sessions[session_id] = restored
                session = restored
            else:
                try:
                    plan = await state_mgr.load(session_id)
                    phase_router.sync_phase_state(plan)
                    return plan.to_dict()
                except (FileNotFoundError, ValueError):
                    raise HTTPException(status_code=404, detail="Session not found")
        phase_router.sync_phase_state(session["plan"])
        return session["plan"].to_dict()

    @app.get("/api/sessions/{session_id}/stats")
    async def get_session_stats(session_id: str):
        session = sessions.get(session_id)
        if not session:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        stats: SessionStats = session.get("stats", SessionStats())
        return stats.to_dict()

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        await ensure_storage_ready()
        meta = await session_store.load(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Session not found")
        await session_store.soft_delete(session_id)
        sessions.pop(session_id, None)
        reflection_cache.pop(session_id, None)
        for key in list(quality_gate_retries):
            if key[0] == session_id:
                quality_gate_retries.pop(key, None)
        return {"status": "deleted"}

    @app.get("/api/messages/{session_id}")
    async def get_messages(session_id: str):
        await ensure_storage_ready()
        meta = await session_store.load(session_id)
        if meta is None or meta["status"] == "deleted":
            raise HTTPException(status_code=404, detail="Session not found")
        rows = await message_store.load_all(session_id)
        return [
            {
                "role": row["role"],
                "content": row["content"],
                "tool_calls": (
                    json.loads(row["tool_calls"]) if row.get("tool_calls") else None
                ),
                "tool_call_id": row.get("tool_call_id"),
                "seq": row["seq"],
            }
            for row in rows
        ]

    @app.get("/api/archives/{session_id}")
    async def get_archive(session_id: str):
        await ensure_storage_ready()
        result = await archive_store.load(session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Archive not found")
        return {
            "session_id": result["session_id"],
            "plan": json.loads(result["plan_json"]),
            "summary": result["summary"],
            "created_at": result["created_at"],
        }
