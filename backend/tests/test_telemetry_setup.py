# backend/tests/test_telemetry_setup.py
import opentelemetry.trace as _trace_module
import pytest
from unittest.mock import MagicMock, patch

from opentelemetry import trace

from config import TelemetryConfig


def _reset_tracer_provider():
    """重置 OTel 全局 TracerProvider，允许在测试间重新设置。"""
    _trace_module._TRACER_PROVIDER_SET_ONCE._done = False
    _trace_module._TRACER_PROVIDER = None


@pytest.fixture(autouse=True)
def reset_otel():
    _reset_tracer_provider()
    yield
    _reset_tracer_provider()


def test_setup_telemetry_enabled():
    """enabled=True 时应配置 TracerProvider。"""
    from telemetry.setup import setup_telemetry

    app = MagicMock()
    config = TelemetryConfig(enabled=True, endpoint="http://localhost:4317")
    setup_telemetry(app, config)

    provider = trace.get_tracer_provider()
    assert not isinstance(provider, trace.NoOpTracerProvider)


def test_setup_telemetry_disabled():
    """enabled=False 时不应配置 TracerProvider，保持 NoOp。"""
    trace.set_tracer_provider(trace.NoOpTracerProvider())

    from telemetry.setup import setup_telemetry

    app = MagicMock()
    config = TelemetryConfig(enabled=False)
    setup_telemetry(app, config)

    provider = trace.get_tracer_provider()
    assert isinstance(provider, trace.NoOpTracerProvider)


def test_setup_telemetry_respects_otel_sdk_disabled(monkeypatch):
    """OTEL_SDK_DISABLED=true 时应保持 NoOp，方便测试环境禁用 exporter。"""
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    trace.set_tracer_provider(trace.NoOpTracerProvider())

    from telemetry.setup import setup_telemetry

    app = MagicMock()
    config = TelemetryConfig(enabled=True)
    setup_telemetry(app, config)

    provider = trace.get_tracer_provider()
    assert isinstance(provider, trace.NoOpTracerProvider)


def test_setup_telemetry_sets_service_name():
    """应在 Resource 中设置 service.name。"""
    from telemetry.setup import setup_telemetry
    from opentelemetry.sdk.trace import TracerProvider as SdkTP

    app = MagicMock()
    config = TelemetryConfig(
        enabled=True,
        service_name="test-service",
        endpoint="http://localhost:4317",
    )
    setup_telemetry(app, config)

    provider = trace.get_tracer_provider()
    if isinstance(provider, SdkTP):
        resource_attrs = dict(provider.resource.attributes)
        assert resource_attrs.get("service.name") == "test-service"


# --- 以下测试迁移自 test_telemetry_integration.py（原文件已删除）---


def test_create_app_calls_setup_telemetry():
    """create_app 应调用 setup_telemetry。"""
    with patch("main.setup_telemetry") as mock_setup:
        from main import create_app

        app = create_app()
        mock_setup.assert_called_once()
        call_args = mock_setup.call_args
        # 第一个参数是 FastAPI app
        assert call_args[0][0] is app
        # 第二个参数是 TelemetryConfig
        assert isinstance(call_args[0][1], TelemetryConfig)
