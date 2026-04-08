# backend/tests/test_loop_payload_compaction.py
"""Unit tests for the payload compaction utilities added to agent.loop.

These helpers trim rich search tool payloads before they are persisted back
into the messages log, in order to keep the LLM context window under
control. The streamed tool_result yielded to the UI must keep the full
payload — only the copy that lands in ``messages`` is trimmed.
"""
from __future__ import annotations

from agent.loop import (
    _WEB_SEARCH_ANSWER_MAX,
    _WEB_SEARCH_SNIPPET_MAX,
    _XHS_TEXT_MAX,
    _compact_web_search_data,
    _compact_xiaohongshu_data,
    _truncate_text,
    compact_tool_result_for_messages,
)
from agent.types import ToolResult


def test_truncate_text_under_limit_returns_unchanged():
    assert _truncate_text("short", 10) == "short"


def test_truncate_text_over_limit_adds_ellipsis():
    text = "a" * 50
    out = _truncate_text(text, 20)
    assert out.endswith("…")
    # 20 chars of content + ellipsis
    assert len(out) == 21


def test_truncate_text_non_string_passthrough():
    assert _truncate_text(None, 5) is None
    assert _truncate_text(123, 5) == 123
    payload = {"k": "v"}
    assert _truncate_text(payload, 5) is payload


def test_compact_web_search_trims_answer_and_snippets():
    long_answer = "答" * (_WEB_SEARCH_ANSWER_MAX + 100)
    long_snippet = "s" * (_WEB_SEARCH_SNIPPET_MAX + 100)
    data = {
        "answer": long_answer,
        "results": [
            {"title": "t1", "content": long_snippet, "url": "https://a"},
            {"title": "t2", "content": "short", "url": "https://b"},
            "not-a-dict",
        ],
    }

    compact = _compact_web_search_data(data)

    assert compact is not data  # returns a new dict
    assert compact["answer"].endswith("…")
    assert len(compact["answer"]) <= _WEB_SEARCH_ANSWER_MAX + 1
    assert compact["results"][0]["content"].endswith("…")
    assert (
        len(compact["results"][0]["content"]) <= _WEB_SEARCH_SNIPPET_MAX + 1
    )
    # URL / title are preserved verbatim.
    assert compact["results"][0]["title"] == "t1"
    assert compact["results"][0]["url"] == "https://a"
    # Short snippets are left alone.
    assert compact["results"][1]["content"] == "short"
    # Non-dict list items pass through unchanged.
    assert compact["results"][2] == "not-a-dict"
    # Original payload is not mutated.
    assert data["answer"] == long_answer
    assert data["results"][0]["content"] == long_snippet


def test_compact_web_search_non_dict_passthrough():
    assert _compact_web_search_data("oops") == "oops"
    assert _compact_web_search_data(None) is None


def test_compact_xiaohongshu_read_note_trims_desc():
    long_desc = "d" * (_XHS_TEXT_MAX + 50)
    data = {
        "operation": "read_note",
        "note": {"id": "n1", "desc": long_desc, "title": "t"},
    }

    compact = _compact_xiaohongshu_data(data)

    assert compact["note"]["desc"].endswith("…")
    assert len(compact["note"]["desc"]) <= _XHS_TEXT_MAX + 1
    assert compact["note"]["id"] == "n1"
    assert compact["note"]["title"] == "t"
    # Original untouched.
    assert data["note"]["desc"] == long_desc


def test_compact_xiaohongshu_get_comments_trims_each_comment():
    long_comment = "c" * (_XHS_TEXT_MAX + 30)
    data = {
        "operation": "get_comments",
        "comments": [
            {"id": "c1", "content": long_comment},
            {"id": "c2", "content": "short"},
            "not-a-dict",
        ],
    }

    compact = _compact_xiaohongshu_data(data)

    assert compact["comments"][0]["content"].endswith("…")
    assert len(compact["comments"][0]["content"]) <= _XHS_TEXT_MAX + 1
    assert compact["comments"][1]["content"] == "short"
    assert compact["comments"][2] == "not-a-dict"
    # Original untouched.
    assert data["comments"][0]["content"] == long_comment


def test_compact_xiaohongshu_unknown_operation_passthrough():
    data = {"operation": "search", "items": [1, 2, 3]}
    compact = _compact_xiaohongshu_data(data)
    # New dict, but the underlying data is preserved.
    assert compact == data


def test_compact_tool_result_web_search_trims_payload():
    long_snippet = "x" * (_WEB_SEARCH_SNIPPET_MAX + 50)
    result = ToolResult(
        tool_call_id="tc1",
        status="success",
        data={
            "results": [{"content": long_snippet, "title": "t"}],
        },
        metadata={"source": "test"},
    )

    compacted = compact_tool_result_for_messages("web_search", result)

    assert compacted is not result
    assert compacted.tool_call_id == "tc1"
    assert compacted.status == "success"
    assert compacted.metadata == {"source": "test"}
    assert compacted.data["results"][0]["content"].endswith("…")
    # Original result not mutated — caller may still emit full payload to UI.
    assert result.data["results"][0]["content"] == long_snippet


def test_compact_tool_result_xiaohongshu_trims_payload():
    long_desc = "y" * (_XHS_TEXT_MAX + 50)
    result = ToolResult(
        tool_call_id="tc2",
        status="success",
        data={"operation": "read_note", "note": {"desc": long_desc}},
    )

    compacted = compact_tool_result_for_messages("xiaohongshu_search", result)

    assert compacted is not result
    assert compacted.data["note"]["desc"].endswith("…")
    assert result.data["note"]["desc"] == long_desc


def test_compact_tool_result_non_target_tool_passthrough():
    result = ToolResult(
        tool_call_id="tc3",
        status="success",
        data={"whatever": "payload"},
    )
    assert compact_tool_result_for_messages("update_plan_state", result) is result
    assert compact_tool_result_for_messages("check_weather", result) is result


def test_compact_tool_result_error_status_passthrough():
    result = ToolResult(
        tool_call_id="tc4",
        status="error",
        data=None,
        error="boom",
        error_code="BOOM",
    )
    assert compact_tool_result_for_messages("web_search", result) is result


def test_compact_tool_result_non_dict_data_passthrough():
    result = ToolResult(
        tool_call_id="tc5",
        status="success",
        data="raw text",
    )
    assert compact_tool_result_for_messages("web_search", result) is result


def test_compact_tool_result_short_payload_returns_same_instance():
    """When nothing actually needs trimming, the helper should return the
    original result object — avoiding pointless allocations on the hot path.
    """
    result = ToolResult(
        tool_call_id="tc6",
        status="success",
        data={
            "answer": "short answer",
            "results": [{"content": "short", "title": "t"}],
        },
    )
    compacted = compact_tool_result_for_messages("web_search", result)
    # _compact_web_search_data always returns a new dict, so the helper will
    # wrap it in a new ToolResult. That's fine — but the trimmed contents
    # must equal the originals.
    assert compacted.data["answer"] == "short answer"
    assert compacted.data["results"][0]["content"] == "short"
