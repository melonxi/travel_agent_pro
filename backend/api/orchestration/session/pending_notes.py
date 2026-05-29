from __future__ import annotations

from agent.tagged_context import runtime_notice_message


def push_pending_system_note(session: dict, content: str) -> None:
    """Buffer a system note to be flushed into messages before next LLM call.

    Writing to session["messages"] during tool execution risks inserting
    a system message between an assistant.tool_calls and its tool responses,
    which breaks OpenAI protocol. Use this helper instead; flush at on_before_llm.
    """
    session.setdefault("_pending_system_notes", []).append(content)


def flush_pending_system_notes(session: dict, msgs: list) -> int:
    """Flush buffered notes into msgs as runtime notices. Returns count flushed."""
    pending = session.get("_pending_system_notes") or []
    if not pending:
        return 0
    for content in pending:
        msgs.append(runtime_notice_message("validation", content))
    session["_pending_system_notes"] = []
    return len(pending)
