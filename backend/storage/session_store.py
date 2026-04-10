from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from storage.database import Database


class SessionStore:
    def __init__(self, db: Database):
        self._db = db

    async def create(
        self,
        session_id: str,
        user_id: str = "default_user",
        title: str | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO sessions (session_id, user_id, title, phase, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, 'active', ?, ?)",
            (session_id, user_id, title, now, now),
        )
        meta = await self.load(session_id)
        if meta is None:
            raise RuntimeError(f"Failed to load newly created session {session_id}")
        return meta

    async def load(self, session_id: str) -> dict[str, Any] | None:
        return await self._db.fetch_one(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        )

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM sessions WHERE status != 'deleted' ORDER BY updated_at DESC, session_id DESC"
        )

    async def update(
        self,
        session_id: str,
        *,
        phase: int | None = None,
        title: str | None = None,
        status: str | None = None,
    ) -> None:
        updates: list[str] = []
        params: list[Any] = []

        if phase is not None:
            updates.append("phase = ?")
            params.append(phase)
        if title is not None:
            updates.append("title = ?")
            params.append(title)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if not updates:
            return

        updates.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(session_id)
        await self._db.execute(
            f"UPDATE sessions SET {', '.join(updates)} WHERE session_id = ?",
            tuple(params),
        )

    async def soft_delete(self, session_id: str) -> None:
        await self.update(session_id, status="deleted")
