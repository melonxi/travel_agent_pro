"""Tests for append_dual_track helper.

The helper synchronously appends a message to both runtime_view
(session["messages"]) and history_view (session["history_messages"]),
attaching phase sidecar tags only to the history copy. Legacy sessions
without a "history_messages" key fall back to single-track append.
"""
from __future__ import annotations

from agent.types import Message, Role
from api.orchestration.session.runtime_view import append_dual_track


class _StubPlan:
    def __init__(self, phase: int = 3, phase3_step: str | None = "skeleton"):
        self.phase = phase
        self.phase3_step = phase3_step


def test_append_dual_track_appends_to_both_views():
    session = {"messages": [], "history_messages": []}
    plan = _StubPlan(phase=3, phase3_step="skeleton")
    msg = Message(role=Role.SYSTEM, content="hello")

    append_dual_track(session, plan, msg)

    assert len(session["messages"]) == 1
    assert len(session["history_messages"]) == 1
    assert session["messages"][0].content == "hello"
    assert session["history_messages"][0].content == "hello"


def test_append_dual_track_only_history_carries_phase_sidecar():
    session = {"messages": [], "history_messages": []}
    plan = _StubPlan(phase=3, phase3_step="skeleton")
    msg = Message(role=Role.SYSTEM, content="hello")

    append_dual_track(session, plan, msg)

    runtime_msg = session["messages"][0]
    history_msg = session["history_messages"][0]
    assert getattr(history_msg, "_phase_tag", None) == 3
    assert getattr(history_msg, "_phase3_step_tag", None) == "skeleton"
    # runtime copy must NOT carry sidecar (avoid leaking implementation
    # details into the in-memory runtime view).
    assert getattr(runtime_msg, "_phase_tag", None) is None
    assert getattr(runtime_msg, "_phase3_step_tag", None) is None


def test_append_dual_track_legacy_session_only_runtime():
    """Legacy session dicts without history_messages must NOT have one
    silently created — they remain single-track."""
    session = {"messages": []}
    plan = _StubPlan()
    msg = Message(role=Role.SYSTEM, content="legacy")

    append_dual_track(session, plan, msg)

    assert len(session["messages"]) == 1
    assert "history_messages" not in session


def test_append_dual_track_runtime_and_history_are_distinct_objects():
    session = {"messages": [], "history_messages": []}
    plan = _StubPlan(phase=1, phase3_step=None)
    msg = Message(role=Role.SYSTEM, content="x")

    append_dual_track(session, plan, msg)

    runtime_msg = session["messages"][0]
    history_msg = session["history_messages"][0]
    assert runtime_msg is not history_msg
    # mutate one, the other must not change
    runtime_msg.content = "mutated"
    assert history_msg.content == "x"


def test_append_dual_track_phase_non_three_skips_step_tag_value():
    """Phase != 3 means phase3_step on history may still get attached
    from plan.phase3_step (helper does not gate); verify behavior is
    'tag what plan says'."""
    session = {"messages": [], "history_messages": []}
    plan = _StubPlan(phase=1, phase3_step=None)
    msg = Message(role=Role.SYSTEM, content="p1")

    append_dual_track(session, plan, msg)

    history_msg = session["history_messages"][0]
    assert getattr(history_msg, "_phase_tag", None) == 1
    assert getattr(history_msg, "_phase3_step_tag", None) is None
