# backend/tests/test_anthropic_provider.py
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.types import Message, Role, ToolResult
from llm.types import ChunkType
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


def test_convert_tool_result_ignores_metadata(provider):
    messages = [
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(
                tool_call_id="tc_1",
                status="success",
                data={"result": 1},
                metadata={"source": "xiaohongshu_cli"},
            ),
        )
    ]
    _, converted = provider._split_system_and_convert(messages)
    payload = json.loads(converted[0]["content"][0]["content"])
    assert payload == {"status": "success", "data": {"result": 1}}


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


@pytest.mark.asyncio
async def test_streaming_with_tools_falls_back_to_nonstream_create(provider):
    mock_response = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "先记录信息。"
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.id = "tool_1"
    tool_block.name = "update_plan_state"
    tool_block.input = {"field": "destination", "value": "东京"}
    mock_response.content = [text_block, tool_block]

    with patch("llm.anthropic_provider.AsyncAnthropic") as MockClient:
        instance = MockClient.return_value
        instance.messages.create = AsyncMock(return_value=mock_response)
        instance.messages.stream = MagicMock()

        test_provider = AnthropicProvider(
            model="claude-sonnet-4-20250514",
            temperature=0.7,
            max_tokens=4096,
        )
        chunks = [
            chunk
            async for chunk in test_provider.chat(
                [Message(role=Role.USER, content="去东京")],
                tools=[
                    {
                        "name": "update_plan_state",
                        "description": "state",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
                stream=True,
            )
        ]

    instance.messages.create.assert_awaited_once()
    instance.messages.stream.assert_not_called()
    assert chunks[0].type == ChunkType.TEXT_DELTA
    assert chunks[0].content == "先记录信息。"
    assert chunks[1].type == ChunkType.TOOL_CALL_START
    assert chunks[1].tool_call is not None
    assert chunks[1].tool_call.name == "update_plan_state"
    assert chunks[-1].type == ChunkType.DONE


@pytest.mark.asyncio
async def test_chat_converts_tool_choice_when_tools_are_present(provider):
    mock_response = MagicMock()
    mock_response.content = []

    with patch("llm.anthropic_provider.AsyncAnthropic") as MockClient:
        instance = MockClient.return_value
        instance.messages.create = AsyncMock(return_value=mock_response)
        instance.messages.stream = MagicMock()
        test_provider = AnthropicProvider(
            model="claude-sonnet-4-20250514",
            temperature=0.7,
            max_tokens=4096,
        )

        chunks = [
            chunk
            async for chunk in test_provider.chat(
                [Message(role=Role.USER, content="hi")],
                tools=[
                    {
                        "name": "search_flights",
                        "description": "Search flights",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
                tool_choice="auto",
                stream=True,
            )
        ]

    assert chunks[-1].type == ChunkType.DONE
    assert instance.messages.create.await_args.kwargs["tool_choice"] == {"type": "auto"}
