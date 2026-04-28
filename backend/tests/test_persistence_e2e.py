"""End-to-end integration tests for dual-track persistence.

These tests pin the SMELL-4 regression caught by review on Task 6.5: the
chat handler must persist the *runtime view* (session["messages"]) — which
is the only track that all main append sites actually mutate — through
finalize_agent_run, ensuring the full conversation history is written to
the real messages table.

Unlike the unit tests in test_chat_handler_persist_count.py (which spy on
deps.persist_messages), these tests wire up real Database / MessageStore /
SessionStore / ArchiveStore / SessionPersistence instances over an
in-memory SQLite, then read back the messages table directly to verify
nothing got dropped.
"""
from __future__ import annotations

import pytest

from agent.types import Message, Role
from api.orchestration.chat.finalization import finalize_agent_run
from api.orchestration.session.persistence import SessionPersistence
from api.orchestration.session.runtime_view import append_dual_track
from main import _make_on_phase_rebuild
from phase.router import PhaseRouter
from state.models import TravelPlanState
from storage.archive_store import ArchiveStore
from storage.database import Database
from storage.message_store import MessageStore
from storage.session_store import SessionStore


class _StubStateMgr:
    """In-memory stand-in for StateManager (avoids touching disk)."""

    def __init__(self):
        self._plans: dict[str, TravelPlanState] = {}

    async def save(self, plan: TravelPlanState) -> None:
        plan.version += 1
        self._plans[plan.session_id] = plan

    async def load(self, session_id: str) -> TravelPlanState:
        if session_id not in self._plans:
            raise FileNotFoundError(session_id)
        return self._plans[session_id]


class _StubMemoryConfig:
    enabled = False


class _StubConfig:
    memory = _StubMemoryConfig()


class _StubRun:
    def __init__(self):
        self.status = "running"
        self.run_id = "r1"
        self.error_code = None
        self.finished_at = None


def _stub_build_agent(plan, user_id, *, session=None, compression_events=None):
    return None


async def _make_persistence_stack(tmp_path=None):
    db = Database(db_path=":memory:")
    await db.initialize()
    session_store = SessionStore(db)
    message_store = MessageStore(db)
    archive_store = ArchiveStore(db)
    state_mgr = _StubStateMgr()
    phase_router = PhaseRouter()

    async def ensure_ready():
        await db.initialize()  # idempotent

    sp = SessionPersistence(
        ensure_storage_ready=ensure_ready,
        db=db,
        session_store=session_store,
        message_store=message_store,
        archive_store=archive_store,
        state_mgr=state_mgr,
        phase_router=phase_router,
        build_agent=_stub_build_agent,
    )
    return db, session_store, message_store, archive_store, state_mgr, sp


def _make_deps(sp, state_mgr, session_store, archive_store):
    deps = type("Deps", (), {})()
    deps.state_mgr = state_mgr
    deps.session_store = session_store
    deps.archive_store = archive_store
    deps.config = _StubConfig()
    deps.persist_messages = sp.persist_messages
    deps.generate_title = lambda plan: "title"
    deps.append_archived_trip_episode_once = None
    return deps


async def _create_session_row(session_store, state_mgr, session_id="sess_aaaaaaaaaaaa"):
    await session_store.create(session_id, user_id="u1", title="t")
    plan = TravelPlanState(session_id=session_id, phase=1, phase3_step="brief")
    await state_mgr.save(plan)
    return plan


@pytest.mark.asyncio
async def test_full_turn_persists_user_system_assistant_to_db():
    """SMELL-4 regression guard: a complete turn (user msg + system feedback +
    assistant reply) must end up in the messages table after finalize.

    Reproduces the bug introduced by reading session["history_messages"] in
    finalize when most main-path appends only mutate session["messages"].
    """
    db, session_store, message_store, archive_store, state_mgr, sp = (
        await _make_persistence_stack()
    )
    plan = await _create_session_row(session_store, state_mgr)
    sid = plan.session_id

    # Empty session as if just opened — both tracks present, cursor at 0
    session: dict = {
        "messages": [],
        "history_messages": [],
        "persisted_count": 0,
        "user_id": "u1",
    }

    # Simulate chat_routes appending the user turn (runtime_view only,
    # mirroring the actual main-path append in chat_routes.py:131)
    session["messages"].append(Message(role=Role.USER, content="hi"))
    # Simulate hooks appending system feedback via dual-track helper
    append_dual_track(
        session, plan, Message(role=Role.SYSTEM, content="feedback")
    )
    # Simulate agent loop appending the assistant reply (runtime_view only)
    session["messages"].append(Message(role=Role.ASSISTANT, content="hello"))

    deps = _make_deps(sp, state_mgr, session_store, archive_store)
    run = _StubRun()
    async for _ in finalize_agent_run(
        deps=deps,
        session=session,
        plan=plan,
        messages=session["messages"],
        run=run,
        phase_before_run=plan.phase,
    ):
        pass

    rows = await message_store.load_all(sid)
    roles = [r["role"] for r in rows]
    contents = [r["content"] for r in rows]
    assert roles == ["user", "system", "assistant"], (
        f"Expected USER + SYSTEM + ASSISTANT in DB, got {roles}; "
        f"this is the SMELL-4 regression — main-path appends went into "
        f"runtime_view but finalize wrote history_view."
    )
    assert contents == ["hi", "feedback", "hello"]
    # phase tag stamped to plan.phase at finalize time
    assert all(r["phase"] == 1 for r in rows)
    # cursor advanced
    assert session["persisted_count"] == 3
    await db.close()


