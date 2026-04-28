from __future__ import annotations

from agent.types import Message, Role
from api.orchestration.session.runtime_view import derive_runtime_view


class _Plan:
    def __init__(self, phase, phase3_step=None):
        self.phase = phase
        self.phase3_step = phase3_step


def _msg(role, content, *, phase, phase3_step=None):
    m = Message(role=role, content=content)
    m.__dict__["_phase_tag"] = phase
    m.__dict__["_phase3_step_tag"] = phase3_step
    return m


def test_runtime_view_returns_only_current_phase_segment():
    history = [
        _msg(Role.SYSTEM, "phase1-sys", phase=1),
        _msg(Role.USER, "p1-用户", phase=1),
        _msg(Role.ASSISTANT, "p1-回复", phase=1),
        _msg(Role.SYSTEM, "phase3-sys", phase=3, phase3_step="brief"),
        _msg(Role.ASSISTANT, "handoff", phase=3, phase3_step="brief"),
        _msg(Role.USER, "phase3 用户消息", phase=3, phase3_step="brief"),
    ]
    plan = _Plan(phase=3, phase3_step="brief")
    runtime = derive_runtime_view(history, plan)
    assert [m.content for m in runtime] == [
        "phase3-sys",
        "handoff",
        "phase3 用户消息",
    ]


def test_runtime_view_isolates_phase3_substep():
    history = [
        _msg(Role.SYSTEM, "p3-brief-sys", phase=3, phase3_step="brief"),
        _msg(Role.USER, "brief-用户", phase=3, phase3_step="brief"),
        _msg(Role.SYSTEM, "p3-skeleton-sys", phase=3, phase3_step="skeleton"),
        _msg(Role.USER, "skeleton-用户", phase=3, phase3_step="skeleton"),
    ]
    plan = _Plan(phase=3, phase3_step="skeleton")
    runtime = derive_runtime_view(history, plan)
    assert [m.content for m in runtime] == ["p3-skeleton-sys", "skeleton-用户"]


def test_runtime_view_after_backtrack_does_not_replay_target_phase_history():
    """回退红线：phase 5→3 后的恢复，不应把 phase 3 的旧消息重新塞回 prompt。"""
    history = [
        _msg(Role.SYSTEM, "p3-old-sys", phase=3, phase3_step="brief"),
        _msg(Role.USER, "p3 旧消息", phase=3, phase3_step="brief"),
        _msg(Role.SYSTEM, "p5-sys", phase=5),
        _msg(Role.USER, "p5 用户", phase=5),
        _msg(Role.SYSTEM, "p3-new-sys-after-backtrack", phase=3, phase3_step="brief"),
        _msg(Role.SYSTEM, "[阶段回退]...", phase=3, phase3_step="brief"),
        _msg(Role.USER, "回退触发消息", phase=3, phase3_step="brief"),
    ]
    plan = _Plan(phase=3, phase3_step="brief")
    runtime = derive_runtime_view(history, plan)
    contents = [m.content for m in runtime]
    assert contents == [
        "p3-new-sys-after-backtrack",
        "[阶段回退]...",
        "回退触发消息",
    ]
    assert "p3 旧消息" not in contents
    assert "p5 用户" not in contents


def test_runtime_view_falls_back_to_tail_when_no_phase_tag():
    """旧数据无 phase 标签：降级为最近一段尾部，并允许调用方触发 rebuild。"""
    history = [
        _msg(Role.SYSTEM, "legacy-sys", phase=None),
        _msg(Role.USER, "legacy-用户", phase=None),
    ]
    plan = _Plan(phase=1)
    runtime = derive_runtime_view(history, plan)
    assert len(runtime) == 2
