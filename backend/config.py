# backend/config.py
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "openai"
    model: str = "gpt-4o"
    temperature: float = 0.7
    max_tokens: int = 4096


@dataclass(frozen=True)
class ApiKeysConfig:
    google_maps: str = ""
    amadeus_key: str = ""
    amadeus_secret: str = ""
    openweather: str = ""


@dataclass(frozen=True)
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    llm_overrides: dict[str, LLMConfig] = field(default_factory=dict)
    api_keys: ApiKeysConfig = field(default_factory=ApiKeysConfig)
    data_dir: str = "./data"
    max_retries: int = 3
    context_compression_threshold: float = 0.5


def _resolve_env(value: str) -> str:
    """Replace ${ENV_VAR} with actual environment variable value."""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        env_name = value[2:-1]
        return os.environ.get(env_name, "")
    return value


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    path = Path(path)
    if not path.exists():
        return AppConfig()

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    llm_raw = raw.get("llm", {})
    llm = LLMConfig(
        **{k: v for k, v in llm_raw.items() if k in LLMConfig.__dataclass_fields__}
    )

    overrides: dict[str, LLMConfig] = {}
    for key, val in raw.get("llm_overrides", {}).items():
        overrides[key] = LLMConfig(
            **{k: v for k, v in val.items() if k in LLMConfig.__dataclass_fields__}
        )

    api_raw = raw.get("api_keys", {})
    api_keys = ApiKeysConfig(
        **{
            k: _resolve_env(v)
            for k, v in api_raw.items()
            if k in ApiKeysConfig.__dataclass_fields__
        }
    )

    return AppConfig(
        llm=llm,
        llm_overrides=overrides,
        api_keys=api_keys,
        data_dir=raw.get("data_dir", "./data"),
        max_retries=raw.get("max_retries", 3),
        context_compression_threshold=raw.get("context_compression_threshold", 0.5),
    )
