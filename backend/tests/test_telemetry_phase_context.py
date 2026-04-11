import opentelemetry.trace as _trace_module
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent.types import Message, Role
from context.manager import ContextManager
from phase.router import PhaseRouter
from state.models import DateRange, TravelPlanState
from telemetry.attributes import EVENT_CONTEXT_COMPRESSION, EVENT_PHASE_PLAN_SNAPSHOT


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


@pytest.mark.asyncio
async def test_phase_transition_creates_span(otel_exporter):
    """Test that phase transition creates a span with correct attributes."""
    router = PhaseRouter()
    plan = TravelPlanState(session_id="s1", destination="Tokyo")

    changed = await router.check_and_apply_transition(plan)

    assert changed
    assert plan.phase == 3

    spans = otel_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "phase.transition" in span_names

    span = next(s for s in spans if s.name == "phase.transition")
    assert span.attributes["phase.from"] == 1
    assert span.attributes["phase.to"] == 3


@pytest.mark.asyncio
async def test_no_transition_no_span(otel_exporter):
    """Test that no transition means no phase.transition span is created."""
    router = PhaseRouter()
    plan = TravelPlanState(session_id="s1", phase=1)

    changed = await router.check_and_apply_transition(plan)

    assert not changed
    assert plan.phase == 1

    spans = otel_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "phase.transition" not in span_names


def test_context_compress_check_creates_span(otel_exporter):
    """Test that should_compress creates a span with correct attributes."""
    manager = ContextManager()
    messages = [
        Message(role=Role.USER, content="Hello " * 100),
        Message(role=Role.ASSISTANT, content="Response " * 50),
    ]
    max_tokens = 1000

    result = manager.should_compress(messages, max_tokens)

    spans = otel_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "context.should_compress" in span_names

    span = next(s for s in spans if s.name == "context.should_compress")
    assert "context.tokens.before" in span.attributes
    assert "context.max_tokens" in span.attributes
    assert span.attributes["context.max_tokens"] == 1000


@pytest.mark.asyncio
async def test_phase_transition_has_plan_snapshot_event(otel_exporter):
    router = PhaseRouter()
    plan = TravelPlanState(
        session_id="s1",
        destination="Tokyo",
        dates=DateRange(start="2026-04-01", end="2026-04-05"),
    )

    changed = await router.check_and_apply_transition(plan)
    assert changed

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "phase.transition")
    events = span.events
    snapshot = next(e for e in events if e.name == EVENT_PHASE_PLAN_SNAPSHOT)
    assert snapshot.attributes["destination"] == "Tokyo"
    assert snapshot.attributes["dates"] == "2026-04-01 ~ 2026-04-05"
    assert snapshot.attributes["daily_plans_count"] == 0


def test_context_compression_event_when_triggered(otel_exporter):
    """压缩判定为 True 时，应添加 context.compression event。"""
    manager = ContextManager()
    messages = [
        Message(role=Role.USER, content="Hello " * 500),
        Message(role=Role.ASSISTANT, content="Response " * 300),
    ]
    max_tokens = 100

    result = manager.should_compress(messages, max_tokens)
    assert result is True

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "context.should_compress")
    events = span.events
    comp_event = next(e for e in events if e.name == EVENT_CONTEXT_COMPRESSION)
    assert comp_event.attributes["message_count"] == 2
    assert "estimated_tokens" in comp_event.attributes


def test_context_no_compression_event_when_not_triggered(otel_exporter):
    """压缩判定为 False 时，不应添加 context.compression event。"""
    manager = ContextManager()
    messages = [
        Message(role=Role.USER, content="Hi"),
    ]
    max_tokens = 100000

    result = manager.should_compress(messages, max_tokens)
    assert result is False

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "context.should_compress")
    events = span.events
    compression_events = [e for e in events if e.name == EVENT_CONTEXT_COMPRESSION]
    assert len(compression_events) == 0
