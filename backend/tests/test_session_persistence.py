import json
import logging
from types import SimpleNamespace

import pytest

from agent.compaction import compact_messages_for_prompt
from agent.types import Message, Role, ToolCall, ToolResult
from api.orchestration.session.context_segments import ContextSegment
from api.orchestration.session.persistence import (
    SessionPersistence,
    deserialize_tool_result,
    sanitize_history_row,
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
        phase2_step=None,
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
        phase2_step=None,
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
        phase=2,
        phase2_step="candidate",
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
async def test_persist_messages_does_not_reappend_compacted_persisted_tool_results():
    rows: list[dict[str, object]] = []

    class _MessageStore:
        async def append_batch(self, session_id, payload):
            rows.extend(payload)

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
    persisted_history = [
        Message(
            role=Role.ASSISTANT,
            tool_calls=[ToolCall(id="tc_1", name="web_search", arguments={})],
            history_persisted=True,
            history_seq=10,
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(
                tool_call_id="tc_1",
                status="success",
                data={
                    "answer": "答" * 1200,
                    "results": [
                        {
                            "title": "title",
                            "url": "https://example.com",
                            "content": "s" * 1200,
                        }
                    ],
                },
            ),
            history_persisted=True,
            history_seq=11,
        ),
        Message(role=Role.USER, content="继续", history_persisted=False),
    ]

    outcome = compact_messages_for_prompt(
        persisted_history,
        prompt_budget=1000,
        tools=[],
    )

    assert outcome.changed
    assert outcome.messages[1].history_persisted is True

    next_seq = await persistence.persist_messages(
        "sess_1",
        outcome.messages,
        phase=3,
        phase2_step="lock",
        run_id="run_2",
        trip_id="trip_1",
        next_history_seq=12,
    )

    assert next_seq == 13
    assert len(rows) == 1
    assert rows[0]["role"] == "user"
    assert rows[0]["content"] == "继续"


@pytest.mark.asyncio
async def test_persist_messages_skips_transient_and_system_messages():
    rows: list[dict[str, object]] = []

    class _MessageStore:
        async def append_batch(self, session_id, payload):
            rows.extend(payload)

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

    next_seq = await persistence.persist_messages(
        "sess-1",
        [
            Message(role=Role.SYSTEM, content="static", transient=True),
            Message(role=Role.USER, content="<turn_context>", transient=True),
            Message(role=Role.SYSTEM, content="legacy dynamic system"),
            Message(role=Role.USER, content="真实用户消息"),
        ],
        phase=1,
        phase2_step=None,
        run_id="run-1",
        trip_id="trip-1",
        next_history_seq=4,
    )

    assert next_seq == 5
    assert [row["role"] for row in rows] == ["user"]
    assert rows[0]["content"] == "真实用户消息"
    assert rows[0]["history_seq"] == 4


def test_deserialize_history_message_drops_legacy_full_system_prompt():
    row = {
        "role": "system",
        "content": "## 当前时间\n\n- 当前本地日期：2026-05-29\n\n## 当前规划状态",
        "history_seq": 3,
    }

    assert sanitize_history_row(row) is None


def test_deserialize_history_message_converts_legacy_quality_gate_system():
    row = {
        "role": "system",
        "content": "[质量门控]\n当前方案评分 3.0/5，请修正。",
        "history_seq": 4,
    }

    history = sanitize_history_row(row)

    assert history is not None
    assert history.message.role == Role.USER
    assert history.message.history_persisted is True
    assert history.message.history_seq == 4
    assert '<app_event kind="quality_gate">' in history.message.content
    assert "当前方案评分" in history.message.content


def test_deserialize_history_message_converts_legacy_summary_to_app_event():
    row = {
        "role": "system",
        "content": "[对话摘要]\n用户想去京都。",
        "history_seq": 5,
    }

    history = sanitize_history_row(row)

    assert history is not None
    assert history.message.role == Role.USER
    assert '<app_event kind="history_summary">' in history.message.content
    assert "用户想去京都" in history.message.content


def test_deserialize_history_message_warns_when_dropping_unknown_system(caplog):
    row = {
        "role": "system",
        "content": "unexpected legacy note",
        "history_seq": 6,
    }

    with caplog.at_level(logging.WARNING):
        assert sanitize_history_row(row) is None

    assert "Dropping legacy system history row" in caplog.text
    assert "history_seq=6" in caplog.text


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
            return TravelPlanState(session_id=session_id, phase=3, destination="东京")

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
        return TravelPlanState(session_id=session_id, phase=3, destination="东京")


class _RestorePhaseRouter:
    def sync_phase_state(self, plan):
        plan.phase = 3

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
                "phase2_step": None,
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
                "phase2_step": None,
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
                "phase2_step": None,
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
                "phase": 3,
                "phase2_step": None,
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
    assert len(restored["history_messages"]) == 3
    assert len(restored["messages"]) == 2
    assert restored["next_history_seq"] == 10
    assert all(message.role != Role.SYSTEM for message in restored["messages"])
    assert restored["messages"][0].role == Role.USER
    assert restored["messages"][0].content == "我想去东京"
    assert restored["messages"][1].role == Role.USER
    assert restored["messages"][1].content == "继续细化每天路线"
    assert restored["history_messages"][1].message.tool_result.data == {"destination": "东京"}
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
                "phase2_step": None,
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
                "phase2_step": None,
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
    assert [message.role for message in restored["messages"]] == [Role.USER, Role.USER]
    assert restored["messages"][1].content == "legacy two"


class _NoopSyncPhaseRouter(_RestorePhaseRouter):
    def sync_phase_state(self, plan):
        return None


class _Phase3SkeletonStateManager:
    async def load(self, session_id):
        return TravelPlanState(
            session_id=session_id,
            phase=2,
            phase2_step="skeleton",
            destination="大阪",
        )


class _Phase3SkeletonMessageStore:
    async def load_all(self, session_id):
        return [
            {
                "role": "user",
                "content": "画像输入",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 0,
                "history_seq": 0,
                "phase": 2,
                "phase2_step": "brief",
                "run_id": "run_brief",
                "trip_id": "trip_1",
            },
            {
                "role": "tool",
                "content": serialize_tool_result(
                    ToolResult(
                        tool_call_id="tc_brief",
                        status="success",
                        data={"trip_brief": "old brief"},
                    )
                ),
                "tool_calls": None,
                "tool_call_id": "tc_brief",
                "provider_state": None,
                "seq": 1,
                "history_seq": 1,
                "phase": 2,
                "phase2_step": "brief",
                "run_id": "run_brief",
                "trip_id": "trip_1",
            },
            {
                "role": "user",
                "content": "生成骨架",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 2,
                "history_seq": 2,
                "phase": 2,
                "phase2_step": "skeleton",
                "run_id": "run_skeleton",
                "trip_id": "trip_1",
            },
        ]


@pytest.mark.asyncio
async def test_restore_session_phase3_substep_keeps_previous_substeps_out_of_runtime():
    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=_RestoreSessionStore(),
        message_store=_Phase3SkeletonMessageStore(),
        archive_store=None,
        state_mgr=_Phase3SkeletonStateManager(),
        phase_router=_NoopSyncPhaseRouter(),
        build_agent=lambda *args, **kwargs: _RestoreAgent(),
        context_manager=_RestoreContextManager(),
        memory_mgr=_RestoreMemoryManager(),
        memory_enabled=True,
    )

    restored = await persistence.restore_session("sess_phase3")

    assert restored is not None
    assert len(restored["history_messages"]) == 3
    assert len(restored["messages"]) == 2
    rendered = "\n".join(str(message.content) for message in restored["messages"])
    assert "生成骨架" in rendered
    assert "画像输入" in rendered
    assert all(message.tool_result is None for message in restored["messages"])
    assert restored["history_messages"][1].message.tool_result.data == {
        "trip_brief": "old brief"
    }


