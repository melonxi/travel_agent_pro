import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from agent.types import Message, Role, ToolCall, ToolResult
from api.orchestration.session.persistence import (
    SessionPersistence,
    deserialize_tool_result,
    serialize_tool_result,
)
from storage.database import Database
from storage.message_store import MessageStore


def test_tool_error_result_serialization_roundtrips_repair_fields():
    result = ToolResult(
        tool_call_id="tc_1",
        status="error",
        error="POI '淄博市博物馆' 重复出现在 plans[0].days[1].candidate_pois[0]",
        error_code="INVALID_VALUE",
        suggestion="请把 '淄博市博物馆' 只保留在其中一天",
    )

    serialized = serialize_tool_result(result)
    restored = deserialize_tool_result("tc_1", serialized)

    assert json.loads(serialized) == {
        "status": "error",
        "data": None,
        "error": "POI '淄博市博物馆' 重复出现在 plans[0].days[1].candidate_pois[0]",
        "error_code": "INVALID_VALUE",
        "suggestion": "请把 '淄博市博物馆' 只保留在其中一天",
    }
    assert restored == result


def test_deserialize_tool_result_keeps_legacy_data_rows_as_success():
    restored = deserialize_tool_result("tc_1", '{"results": []}')

    assert restored == ToolResult(
        tool_call_id="tc_1",
        status="success",
        data={"results": []},
    )


@pytest.mark.asyncio
async def test_session_persistence_roundtrips_message_provider_state():
    rows: list[dict[str, object]] = []

    class _MessageStore:
        async def append_batch(self, session_id, payload):
            rows.extend(payload)

        async def load_all(self, session_id):
            return rows

    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=None,
        message_store=_MessageStore(),
        archive_store=None,
        state_mgr=None,
        phase_router=None,
        build_agent=lambda *args, **kwargs: None,
    )
    messages = [
        Message(
            role=Role.ASSISTANT,
            content="先查",
            tool_calls=[ToolCall(id="tc_1", name="web_search", arguments={})],
            provider_state={"reasoning_content": "需要验证。"},
        )
    ]

    await persistence.persist_messages(
        "sess_1", messages, phase=1, phase3_step=None, persisted_count=0
    )

    assert json.loads(rows[0]["provider_state"]) == {"reasoning_content": "需要验证。"}


async def _noop(*args, **kwargs):
    return None


@pytest_asyncio.fixture
async def persistence_factory():
    created: list[Database] = []

    async def _factory(session_id: str, phase: int = 1):
        db = Database(":memory:")
        await db.initialize()
        created.append(db)
        await db.execute(
            "INSERT INTO sessions (session_id, user_id, title, phase, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, "u", "t", phase, "active", "2026-04-28T00:00:00+00:00", "2026-04-28T00:00:00+00:00"),
        )
        message_store = MessageStore(db)

        async def _ensure_ready():
            return None

        persistence = SessionPersistence(
            ensure_storage_ready=_ensure_ready,
            db=db,
            session_store=None,
            message_store=message_store,
            archive_store=None,
            state_mgr=None,
            phase_router=None,
            build_agent=lambda *a, **kw: None,
        )
        return persistence, db, None

    yield _factory

    for db in created:
        await db.close()


@pytest.mark.asyncio
async def test_persist_messages_appends_without_deleting_history(persistence_factory):
    persistence, db, _ = await persistence_factory("sess-A", phase=1)

    msgs = [
        Message(role=Role.SYSTEM, content="sys-phase1"),
        Message(role=Role.USER, content="第一轮用户消息"),
    ]
    new_count = await persistence.persist_messages(
        "sess-A", msgs, phase=1, phase3_step=None, persisted_count=0
    )
    assert new_count == 2

    msgs.append(Message(role=Role.ASSISTANT, content="phase1 回复"))
    msgs.append(Message(role=Role.USER, content="切换前最后一条用户"))
    new_count = await persistence.persist_messages(
        "sess-A", msgs, phase=1, phase3_step=None, persisted_count=2
    )
    assert new_count == 4

    rebuilt = [
        Message(role=Role.SYSTEM, content="sys-phase3"),
        Message(role=Role.ASSISTANT, content="handoff"),
        Message(role=Role.USER, content="切换前最后一条用户"),
    ]
    new_count = await persistence.persist_messages(
        "sess-A", rebuilt, phase=3, phase3_step="brief", persisted_count=4
    )
    assert new_count == 4

    rows = await db.fetch_all(
        "SELECT role, content, phase, phase3_step, seq FROM messages "
        "WHERE session_id = ? ORDER BY seq ASC",
        ("sess-A",),
    )
    assert [r["seq"] for r in rows] == [0, 1, 2, 3]
    assert all(r["phase"] == 1 for r in rows)
    assert [r["role"] for r in rows] == ["system", "user", "assistant", "user"]


@pytest.mark.asyncio
async def test_persist_messages_does_not_double_write_already_persisted(persistence_factory):
    persistence, db, _ = await persistence_factory("sess-B", phase=1)
    msgs = [
        Message(role=Role.SYSTEM, content="sys"),
        Message(role=Role.USER, content="hi"),
    ]
    count1 = await persistence.persist_messages(
        "sess-B", msgs, phase=1, phase3_step=None, persisted_count=0
    )
    count2 = await persistence.persist_messages(
        "sess-B", msgs, phase=1, phase3_step=None, persisted_count=count1
    )
    assert count2 == count1

    rows = await db.fetch_all("SELECT seq FROM messages WHERE session_id = ? ORDER BY seq", ("sess-B",))
    assert [r["seq"] for r in rows] == [0, 1]


