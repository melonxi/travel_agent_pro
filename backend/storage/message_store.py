from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from storage.database import Database


class MessageStore:
    def __init__(self, db: Database):
        self._db = db

    async def append(
        self,
        session_id: str,
        role: str,
        content: str | None,
        *,
        tool_calls: str | None = None,
        tool_call_id: str | None = None,
        seq: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, created_at, seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, role, content, tool_calls, tool_call_id, now, seq),
        )

    async def append_batch(self, session_id: str, rows: list[dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            await self._db.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, created_at, seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    row["role"],
                    row.get("content"),
                    row.get("tool_calls"),
                    row.get("tool_call_id"),
                    now,
                    row["seq"],
                ),
            )

    async def load_all(self, session_id: str) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY seq ASC, id ASC",
            (session_id,),
        )
