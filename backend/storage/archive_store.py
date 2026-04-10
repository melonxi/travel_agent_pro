from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from storage.database import Database


class ArchiveStore:
    def __init__(self, db: Database):
        self._db = db

    async def save(
        self,
        session_id: str,
        plan_json: str,
        summary: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO archives (session_id, plan_json, summary, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, plan_json, summary, now),
        )

    async def load(self, session_id: str) -> dict[str, Any] | None:
        return await self._db.fetch_one(
            "SELECT * FROM archives WHERE session_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (session_id,),
        )

    async def save_snapshot(
        self,
        session_id: str,
        phase: int,
        plan_json: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO plan_snapshots (session_id, phase, plan_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, phase, plan_json, now),
        )

    async def load_latest_snapshot(self, session_id: str) -> dict[str, Any] | None:
        return await self._db.fetch_one(
            "SELECT * FROM plan_snapshots WHERE session_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (session_id,),
        )
