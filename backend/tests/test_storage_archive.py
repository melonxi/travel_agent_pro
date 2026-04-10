import json

import pytest
import pytest_asyncio

from storage.archive_store import ArchiveStore
from storage.database import Database
from storage.session_store import SessionStore


@pytest_asyncio.fixture
async def stores():
    db = Database(":memory:")
    await db.initialize()
    session_store = SessionStore(db)
    archive_store = ArchiveStore(db)
    await session_store.create("sess_arc_test_001")
    await session_store.create("sess_arc_test_002")
    yield session_store, archive_store
    await db.close()


@pytest.mark.asyncio
async def test_save_and_load_archive(stores):
    _, archive_store = stores
    plan = {"destination": "东京", "phase": 7, "daily_plans": []}
    await archive_store.save("sess_arc_test_001", json.dumps(plan, ensure_ascii=False), summary="东京 · 5天4晚")
    result = await archive_store.load("sess_arc_test_001")
    assert result is not None
    assert result["summary"] == "东京 · 5天4晚"
    loaded_plan = json.loads(result["plan_json"])
    assert loaded_plan["destination"] == "东京"


@pytest.mark.asyncio
async def test_load_nonexistent(stores):
    _, archive_store = stores
    result = await archive_store.load("sess_arc_test_002")
    assert result is None


@pytest.mark.asyncio
async def test_save_snapshot_and_load_latest(stores):
    _, archive_store = stores
    plan_v1 = {"phase": 1, "destination": None}
    plan_v3 = {"phase": 3, "destination": "东京"}
    await archive_store.save_snapshot("sess_arc_test_001", 1, json.dumps(plan_v1, ensure_ascii=False))
    await archive_store.save_snapshot("sess_arc_test_001", 3, json.dumps(plan_v3, ensure_ascii=False))
    latest = await archive_store.load_latest_snapshot("sess_arc_test_001")
    assert latest is not None
    loaded = json.loads(latest["plan_json"])
    assert loaded["phase"] == 3
    assert loaded["destination"] == "东京"


@pytest.mark.asyncio
async def test_load_latest_snapshot_empty(stores):
    _, archive_store = stores
    latest = await archive_store.load_latest_snapshot("sess_arc_test_002")
    assert latest is None
