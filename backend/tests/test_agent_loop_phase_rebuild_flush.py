"""Tests for AgentLoop.on_phase_rebuild flush callback."""
from __future__ import annotations

from typing import Any

import pytest

from agent.loop import AgentLoop
from agent.types import Message, Role, ToolResult


class _StubLLM:
    pass


class _StubToolEngine:
    def get_tools_for_phase(self, phase, plan):
        return []


class _StubHooks:
    pass


class _StubPlan:
    def __init__(self, phase: int = 1, phase3_step: str | None = None):
        self.phase = phase
        self.phase3_step = phase3_step


def _make_loop(callback=None, plan=None) -> AgentLoop:
    return AgentLoop(
        llm=_StubLLM(),
        tool_engine=_StubToolEngine(),
        hooks=_StubHooks(),
        plan=plan if plan is not None else _StubPlan(phase=1),
        on_phase_rebuild=callback,
    )


def _ok_result() -> ToolResult:
    return ToolResult(tool_call_id="x", status="success")


@pytest.mark.asyncio
async def test_on_phase_rebuild_invoked_before_phase_change_with_pre_state(
    monkeypatch,
):
    invocations: list[dict[str, Any]] = []

    async def callback(*, messages, from_phase, from_step):
        invocations.append(
            {
                "messages_snapshot": list(messages),
                "from_phase": from_phase,
                "from_step": from_step,
            }
        )

    loop = _make_loop(callback=callback)

    rebuild_calls: list[dict[str, Any]] = []

    async def fake_rebuild(**kwargs):
        rebuild_calls.append(kwargs)
        return [Message(role=Role.SYSTEM, content="post-rebuild")]

    monkeypatch.setattr(
        "agent.loop.rebuild_messages_for_phase_change", fake_rebuild
    )

    pre_messages = [
        Message(role=Role.USER, content="phase1 user"),
        Message(role=Role.ASSISTANT, content="phase1 reply"),
    ]

    rebuilt = await loop._rebuild_messages_for_phase_change(
        messages=pre_messages,
        from_phase=1,
        to_phase=3,
        from_step=None,
        original_user_message=pre_messages[0],
        result=_ok_result(),
    )

    assert len(invocations) == 1
    assert invocations[0]["from_phase"] == 1
    assert invocations[0]["from_step"] is None
    assert [m.content for m in invocations[0]["messages_snapshot"]] == [
        "phase1 user",
        "phase1 reply",
    ]
    assert len(rebuild_calls) == 1
    assert rebuilt[0].content == "post-rebuild"


@pytest.mark.asyncio
async def test_on_phase_rebuild_invoked_before_phase3_step_change_with_pre_step(
    monkeypatch,
):
    invocations: list[dict[str, Any]] = []

    async def callback(*, messages, from_phase, from_step):
        invocations.append({"from_phase": from_phase, "from_step": from_step})

    loop = _make_loop(
        callback=callback,
        plan=_StubPlan(phase=3, phase3_step="skeleton"),
    )

    async def fake_rebuild(**kwargs):
        return [Message(role=Role.SYSTEM, content="rebuilt")]

    monkeypatch.setattr(
        "agent.loop.rebuild_messages_for_phase3_step_change", fake_rebuild
    )

    pre_messages = [Message(role=Role.USER, content="brief 用户")]

    await loop._rebuild_messages_for_phase3_step_change(
        messages=pre_messages,
        original_user_message=pre_messages[0],
        from_step="brief",
    )

    assert len(invocations) == 1
    assert invocations[0]["from_phase"] == 3
    assert invocations[0]["from_step"] == "brief"


@pytest.mark.asyncio
async def test_on_phase_rebuild_failure_does_not_block_rebuild(monkeypatch):
    called: list[int] = []

    async def callback(**_kwargs):
        called.append(1)
        raise RuntimeError("persistence is down")

    loop = _make_loop(callback=callback)

    async def fake_rebuild(**kwargs):
        return [Message(role=Role.SYSTEM, content="ok")]

    monkeypatch.setattr(
        "agent.loop.rebuild_messages_for_phase_change", fake_rebuild
    )

    rebuilt = await loop._rebuild_messages_for_phase_change(
        messages=[Message(role=Role.USER, content="x")],
        from_phase=1,
        to_phase=3,
        from_step=None,
        original_user_message=Message(role=Role.USER, content="x"),
        result=_ok_result(),
    )

    assert called == [1]  # 确认 callback 真的被调用而不是被跳过
    assert rebuilt[0].content == "ok"


@pytest.mark.asyncio
async def test_on_phase_rebuild_none_callback_skips_invocation(monkeypatch):
    loop = _make_loop(callback=None)

    async def fake_rebuild(**kwargs):
        return [Message(role=Role.SYSTEM, content="ok")]

    monkeypatch.setattr(
        "agent.loop.rebuild_messages_for_phase_change", fake_rebuild
    )

    rebuilt = await loop._rebuild_messages_for_phase_change(
        messages=[],
        from_phase=1,
        to_phase=3,
        from_step=None,
        original_user_message=Message(role=Role.USER, content="x"),
        result=_ok_result(),
    )

    assert rebuilt[0].content == "ok"


@pytest.mark.asyncio
async def test_on_phase_rebuild_callback_cannot_mutate_messages_for_rebuild(
    monkeypatch,
):
    """callback 收到浅拷贝，mutate 不影响后续 rebuild 看到的 messages。"""

    async def callback(*, messages, **_kwargs):
        messages.clear()  # mutate！

    loop = _make_loop(callback=callback)

    rebuild_seen: list[list[Message]] = []

    async def fake_rebuild(**kwargs):
        return [Message(role=Role.SYSTEM, content="rebuilt")]

    monkeypatch.setattr("agent.loop.rebuild_messages_for_phase_change", fake_rebuild)

    pre_messages = [
        Message(role=Role.USER, content="m1"),
        Message(role=Role.ASSISTANT, content="m2"),
    ]
    await loop._rebuild_messages_for_phase_change(
        messages=pre_messages,
        from_phase=1,
        to_phase=3,
        from_step=None,
        original_user_message=pre_messages[0],
        result=ToolResult(tool_call_id="x", status="success"),
    )

    # callback 清了它收到的列表，但原 list 应该不受影响
    assert len(pre_messages) == 2
    assert [m.content for m in pre_messages] == ["m1", "m2"]
    _ = rebuild_seen  # 占位，避免 lint 警告
