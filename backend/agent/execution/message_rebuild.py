from __future__ import annotations

from typing import Any

from agent.types import Message, Role, ToolResult


def copy_message(message: Message) -> Message:
    return Message(
        role=message.role,
        content=message.content,
        tool_calls=message.tool_calls,
        tool_result=message.tool_result,
        name=message.name,
        provider_state=message.provider_state,
        incomplete=message.incomplete,
        history_persisted=message.history_persisted,
        history_seq=message.history_seq,
    )


def extract_original_user_message(messages: list[Message]) -> Message:
    for message in reversed(messages):
        if message.role == Role.USER:
            return copy_message(message)
    return Message(role=Role.USER, content="")


def current_tool_names(
    *,
    tool_engine: Any,
    plan: Any | None,
    phase: int | None = None,
) -> list[str]:
    target_phase = phase if phase is not None else (
        plan.phase if plan is not None else None
    )
    if target_phase is None:
        return []
    return [
        tool["name"]
        for tool in tool_engine.get_tools_for_phase(target_phase, plan)
    ]


def build_backtrack_notice(
    *,
    plan: Any | None,
    from_phase: int,
    to_phase: int,
    result: ToolResult,
) -> str:
    reason = "用户请求回退"
    if isinstance(result.data, dict) and result.data.get("reason"):
        reason = str(result.data["reason"])
    elif getattr(plan, "backtrack_history", None):
        reason = plan.backtrack_history[-1].reason
    return f"[阶段回退]\n用户从 phase {from_phase} 回退到 phase {to_phase}，原因：{reason}"


async def rebuild_messages_for_phase_change(
    *,
    phase_router: Any | None,
    context_manager: Any | None,
    plan: Any | None,
    memory_mgr: Any | None,
    memory_enabled: bool,
    user_id: str,
    tool_engine: Any,
    from_phase: int,
    to_phase: int,
    original_user_message: Message,
    result: ToolResult,
) -> list[Message]:
    if (
        phase_router is None
        or context_manager is None
        or plan is None
        or memory_mgr is None
    ):
        raise RuntimeError("Phase-aware rebuild requires router/context/plan/memory")

    phase_prompt = phase_router.get_prompt_for_plan(plan)
    memory_context, _recalled_ids, *_ = (
        await memory_mgr.generate_context(user_id, plan)
        if memory_enabled
        else ("暂无相关用户记忆", [], 0, 0, 0)
    )
    rebuilt = [
        context_manager.build_system_message(
            plan,
            phase_prompt,
            memory_context,
            available_tools=current_tool_names(
                tool_engine=tool_engine,
                plan=plan,
                phase=to_phase,
            ),
        )
    ]

    if to_phase < from_phase:
        rebuilt.append(
            Message(
                role=Role.SYSTEM,
                content=build_backtrack_notice(
                    plan=plan,
                    from_phase=from_phase,
                    to_phase=to_phase,
                    result=result,
                ),
            )
        )
    else:
        rebuilt.append(
            Message(
                role=Role.ASSISTANT,
                content=context_manager.build_phase_handoff_note(
                    plan=plan,
                    from_phase=from_phase,
                    to_phase=to_phase,
                ),
            )
        )

    rebuilt.append(copy_message(original_user_message))
    return rebuilt


async def rebuild_messages_for_phase3_step_change(
    *,
    phase_router: Any | None,
    context_manager: Any | None,
    plan: Any | None,
    memory_mgr: Any | None,
    memory_enabled: bool,
    user_id: str,
    tool_engine: Any,
    original_user_message: Message,
) -> list[Message]:
    if (
        phase_router is None
        or context_manager is None
        or plan is None
        or memory_mgr is None
    ):
        raise RuntimeError("Phase3 step rebuild requires router/context/plan/memory")

    phase_prompt = phase_router.get_prompt_for_plan(plan)
    memory_context, _recalled_ids, *_ = (
        await memory_mgr.generate_context(user_id, plan)
        if memory_enabled
        else ("暂无相关用户记忆", [], 0, 0, 0)
    )
    return [
        context_manager.build_system_message(
            plan,
            phase_prompt,
            memory_context,
            available_tools=current_tool_names(
                tool_engine=tool_engine,
                plan=plan,
                phase=plan.phase,
            ),
        ),
        copy_message(original_user_message),
    ]
