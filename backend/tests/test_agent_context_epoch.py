from __future__ import annotations

import pytest

from agent.loop import AgentLoop
from agent.types import Message, Role, ToolResult


class _ToolEngine:
    def get_tools_for_phase(self, phase, plan):
        return []


class _Hooks:
    async def run(self, *args, **kwargs):
        return None


class _Plan:
    phase = 3
    phase3_step = "candidate"


def _loop(on_context_rebuild):
    return AgentLoop(
        llm=object(),
        tool_engine=_ToolEngine(),
        hooks=_Hooks(),
        phase_router=object(),
        context_manager=object(),
        plan=_Plan(),
        memory_mgr=None,
        memory_enabled=False,
        on_context_rebuild=on_context_rebuild,
    )


@pytest.mark.asyncio
async def test_phase_forward_rebuild_advances_context_epoch_before_rebuild(monkeypatch):
    calls = []

    async def on_context_rebuild(**kwargs):
        calls.append(kwargs)

    loop = _loop(on_context_rebuild)

    async def fake_rebuild(**kwargs):
        assert calls[-1]["rebuild_reason"] == "phase_forward"
        return [Message(role=Role.SYSTEM, content="new phase")]

    monkeypatch.setattr("agent.loop.rebuild_messages_for_phase_change", fake_rebuild)

    await loop._rebuild_messages_for_phase_change(
        messages=[Message(role=Role.USER, content="go")],
        from_phase=1,
        to_phase=3,
        original_user_message=Message(role=Role.USER, content="go"),
        result=ToolResult(tool_call_id="tc", status="success", data={}),
    )

    assert calls == [
        {
            "messages": [Message(role=Role.USER, content="go")],
            "from_phase": 1,
            "from_phase3_step": None,
            "to_phase": 3,
            "to_phase3_step": "candidate",
            "rebuild_reason": "phase_forward",
        }
    ]


@pytest.mark.asyncio
async def test_backtrack_rebuild_advances_context_epoch_with_backtrack_reason(monkeypatch):
    calls = []

    async def on_context_rebuild(**kwargs):
        calls.append(kwargs)

    loop = _loop(on_context_rebuild)

    async def fake_rebuild(**kwargs):
        return [Message(role=Role.SYSTEM, content="backtracked")]

    monkeypatch.setattr("agent.loop.rebuild_messages_for_phase_change", fake_rebuild)

    await loop._rebuild_messages_for_phase_change(
        messages=[Message(role=Role.TOOL, content=None)],
        from_phase=5,
        to_phase=3,
        original_user_message=Message(role=Role.USER, content="重做框架"),
        result=ToolResult(
            tool_call_id="tc",
            status="success",
            data={"backtrack": {"from_phase": 5, "to_phase": 3}},
        ),
    )

    assert calls[0]["rebuild_reason"] == "backtrack"
    assert calls[0]["from_phase"] == 5
    assert calls[0]["to_phase"] == 3


@pytest.mark.asyncio
async def test_phase3_step_change_rebuild_advances_context_epoch(monkeypatch):
    calls = []

    async def on_context_rebuild(**kwargs):
        calls.append(kwargs)

    loop = _loop(on_context_rebuild)

    async def fake_rebuild(**kwargs):
        return [Message(role=Role.SYSTEM, content="new step")]

    monkeypatch.setattr("agent.loop.rebuild_messages_for_phase3_step_change", fake_rebuild)

    await loop._rebuild_messages_for_phase3_step_change(
        messages=[Message(role=Role.USER, content="候选池好了")],
        original_user_message=Message(role=Role.USER, content="候选池好了"),
        from_phase3_step="brief",
        to_phase3_step="candidate",
    )

    assert calls == [
        {
            "messages": [Message(role=Role.USER, content="候选池好了")],
            "from_phase": 3,
            "from_phase3_step": "brief",
            "to_phase": 3,
            "to_phase3_step": "candidate",
            "rebuild_reason": "phase3_step_change",
        }
    ]
