# backend/telemetry/setup.py
from __future__ import annotations

from fastapi import FastAPI

from config import TelemetryConfig


def setup_telemetry(app: FastAPI, config: TelemetryConfig) -> None:
    """初始化 OTel tracing。enabled=False 时为 no-op。"""
    if not config.enabled:
        return

    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    resource = Resource.create({"service.name": config.service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=config.endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
