from config import AppConfig, XhsConfig, load_config


def test_xhs_config_defaults_to_disabled():
    """安全默认：XHS CLI 有封号风险，未显式开启时必须关闭。"""
    cfg = XhsConfig()
    assert cfg.enabled is False
    assert cfg.cli_bin == "xhs"
    assert cfg.cli_timeout == 30


def test_app_config_has_xhs_field():
    cfg = AppConfig()
    assert isinstance(cfg.xhs, XhsConfig)
    assert cfg.xhs.enabled is False


def test_load_config_without_xhs_section_stays_disabled(tmp_path):
    """陌生人 clone 场景：config.yaml 没有 xhs 段时默认关闭。"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("{}\n")

    cfg = load_config(str(cfg_file))
    assert cfg.xhs.enabled is False


def test_load_config_allows_explicit_opt_in(tmp_path):
    """显式 opt-in 仍然可用（本机自担风险）。"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
xhs:
  enabled: true
"""
    )

    cfg = load_config(str(cfg_file))
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
