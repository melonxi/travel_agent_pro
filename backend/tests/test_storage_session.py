import pytest
import pytest_asyncio

from storage.database import Database
from storage.session_store import SessionStore


@pytest_asyncio.fixture
async def store():
    db = Database(":memory:")
    await db.initialize()
    session_store = SessionStore(db)
    yield session_store
    await db.close()


@pytest.mark.asyncio
async def test_create_and_load(store: SessionStore):
    await store.create("sess_abc123def456", "user1", "东京5日游")
    meta = await store.load("sess_abc123def456")
    assert meta is not None
    assert meta["session_id"] == "sess_abc123def456"
    assert meta["user_id"] == "user1"
    assert meta["title"] == "东京5日游"
    assert meta["phase"] == 1
    assert meta["status"] == "active"


@pytest.mark.asyncio
async def test_load_nonexistent(store: SessionStore):
    meta = await store.load("sess_nonexistent1")
    assert meta is None


@pytest.mark.asyncio
async def test_list_sessions(store: SessionStore):
    await store.create("sess_aaaaaaaaaaaa", "user1", "会话A")
    await store.create("sess_bbbbbbbbbbbb", "user1", "会话B")
    await store.create("sess_cccccccccccc", "user1", "会话C")
    await store.soft_delete("sess_bbbbbbbbbbbb")
    result = await store.list_sessions()
    assert len(result) == 2
    ids = [row["session_id"] for row in result]
    assert "sess_bbbbbbbbbbbb" not in ids


@pytest.mark.asyncio
async def test_update_phase_and_title(store: SessionStore):
    await store.create("sess_update123456", "user1", "初始标题")
    await store.update("sess_update123456", phase=3, title="东京 · 5天4晚")
    meta = await store.load("sess_update123456")
    assert meta is not None
    assert meta["phase"] == 3
    assert meta["title"] == "东京 · 5天4晚"


@pytest.mark.asyncio
async def test_soft_delete(store: SessionStore):
    await store.create("sess_delete123456", "user1", "待删除")
    await store.soft_delete("sess_delete123456")
    meta = await store.load("sess_delete123456")
    assert meta is not None
    assert meta["status"] == "deleted"


@pytest.mark.asyncio
async def test_update_status_to_archived(store: SessionStore):
    await store.create("sess_archive12345", "user1", "待归档")
    await store.update("sess_archive12345", status="archived")
    meta = await store.load("sess_archive12345")
    assert meta is not None
    assert meta["status"] == "archived"
