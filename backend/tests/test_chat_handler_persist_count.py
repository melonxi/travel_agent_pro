"""Verify chat handler honors persisted_count cursor for incremental persistence.

These tests pin the contract between finalize_agent_run / persist_run_safely
and SessionPersistence.persist_messages new signature:
    persist_messages(session_id, messages, *, phase, phase3_step, persisted_count)
returning the new persisted_count to be written back into session dict.
"""
from __future__ import annotations

import pytest

from agent.types import Message, Role
from api.orchestration.chat.finalization import finalize_agent_run, persist_run_safely


class _StubStateMgr:
    def __init__(self):
        self.saved = []

    async def save(self, plan):
        self.saved.append(plan)


class _StubSessionStore:
    def __init__(self):
        self.updated = []

    async def update(self, session_id, **kwargs):
        self.updated.append((session_id, kwargs))


class _StubArchiveStore:
    def __init__(self):
        self.calls = []

    async def save_snapshot(self, *args, **kwargs):
        self.calls.append(("save_snapshot", args, kwargs))

    async def save(self, *args, **kwargs):
        self.calls.append(("save", args, kwargs))


class _StubMemoryConfig:
    enabled = False


class _StubConfig:
    memory = _StubMemoryConfig()


class _StubPlan:
    def __init__(self, phase=3, phase3_step="skeleton"):
        self.session_id = "S1"
        self.phase = phase
        self.phase3_step = phase3_step
        self.version = 0

    def to_dict(self):
        return {"phase": self.phase, "phase3_step": self.phase3_step}


class _StubRun:
    def __init__(self):
        self.status = "running"
        self.run_id = "r1"
        self.error_code = None
        self.finished_at = None
        self.can_continue = False
        self.continuation_context = None


class _PersistMessagesSpy:
    def __init__(self):
        self.calls: list[dict] = []
        self.return_value: int | None = None

    async def __call__(
        self,
        session_id,
        messages,
        *,
        phase,
        phase3_step,
        persisted_count,
    ):
        self.calls.append(
            {
                "session_id": session_id,
                "n_messages": len(messages),
                "phase": phase,
                "phase3_step": phase3_step,
                "persisted_count": persisted_count,
            }
        )
        return (
            self.return_value
            if self.return_value is not None
            else len(messages)
        )


def _make_deps(spy):
    deps = type("Deps", (), {})()
    deps.state_mgr = _StubStateMgr()
    deps.session_store = _StubSessionStore()
    deps.archive_store = _StubArchiveStore()
    deps.config = _StubConfig()
    deps.persist_messages = spy
    deps.generate_title = lambda plan: "title"
    deps.append_archived_trip_episode_once = None
    return deps


@pytest.mark.asyncio
async def test_finalize_agent_run_passes_persisted_count_and_writes_back():
    spy = _PersistMessagesSpy()
    deps = _make_deps(spy)
    plan = _StubPlan(phase=3, phase3_step="skeleton")
    session = {"user_id": "u", "persisted_count": 5}
    messages = [Message(role=Role.USER, content=f"m{i}") for i in range(7)]
    run = _StubRun()

    async for _ in finalize_agent_run(
        deps=deps,
        session=session,
        plan=plan,
        messages=messages,
        run=run,
        phase_before_run=3,
    ):
        pass

    assert len(spy.calls) == 1
    assert spy.calls[0]["session_id"] == "S1"
    assert spy.calls[0]["persisted_count"] == 5
    assert spy.calls[0]["phase"] == 3
    assert spy.calls[0]["phase3_step"] == "skeleton"
    assert spy.calls[0]["n_messages"] == 7
    assert session["persisted_count"] == 7


@pytest.mark.asyncio
async def test_persist_run_safely_passes_persisted_count_and_writes_back():
    spy = _PersistMessagesSpy()
    deps = _make_deps(spy)
    plan = _StubPlan(phase=3, phase3_step="skeleton")
    session = {"persisted_count": 2}
    messages = [Message(role=Role.USER, content=f"m{i}") for i in range(4)]
    run = _StubRun()

    await persist_run_safely(
        deps=deps,
        session=session,
        plan=plan,
        messages=messages,
        run=run,
    )

    assert len(spy.calls) == 1
    assert spy.calls[0]["persisted_count"] == 2
    assert spy.calls[0]["phase"] == 3
    assert spy.calls[0]["phase3_step"] == "skeleton"
    assert session["persisted_count"] == 4


@pytest.mark.asyncio
async def test_finalize_uses_zero_if_session_lacks_persisted_count():
    """Backward-compat: legacy session dict without persisted_count → treat as 0."""
    spy = _PersistMessagesSpy()
    deps = _make_deps(spy)
    plan = _StubPlan(phase=3, phase3_step="skeleton")
    session = {"user_id": "u"}  # no persisted_count
    messages = [Message(role=Role.USER, content="x")]
    run = _StubRun()

    async for _ in finalize_agent_run(
        deps=deps,
        session=session,
        plan=plan,
        messages=messages,
        run=run,
        phase_before_run=3,
    ):
        pass

    assert spy.calls[0]["persisted_count"] == 0
    assert session["persisted_count"] == 1


class _SessionPersistenceSpy:
    """Spy double for SessionPersistence used in on_phase_rebuild tests."""

    def __init__(self, return_value: int):
        self._return_value = return_value
        self.calls: list[dict] = []

    async def persist_messages(
        self,
        session_id,
        messages,
        *,
        phase,
        phase3_step,
        persisted_count,
    ):
        self.calls.append(
            {
                "session_id": session_id,
                "messages": list(messages),
                "phase": phase,
                "phase3_step": phase3_step,
                "persisted_count": persisted_count,
            }
        )
        return self._return_value


@pytest.mark.asyncio
async def test_on_phase_rebuild_callback_persists_and_updates_cursor():
    """The on_phase_rebuild factory wires phase/from_step/persisted_count
    into SessionPersistence.persist_messages and writes the new count back
    into the session dict.
    """
    from main import _make_on_phase_rebuild

    sp = _SessionPersistenceSpy(return_value=5)
    session = {"persisted_count": 3}
    msgs = [Message(role=Role.USER, content=f"m{i}") for i in range(5)]

    callback = _make_on_phase_rebuild(sp, session, "sess-1")
    await callback(messages=msgs, from_phase=1, from_step="brief")

    assert len(sp.calls) == 1
    call = sp.calls[0]
    assert call["session_id"] == "sess-1"
    assert call["phase"] == 1
    assert call["phase3_step"] == "brief"
    assert call["persisted_count"] == 3
    assert len(call["messages"]) == 5
    assert session["persisted_count"] == 5
