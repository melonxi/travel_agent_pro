from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from agent.types import Message, Role, ToolCall, ToolResult

DEFAULT_SAFETY_MARGIN = 2000
MIN_PROMPT_BUDGET = 1024
SOFT_COMPACT_RATIO = 0.6
AGGRESSIVE_COMPACT_RATIO = 0.85
OVERSIZED_TOOL_RATIO = 0.2
OVERSIZED_TOOL_MIN_TOKENS = 600


@dataclass(frozen=True)
class CompactionOutcome:
    messages: list[Message]
    estimated_before: int
    estimated_after: int
    usage_ratio_before: float
    changed: bool
    mode: str | None = None
    compacted_tool_messages: int = 0
    largest_tool_tokens: int = 0


def compute_prompt_budget(
    context_window: int,
    max_output_tokens: int,
    *,
    safety_margin: int = DEFAULT_SAFETY_MARGIN,
) -> int:
    return max(MIN_PROMPT_BUDGET, context_window - max_output_tokens - safety_margin)


def estimate_messages_tokens(
    messages: list[Message],
    *,
    tools: list[dict[str, Any]] | None = None,
) -> int:
    total = sum(estimate_message_tokens(message) for message in messages)
    if tools:
        total += _estimate_text_tokens(_safe_dump(tools))
    return total


def estimate_message_tokens(message: Message) -> int:
    parts: list[str] = []
    if message.content:
        parts.append(message.content)

    if message.tool_calls:
        parts.extend(_tool_call_parts(message.tool_calls))

    if message.tool_result:
        parts.append(
            _safe_dump(
                {
                    "status": message.tool_result.status,
                    "data": message.tool_result.data,
                }
            )
        )

    return sum(_estimate_text_tokens(part) for part in parts)


def compact_messages_for_prompt(
    messages: list[Message],
    *,
    prompt_budget: int,
    tools: list[dict[str, Any]] | None = None,
    soft_ratio: float = SOFT_COMPACT_RATIO,
    aggressive_ratio: float = AGGRESSIVE_COMPACT_RATIO,
    oversized_tool_ratio: float = OVERSIZED_TOOL_RATIO,
    oversized_tool_min_tokens: int = OVERSIZED_TOOL_MIN_TOKENS,
) -> CompactionOutcome:
    estimated_before = estimate_messages_tokens(messages, tools=tools)
    usage_ratio_before = estimated_before / prompt_budget if prompt_budget else 0.0

    pending_tool_calls: dict[str, ToolCall] = {}
    largest_tool_tokens = 0
    tool_message_specs: list[tuple[int, str | None]] = []

    for idx, message in enumerate(messages):
        if message.role == Role.ASSISTANT and message.tool_calls:
            for tool_call in message.tool_calls:
                pending_tool_calls[tool_call.id] = tool_call
        if message.role == Role.TOOL and message.tool_result:
            tool_call = pending_tool_calls.get(message.tool_result.tool_call_id)
            tool_name = tool_call.name if tool_call else None
            tool_tokens = estimate_message_tokens(message)
            largest_tool_tokens = max(largest_tool_tokens, tool_tokens)
            tool_message_specs.append((idx, tool_name))

    oversized_tool = (
        bool(tool_message_specs)
        and prompt_budget > 0
        and largest_tool_tokens
        >= max(int(prompt_budget * oversized_tool_ratio), oversized_tool_min_tokens)
    )
    if usage_ratio_before < soft_ratio and not oversized_tool:
        return CompactionOutcome(
            messages=messages,
            estimated_before=estimated_before,
            estimated_after=estimated_before,
            usage_ratio_before=usage_ratio_before,
            changed=False,
            largest_tool_tokens=largest_tool_tokens,
        )

    mode = (
        "aggressive"
        if usage_ratio_before >= aggressive_ratio
        or largest_tool_tokens
        >= max(int(prompt_budget * max(oversized_tool_ratio * 1.6, 0.3)), oversized_tool_min_tokens * 2)
        else "moderate"
    )

    new_messages = list(messages)
    changed = False
    compacted_tool_messages = 0
    for idx, tool_name in tool_message_specs:
        message = new_messages[idx]
        compacted = compact_tool_message(message, tool_name=tool_name, mode=mode)
        if compacted is not message:
            new_messages[idx] = compacted
            changed = True
            compacted_tool_messages += 1

    estimated_after = estimate_messages_tokens(new_messages, tools=tools)
    return CompactionOutcome(
        messages=new_messages if changed else messages,
        estimated_before=estimated_before,
        estimated_after=estimated_after,
        usage_ratio_before=usage_ratio_before,
        changed=changed,
        mode=mode if changed else None,
        compacted_tool_messages=compacted_tool_messages,
        largest_tool_tokens=largest_tool_tokens,
    )