@pytest.mark.asyncio
async def test_phase_rebuild_callback_tags_old_segment_with_from_phase():
    """Phase boundary: when on_phase_rebuild callback fires (just before
    agent loop replaces messages with the to_phase rebuilt prompt set),
    the entire current runtime_view must be flushed to DB tagged with
    from_phase / from_step — never the to_phase that plan has just
    advanced to. This guards the phase tagging contract for replays.
    """
    db, session_store, message_store, archive_store, state_mgr, sp = (
        await _make_persistence_stack()
    )
    plan = await _create_session_row(session_store, state_mgr)
    sid = plan.session_id

    session: dict = {
        "messages": [],
        "history_messages": [],
        "persisted_count": 0,
        "user_id": "u1",
    }

    # Old phase (1) accumulates two messages
    session["messages"].append(Message(role=Role.USER, content="brief?"))
    session["messages"].append(Message(role=Role.ASSISTANT, content="brief done"))

    # on_phase_rebuild fires BEFORE agent loop swaps in to_phase prompts;
    # plan.phase is still the from_phase at this point in the real flow.
    # The callback flushes runtime_view tagged with from_phase=1 / from_step.
    callback = _make_on_phase_rebuild(sp, session, sid)
    await callback(messages=session["messages"], from_phase=1, from_step="brief")

    rows = await message_store.load_all(sid)
    assert [r["content"] for r in rows] == ["brief?", "brief done"]
    # Both rows tagged with from_phase=1 — NOT to_phase (rebuild has not
    # advanced the cursor yet from the persistence point of view)
    assert all(r["phase"] == 1 for r in rows)
    assert all(r["phase3_step"] == "brief" for r in rows)
    # Cursor advanced to len(flushed)
    assert session["persisted_count"] == 2
    await db.close()


@pytest.mark.asyncio
async def test_restore_session_cursor_aligns_with_runtime_view():
    """After finalize + restore, the persisted_count cursor must equal
    len(runtime_view), so a subsequent persist_messages call performs
    a clean incremental append with no duplication and no IntegrityError.
    """
    db, session_store, message_store, archive_store, state_mgr, sp = (
        await _make_persistence_stack()
    )
    plan = await _create_session_row(session_store, state_mgr)
    sid = plan.session_id

    session: dict = {
        "messages": [],
        "history_messages": [],
        "persisted_count": 0,
        "user_id": "u1",
    }
    session["messages"].append(Message(role=Role.USER, content="m0"))
    session["messages"].append(Message(role=Role.ASSISTANT, content="m1"))

    deps = _make_deps(sp, state_mgr, session_store, archive_store)
    run = _StubRun()
    async for _ in finalize_agent_run(
        deps=deps,
        session=session,
        plan=plan,
        messages=session["messages"],
        run=run,
        phase_before_run=plan.phase,
    ):
        pass

    # Simulate restart: restore_session reads from DB
    restored = await sp.restore_session(sid)
    assert restored is not None
    runtime_view = restored["messages"]
    # cursor must align with runtime_view length so the next append slice
    # is non-overlapping with what's already in DB
    assert restored["persisted_count"] == len(runtime_view), (
        f"persisted_count={restored['persisted_count']} but "
        f"len(runtime_view)={len(runtime_view)} — Task 6 bug regressed."
    )

    # Append a new message and finalize again — no duplication, no error
    restored["messages"].append(Message(role=Role.USER, content="m2"))
    if "history_messages" in restored:
        # mirror what append_dual_track would do for a real append site
        # — but the helper is not strictly required here; we just need
        # cursor math to work.
        pass

    run2 = _StubRun()
    async for _ in finalize_agent_run(
        deps=deps,
        session=restored,
        plan=restored["plan"],
        messages=restored["messages"],
        run=run2,
        phase_before_run=restored["plan"].phase,
    ):
        pass

    rows = await message_store.load_all(sid)
    contents = [r["content"] for r in rows]
    assert contents == ["m0", "m1", "m2"], (
        f"Expected no duplicates after restore + append, got {contents}"
    )
    await db.close()
