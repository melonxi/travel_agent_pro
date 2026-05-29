from __future__ import annotations

from agent.types import Message, Role


def is_persisted_history_message(message: Message) -> bool:
    return not message.transient and message.role != Role.SYSTEM


def clean_persisted_session_messages(messages: list[Message]) -> list[Message]:
    return [message for message in messages if is_persisted_history_message(message)]


def active_runtime_messages(session: dict) -> list[Message]:
    active = session.get("_active_runtime_messages")
    if isinstance(active, list):
        return active
    return session["messages"]


def strip_non_initial_system_messages(
    messages: list[Message],
    *,
    logger,
    context: str,
) -> int:
    kept: list[Message] = []
    removed = 0
    for index, message in enumerate(messages):
        if message.role == Role.SYSTEM and index != 0:
            removed += 1
            logger.warning(
                "Stripping non-initial system message from %s index=%s preview=%r",
                context,
                index,
                (message.content or "")[:120],
            )
            continue
        kept.append(message)
    if removed:
        messages[:] = kept
    return removed


def assert_main_runtime_prompt_shape(messages: list[Message]) -> None:
    for index, message in enumerate(messages):
        if message.role == Role.SYSTEM and index != 0:
            raise AssertionError(
                "Role.SYSTEM is only allowed at index 0 in main runtime prompts"
            )
