from __future__ import annotations

from copy import copy
from dataclasses import dataclass
from typing import Any

from agent.execution.message_rebuild import copy_message, current_tool_names
from agent.types import Message, Role


@dataclass(frozen=True)
class HistoryMessage:
    message: Message
    phase: int | None = None
    phase2_step: str | None = None
    history_seq: int | None = None
    context_epoch: int | None = None
    rebuild_reason: str | None = None
    run_id: str | None = None
    trip_id: str | None = None


RESTORE_CACHE_FIRST_MAX_MESSAGES = 120


def build_cache_first_history_for_restore(
    history_view: list[HistoryMessage],
    *,
    max_messages: int = RESTORE_CACHE_FIRST_MAX_MESSAGES,
) -> list[Message]:
    restored: list[Message] = []
    for item in history_view:
        message = item.message
        if message.role == Role.SYSTEM:
            continue
        if message.transient:
            continue
        restored.append(copy_message(message))
    if max_messages > 0 and len(restored) > max_messages:
        return restored[-max_messages:]
    return restored


def _is_backtrack_result(history_message: HistoryMessage) -> bool:
    result = history_message.message.tool_result
    return (
        result is not None
        and isinstance(result.data, dict)
        and bool(result.data.get("backtracked"))
    )


def _has_reliable_phase_metadata(history_view: list[HistoryMessage]) -> bool:
    return any(
        item.phase is not None or item.phase2_step is not None
        for item in history_view
    )


def _latest_backtrack_index(history_view: list[HistoryMessage]) -> int | None:
    for index in range(len(history_view) - 1, -1, -1):
        if _is_backtrack_result(history_view[index]):
            return index
    return None


def _latest_user_after_index(
    history_view: list[HistoryMessage],
    start_index: int,
    *,
    context_epoch: int | None = None,
) -> Message | None:
    for item in reversed(history_view[: start_index + 1]):
        if item.message.role == Role.USER:
            if context_epoch is not None and item.context_epoch != context_epoch:
                continue
            return copy_message(item.message)
    return None


def _latest_epoch(history_view: list[HistoryMessage]) -> int | None:
    epochs = [
        int(item.context_epoch)
        for item in history_view
        if item.context_epoch is not None
    ]
    return max(epochs) if epochs else None


def _latest_current_phase_user(
    history_view: list[HistoryMessage],
    *,
    phase: int,
    phase2_step: str | None,
    context_epoch: int | None = None,
) -> Message | None:
    for item in reversed(history_view):
        if item.message.role != Role.USER:
            continue
        if context_epoch is not None and item.context_epoch != context_epoch:
            continue
        if item.phase != phase:
            continue
        if phase == 2 and item.phase2_step != phase2_step:
            continue
        return copy_message(item.message)
    return None


def _latest_user(history_view: list[HistoryMessage]) -> Message | None:
    for item in reversed(history_view):
        if item.message.role == Role.USER:
            return copy_message(item.message)
    return None


def _latest_user_in_epoch(
    history_view: list[HistoryMessage],
    context_epoch: int,
) -> Message | None:
    for item in reversed(history_view):
        if item.context_epoch == context_epoch and item.message.role == Role.USER:
            return copy_message(item.message)
    return None


def select_restore_anchor(
    *,
    history_view: list[HistoryMessage],
    plan: Any,
) -> Message:
    latest_epoch = _latest_epoch(history_view)
    backtrack_index = _latest_backtrack_index(history_view)
    if backtrack_index is not None:
        anchor = (
            _latest_user_after_index(
                history_view,
                backtrack_index,
                context_epoch=latest_epoch,
            )
            if latest_epoch is not None
            else None
        )
        if anchor is not None:
            return anchor
        anchor = _latest_user_after_index(history_view, backtrack_index)
        if anchor is not None:
            return anchor

    if _has_reliable_phase_metadata(history_view):
        anchor = (
            _latest_current_phase_user(
                history_view,
                phase=plan.phase,
                phase2_step=getattr(plan, "phase2_step", None),
                context_epoch=latest_epoch,
            )
            if latest_epoch is not None
            else None
        )
        if anchor is not None:
            return anchor
        anchor = _latest_current_phase_user(
            history_view,
            phase=plan.phase,
            phase2_step=getattr(plan, "phase2_step", None),
        )
        if anchor is not None:
            return anchor

    if latest_epoch is not None:
        anchor = _latest_user_in_epoch(history_view, latest_epoch)
        if anchor is not None:
            return anchor

    anchor = _latest_user(history_view)
    if anchor is not None:
        return anchor

    return Message(role=Role.USER, content="")


def _restore_runtime_plan(plan: Any) -> Any:
    if getattr(plan, "phase", None) == 2:
        return plan

    runtime_plan = copy(plan)
    if hasattr(runtime_plan, "phase2_step"):
        runtime_plan.phase2_step = None
    return runtime_plan


async def build_runtime_view_for_restore(
    *,
    history_view: list[HistoryMessage],
    plan: Any,
    user_id: str,
    phase_router: Any,
    context_manager: Any,
    memory_mgr: Any,
    memory_enabled: bool,
    tool_engine: Any,
) -> list[Message]:
    return build_cache_first_history_for_restore(history_view)
