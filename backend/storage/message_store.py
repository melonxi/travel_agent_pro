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
        provider_state: str | None = None,
        seq: int,
        phase: int | None = None,
        phase3_step: str | None = None,
        history_seq: int | None = None,
        run_id: str | None = None,
        trip_id: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO messages "
            "(session_id, role, content, tool_calls, tool_call_id, provider_state, created_at, seq, phase, phase3_step, history_seq, run_id, trip_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                role,
                content,
                tool_calls,
                tool_call_id,
                provider_state,
                now,
                seq,
                phase,
                phase3_step,
                history_seq,
                run_id,
                trip_id,
            ),
        )

    async def append_batch(self, session_id: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._db.conn.executemany(
                "INSERT INTO messages "
                "(session_id, role, content, tool_calls, tool_call_id, provider_state, created_at, seq, phase, phase3_step, history_seq, run_id, trip_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        session_id,
                        row["role"],
                        row.get("content"),
                        row.get("tool_calls"),
                        row.get("tool_call_id"),
                        row.get("provider_state"),
                        now,
                        row["seq"],
                        row.get("phase"),
                        row.get("phase3_step"),
                        row.get("history_seq"),
                        row.get("run_id"),
                        row.get("trip_id"),
                    )
                    for row in rows
                ],
            )
            await self._db.conn.commit()
        except Exception:
            await self._db.conn.rollback()
            raise

    async def load_all(self, session_id: str) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? "
            "ORDER BY CASE WHEN history_seq IS NULL THEN 0 ELSE 1 END ASC, "
            "history_seq ASC, seq ASC, id ASC",
            (session_id,),
        )

    async def load_frontend_view(self, session_id: str) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? AND role != 'system' "
            "ORDER BY CASE WHEN history_seq IS NULL THEN 0 ELSE 1 END ASC, "
            "history_seq ASC, seq ASC, id ASC",
            (session_id,),
        )

    async def max_history_seq(self, session_id: str) -> int | None:
        row = await self._db.fetch_one(
            "SELECT MAX(history_seq) AS max_history_seq "
            "FROM messages WHERE session_id = ?",
            (session_id,),
        )
        if row is None or row["max_history_seq"] is None:
            return None
        return int(row["max_history_seq"])
