from unittest.mock import patch, MagicMock
from config import TelemetryConfig


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
