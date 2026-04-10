import json

import pytest
import pytest_asyncio

from state.models import TravelPlanState
from storage.archive_store import ArchiveStore
from storage.database import Database
from storage.message_store import MessageStore
from storage.session_store import SessionStore


@pytest_asyncio.fixture
async def storage_path(tmp_path):
    return tmp_path / "sessions.db"


@pytest.mark.asyncio
async def test_full_session_roundtrip(storage_path):
    db = Database(str(storage_path))
    await db.initialize()
    sessions = SessionStore(db)
    messages = MessageStore(db)
    archives = ArchiveStore(db)

    session_id = "sess_roundtrip01"
    await sessions.create(session_id, "user1", "东京5日游")
    await messages.append(session_id, "system", "你是旅行规划助手。", seq=0)
    await messages.append(session_id, "user", "我想去东京玩5天", seq=1)
    await messages.append(session_id, "assistant", "好的！让我为你规划东京5日游。", seq=2)

    plan = TravelPlanState(session_id=session_id, phase=3, destination="东京")
    await archives.save_snapshot(session_id, 3, json.dumps(plan.to_dict(), ensure_ascii=False))
    await sessions.update(session_id, phase=3, title="东京 · 5天4晚")
    await db.close()

    restored_db = Database(str(storage_path))
    await restored_db.initialize()
    restored_sessions = SessionStore(restored_db)
    restored_messages = MessageStore(restored_db)
    restored_archives = ArchiveStore(restored_db)

    meta = await restored_sessions.load(session_id)
    assert meta is not None
    assert meta["title"] == "东京 · 5天4晚"
    assert meta["phase"] == 3

    loaded_messages = await restored_messages.load_all(session_id)
    assert len(loaded_messages) == 3
    assert loaded_messages[0]["role"] == "system"
    assert loaded_messages[1]["content"] == "我想去东京玩5天"
    assert loaded_messages[2]["role"] == "assistant"

    snapshot = await restored_archives.load_latest_snapshot(session_id)
    assert snapshot is not None
    restored_plan = TravelPlanState.from_dict(json.loads(snapshot["plan_json"]))
    assert restored_plan.destination == "东京"
    assert restored_plan.phase == 3

    await restored_db.close()


@pytest.mark.asyncio
async def test_archived_session_has_archive(storage_path):
    db = Database(str(storage_path))
    await db.initialize()
    sessions = SessionStore(db)
    archives = ArchiveStore(db)

    session_id = "sess_archive_001"
    await sessions.create(session_id, "user1")
    plan = TravelPlanState(session_id=session_id, phase=7, destination="大阪")
    await archives.save(session_id, json.dumps(plan.to_dict(), ensure_ascii=False), summary="大阪 · 3天2晚")
    await sessions.update(session_id, status="archived")

    meta = await sessions.load(session_id)
    assert meta is not None
    assert meta["status"] == "archived"

    archive = await archives.load(session_id)
    assert archive is not None
    assert archive["summary"] == "大阪 · 3天2晚"

    await db.close()


@pytest.mark.asyncio
async def test_deleted_session_not_in_list(storage_path):
    db = Database(str(storage_path))
    await db.initialize()
    sessions = SessionStore(db)

    await sessions.create("sess_visible_001", "user1", "可见会话")
    await sessions.create("sess_deleted_001", "user1", "已删除")
    await sessions.soft_delete("sess_deleted_001")

    result = await sessions.list_sessions()
    ids = [row["session_id"] for row in result]
    assert "sess_visible_001" in ids
    assert "sess_deleted_001" not in ids

    await db.close()
