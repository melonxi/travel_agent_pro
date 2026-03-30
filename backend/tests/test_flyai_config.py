# backend/tests/test_flyai_config.py
import pytest
from config import load_config, FlyAIConfig, AppConfig


def test_flyai_config_defaults():
    """FlyAIConfig should have sensible defaults."""
    cfg = FlyAIConfig()
    assert cfg.enabled is True
    assert cfg.cli_timeout == 30
    assert cfg.api_key is None


def test_app_config_has_flyai_field():
    """AppConfig should include a flyai field."""
    cfg = AppConfig()
    assert isinstance(cfg.flyai, FlyAIConfig)
    assert cfg.flyai.enabled is True


def test_load_config_parses_flyai(tmp_path):
    """load_config should parse the flyai section from YAML."""
    yaml_content = """
flyai:
  enabled: false
  cli_timeout: 15
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)
    cfg = load_config(str(cfg_file))
    assert cfg.flyai.enabled is False
    assert cfg.flyai.cli_timeout == 15
    assert cfg.flyai.api_key is None


def test_load_config_flyai_missing(tmp_path):
    """When flyai section is absent, defaults should apply."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("llm:\n  provider: openai\n")
    cfg = load_config(str(cfg_file))
    assert cfg.flyai.enabled is True
    assert cfg.flyai.cli_timeout == 30
