from pathlib import Path

from main import _resolve_app_data_dir


def test_default_config_data_dir_resolves_to_repo_root_when_started_from_backend(
    monkeypatch,
):
    repo_root = Path(__file__).resolve().parents[2]
    backend_dir = repo_root / "backend"
    monkeypatch.chdir(backend_dir)

    assert _resolve_app_data_dir("config.yaml", "./data") == str(
        (repo_root / "data").resolve()
    )


def test_explicit_relative_config_data_dir_resolves_next_to_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("data_dir: ./data\n", encoding="utf-8")

    assert _resolve_app_data_dir(str(config_path), "./data") == str(
        (tmp_path / "data").resolve()
    )


def test_absolute_data_dir_is_preserved(tmp_path):
    data_dir = tmp_path / "app-data"

    assert _resolve_app_data_dir("config.yaml", str(data_dir)) == str(data_dir)
