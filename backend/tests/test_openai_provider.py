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


def test_convert_tool_error_message_includes_repair_fields(provider):
    from agent.types import ToolResult

    msg = Message(
        role=Role.TOOL,
        tool_result=ToolResult(
            tool_call_id="tc_1",
            status="error",
            error="POI '淄博市博物馆' 重复出现在 plans[0].days[1].candidate_pois[0] 和 plans[0].days[2].locked_pois[0]",
            error_code="INVALID_VALUE",
            suggestion="请把 '淄博市博物馆' 只保留在其中一天",
        ),
    )
    converted = provider._convert_messages([msg])
    assert json.loads(converted[0]["content"]) == {
        "status": "error",
        "data": None,
        "error": "POI '淄博市博物馆' 重复出现在 plans[0].days[1].candidate_pois[0] 和 plans[0].days[2].locked_pois[0]",
        "error_code": "INVALID_VALUE",
        "suggestion": "请把 '淄博市博物馆' 只保留在其中一天",
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

        chunks = [
            chunk
            async for chunk in provider.chat([Message(role=Role.USER, content="hi")])
        ]

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

        chunks = [
            chunk
            async for chunk in provider.chat(
                [Message(role=Role.USER, content="hi")], tools=[]
            )
        ]

    assert [chunk.type for chunk in chunks] == [
        ChunkType.TOOL_CALL_START,
        ChunkType.DONE,
    ]
    assert chunks[0].tool_call == ToolCall(
        id="call_1",
        name="search_poi",
        arguments={"query": "东京塔"},
    )


async def test_chat_passes_tool_choice_when_tools_are_present(provider):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=None))
        ]
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
                tool_choice={
                    "type": "function",
                    "function": {"name": "search_flights"},
                },
                stream=False,
            )
        ]

    assert chunks[-1].type == ChunkType.DONE
    assert instance.chat.completions.create.await_args.kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "search_flights"},
    }


# ── Error classification & retry tests ──


from llm.errors import LLMError, LLMErrorCode


def test_classify_error_api_status_503(provider):
    import openai

    exc = openai.APIStatusError(
        message="overloaded",
        response=MagicMock(status_code=503),
        body=None,
    )
    result = provider._classify_error(exc)
    assert isinstance(result, LLMError)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is True


def test_classify_error_api_status_429(provider):
    import openai

    exc = openai.APIStatusError(
        message="rate limited",
        response=MagicMock(status_code=429),
        body=None,
    )
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.RATE_LIMITED


def test_classify_error_connection_error(provider):
    import openai

    exc = openai.APIConnectionError(request=MagicMock())
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is True


def test_classify_error_unknown_exception(provider):
    exc = RuntimeError("something weird")
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is False


def test_provider_name(provider):
    assert provider.provider_name == "openai"


@pytest.mark.asyncio
async def test_chat_raises_llm_error_on_api_failure(provider):
    import openai

    with (
        patch("llm.openai_provider.AsyncOpenAI") as MockClient,
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(
            side_effect=openai.APIStatusError(
                message="overloaded",
                response=MagicMock(status_code=503),
                body=None,
            )
        )
        provider.client = instance
        with pytest.raises(LLMError) as exc_info:
            async for _ in provider.chat(
                [Message(role=Role.USER, content="hi")],
                stream=False,
            ):
                pass
        assert exc_info.value.code == LLMErrorCode.TRANSIENT
        # 503 is retryable, should retry 2 times (3 total calls)
        assert instance.chat.completions.create.await_count == 3
        assert mock_sleep.await_count == 2


def test_classify_error_opaque_api_error_busy(provider):
    import openai

    exc = openai.APIError(
        message="Xunfei request failed with code: 10012, "
        "message: EngineInternalError:The system is busy",
        request=MagicMock(),
        body=None,
    )
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is True


def test_classify_error_api_status_error_unchanged(provider):
    """强类型分支行为回归：APIStatusError 仍走 classify_by_http_status。"""
    import openai

    exc = openai.APIStatusError(
        message="bad request",
        response=MagicMock(status_code=400),
        body=None,
    )
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.BAD_REQUEST
    assert result.retryable is False
