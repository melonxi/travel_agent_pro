import opentelemetry.trace as _trace_module
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent.types import ToolCall
from tools.base import ToolDef, ToolError
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


async def test_tool_execute_creates_span(otel_exporter):
    engine = ToolEngine()

    async def my_tool(**kwargs):
        return {"result": "ok"}

    engine.register(ToolDef(
        name="test_tool", description="test", phases=[1], parameters={}, _fn=my_tool,
    ))

    call = ToolCall(id="t1", name="test_tool", arguments={})
    result = await engine.execute(call)

    assert result.status == "success"
    spans = otel_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "tool.execute" in span_names

    span = next(s for s in spans if s.name == "tool.execute")
    assert span.attributes["tool.name"] == "test_tool"
    assert span.attributes["tool.status"] == "success"


async def test_tool_execute_error_span(otel_exporter):
    engine = ToolEngine()

    async def fail_tool(**kwargs):
        raise ToolError("bad input", error_code="INVALID_INPUT")

    engine.register(ToolDef(
        name="fail_tool", description="test", phases=[1], parameters={}, _fn=fail_tool,
    ))

    call = ToolCall(id="t2", name="fail_tool", arguments={})
    result = await engine.execute(call)

    assert result.status == "error"
    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "tool.execute")
    assert span.attributes["tool.name"] == "fail_tool"
    assert span.attributes["tool.status"] == "error"
    assert span.attributes["tool.error_code"] == "INVALID_INPUT"


import json
from telemetry.attributes import EVENT_TOOL_INPUT, EVENT_TOOL_OUTPUT


async def test_tool_execute_has_input_event(otel_exporter):
    engine = ToolEngine()

    async def my_tool(**kwargs):
        return {"result": "ok"}

    engine.register(ToolDef(
        name="test_tool", description="test", phases=[1], parameters={}, _fn=my_tool,
    ))

    call = ToolCall(id="t1", name="test_tool", arguments={"dest": "Tokyo"})
    await engine.execute(call)

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "tool.execute")
    events = span.events
    input_event = next(e for e in events if e.name == EVENT_TOOL_INPUT)
    assert "arguments" in input_event.attributes
    parsed = json.loads(input_event.attributes["arguments"])
    assert parsed["dest"] == "Tokyo"


async def test_tool_execute_has_output_event_success(otel_exporter):
    engine = ToolEngine()

    async def my_tool(**kwargs):
        return {"flights": ["ANA"]}

    engine.register(ToolDef(
        name="test_tool", description="test", phases=[1], parameters={}, _fn=my_tool,
    ))

    call = ToolCall(id="t1", name="test_tool", arguments={})
    await engine.execute(call)

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "tool.execute")
    events = span.events
    output_event = next(e for e in events if e.name == EVENT_TOOL_OUTPUT)
    assert "data" in output_event.attributes


async def test_tool_execute_has_output_event_error(otel_exporter):
    engine = ToolEngine()

    async def fail_tool(**kwargs):
        raise ToolError("bad", error_code="BAD_INPUT")

    engine.register(ToolDef(
        name="fail_tool", description="test", phases=[1], parameters={}, _fn=fail_tool,
    ))

    call = ToolCall(id="t2", name="fail_tool", arguments={})
    await engine.execute(call)

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "tool.execute")
    events = span.events
    output_event = next(e for e in events if e.name == EVENT_TOOL_OUTPUT)
    assert "error" in output_event.attributes
    assert "error_code" in output_event.attributes


async def test_tool_input_event_truncated(otel_exporter):
    engine = ToolEngine()

    async def my_tool(**kwargs):
        return {"ok": True}

    engine.register(ToolDef(
        name="test_tool", description="test", phases=[1], parameters={}, _fn=my_tool,
    ))

    long_arg = "x" * 1000
    call = ToolCall(id="t3", name="test_tool", arguments={"data": long_arg})
    await engine.execute(call)

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "tool.execute")
    events = span.events
    input_event = next(e for e in events if e.name == "tool.input")
    assert input_event.attributes["arguments"].endswith("...(truncated)")
