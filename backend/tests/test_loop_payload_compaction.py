# backend/tests/test_loop_payload_compaction.py
from __future__ import annotations

from agent.compaction import (
    compact_messages_for_prompt,
    compute_prompt_budget,
    estimate_message_tokens,
    estimate_messages_tokens,
)
from agent.types import Message, Role, ToolCall, ToolResult


def test_compute_prompt_budget_reserves_output_and_safety_margin():
    assert compute_prompt_budget(128000, 4096) == 121904
    assert compute_prompt_budget(2000, 1500) == 1024


def test_estimate_message_tokens_counts_tool_calls_and_tool_results():
    message = Message(
        role=Role.ASSISTANT,
        content="先查资料",
        tool_calls=[
            ToolCall(
                id="tc1",
                name="web_search",
                arguments={"query": "京都 樱花 2026", "max_results": 5},
            )
        ],
    )
    tool_result_message = Message(
        role=Role.TOOL,
        tool_result=ToolResult(
            tool_call_id="tc1",
            status="success",
            data={"answer": "a" * 900},
        ),
    )

    assert estimate_message_tokens(message) > 0
    assert estimate_message_tokens(tool_result_message) >= 300


def test_estimate_messages_tokens_includes_tool_schemas():
    messages = [Message(role=Role.USER, content="帮我查京都樱花")]
    tools = [
        {
            "name": "web_search",
            "description": "d" * 900,
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "q" * 300}},
            },
        }
    ]

    base = estimate_messages_tokens(messages)
    with_tools = estimate_messages_tokens(messages, tools=tools)

    assert with_tools > base


def test_compact_messages_for_prompt_keeps_full_payload_under_soft_ratio():
    long_snippet = "s" * 500
    messages = [
        Message(
            role=Role.ASSISTANT,
            tool_calls=[ToolCall(id="tc1", name="web_search", arguments={"query": "京都"})],
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(
                tool_call_id="tc1",
                status="success",
                data={
                    "answer": "简短答案",
                    "results": [{"title": "t1", "url": "https://a", "content": long_snippet}],
                },
            ),
        ),
    ]

    outcome = compact_messages_for_prompt(messages, prompt_budget=10000, tools=[])

    assert not outcome.changed
    assert outcome.messages is messages
    assert messages[1].tool_result.data["results"][0]["content"] == long_snippet


def test_compact_messages_for_prompt_trims_web_search_and_caps_results():
    long_answer = "答" * 1200
    long_snippet = "s" * 1200
    messages = [
        Message(
            role=Role.ASSISTANT,
            tool_calls=[ToolCall(id="tc1", name="web_search", arguments={"query": "京都"})],
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(
                tool_call_id="tc1",
                status="success",
                data={
                    "answer": long_answer,
                    "results": [
                        {
                            "title": f"title-{i}",
                            "url": f"https://example.com/{i}",
                            "content": long_snippet,
                            "score": 0.9,
                        }
                        for i in range(10)
                    ],
                },
            ),
        ),
    ]

    outcome = compact_messages_for_prompt(messages, prompt_budget=1000, tools=[])

    assert outcome.changed
    assert outcome.mode in {"moderate", "aggressive"}
    compacted = outcome.messages[1].tool_result.data
    assert compacted["answer"].endswith("…")
    assert compacted["results"][0]["url"] == "https://example.com/0"
    assert len(compacted["results"]) <= 8
    assert compacted["results_omitted_count"] >= 2
    assert messages[1].tool_result.data["answer"] == long_answer
    assert len(messages[1].tool_result.data["results"]) == 10


def test_compact_messages_for_prompt_trims_xiaohongshu_search_notes_handles():
    items = [
        {
            "note_id": f"note_{i}",
            "title": f"title-{i}",
            "liked_count": str(i),
            "note_type": "image",
            "url": f"https://www.xiaohongshu.com/explore/{i}?xsec_token=token_{i}",
            "extra": "ignored",
        }
        for i in range(15)
    ]
    messages = [
        Message(
            role=Role.ASSISTANT,
            tool_calls=[
                ToolCall(
                    id="tc2",
                    name="xiaohongshu_search",
                    arguments={"operation": "search_notes", "keyword": "京都 旅行"},
                )
            ],
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(
                tool_call_id="tc2",
                status="success",
                data={
                    "operation": "search_notes",
                    "keyword": "京都 旅行",
                    "has_more": True,
                    "items": items,
                },
            ),
        ),
    ]

    outcome = compact_messages_for_prompt(messages, prompt_budget=500, tools=[])

    assert outcome.changed
    compacted = outcome.messages[1].tool_result.data
    assert len(compacted["items"]) <= 8
    assert compacted["items"][0]["url"] == "https://www.xiaohongshu.com/explore/0"
    assert "extra" not in compacted["items"][0]
    assert compacted["items_omitted_count"] >= 7


def test_compact_messages_for_prompt_trims_xiaohongshu_note_and_comments():
    long_desc = "d" * 900
    long_comment = "c" * 700
    messages = [
        Message(
            role=Role.ASSISTANT,
            tool_calls=[
                ToolCall(
                    id="tc3",
                    name="xiaohongshu_search",
                    arguments={"operation": "read_note", "note_ref": "note_1"},
                )
            ],
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(
                tool_call_id="tc3",
                status="success",
                data={
                    "operation": "read_note",
                    "note": {
                        "note_id": "note_1",
                        "title": "京都慢旅行",
                        "desc": long_desc,
                        "url": "https://www.xiaohongshu.com/explore/note_1",
                        "tags": ["京都", "赏樱"],
                    },
                },
            ),
        ),
        Message(
            role=Role.ASSISTANT,
            tool_calls=[
                ToolCall(
                    id="tc4",
                    name="xiaohongshu_search",
                    arguments={"operation": "get_comments", "note_ref": "note_1"},
                )
            ],
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(
                tool_call_id="tc4",
                status="success",
                data={
                    "operation": "get_comments",
                    "comments": [
                        {"nickname": f"user-{i}", "content": long_comment, "like_count": str(i)}
                        for i in range(14)
                    ],
                },
            ),
        ),
    ]

    outcome = compact_messages_for_prompt(messages, prompt_budget=800, tools=[])

    assert outcome.changed
    note = outcome.messages[1].tool_result.data["note"]
    comments = outcome.messages[3].tool_result.data
    assert note["desc"].endswith("…")
    assert note["url"] == "https://www.xiaohongshu.com/explore/note_1"
    assert len(comments["comments"]) <= 12
    assert comments["comments"][0]["content"].endswith("…")
    assert comments["comments_omitted_count"] >= 2


def test_compact_messages_for_prompt_does_not_overcompress_medium_xhs_note_when_budget_has_room():
    long_desc = "京都攻略\n" * 110
    messages = [
        Message(
            role=Role.ASSISTANT,
            tool_calls=[
                ToolCall(
                    id="tc5",
                    name="xiaohongshu_search",
                    arguments={"operation": "read_note", "note_ref": "note_1"},
                )
            ],
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(
                tool_call_id="tc5",
                status="success",
                data={
                    "operation": "read_note",
                    "note": {
                        "note_id": "note_1",
                        "title": "京都攻略",
                        "desc": long_desc,
                        "url": "https://www.xiaohongshu.com/explore/note_1",
                        "tags": ["京都"],
                    },
                },
            ),
        ),
    ]

    outcome = compact_messages_for_prompt(messages, prompt_budget=1500, tools=[])

    assert not outcome.changed
    assert outcome.messages is messages
    assert outcome.messages[1].tool_result.data["note"]["desc"] == long_desc