class _BacktrackToPhase3StateManager:
    async def load(self, session_id):
        return TravelPlanState(
            session_id=session_id,
            phase=2,
            phase2_step="brief",
            destination="京都",
        )


class _BacktrackToPhase3MessageStore:
    async def load_all(self, session_id):
        return [
            {
                "role": "user",
                "content": "老 Phase 2 输入",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 0,
                "history_seq": 0,
                "phase": 2,
                "phase2_step": "brief",
                "run_id": "run_old_phase3",
                "trip_id": "trip_1",
            },
            {
                "role": "tool",
                "content": serialize_tool_result(
                    ToolResult(
                        tool_call_id="tc_old_phase3",
                        status="success",
                        data={"trip_brief": "old target phase segment"},
                    )
                ),
                "tool_calls": None,
                "tool_call_id": "tc_old_phase3",
                "provider_state": None,
                "seq": 1,
                "history_seq": 1,
                "phase": 2,
                "phase2_step": "brief",
                "run_id": "run_old_phase3",
                "trip_id": "trip_1",
            },
            {
                "role": "user",
                "content": "预算太高，回到框架规划",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 2,
                "history_seq": 8,
                "phase": 3,
                "phase2_step": None,
                "run_id": "run_backtrack",
                "trip_id": "trip_1",
            },
            {
                "role": "tool",
                "content": serialize_tool_result(
                    ToolResult(
                        tool_call_id="tc_backtrack",
                        status="success",
                        data={
                            "backtracked": True,
                            "to_phase": 2,
                            "reason": "预算太高",
                        },
                    )
                ),
                "tool_calls": None,
                "tool_call_id": "tc_backtrack",
                "provider_state": None,
                "seq": 3,
                "history_seq": 9,
                "phase": 3,
                "phase2_step": None,
                "run_id": "run_backtrack",
                "trip_id": "trip_1",
            },
        ]


