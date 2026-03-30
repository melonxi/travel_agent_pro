from telemetry.attributes import (
    truncate,
    EVENT_TOOL_INPUT,
    EVENT_TOOL_OUTPUT,
    EVENT_LLM_REQUEST,
    EVENT_LLM_RESPONSE,
    EVENT_PHASE_PLAN_SNAPSHOT,
    EVENT_CONTEXT_COMPRESSION,
)


def test_truncate_short_string():
    assert truncate("hello") == "hello"


def test_truncate_exact_boundary():
    s = "a" * 512
    assert truncate(s) == s


def test_truncate_long_string():
    s = "a" * 600
    result = truncate(s)
    assert len(result) == 512 + len("...(truncated)")
    assert result.endswith("...(truncated)")


def test_truncate_custom_max():
    s = "a" * 300
    result = truncate(s, max_len=100)
    assert result == "a" * 100 + "...(truncated)"


def test_event_constants_exist():
    assert EVENT_TOOL_INPUT == "tool.input"
    assert EVENT_TOOL_OUTPUT == "tool.output"
    assert EVENT_LLM_REQUEST == "llm.request"
    assert EVENT_LLM_RESPONSE == "llm.response"
    assert EVENT_PHASE_PLAN_SNAPSHOT == "phase.plan_snapshot"
    assert EVENT_CONTEXT_COMPRESSION == "context.compression"
