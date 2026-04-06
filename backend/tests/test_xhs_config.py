from config import AppConfig, XhsConfig, load_config


def test_xhs_config_defaults():
    cfg = XhsConfig()
    assert cfg.enabled is True
    assert cfg.cli_bin == "xhs"
    assert cfg.cli_timeout == 30


def test_app_config_has_xhs_field():
    cfg = AppConfig()
    assert isinstance(cfg.xhs, XhsConfig)
    assert cfg.xhs.enabled is True


def test_load_config_parses_xhs(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
xhs:
  enabled: false
  cli_bin: /opt/bin/xhs
  cli_timeout: 12
"""
    )

    cfg = load_config(str(cfg_file))
    assert cfg.xhs.enabled is False
    assert cfg.xhs.cli_bin == "/opt/bin/xhs"
    assert cfg.xhs.cli_timeout == 12


def test_load_config_xhs_env_override(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
xhs:
  cli_bin: /yaml/bin/xhs
  cli_timeout: 8
"""
    )
    monkeypatch.setenv("XHS_CLI_BIN", "/env/bin/xhs")
    monkeypatch.setenv("XHS_CLI_TIMEOUT", "45")

    cfg = load_config(str(cfg_file))
    assert cfg.xhs.cli_bin == "/env/bin/xhs"
    assert cfg.xhs.cli_timeout == 45
