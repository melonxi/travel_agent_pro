from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL DEFAULT 'default_user',
    title        TEXT,
    phase        INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    last_run_id     TEXT,
    last_run_status TEXT,
    last_run_error  TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role         TEXT NOT NULL,
    content      TEXT,
    tool_calls   TEXT,
    tool_call_id TEXT,
    provider_state TEXT,
    phase        INTEGER,
    phase3_step  TEXT,
    created_at   TEXT NOT NULL,
    seq          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    phase        INTEGER NOT NULL,
    plan_json    TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archives (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    plan_json    TEXT NOT NULL,
    summary      TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_snapshots_session ON plan_snapshots(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_archives_session ON archives(session_id, created_at);
"""


class Database:
    def __init__(self, db_path: str = "data/sessions.db"):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        if self._conn is not None:
            return
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._migrate_sessions_table()
        await self._migrate_messages_table()
        await self._conn.commit()

    async def _migrate_sessions_table(self) -> None:
        async with self.conn.execute("PRAGMA table_info(sessions)") as cursor:
            rows = await cursor.fetchall()

        existing_columns = {row["name"] for row in rows}
        missing_columns = (
            ("last_run_id", "TEXT"),
            ("last_run_status", "TEXT"),
            ("last_run_error", "TEXT"),
        )
        for column_name, column_type in missing_columns:
            if column_name in existing_columns:
                continue
            await self.conn.execute(
                f"ALTER TABLE sessions ADD COLUMN {column_name} {column_type}"
            )

    async def _migrate_messages_table(self) -> None:
        # NOTE: Column names/types are source-level constants only.
        # SQLite DDL cannot bind identifiers, so we interpolate via f-string.
        # NEVER read these from config or external input.
        async with self.conn.execute("PRAGMA table_info(messages)") as cursor:
            rows = await cursor.fetchall()

        existing_columns = {row["name"] for row in rows}
        missing_columns: tuple[tuple[str, str], ...] = (
            ("provider_state", "TEXT"),
            ("phase", "INTEGER"),
            ("phase3_step", "TEXT"),
        )
        for column_name, column_type in missing_columns:
            if column_name in existing_columns:
                continue
            await self.conn.execute(
                f"ALTER TABLE messages ADD COLUMN {column_name} {column_type}"
            )

    async def close(self) -> None:
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._conn

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        cursor = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cursor

    async def fetch_one(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> dict[str, Any] | None:
        async with self.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def fetch_all(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]
