from __future__ import annotations

import aiosqlite
import pytest

from storage.database import Database


@pytest.mark.asyncio
async def test_migrate_legacy_messages_table_adds_phase_columns(tmp_path):
    db_path = tmp_path / "legacy.db"
    # 模拟旧 schema：messages 表只有 provider_state，没有 phase / phase3_step
    # messages has FK to sessions, must be created first
    async with aiosqlite.connect(db_path) as raw:
        await raw.executescript(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                provider_state TEXT,
                created_at TEXT NOT NULL,
                seq INTEGER NOT NULL
            );
            """
        )
        await raw.commit()

    db = Database(str(db_path))
    await db.initialize()
    try:
        rows = await db.fetch_all("PRAGMA table_info(messages)")
    finally:
        await db.close()

    column_names = {row["name"] for row in rows}
    assert "phase" in column_names
    assert "phase3_step" in column_names


@pytest.mark.asyncio
async def test_initialize_fresh_db_has_phase_columns(tmp_path):
    db_path = tmp_path / "fresh.db"
    db = Database(str(db_path))
    await db.initialize()
    try:
        rows = await db.fetch_all("PRAGMA table_info(messages)")
    finally:
        await db.close()

    column_names = {row["name"] for row in rows}
    assert "phase" in column_names
    assert "phase3_step" in column_names


@pytest.mark.asyncio
async def test_initialize_is_idempotent_on_migrated_db(tmp_path):
    db_path = tmp_path / "idempotent.db"

    db = Database(str(db_path))
    await db.initialize()
    await db.close()

    # 二次 initialize 不应抛 duplicate column name
    db2 = Database(str(db_path))
    await db2.initialize()
    try:
        rows = await db2.fetch_all("PRAGMA table_info(messages)")
    finally:
        await db2.close()

    column_names = [row["name"] for row in rows]
    assert "phase" in column_names
    assert "phase3_step" in column_names
    # 列仍然只有一份
    assert column_names.count("phase") == 1
    assert column_names.count("phase3_step") == 1
    assert column_names.count("provider_state") == 1
