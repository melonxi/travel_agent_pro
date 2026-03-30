import os
import tempfile
from pathlib import Path
from config import load_config, TelemetryConfig


def test_telemetry_config_defaults():
    cfg = TelemetryConfig()
    assert cfg.enabled is True
    assert cfg.endpoint == "http://localhost:4317"
    assert cfg.service_name == "travel-agent-pro"


def test_load_config_without_telemetry_section():
    """config.yaml 没有 telemetry 段时使用默认值。"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("llm:\n  provider: openai\n")
        f.flush()
        cfg = load_config(f.name)
    os.unlink(f.name)
    assert cfg.telemetry.enabled is True
    assert cfg.telemetry.endpoint == "http://localhost:4317"


def test_load_config_with_telemetry_section():
    """config.yaml 有 telemetry 段时使用配置值。"""
    yaml_content = """
llm:
  provider: openai
telemetry:
  enabled: false
  endpoint: "http://otel-collector:4317"
  service_name: "my-app"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        cfg = load_config(f.name)
    os.unlink(f.name)
    assert cfg.telemetry.enabled is False
    assert cfg.telemetry.endpoint == "http://otel-collector:4317"
    assert cfg.telemetry.service_name == "my-app"
