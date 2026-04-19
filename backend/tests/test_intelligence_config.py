from config import AppConfig, load_config


def test_app_config_has_intelligence_defaults():
    cfg = AppConfig()
    assert cfg.max_retries == 60
    assert cfg.quality_gate.threshold == 3.5
    assert cfg.quality_gate.max_retries == 2
    assert cfg.parallel_tool_execution is True
    assert cfg.memory_extraction.enabled is True
    assert cfg.memory_extraction.model == "gpt-4o-mini"
    assert cfg.guardrails.enabled is True
    assert cfg.guardrails.disabled_rules == []


def test_load_config_parses_agent_intelligence_sections(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
llm:
  provider: openai
quality_gate:
  threshold: 4.0
  max_retries: 3
parallel_tool_execution: false
memory_extraction:
  enabled: false
  model: gpt-4o-mini
guardrails:
  enabled: false
  disabled_rules:
    - price_anomaly
""",
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_file))

    assert cfg.quality_gate.threshold == 4.0
    assert cfg.quality_gate.max_retries == 3
    assert cfg.parallel_tool_execution is False
    assert cfg.memory_extraction.enabled is False
    assert cfg.memory_extraction.model == "gpt-4o-mini"
    assert cfg.guardrails.enabled is False
    assert cfg.guardrails.disabled_rules == ["price_anomaly"]
