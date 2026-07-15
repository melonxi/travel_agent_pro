"""P2-5：soft judge / gate 反馈必须走 pending_notes，不能直接 append 运行时消息。"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent.types import Message, Role, ToolResult
from api.orchestration.agent import hooks as hooks_mod
from api.orchestration.agent.hooks import build_agent_hooks
from config import QualityGateConfig


def _minimal_config():
    cfg = MagicMock()
    cfg.quality_gate = QualityGateConfig(threshold=4.0, max_retries=2)
    cfg.llm = MagicMock()
    return cfg


@pytest.mark.asyncio
async def test_hard_constraint_gate_feedback_uses_pending_notes(monkeypatch):
    sessions = {
        "s1": {
            "messages": [],
            "_active_runtime_messages": [
                Message(role=Role.ASSISTANT, content=None, tool_calls=[]),
                Message(
                    role=Role.TOOL,
                    content="ok",
                    tool_result=ToolResult(
                        tool_call_id="tc1", status="success", data="ok"
                    ),
                ),
            ],
        }
    }
    plan = MagicMock()
    plan.session_id = "s1"
    plan.phase = 3
    plan.dates = None
    plan.budget = None
    plan.destination = "东京"
    plan.to_dict.return_value = {}
    plan.preferences = []

    monkeypatch.setattr(
        hooks_mod,
        "validate_hard_constraints",
        lambda p: ["预算超支 ¥1000"],
    )

    hooks, _events = build_agent_hooks(
        plan=plan,
        sessions=sessions,
        resolved_context_window={},
        config=_minimal_config(),
        context_mgr=MagicMock(),
        compression_events=[],
        create_llm_provider_func=MagicMock(),
        collect_forced_tool_call_arguments=MagicMock(),
        quality_gate_retries={},
    )

    result = await hooks.run_gate(
        "before_phase_transition",
        plan=plan,
        from_phase=3,
        to_phase=4,
    )

    assert result.allowed is False
    session = sessions["s1"]
    notes = session.get("_pending_system_notes") or []
    assert notes
    assert any("硬约束" in note or "预算" in note for note in notes)
    # 不得直接污染 active runtime messages（会插在 tool_calls 与 tool 之间）。
    assert all(
        not (getattr(m, "content", None) or "").startswith("[质量门控]")
        for m in session["_active_runtime_messages"]
    )
