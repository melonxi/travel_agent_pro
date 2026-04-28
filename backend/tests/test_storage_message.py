import json
import sqlite3

import pytest
import pytest_asyncio

from storage.database import Database
from storage.message_store import MessageStore
from storage.session_store import SessionStore


@pytest_asyncio.fixture
async def stores():
    db = Database(":memory:")
    await db.initialize()
    session_store = SessionStore(db)
    message_store = MessageStore(db)
    await session_store.create("sess_msg_test_001")
    yield session_store, message_store
    await db.close()


@pytest.mark.asyncio
async def test_append_and_load(stores):
    _, message_store = stores
    await message_store.append("sess_msg_test_001", "user", "你好", seq=1)
    await message_store.append(
        "sess_msg_test_001",
        "assistant",
        "你好！有什么可以帮助你的？",
        seq=2,
    )
    messages = await message_store.load_all("sess_msg_test_001")
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "你好"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["seq"] == 2


@pytest.mark.asyncio
async def test_load_empty_session(stores):
    _, message_store = stores
    messages = await message_store.load_all("sess_msg_test_001")
    assert messages == []


@pytest.mark.asyncio
async def test_append_with_tool_calls(stores):
    _, message_store = stores
    tool_calls = [{"id": "tc1", "name": "search_flights", "arguments": {"origin": "北京"}}]
    await message_store.append(
        "sess_msg_test_001",
        "assistant",
        None,
        tool_calls=json.dumps(tool_calls, ensure_ascii=False),
        seq=1,
    )
    messages = await message_store.load_all("sess_msg_test_001")
    assert len(messages) == 1
    loaded_tc = json.loads(messages[0]["tool_calls"])
    assert loaded_tc[0]["name"] == "search_flights"


@pytest.mark.asyncio
async def test_append_with_tool_call_id(stores):
    _, message_store = stores
    await message_store.append(
        "sess_msg_test_001",
        "tool",
        '{"status": "success"}',
        tool_call_id="tc1",
        seq=1,
    )
    messages = await message_store.load_all("sess_msg_test_001")
    assert messages[0]["tool_call_id"] == "tc1"


@pytest.mark.asyncio
async def test_seq_ordering(stores):
    _, message_store = stores
    await message_store.append("sess_msg_test_001", "assistant", "second", seq=2)
    await message_store.append("sess_msg_test_001", "user", "first", seq=1)
    await message_store.append("sess_msg_test_001", "assistant", "third", seq=3)
    messages = await message_store.load_all("sess_msg_test_001")
    assert [message["content"] for message in messages] == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_append_batch(stores):
    _, message_store = stores
    rows = [
        {"role": "user", "content": "msg1", "seq": 1},
        {"role": "assistant", "content": "msg2", "seq": 2},
        {"role": "user", "content": "msg3", "seq": 3},
    ]
    await message_store.append_batch("sess_msg_test_001", rows)
    messages = await message_store.load_all("sess_msg_test_001")
    assert len(messages) == 3
    assert messages[2]["content"] == "msg3"


@pytest.mark.asyncio
async def test_append_writes_history_metadata(stores):
    _, message_store = stores

    await message_store.append(
        "sess_msg_test_001",
        "assistant",
        "查到东京适合亲子游",
        seq=9,
        phase=1,
        phase3_step=None,
        history_seq=0,
        run_id="run_1",
        trip_id="trip_1",
    )

    messages = await message_store.load_all("sess_msg_test_001")
    assert messages[0]["phase"] == 1
    assert messages[0]["phase3_step"] is None
    assert messages[0]["history_seq"] == 0
    assert messages[0]["run_id"] == "run_1"
    assert messages[0]["trip_id"] == "trip_1"


@pytest.mark.asyncio
async def test_load_all_orders_by_history_seq_before_legacy_seq(stores):
    _, message_store = stores

    await message_store.append(
        "sess_msg_test_001",
        "assistant",
        "new-second",
        seq=0,
        history_seq=11,
    )
    await message_store.append(
        "sess_msg_test_001",
        "assistant",
        "new-first",
        seq=99,
        history_seq=10,
    )
    await message_store.append(
        "sess_msg_test_001",
        "assistant",
        "legacy",
        seq=1,
    )

    messages = await message_store.load_all("sess_msg_test_001")
    assert [message["content"] for message in messages] == [
        "legacy",
        "new-first",
        "new-second",
    ]


@pytest.mark.asyncio
async def test_max_history_seq_ignores_legacy_rows(stores):
    _, message_store = stores
    await message_store.append("sess_msg_test_001", "user", "legacy", seq=20)
    await message_store.append(
        "sess_msg_test_001",
        "assistant",
        "new",
        seq=21,
        history_seq=4,
    )

    assert await message_store.max_history_seq("sess_msg_test_001") == 4


@pytest.mark.asyncio
async def test_load_frontend_view_filters_system_rows(stores):
    _, message_store = stores
    await message_store.append(
        "sess_msg_test_001",
        "system",
        "内部系统提示",
        seq=0,
        history_seq=0,
    )
    await message_store.append(
        "sess_msg_test_001",
        "user",
        "去东京",
        seq=1,
        history_seq=1,
    )

    messages = await message_store.load_frontend_view("sess_msg_test_001")
    assert [message["role"] for message in messages] == ["user"]


@pytest.mark.asyncio
async def test_append_batch_rolls_back_partial_rows_on_history_seq_conflict(stores):
    _, message_store = stores

    with pytest.raises(sqlite3.IntegrityError):
        await message_store.append_batch(
            "sess_msg_test_001",
            [
                {"role": "user", "content": "first", "seq": 1, "history_seq": 1},
                {
                    "role": "assistant",
                    "content": "duplicate",
                    "seq": 2,
                    "history_seq": 1,
                },
            ],
        )

    await message_store.append("sess_msg_test_001", "user", "later", seq=3)
    messages = await message_store.load_all("sess_msg_test_001")

    assert [message["content"] for message in messages] == ["later"]
