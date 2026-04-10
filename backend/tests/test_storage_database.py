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
