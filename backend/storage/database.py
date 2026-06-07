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
    created_at   TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    phase        INTEGER,
    phase2_step  TEXT,
    history_seq  INTEGER,
    run_id       TEXT,
    trip_id      TEXT,
    context_epoch INTEGER,
    rebuild_reason TEXT
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

CREATE TABLE IF NOT EXISTS trace_runs (
    run_id              TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    trip_id             TEXT,
    context_epoch       INTEGER,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    status              TEXT NOT NULL,
    final_phase         INTEGER,
    final_phase2_step   TEXT,
    total_input_tokens  INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost_usd      REAL NOT NULL DEFAULT 0,
    total_duration_ms   REAL NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace_events (
    event_id     TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES trace_runs(run_id) ON DELETE CASCADE,
    sequence     INTEGER NOT NULL,
    event_type   TEXT NOT NULL,
    phase        INTEGER,
    phase2_step  TEXT,
    iteration    INTEGER,
    tool_name    TEXT,
    llm_provider TEXT,
    llm_model    TEXT,
    status       TEXT,
    duration_ms  REAL,
    cost_usd     REAL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE(run_id, sequence)
);

CREATE TABLE IF NOT EXISTS trace_grades (
    grade_id                TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES trace_runs(run_id) ON DELETE CASCADE,
    rubric_id               TEXT NOT NULL,
    status                  TEXT NOT NULL,
    score                   INTEGER NOT NULL,
    reason                  TEXT NOT NULL,
    evidence_event_ids_json TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    UNIQUE(run_id, rubric_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_snapshots_session ON plan_snapshots(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_archives_session ON archives(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_trace_runs_session_created
ON trace_runs(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_trace_runs_trip
ON trace_runs(session_id, trip_id);
CREATE INDEX IF NOT EXISTS idx_trace_events_run_sequence
ON trace_events(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_trace_events_type
ON trace_events(run_id, event_type);
CREATE INDEX IF NOT EXISTS idx_trace_events_tool
ON trace_events(run_id, tool_name);
CREATE INDEX IF NOT EXISTS idx_trace_grades_run
ON trace_grades(run_id);
CREATE INDEX IF NOT EXISTS idx_trace_grades_status
ON trace_grades(status);
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
        await self._migrate_trace_tables()
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
        async with self.conn.execute("PRAGMA table_info(messages)") as cursor:
            rows = await cursor.fetchall()

        existing_columns = {row["name"] for row in rows}
        missing_columns = (
            ("provider_state", "TEXT"),
            ("phase", "INTEGER"),
            ("phase2_step", "TEXT"),
            ("history_seq", "INTEGER"),
            ("run_id", "TEXT"),
            ("trip_id", "TEXT"),
            ("context_epoch", "INTEGER"),
            ("rebuild_reason", "TEXT"),
        )
        for column_name, column_type in missing_columns:
            if column_name in existing_columns:
                continue
            await self.conn.execute(
                f"ALTER TABLE messages ADD COLUMN {column_name} {column_type}"
            )

        await self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_session_history_unique "
            "ON messages(session_id, history_seq) WHERE history_seq IS NOT NULL"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_history "
            "ON messages(session_id, history_seq)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_phase "
            "ON messages(session_id, phase, phase2_step, history_seq)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_epoch "
            "ON messages(session_id, context_epoch, history_seq)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_trip_epoch "
            "ON messages(session_id, trip_id, context_epoch)"
        )

    async def _migrate_trace_tables(self) -> None:
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trace_runs (
                run_id              TEXT PRIMARY KEY,
                session_id          TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                trip_id             TEXT,
                context_epoch       INTEGER,
                started_at          TEXT NOT NULL,
                ended_at            TEXT,
                status              TEXT NOT NULL,
                final_phase         INTEGER,
                final_phase2_step   TEXT,
                total_input_tokens  INTEGER NOT NULL DEFAULT 0,
                total_output_tokens INTEGER NOT NULL DEFAULT 0,
                total_cost_usd      REAL NOT NULL DEFAULT 0,
                total_duration_ms   REAL NOT NULL DEFAULT 0,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trace_events (
                event_id     TEXT PRIMARY KEY,
                run_id       TEXT NOT NULL REFERENCES trace_runs(run_id) ON DELETE CASCADE,
                sequence     INTEGER NOT NULL,
                event_type   TEXT NOT NULL,
                phase        INTEGER,
                phase2_step  TEXT,
                iteration    INTEGER,
                tool_name    TEXT,
                llm_provider TEXT,
                llm_model    TEXT,
                status       TEXT,
                duration_ms  REAL,
                cost_usd     REAL,
                payload_json TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                UNIQUE(run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS trace_grades (
                grade_id                TEXT PRIMARY KEY,
                run_id                  TEXT NOT NULL REFERENCES trace_runs(run_id) ON DELETE CASCADE,
                rubric_id               TEXT NOT NULL,
                status                  TEXT NOT NULL,
                score                   INTEGER NOT NULL,
                reason                  TEXT NOT NULL,
                evidence_event_ids_json TEXT NOT NULL,
                created_at              TEXT NOT NULL,
                UNIQUE(run_id, rubric_id)
            );
            CREATE INDEX IF NOT EXISTS idx_trace_runs_session_created
            ON trace_runs(session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_trace_runs_trip
            ON trace_runs(session_id, trip_id);
            CREATE INDEX IF NOT EXISTS idx_trace_events_run_sequence
            ON trace_events(run_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_trace_events_type
            ON trace_events(run_id, event_type);
            CREATE INDEX IF NOT EXISTS idx_trace_events_tool
            ON trace_events(run_id, tool_name);
            CREATE INDEX IF NOT EXISTS idx_trace_grades_run
            ON trace_grades(run_id);
            CREATE INDEX IF NOT EXISTS idx_trace_grades_status
            ON trace_grades(status);
            """
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