@pytest.mark.asyncio
async def test_persist_messages_uses_phase_before_run_for_pre_rebuild_tail(persistence_factory):
    """phase 切换前 tail 应该带传入的 phase（即 phase_before_run），而不是切换后的 phase。"""
    persistence, db, _ = await persistence_factory("sess-C", phase=1)
    pre_tail = [
        Message(role=Role.USER, content="切换前用户消息"),
        Message(role=Role.ASSISTANT, content="切换前模型回复"),
    ]
    await persistence.persist_messages(
        "sess-C", pre_tail, phase=1, phase3_step=None, persisted_count=0
    )

    rows = await db.fetch_all(
        "SELECT phase, phase3_step FROM messages WHERE session_id = ? ORDER BY seq", ("sess-C",)
    )
    assert all(r["phase"] == 1 for r in rows)
    assert all(r["phase3_step"] is None for r in rows)


@pytest.mark.asyncio
async def test_persist_messages_assigns_seq_and_phase3_step(persistence_factory):
    """phase3 + step 标签应该正确写入；seq 从 persisted_count 起步连续递增。"""
    persistence, db, _ = await persistence_factory("sess-D", phase=3)

    first = [
        Message(role=Role.SYSTEM, content="p3-sys"),
        Message(role=Role.USER, content="brief 用户"),
    ]
    new_count = await persistence.persist_messages(
        "sess-D", first, phase=3, phase3_step="brief", persisted_count=0
    )
    assert new_count == 2

    second = first + [
        Message(role=Role.ASSISTANT, content="p3-skeleton-sys"),
        Message(role=Role.USER, content="skeleton 用户"),
    ]
    new_count = await persistence.persist_messages(
        "sess-D", second, phase=3, phase3_step="skeleton", persisted_count=2
    )
    assert new_count == 4

    rows = await db.fetch_all(
        "SELECT seq, phase, phase3_step FROM messages WHERE session_id = ? ORDER BY seq",
        ("sess-D",),
    )
    assert [r["seq"] for r in rows] == [0, 1, 2, 3]
    assert [r["phase"] for r in rows] == [3, 3, 3, 3]
    assert [r["phase3_step"] for r in rows] == ["brief", "brief", "skeleton", "skeleton"]


@pytest.mark.asyncio
async def test_persist_messages_serialization_branches_carry_phase_tags(persistence_factory):
    """tool_calls / tool_result / provider_state 三种序列化分支也要正确带上 phase / phase3_step / seq。"""
    persistence, db, _ = await persistence_factory("sess-E", phase=3)

    tool_call = ToolCall(
        id="call-1",
        name="search",
        arguments={"q": "x"},
        human_label="搜索",
    )
    tool_result = ToolResult(
        tool_call_id="call-1",
        status="success",
        data={"results": []},
    )
    msgs = [
        Message(role=Role.SYSTEM, content="sys"),
        Message(role=Role.ASSISTANT, content="", tool_calls=[tool_call]),
        Message(role=Role.TOOL, tool_result=tool_result),
        Message(
            role=Role.ASSISTANT,
            content="reply",
            provider_state={"reasoning_content": "rc"},
        ),
    ]
    new_count = await persistence.persist_messages(
        "sess-E", msgs, phase=3, phase3_step="brief", persisted_count=0
    )
    assert new_count == 4

    rows = await db.fetch_all(
        "SELECT seq, phase, phase3_step, role, tool_calls, tool_call_id, provider_state "
        "FROM messages WHERE session_id = ? ORDER BY seq",
        ("sess-E",),
    )
    assert [r["seq"] for r in rows] == [0, 1, 2, 3]
    assert [r["phase"] for r in rows] == [3, 3, 3, 3]
    assert [r["phase3_step"] for r in rows] == ["brief", "brief", "brief", "brief"]

    # row 0: 普通 system 消息，所有序列化字段都为空
    assert rows[0]["tool_calls"] is None
    assert rows[0]["tool_call_id"] is None
    assert rows[0]["provider_state"] is None

    # row 1: tool_calls 分支
    assert rows[1]["tool_calls"] is not None
    parsed_tool_calls = json.loads(rows[1]["tool_calls"])
    assert parsed_tool_calls[0]["id"] == "call-1"
    assert parsed_tool_calls[0]["name"] == "search"

    # row 2: tool_result 分支 → tool_call_id 写入
    assert rows[2]["tool_call_id"] == "call-1"

    # row 3: provider_state 分支
    assert rows[3]["provider_state"] is not None
    assert json.loads(rows[3]["provider_state"]) == {"reasoning_content": "rc"}


@pytest.mark.asyncio
async def test_persist_messages_rejects_negative_persisted_count(persistence_factory):
    persistence, _, _ = await persistence_factory("sess-F")
    with pytest.raises(ValueError, match="persisted_count must be >= 0"):
        await persistence.persist_messages(
            "sess-F",
            [Message(role=Role.USER, content="x")],
            phase=1,
            phase3_step=None,
            persisted_count=-1,
        )
