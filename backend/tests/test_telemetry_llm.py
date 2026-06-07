# backend/tests/test_telemetry_llm.py
import asyncio
import opentelemetry.trace as _trace_module
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent.types import Message, Role
from llm.types import ChunkType
from telemetry.attributes import LLM_PROVIDER, LLM_MODEL


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


def _openai_stream_chunk(*, content: str | None = None, finish_reason=None):
    delta = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def _openai_usage_chunk(
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    prompt_cache_hit_tokens: int | None = None,
    prompt_cache_miss_tokens: int | None = None,
):
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    if prompt_cache_hit_tokens is not None:
        usage.prompt_cache_hit_tokens = prompt_cache_hit_tokens
    if prompt_cache_miss_tokens is not None:
        usage.prompt_cache_miss_tokens = prompt_cache_miss_tokens
    return SimpleNamespace(choices=[], usage=usage)


def _reset_tracer_provider():
    _trace_module._TRACER_PROVIDER_SET_ONCE._done = False
    _trace_module._TRACER_PROVIDER = None


@pytest.fixture(autouse=True)
def otel_exporter():
    _reset_tracer_provider()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    provider.shutdown()


async def test_openai_chat_creates_span(otel_exporter):
    """OpenAI provider chat 应创建 llm.chat span。"""
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "hi"
    mock_choice.message.tool_calls = None
    mock_response.choices = [mock_choice]

    with patch("llm.openai_provider.AsyncOpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(return_value=mock_response)

        from llm.openai_provider import OpenAIProvider

        provider = OpenAIProvider(model="gpt-4o")
        messages = [Message(role=Role.USER, content="hello")]

        chunks = []
        async for c in provider.chat(messages, stream=False):
            chunks.append(c)

    spans = otel_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "llm.chat" in span_names

    span = next(s for s in spans if s.name == "llm.chat")
    assert span.attributes[LLM_PROVIDER] == "openai"
    assert span.attributes[LLM_MODEL] == "gpt-4o"


from telemetry.attributes import EVENT_LLM_REQUEST, EVENT_LLM_RESPONSE


async def test_openai_chat_has_request_event(otel_exporter):
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "hello"
    mock_choice.message.tool_calls = None
    mock_response.choices = [mock_choice]

    with patch("llm.openai_provider.AsyncOpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(return_value=mock_response)

        from llm.openai_provider import OpenAIProvider

        provider = OpenAIProvider(model="gpt-4o")
        messages = [Message(role=Role.USER, content="hello")]

        async for _ in provider.chat(messages, stream=False):
            pass

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "llm.chat")
    events = span.events

    req_event = next(e for e in events if e.name == EVENT_LLM_REQUEST)
    assert req_event.attributes["message_count"] == 1
    assert req_event.attributes["has_tools"] is False

    resp_event = next(e for e in events if e.name == EVENT_LLM_RESPONSE)
    assert "text_preview" in resp_event.attributes


async def test_openai_chat_request_event_with_tools(otel_exporter):
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "ok"
    mock_choice.message.tool_calls = None
    mock_response.choices = [mock_choice]

    with patch("llm.openai_provider.AsyncOpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(return_value=mock_response)

        from llm.openai_provider import OpenAIProvider

        provider = OpenAIProvider(model="gpt-4o")
        messages = [Message(role=Role.USER, content="hello")]
        tools = [{"name": "search", "description": "search", "parameters": {}}]

        async for _ in provider.chat(messages, tools=tools, stream=False):
            pass

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "llm.chat")
    req_event = next(e for e in span.events if e.name == EVENT_LLM_REQUEST)
    assert req_event.attributes["has_tools"] is True


async def test_openai_stream_can_close_from_different_task_without_otel_context_error(
    otel_exporter,
):
    stream = _AsyncChunkStream(
        [
            _openai_stream_chunk(content="hello"),
            _openai_stream_chunk(finish_reason="stop"),
        ]
    )

    with patch("llm.openai_provider.AsyncOpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(return_value=stream)

        from llm.openai_provider import OpenAIProvider

        provider = OpenAIProvider(model="gpt-4o")
        generator = provider.chat([Message(role=Role.USER, content="hello")])

        first = await generator.__anext__()
        assert first.content == "hello"

        close_task = asyncio.create_task(generator.aclose())
        await close_task

    spans = otel_exporter.get_finished_spans()
    assert any(span.name == "llm.chat" for span in spans)


async def test_openai_stream_preserves_final_usage_after_finish_reason(otel_exporter):
    stream = _AsyncChunkStream(
        [
            _openai_stream_chunk(content="hello"),
            _openai_stream_chunk(finish_reason="stop"),
            _openai_usage_chunk(
                prompt_tokens=100,
                completion_tokens=5,
                prompt_cache_hit_tokens=80,
                prompt_cache_miss_tokens=20,
            ),
        ]
    )

    with patch("llm.openai_provider.AsyncOpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(return_value=stream)

        from llm.openai_provider import OpenAIProvider

        provider = OpenAIProvider(model="deepseek-v4-flash")
        chunks = [
            chunk
            async for chunk in provider.chat([Message(role=Role.USER, content="hello")])
        ]

    usage_chunk = next(chunk for chunk in chunks if chunk.type == ChunkType.USAGE)
    assert usage_chunk.usage_info == {
        "input_tokens": 100,
        "output_tokens": 5,
        "prompt_cache_hit_tokens": 80,
        "prompt_cache_miss_tokens": 20,
    }
    assert chunks[-1].type == ChunkType.DONE


async def test_openai_non_stream_usage_includes_deepseek_cache_tokens(otel_exporter):
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "hi"
    mock_choice.message.tool_calls = None
    mock_response.choices = [mock_choice]
    mock_response.usage = SimpleNamespace(
        prompt_tokens=120,
        completion_tokens=8,
        prompt_cache_hit_tokens=90,
        prompt_cache_miss_tokens=30,
    )

    with patch("llm.openai_provider.AsyncOpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(return_value=mock_response)

        from llm.openai_provider import OpenAIProvider

        provider = OpenAIProvider(model="deepseek-v4-flash")
        chunks = [
            chunk
            async for chunk in provider.chat(
                [Message(role=Role.USER, content="hello")], stream=False
            )
        ]

    usage_chunk = next(chunk for chunk in chunks if chunk.type == ChunkType.USAGE)
    assert usage_chunk.usage_info["prompt_cache_hit_tokens"] == 90
    assert usage_chunk.usage_info["prompt_cache_miss_tokens"] == 30


async def test_anthropic_chat_has_request_event(otel_exporter):
    mock_response = MagicMock()
    mock_block = MagicMock()
    mock_block.type = "text"
    mock_block.text = "bonjour"
    mock_response.content = [mock_block]

    with patch("llm.anthropic_provider.AsyncAnthropic") as MockClient:
        instance = MockClient.return_value
        instance.messages.create = AsyncMock(return_value=mock_response)

        from llm.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(model="claude-sonnet-4-20250514")
        messages = [Message(role=Role.USER, content="hello")]

        async for _ in provider.chat(messages, stream=False):
            pass

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "llm.chat")
    events = span.events

    req_event = next(e for e in events if e.name == EVENT_LLM_REQUEST)
    assert req_event.attributes["message_count"] == 1
    assert req_event.attributes["has_tools"] is False

    resp_event = next(e for e in events if e.name == EVENT_LLM_RESPONSE)
    assert resp_event.attributes["text_preview"] == "bonjour"
