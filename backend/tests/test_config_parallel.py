# backend/tests/test_config_parallel.py
from config import load_config, Phase5ParallelConfig


def test_phase5_parallel_defaults():
    """默认配置应启用并行模式。"""
    cfg = load_config()
    assert isinstance(cfg.phase5_parallel, Phase5ParallelConfig)
    assert cfg.phase5_parallel.enabled is True
    assert cfg.phase5_parallel.max_workers == 5
    assert cfg.phase5_parallel.worker_max_iterations == 60
    assert cfg.phase5_parallel.worker_timeout_seconds == 1200
    assert cfg.phase5_parallel.fallback_to_serial is True
    assert cfg.phase5_parallel.artifact_root == "./data/phase5_runs"


def test_phase5_parallel_disabled():
    """Phase5ParallelConfig 可手动构造为 disabled。"""
    cfg = load_config()
    # 此测试验证 Phase5ParallelConfig 可被构造为 disabled
    disabled = Phase5ParallelConfig(enabled=False)
    assert disabled.enabled is False


def test_phase5_parallel_from_yaml_disabled(tmp_path):
    """从 YAML 加载 enabled: false 应正确解析。"""
    yaml_content = """\
phase5:
  parallel:
    enabled: false
    max_workers: 3
    artifact_root: "{artifact_root}"
"""
    artifact_root = tmp_path / "phase5-artifacts"
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml_content.format(artifact_root=artifact_root))
    cfg = load_config(str(config_file))
    assert cfg.phase5_parallel.enabled is False
    assert cfg.phase5_parallel.max_workers == 3
    assert cfg.phase5_parallel.artifact_root == str(artifact_root)
    # Other fields should have defaults
    assert cfg.phase5_parallel.worker_max_iterations == 60
    assert cfg.phase5_parallel.worker_timeout_seconds == 1200
