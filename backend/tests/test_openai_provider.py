# backend/tests/test_openai_provider.py
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.types import Message, Role, ToolCall
from llm.types import LLMChunk, ChunkType
from llm.openai_provider import OpenAIProvider


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return OpenAIProvider(model="gpt-4o", temperature=0.7, max_tokens=4096)


def test_convert_messages_splits_system(provider):
    messages = [
        Message(role=Role.SYSTEM, content="You are helpful"),
        Message(role=Role.USER, content="Hello"),
    ]
    converted = provider._convert_messages(messages)
    assert converted[0]["role"] == "system"
    assert converted[0]["content"] == "You are helpful"
    assert converted[1]["role"] == "user"


def test_convert_tool_result_message(provider):
    from agent.types import ToolResult

    msg = Message(
        role=Role.TOOL,
        tool_result=ToolResult(
            tool_call_id="tc_1", status="success", data={"flights": []}
        ),
    )
    converted = provider._convert_messages([msg])
    assert converted[0]["role"] == "tool"
    assert converted[0]["tool_call_id"] == "tc_1"


def test_convert_tools(provider):
    tool_defs = [
        {
            "name": "search_flights",
            "description": "Search flights",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "IATA code"},
                },
                "required": ["origin"],
            },
        }
    ]
    converted = provider._convert_tools(tool_defs)
    assert converted[0]["type"] == "function"
    assert converted[0]["function"]["name"] == "search_flights"
