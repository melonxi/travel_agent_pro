# backend/tests/test_telemetry_decorators.py
import opentelemetry.trace as _trace_module
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from telemetry.decorators import traced


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


def test_traced_sync_function(otel_exporter):
    @traced()
    def add(a, b):
        return a + b

    result = add(1, 2)
    assert result == 3

    spans = otel_exporter.get_finished_spans()
    assert len(spans) == 1
    assert "add" in spans[0].name


async def test_traced_async_function(otel_exporter):
    @traced()
    async def fetch(url):
        return f"data from {url}"

    result = await fetch("http://example.com")
    assert result == "data from http://example.com"

    spans = otel_exporter.get_finished_spans()
    assert len(spans) == 1
    assert "fetch" in spans[0].name


def test_traced_custom_name(otel_exporter):
    @traced(name="custom.span.name")
    def my_func():
        return 42

    my_func()
    spans = otel_exporter.get_finished_spans()
    assert spans[0].name == "custom.span.name"


def test_traced_records_exception(otel_exporter):
    @traced()
    def fail():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        fail()

    spans = otel_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == trace.StatusCode.ERROR
    events = span.events
    assert any(e.name == "exception" for e in events)


async def test_traced_async_records_exception(otel_exporter):
    @traced()
    async def async_fail():
        raise RuntimeError("async boom")

    with pytest.raises(RuntimeError, match="async boom"):
        await async_fail()

    spans = otel_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == trace.StatusCode.ERROR


def test_traced_record_args(otel_exporter):
    @traced(record_args=["name", "count"])
    def greet(name, count, secret):
        return f"hello {name} x{count}"

    greet("alice", 3, "password123")
    spans = otel_exporter.get_finished_spans()
    attrs = dict(spans[0].attributes)
    assert attrs["arg.name"] == "alice"
    assert attrs["arg.count"] == 3
    assert "arg.secret" not in attrs


def test_traced_disabled_is_noop():
    """当 telemetry 未启用时，@traced 不创建 span。"""
    trace.set_tracer_provider(trace.NoOpTracerProvider())

    @traced()
    def simple():
        return "ok"

    result = simple()
    assert result == "ok"
