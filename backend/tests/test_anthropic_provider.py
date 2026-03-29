# backend/tests/test_anthropic_provider.py
import pytest

from agent.types import Message, Role, ToolResult
from llm.anthropic_provider import AnthropicProvider


@pytest.fixture
def provider():
    return AnthropicProvider(
        model="claude-sonnet-4-20250514", temperature=0.7, max_tokens=4096
    )


def test_split_system(provider):
    messages = [
        Message(role=Role.SYSTEM, content="You are helpful"),
        Message(role=Role.USER, content="Hello"),
    ]
    system, converted = provider._split_system_and_convert(messages)
    assert system == "You are helpful"
    assert len(converted) == 1
    assert converted[0]["role"] == "user"


def test_convert_tool_result(provider):
    messages = [
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(
                tool_call_id="tc_1", status="success", data={"result": 1}
            ),
        )
    ]
    _, converted = provider._split_system_and_convert(messages)
    assert converted[0]["role"] == "user"
    assert converted[0]["content"][0]["type"] == "tool_result"
    assert converted[0]["content"][0]["tool_use_id"] == "tc_1"


def test_convert_tools(provider):
    tool_defs = [
        {
            "name": "search_flights",
            "description": "Search flights",
            "parameters": {
                "type": "object",
                "properties": {"origin": {"type": "string"}},
                "required": ["origin"],
            },
        }
    ]
    converted = provider._convert_tools(tool_defs)
    assert converted[0]["name"] == "search_flights"
    assert converted[0]["input_schema"]["type"] == "object"
