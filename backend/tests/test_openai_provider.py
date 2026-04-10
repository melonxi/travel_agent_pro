# backend/tests/test_openai_provider.py
import json
from types import SimpleNamespace
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


def test_convert_tool_result_message_ignores_metadata(provider):
    from agent.types import ToolResult

    msg = Message(
        role=Role.TOOL,
        tool_result=ToolResult(
            tool_call_id="tc_1",
            status="success",
            data={"flights": []},
            metadata={"source": "amadeus"},
        ),
    )
    converted = provider._convert_messages([msg])
    assert json.loads(converted[0]["content"]) == {
        "status": "success",
        "data": {"flights": []},
    }


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


class _AsyncChunkStream:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _stream_chunk(*, delta=None, finish_reason=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)]
    )


def _delta(*, content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


async def test_streaming_chat_emits_done_when_finish_reason_arrives_without_delta(
    provider,
):
    stream = _AsyncChunkStream(
        [
            _stream_chunk(delta=_delta(content="hello")),
            _stream_chunk(delta=None, finish_reason="stop"),
        ]
    )

    with patch("llm.openai_provider.AsyncOpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(return_value=stream)
        provider.client = instance

        chunks = [chunk async for chunk in provider.chat([Message(role=Role.USER, content="hi")])]

    assert [chunk.type for chunk in chunks] == [ChunkType.TEXT_DELTA, ChunkType.DONE]
    assert chunks[0].content == "hello"


async def test_streaming_chat_flushes_tool_calls_when_finish_reason_chunk_has_no_delta(
    provider,
):
    tool_call_delta = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(
            name="search_poi",
            arguments='{"query":"东京塔"}',
        ),
    )
    stream = _AsyncChunkStream(
        [
            _stream_chunk(delta=_delta(tool_calls=[tool_call_delta])),
            _stream_chunk(delta=None, finish_reason="tool_calls"),
        ]
    )

    with patch("llm.openai_provider.AsyncOpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(return_value=stream)
        provider.client = instance

        chunks = [chunk async for chunk in provider.chat([Message(role=Role.USER, content="hi")], tools=[])]

    assert [chunk.type for chunk in chunks] == [ChunkType.TOOL_CALL_START, ChunkType.DONE]
    assert chunks[0].tool_call == ToolCall(
        id="call_1",
        name="search_poi",
        arguments={"query": "东京塔"},
    )


async def test_chat_passes_tool_choice_when_tools_are_present(provider):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=None))]
    )

    with patch("llm.openai_provider.AsyncOpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(return_value=response)
        provider.client = instance

        chunks = [
            chunk
            async for chunk in provider.chat(
                [Message(role=Role.USER, content="hi")],
                tools=[
                    {
                        "name": "search_flights",
                        "description": "Search flights",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
                tool_choice={"type": "function", "function": {"name": "search_flights"}},
                stream=False,
            )
        ]

    assert chunks[-1].type == ChunkType.DONE
    assert instance.chat.completions.create.await_args.kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "search_flights"},
    }