@pytest.mark.asyncio
async def test_restore_session_after_backtrack_does_not_replay_old_target_phase():
    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=_RestoreSessionStore(),
        message_store=_BacktrackToPhase3MessageStore(),
        archive_store=None,
        state_mgr=_BacktrackToPhase3StateManager(),
        phase_router=_NoopSyncPhaseRouter(),
        build_agent=lambda *args, **kwargs: _RestoreAgent(),
        context_manager=_RestoreContextManager(),
        memory_mgr=_RestoreMemoryManager(),
        memory_enabled=True,
    )

    restored = await persistence.restore_session("sess_backtrack")

    assert restored is not None
    assert len(restored["messages"]) == 2
    rendered = "\n".join(str(message.content) for message in restored["messages"])
    assert "预算太高，回到框架规划" in rendered
    assert "老 Phase 2 输入" in rendered
    assert all(message.tool_result is None for message in restored["messages"])
    assert restored["history_messages"][1].message.tool_result.data == {
        "trip_brief": "old target phase segment"
    }


@pytest.mark.asyncio
async def test_persistence_lists_context_segments_from_message_store_rows():
    rows = [
        {"session_id": "sess-1", "context_epoch": 0, "phase": 1, "phase2_step": None, "trip_id": "trip-a", "run_id": "run-1", "history_seq": 0, "rebuild_reason": None},
        {"session_id": "sess-1", "context_epoch": 1, "phase": 2, "phase2_step": "brief", "trip_id": "trip-a", "run_id": "run-2", "history_seq": 1, "rebuild_reason": "phase_forward"},
    ]

    class _MessageStore:
        async def load_all(self, session_id):
            assert session_id == "sess-1"
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

    segments = await persistence.list_context_segments("sess-1")

    assert segments == [
        ContextSegment(
            session_id="sess-1",
            context_epoch=0,
            phase=1,
            phase2_step=None,
            trip_id="trip-a",
            run_ids=("run-1",),
            start_history_seq=0,
            end_history_seq=0,
            message_count=1,
            rebuild_reason=None,
        ),
        ContextSegment(
            session_id="sess-1",
            context_epoch=1,
            phase=2,
            phase2_step="brief",
            trip_id="trip-a",
            run_ids=("run-2",),
            start_history_seq=1,
            end_history_seq=1,
            message_count=1,
            rebuild_reason="phase_forward",
        ),
    ]


@pytest.mark.asyncio
async def test_persistence_loads_context_segment_messages_without_http_route():
    calls = []

    class _MessageStore:
        async def load_by_context_epoch(self, session_id, context_epoch):
            calls.append((session_id, context_epoch))
            return [
                {"role": "tool", "content": "raw tool body", "history_seq": 12, "context_epoch": 4}
            ]

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

    rows = await persistence.load_context_segment_messages("sess-1", 4)

    assert calls == [("sess-1", 4)]
    assert rows[0]["content"] == "raw tool body"


@pytest.mark.asyncio
async def test_restore_session_initializes_current_context_epoch_from_history(monkeypatch):
    class _SessionStore:
        async def load(self, session_id):
            return {"session_id": session_id, "status": "active", "user_id": "user-1"}

    class _StateMgr:
        async def load(self, session_id):
            return TravelPlanState(session_id=session_id, phase=2, destination="杭州")

    class _MessageStore:
        async def load_all(self, session_id):
            return [
                {"role": "user", "content": "旧消息", "context_epoch": 2, "history_seq": 10}
            ]

    class _ArchiveStore:
        async def load_latest_snapshot(self, session_id):
            return None

    class _PhaseRouter:
        def sync_phase_state(self, plan):
            return None

    async def _fake_runtime_view(**kwargs):
        return [Message(role=Role.USER, content="restored user")]

    monkeypatch.setattr(
        "api.orchestration.session.persistence.build_runtime_view_for_restore",
        _fake_runtime_view,
    )

    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=_SessionStore(),
        message_store=_MessageStore(),
        archive_store=_ArchiveStore(),
        state_mgr=_StateMgr(),
        phase_router=_PhaseRouter(),
        build_agent=lambda *args, **kwargs: SimpleNamespace(tool_engine=object()),
    )

    restored = await persistence.restore_session("sess-1")

    assert restored["current_context_epoch"] == 2
    assert restored["history_messages"][0].message.content == "旧消息"
    assert len(restored["messages"]) == 1


@pytest.mark.asyncio
async def test_persist_messages_writes_context_epoch_and_rebuild_reason():
    rows: list[dict[str, object]] = []

    class _MessageStore:
        async def append_batch(self, session_id, payload):
            rows.extend(payload)

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

    next_seq = await persistence.persist_messages(
        "sess-1",
        [Message(role=Role.USER, content="phase handoff")],
        phase=2,
        phase2_step="brief",
        run_id="run-1",
        trip_id="trip-a",
        next_history_seq=7,
        context_epoch=4,
        rebuild_reason="phase_forward",
    )

    assert next_seq == 8
    assert rows[0]["history_seq"] == 7
    assert rows[0]["context_epoch"] == 4
    assert rows[0]["rebuild_reason"] == "phase_forward"
