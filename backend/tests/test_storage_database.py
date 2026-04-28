import sqlite3

import pytest
import pytest_asyncio

from storage.database import Database


@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_initialize_creates_tables(db: Database):
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    table_names = [row["name"] for row in rows]
    assert "sessions" in table_names
    assert "messages" in table_names
    assert "plan_snapshots" in table_names
    assert "archives" in table_names


@pytest.mark.asyncio
async def test_initialize_is_idempotent(db: Database):
    await db.initialize()
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_execute_and_fetch(db: Database):
    await db.execute(
        "INSERT INTO sessions (session_id, user_id, title, phase, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "s1",
            "u1",
            "test",
            1,
            "active",
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
        ),
    )
    row = await db.fetch_one("SELECT * FROM sessions WHERE session_id = ?", ("s1",))
    assert row is not None
    assert row["session_id"] == "s1"
    assert row["user_id"] == "u1"


@pytest.mark.asyncio
async def test_initialize_migrates_legacy_sessions_schema(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            session_id   TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL DEFAULT 'default_user',
            title        TEXT,
            phase        INTEGER NOT NULL DEFAULT 1,
            status       TEXT NOT NULL DEFAULT 'active',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    database = Database(str(db_path))
    await database.initialize()
    columns = await database.fetch_all("PRAGMA table_info(sessions)")
    await database.close()

    column_names = {column["name"] for column in columns}
    assert "last_run_id" in column_names
    assert "last_run_status" in column_names
    assert "last_run_error" in column_names


@pytest.mark.asyncio
async def test_initialize_migrates_legacy_messages_schema(tmp_path):
    db_path = tmp_path / "legacy-messages.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            session_id   TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL DEFAULT 'default_user',
            title        TEXT,
            phase        INTEGER NOT NULL DEFAULT 1,
            status       TEXT NOT NULL DEFAULT 'active',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        CREATE TABLE messages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
            role         TEXT NOT NULL,
            content      TEXT,
            tool_calls   TEXT,
            tool_call_id TEXT,
            created_at   TEXT NOT NULL,
            seq          INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    database = Database(str(db_path))
    await database.initialize()
    columns = await database.fetch_all("PRAGMA table_info(messages)")
    await database.close()

    column_names = {column["name"] for column in columns}
    assert "provider_state" in column_names
