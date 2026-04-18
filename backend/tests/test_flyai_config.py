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


def test_load_config_reads_flyai_api_key_from_env(monkeypatch, tmp_path):
    """load_config should fall back to FLYAI_API_KEY when YAML omits flyai.api_key."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("flyai:\n  enabled: true\n")
    monkeypatch.setenv("FLYAI_API_KEY", "env-flyai-key")

    cfg = load_config(str(cfg_file))

    assert cfg.flyai.api_key == "env-flyai-key"


def test_load_config_falls_back_to_repo_root_for_relative_path(monkeypatch, tmp_path):
    """Relative config path should resolve from repo root when cwd is backend/."""
    backend_dir = tmp_path / "project" / "backend"
    backend_dir.mkdir(parents=True)
    fake_config_module = backend_dir / "config.py"
    fake_config_module.write_text("")

    repo_root_config = backend_dir.parent / "config.yaml"
    repo_root_config.write_text("max_retries: 9\n")

    import config as config_module

    monkeypatch.setattr(config_module, "__file__", str(fake_config_module))
    monkeypatch.chdir(backend_dir)
    cfg = load_config("config.yaml")
    assert cfg.max_retries == 9
