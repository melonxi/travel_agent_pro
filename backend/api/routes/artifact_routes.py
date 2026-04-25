from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from api.trace import build_trace


def register_artifact_routes(
    app: FastAPI,
    *,
    sessions: dict[str, dict],
    ensure_storage_ready,
    session_store,
    state_mgr,
) -> None:
    @app.get("/api/sessions/{session_id}/trace")
    async def get_session_trace(session_id: str):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        agent = session.get("agent")
        engine = getattr(agent, "tool_engine", None) if agent else None
        return build_trace(session_id, session, tool_engine=engine)

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
