from __future__ import annotations


def push_pending_system_note(session: dict, content: str) -> None:
    """Buffer a system note to be flushed into messages before next LLM call.

    Writing to session["messages"] during tool execution risks inserting
    a system message between an assistant.tool_calls and its tool responses,
    which breaks OpenAI protocol. Use this helper instead; flush at on_before_llm.
    """
    session.setdefault("_pending_system_notes", []).append(content)


def flush_pending_system_notes(session: dict, msgs: list) -> int:
    """Flush buffered notes into msgs as SYSTEM messages. Returns count flushed."""
    from agent.types import Message, Role

    pending = session.get("_pending_system_notes") or []
    if not pending:
        return 0
    for content in pending:
        msgs.append(Message(role=Role.SYSTEM, content=content))
    session["_pending_system_notes"] = []
    return len(pending)
