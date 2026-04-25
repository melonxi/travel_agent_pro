from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from sse_starlette.sse import EventSourceResponse


logger = logging.getLogger(__name__)


def register_internal_task_routes(
    app: FastAPI,
    *,
    sessions: dict[str, dict],
    ensure_storage_ready,
    restore_session,
    memory_active_tasks,
    memory_task_subscribers,
    memory_task_stream,
) -> None:
    @app.get("/api/internal-tasks/{session_id}/stream")
    async def stream_internal_tasks(session_id: str, request: Request):
        await ensure_storage_ready()
        session = sessions.get(session_id)
        if not session:
            restored = await restore_session(session_id)
            if restored is None:
                raise HTTPException(status_code=404, detail="Session not found")
            sessions[session_id] = restored

        logger.warning(
            "后台记忆任务 SSE 请求 session=%s restored=%s active_tasks=%s subscribers=%s",
            session_id,
            session is None,
            len(memory_active_tasks.get(session_id, {})),
            len(memory_task_subscribers.get(session_id, set())),
        )
        return EventSourceResponse(memory_task_stream(session_id, request))

    @app.get("/api/internal-tasks/{session_id}")
    async def list_internal_tasks(session_id: str):
        await ensure_storage_ready()
        session = sessions.get(session_id)
        if not session:
            restored = await restore_session(session_id)
            if restored is None:
                raise HTTPException(status_code=404, detail="Session not found")
            sessions[session_id] = restored

        tasks = sorted(
            memory_active_tasks.get(session_id, {}).values(),
            key=lambda task: task.started_at or task.ended_at or 0,
        )
        logger.warning(
            "后台记忆任务快照请求 session=%s tasks=%s kinds=%s",
            session_id,
            len(tasks),
            [task.kind for task in tasks],
        )
        return {"tasks": [task.to_dict() for task in tasks]}
