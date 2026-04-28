import json
from types import SimpleNamespace

import pytest

from agent.types import Message, Role, ToolCall, ToolResult
from api.orchestration.session.persistence import (
    SessionPersistence,
    deserialize_tool_result,
    serialize_tool_result,
)


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
        "sess_1",
        messages,
        phase=1,
        phase3_step=None,
        run_id=None,
        trip_id=None,
        next_history_seq=0,
    )

    assert json.loads(rows[0]["provider_state"]) == {"reasoning_content": "需要验证。"}


@pytest.mark.asyncio
async def test_persist_messages_appends_without_delete_and_returns_next_history_seq():
    rows: list[dict[str, object]] = []
    deletes: list[tuple[str, tuple[object, ...]]] = []

    class _MessageStore:
        async def append_batch(self, session_id, payload):
            rows.extend(payload)

        async def load_all(self, session_id):
            return rows

    async def _execute(sql, params=()):
        if sql.startswith("DELETE FROM messages"):
            deletes.append((sql, params))

    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_execute),
        session_store=None,
        message_store=_MessageStore(),
        archive_store=None,
        state_mgr=None,
        phase_router=None,
        build_agent=lambda *args, **kwargs: None,
    )
    messages = [
        Message(role=Role.USER, content="去东京"),
        Message(
            role=Role.ASSISTANT,
            content=None,
            tool_calls=[ToolCall(id="tc_1", name="quick_travel_search", arguments={})],
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(tool_call_id="tc_1", status="success", data={"ok": True}),
        ),
    ]

    next_seq = await persistence.persist_messages(
        "sess_1",
        messages,
        phase=1,
        phase3_step=None,
        run_id="run_1",
        trip_id="trip_1",
        next_history_seq=7,
    )

    assert deletes == []
    assert next_seq == 10
    assert [row["history_seq"] for row in rows] == [7, 8, 9]
    assert [row["seq"] for row in rows] == [7, 8, 9]
    assert {row["phase"] for row in rows} == {1}
    assert {row["run_id"] for row in rows} == {"run_1"}
    assert {row["trip_id"] for row in rows} == {"trip_1"}
    assert all(message.history_persisted for message in messages)
    assert [message.history_seq for message in messages] == [7, 8, 9]


@pytest.mark.asyncio
async def test_persist_messages_skips_already_persisted_messages_without_len_cursor():
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
    already_flushed = Message(
        role=Role.USER,
        content="旧 runtime anchor",
        history_persisted=True,
        history_seq=3,
    )
    new_reply = Message(role=Role.ASSISTANT, content="继续规划")

    next_seq = await persistence.persist_messages(
        "sess_1",
        [already_flushed, new_reply],
        phase=3,
        phase3_step="candidate",
        run_id="run_2",
        trip_id="trip_1",
        next_history_seq=4,
    )

    assert next_seq == 5
    assert len(rows) == 1
    assert rows[0]["content"] == "继续规划"
    assert rows[0]["history_seq"] == 4
    assert already_flushed.history_seq == 3
    assert new_reply.history_persisted is True


@pytest.mark.asyncio
async def test_restore_session_initializes_next_history_seq_from_database():
    class _SessionStore:
        async def load(self, session_id):
            return {"status": "active", "user_id": "user_1"}

    class _ArchiveStore:
        async def load_latest_snapshot(self, session_id):
            return None

    class _StateManager:
        async def load(self, session_id):
            return SimpleNamespace(session_id=session_id)

    class _MessageStore:
        async def load_all(self, session_id):
            return [
                {"role": "user", "content": "legacy", "seq": 1, "history_seq": None},
                {"role": "assistant", "content": "new", "seq": 2, "history_seq": 12},
            ]

        async def max_history_seq(self, session_id):
            return 12

    class _PhaseRouter:
        def sync_phase_state(self, plan):
            return None

    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=_SessionStore(),
        message_store=_MessageStore(),
        archive_store=_ArchiveStore(),
        state_mgr=_StateManager(),
        phase_router=_PhaseRouter(),
        build_agent=lambda *args, **kwargs: "agent",
    )

    restored = await persistence.restore_session("sess_1")

    assert restored["next_history_seq"] == 13
    assert all(message.history_persisted for message in restored["messages"])
    assert restored["messages"][1].history_seq == 12


async def _noop(*args, **kwargs):
    return None
