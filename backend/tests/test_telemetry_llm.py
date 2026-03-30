# backend/tests/test_telemetry_llm.py
import opentelemetry.trace as _trace_module
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent.types import Message, Role
from telemetry.attributes import LLM_PROVIDER, LLM_MODEL


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
