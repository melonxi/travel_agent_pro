import json
from types import SimpleNamespace

import pytest

from agent.types import Message, Role, ToolCall, ToolResult
from api.orchestration.session.persistence import (
    SessionPersistence,
    deserialize_tool_result,
    serialize_tool_result,
)
from state.models import TravelPlanState


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
            return TravelPlanState(session_id=session_id, phase=5, destination="东京")

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

        def get_prompt_for_plan(self, plan):
            return f"restore prompt phase={plan.phase}"

    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=_SessionStore(),
        message_store=_MessageStore(),
        archive_store=_ArchiveStore(),
        state_mgr=_StateManager(),
        phase_router=_PhaseRouter(),
        build_agent=lambda *args, **kwargs: _RestoreAgent(),
        context_manager=_RestoreContextManager(),
        memory_mgr=_RestoreMemoryManager(),
        memory_enabled=False,
    )

    restored = await persistence.restore_session("sess_1")

    assert restored["next_history_seq"] == 13
    assert all(item.message.history_persisted for item in restored["history_messages"])
    assert restored["history_messages"][1].message.history_seq == 12


async def _noop(*args, **kwargs):
    return None


class _RestoreSessionStore:
    async def load(self, session_id):
        return {
            "session_id": session_id,
            "user_id": "user_restore",
            "status": "active",
        }


class _RestoreStateManager:
    async def load(self, session_id):
        return TravelPlanState(session_id=session_id, phase=5, destination="东京")


class _RestorePhaseRouter:
    def sync_phase_state(self, plan):
        plan.phase = 5

    def get_prompt_for_plan(self, plan):
        return f"restore prompt phase={plan.phase}"


class _RestoreContextManager:
    def build_system_message(self, plan, phase_prompt, memory_context, *, available_tools):
        return Message(
            role=Role.SYSTEM,
            content=(
                f"rebuilt system {phase_prompt} {memory_context} "
                f"{','.join(available_tools)}"
            ),
        )


class _RestoreMemoryManager:
    async def generate_context(self, user_id, plan):
        return ("restore memory", [], 0, 0, 0)


class _RestoreToolEngine:
    def get_tools_for_phase(self, phase, plan):
        return [{"name": "save_day_plan"}, {"name": "request_backtrack"}]


class _RestoreAgent:
    def __init__(self):
        self.tool_engine = _RestoreToolEngine()


class _RestoreMessageStore:
    async def load_all(self, session_id):
        return [
            {
                "role": "system",
                "content": "old persisted system",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 0,
                "history_seq": 0,
                "phase": 1,
                "phase3_step": None,
                "run_id": "run_old",
                "trip_id": "trip_1",
            },
            {
                "role": "user",
                "content": "我想去东京",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 1,
                "history_seq": 1,
                "phase": 1,
                "phase3_step": None,
                "run_id": "run_old",
                "trip_id": "trip_1",
            },
            {
                "role": "tool",
                "content": serialize_tool_result(
                    ToolResult(
                        tool_call_id="tc_old",
                        status="success",
                        data={"destination": "东京"},
                    )
                ),
                "tool_calls": None,
                "tool_call_id": "tc_old",
                "provider_state": None,
                "seq": 2,
                "history_seq": 2,
                "phase": 1,
                "phase3_step": None,
                "run_id": "run_old",
                "trip_id": "trip_1",
            },
            {
                "role": "user",
                "content": "继续细化每天路线",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 3,
                "history_seq": 9,
                "phase": 5,
                "phase3_step": None,
                "run_id": "run_new",
                "trip_id": "trip_1",
            },
        ]


@pytest.mark.asyncio
async def test_restore_session_returns_short_runtime_and_internal_history():
    built_agents = []

    def build_agent(plan, user_id, *, compression_events=None):
        agent = _RestoreAgent()
        built_agents.append((agent, plan, user_id, compression_events))
        return agent

    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=_RestoreSessionStore(),
        message_store=_RestoreMessageStore(),
        archive_store=None,
        state_mgr=_RestoreStateManager(),
        phase_router=_RestorePhaseRouter(),
        build_agent=build_agent,
        context_manager=_RestoreContextManager(),
        memory_mgr=_RestoreMemoryManager(),
        memory_enabled=True,
    )

    restored = await persistence.restore_session("sess_restore")

    assert restored is not None
    assert len(restored["history_messages"]) == 4
    assert len(restored["messages"]) == 2
    assert len(restored["messages"]) < len(restored["history_messages"])
    assert restored["next_history_seq"] == 10
    assert restored["messages"][0].role == Role.SYSTEM
    assert restored["messages"][0].content.startswith("rebuilt system restore prompt phase=5")
    assert "save_day_plan" in restored["messages"][0].content
    assert restored["messages"][1].role == Role.USER
    assert restored["messages"][1].content == "继续细化每天路线"
    assert all(message.role != Role.TOOL for message in restored["messages"])
    assert all(message.tool_result is None for message in restored["messages"])
    assert restored["history_messages"][2].message.tool_result.data == {"destination": "东京"}
    assert built_agents[0][2] == "user_restore"
    assert restored["agent"] is built_agents[0][0]


class _LegacyRestoreMessageStore:
    async def load_all(self, session_id):
        return [
            {
                "role": "user",
                "content": "legacy one",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 0,
                "history_seq": None,
                "phase": None,
                "phase3_step": None,
                "run_id": None,
                "trip_id": None,
            },
            {
                "role": "user",
                "content": "legacy two",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 1,
                "history_seq": None,
                "phase": None,
                "phase3_step": None,
                "run_id": None,
                "trip_id": None,
            },
        ]


@pytest.mark.asyncio
async def test_restore_session_legacy_history_seq_falls_back_to_history_length():
    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=_RestoreSessionStore(),
        message_store=_LegacyRestoreMessageStore(),
        archive_store=None,
        state_mgr=_RestoreStateManager(),
        phase_router=_RestorePhaseRouter(),
        build_agent=lambda *args, **kwargs: _RestoreAgent(),
        context_manager=_RestoreContextManager(),
        memory_mgr=_RestoreMemoryManager(),
        memory_enabled=False,
    )

    restored = await persistence.restore_session("sess_legacy")

    assert restored is not None
    assert restored["next_history_seq"] == 2
    assert [message.role for message in restored["messages"]] == [Role.SYSTEM, Role.USER]
    assert restored["messages"][1].content == "legacy two"
