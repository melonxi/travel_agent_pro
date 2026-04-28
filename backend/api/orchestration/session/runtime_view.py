from __future__ import annotations

from typing import Any

from agent.types import Message


def _phase_tag(message: Message) -> int | None:
    return getattr(message, "_phase_tag", None)


def _phase3_step_tag(message: Message) -> str | None:
    return getattr(message, "_phase3_step_tag", None)


def derive_runtime_view(history: list[Message], plan: Any) -> list[Message]:
    """从 history view 派生与未中断会话一致的 runtime view。

    规则：
      1. 从尾部向前扫描，找到第一条「与当前 phase（及 phase3_step）不一致」的边界；
         返回边界之后的连续段。
      2. 若整段 history 都属于当前 phase，返回全量。
      3. 若历史中没有任何一条匹配当前 phase（如旧数据缺 phase 标签），
         降级为返回 history 全量；调用方需在此情况下走 needs_rebuild=True 路径。
    """
    if not history:
        return []

    target_phase = plan.phase
    target_step = getattr(plan, "phase3_step", None) if target_phase == 3 else None

    cut: int | None = None
    for idx in range(len(history) - 1, -1, -1):
        msg = history[idx]
        msg_phase = _phase_tag(msg)
        msg_step = _phase3_step_tag(msg)
        if msg_phase != target_phase:
            cut = idx + 1
            break
        if target_phase == 3 and msg_step != target_step:
            cut = idx + 1
            break
    else:
        cut = 0  # 整段 history 都属于当前 phase

    if cut is None or cut >= len(history):
        # 无任何匹配 → 降级返回全量，让调用方决定是否 rebuild
        return list(history)

    return list(history[cut:])
