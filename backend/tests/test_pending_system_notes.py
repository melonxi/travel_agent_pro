"""Unit tests for pending system notes helpers.

These helpers exist so that system messages triggered during tool
execution (e.g. `[实时约束检查]`) don't get appended to `session["messages"]`
in the middle of a parallel tool_calls sequence. They're buffered and
flushed exactly once, just before the next LLM call.
"""

import pytest

from agent.types import Message, Role
from main import flush_pending_system_notes, push_pending_system_note


def _new_session() -> dict:
    return {"messages": []}


def test_push_initializes_buffer_when_missing():
    session = _new_session()
    push_pending_system_note(session, "hello")
    assert session["_pending_system_notes"] == ["hello"]


def test_push_appends_in_order():
    session = _new_session()
    push_pending_system_note(session, "first")
    push_pending_system_note(session, "second")
    assert session["_pending_system_notes"] == ["first", "second"]


def test_push_does_not_touch_messages():
    session = _new_session()
    push_pending_system_note(session, "hello")
    assert session["messages"] == []


def test_flush_appends_each_note_as_runtime_notice():
    session = {"messages": [], "_pending_system_notes": ["a", "b"]}
    msgs: list[Message] = []
    count = flush_pending_system_notes(session, msgs)
    assert count == 2
    assert [m.role for m in msgs] == [Role.USER, Role.USER]
    assert all(m.transient for m in msgs)
    assert all(m.content.startswith('<runtime_notice kind="validation">') for m in msgs)
    assert "a" in msgs[0].content
    assert "b" in msgs[1].content


def test_flush_clears_buffer():
    session = {"messages": [], "_pending_system_notes": ["a"]}
    flush_pending_system_notes(session, [])
    assert session["_pending_system_notes"] == []


def test_flush_is_noop_when_buffer_empty():
    session = {"messages": [], "_pending_system_notes": []}
    msgs: list[Message] = []
    count = flush_pending_system_notes(session, msgs)
    assert count == 0
    assert msgs == []


def test_flush_is_noop_when_buffer_missing():
    session = {"messages": []}
    msgs: list[Message] = []
    count = flush_pending_system_notes(session, msgs)
    assert count == 0
    assert msgs == []


def test_flush_does_not_touch_existing_messages():
    existing = Message(role=Role.USER, content="hi")
    msgs = [existing]
    session = {"messages": [], "_pending_system_notes": ["note"]}
    flush_pending_system_notes(session, msgs)
    assert msgs[0] is existing
    assert msgs[1].role == Role.USER
    assert msgs[1].transient is True
    assert '<runtime_notice kind="validation">' in msgs[1].content
    assert "note" in msgs[1].content
