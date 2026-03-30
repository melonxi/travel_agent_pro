# backend/tests/test_telemetry_agent_loop.py
import opentelemetry.trace as _trace_module
import pytest
from unittest.mock import AsyncMock, MagicMock
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role
from llm.types import ChunkType, LLMChunk
from tools.engine import ToolEngine


def _reset_tracer_provider():
    """重置 OTel 全局 TracerProvider，允许在测试间重新设置。"""
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
    _reset_tracer_provider()


async def test_agent_loop_creates_span(otel_exporter):
    """AgentLoop.run() 应创建 agent_loop.run span。"""
    async def fake_chat(messages, tools=None, stream=True):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="hello")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    engine = ToolEngine()
    hooks = HookManager()

    loop = AgentLoop(llm=llm, tool_engine=engine, hooks=hooks)
    messages = [Message(role=Role.USER, content="hi")]

    chunks = []
    async for chunk in loop.run(messages, phase=1):
        chunks.append(chunk)

    spans = otel_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert any("agent_loop.run" in n for n in span_names)


async def test_agent_loop_run_span_has_phase_attribute(otel_exporter):
    """agent_loop.run span 应设置 agent.phase 属性。"""
    async def fake_chat(messages, tools=None, stream=True):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="ok")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    engine = ToolEngine()
    hooks = HookManager()

    loop = AgentLoop(llm=llm, tool_engine=engine, hooks=hooks)
    messages = [Message(role=Role.USER, content="test")]

    async for _ in loop.run(messages, phase=3):
        pass

    spans = otel_exporter.get_finished_spans()
    run_spans = [s for s in spans if s.name == "agent_loop.run"]
    assert len(run_spans) == 1
    assert run_spans[0].attributes.get("agent.phase") == 3


async def test_agent_loop_creates_iteration_span(otel_exporter):
    """AgentLoop.run() 应为每次迭代创建 agent_loop.iteration span。"""
    async def fake_chat(messages, tools=None, stream=True):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    engine = ToolEngine()
    hooks = HookManager()

    loop = AgentLoop(llm=llm, tool_engine=engine, hooks=hooks)
    messages = [Message(role=Role.USER, content="hi")]

    async for _ in loop.run(messages, phase=1):
        pass

    spans = otel_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert any("agent_loop.iteration" in n for n in span_names)


async def test_agent_loop_iteration_span_has_iteration_attribute(otel_exporter):
    """agent_loop.iteration span 应设置 agent.iteration 属性为 0（首次迭代）。"""
    async def fake_chat(messages, tools=None, stream=True):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    engine = ToolEngine()
    hooks = HookManager()

    loop = AgentLoop(llm=llm, tool_engine=engine, hooks=hooks)
    messages = [Message(role=Role.USER, content="hi")]

    async for _ in loop.run(messages, phase=2):
        pass

    spans = otel_exporter.get_finished_spans()
    iter_spans = [s for s in spans if s.name == "agent_loop.iteration"]
    assert len(iter_spans) >= 1
    assert iter_spans[0].attributes.get("agent.iteration") == 0