def compact_tool_message(
    message: Message,
    *,
    tool_name: str | None,
    mode: str,
) -> Message:
    if message.role != Role.TOOL or not message.tool_result:
        return message

    compacted_result = compact_tool_result_for_prompt(
        tool_name=tool_name,
        result=message.tool_result,
        mode=mode,
    )
    if compacted_result is message.tool_result:
        return message
    return Message(
        role=message.role,
        content=message.content,
        tool_calls=message.tool_calls,
        tool_result=compacted_result,
        name=message.name,
    )


def compact_tool_result_for_prompt(
    *,
    tool_name: str | None,
    result: ToolResult,
    mode: str,
) -> ToolResult:
    if result.status != "success" or not isinstance(result.data, dict) or not tool_name:
        return result

    if tool_name == "web_search":
        compacted_data = _compact_web_search_data(result.data, mode=mode)
    elif tool_name == "xiaohongshu_search":
        compacted_data = _compact_xiaohongshu_data(result.data, mode=mode)
    else:
        return result

    if compacted_data == result.data:
        return result

    return ToolResult(
        tool_call_id=result.tool_call_id,
        status=result.status,
        data=compacted_data,
        metadata=result.metadata,
        error=result.error,
        error_code=result.error_code,
        suggestion=result.suggestion,
    )


def _compact_web_search_data(data: dict[str, Any], *, mode: str) -> dict[str, Any]:
    answer_limit = 600 if mode == "moderate" else 400
    snippet_limit = 300 if mode == "moderate" else 200
    result_limit = 8 if mode == "moderate" else 5

    compact = dict(data)
    if "answer" in compact:
        compact["answer"] = _truncate_text(compact["answer"], answer_limit)

    results = compact.get("results")
    if not isinstance(results, list):
        return compact

    trimmed_results: list[Any] = []
    for item in results[:result_limit]:
        if not isinstance(item, dict):
            trimmed_results.append(item)
            continue
        trimmed_results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": _truncate_text(item.get("content", ""), snippet_limit),
                "score": item.get("score"),
            }
        )
    compact["results"] = trimmed_results
    omitted = len(results) - len(trimmed_results)
    if omitted > 0:
        compact["results_omitted_count"] = omitted
    return compact


def _compact_xiaohongshu_data(data: dict[str, Any], *, mode: str) -> dict[str, Any]:
    compact = dict(data)
    operation = data.get("operation")

    if operation == "search_notes":
        item_limit = 12 if mode == "moderate" else 8
        items = compact.get("items")
        if isinstance(items, list):
            compact["items"] = [
                _compact_xhs_search_item(item)
                for item in items[:item_limit]
            ]
            omitted = len(items) - len(compact["items"])
            if omitted > 0:
                compact["items_omitted_count"] = omitted
        return compact

    if operation == "read_note":
        desc_limit = 400 if mode == "moderate" else 300
        note = compact.get("note")
        if isinstance(note, dict):
            compact["note"] = {
                "note_id": note.get("note_id", ""),
                "title": note.get("title", ""),
                "desc": _truncate_text(note.get("desc", ""), desc_limit),
                "liked_count": note.get("liked_count", ""),
                "collected_count": note.get("collected_count", ""),
                "comment_count": note.get("comment_count", ""),
                "tags": note.get("tags", []),
                "note_type": note.get("note_type", ""),
                "url": note.get("url", ""),
            }
        return compact

    if operation == "get_comments":
        comment_limit = 12 if mode == "moderate" else 8
        content_limit = 260 if mode == "moderate" else 200
        comments = compact.get("comments")
        if isinstance(comments, list):
            compact["comments"] = [
                _compact_xhs_comment(comment, content_limit=content_limit)
                for comment in comments[:comment_limit]
            ]
            omitted = len(comments) - len(compact["comments"])
            if omitted > 0:
                compact["comments_omitted_count"] = omitted
        return compact

    return compact


def _compact_xhs_search_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    return {
        "note_id": item.get("note_id", ""),
        "title": item.get("title", ""),
        "liked_count": item.get("liked_count", ""),
        "note_type": item.get("note_type", ""),
        "url": _strip_url_query(item.get("url", "")),
    }


def _compact_xhs_comment(comment: Any, *, content_limit: int) -> Any:
    if not isinstance(comment, dict):
        return comment
    return {
        "nickname": comment.get("nickname", ""),
        "content": _truncate_text(comment.get("content", ""), content_limit),
        "like_count": comment.get("like_count", ""),
    }


def _tool_call_parts(tool_calls: list[ToolCall]) -> list[str]:
    parts: list[str] = []
    for tool_call in tool_calls:
        parts.append(tool_call.id)
        parts.append(tool_call.name)
        parts.append(_safe_dump(tool_call.arguments))
    return parts


def _safe_dump(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return repr(value)


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 3)


def _truncate_text(value: Any, limit: int) -> Any:
    if not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def _strip_url_query(url: str) -> str:
    if not isinstance(url, str) or not url:
        return url
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
