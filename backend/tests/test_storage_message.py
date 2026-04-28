import json

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
