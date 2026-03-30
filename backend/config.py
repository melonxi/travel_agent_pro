# backend/config.py
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load .env file as early as possible so that all env vars
# (OPENAI_API_KEY, OPENAI_BASE_URL, ANTHROPIC_*, etc.) are available
# both to our config logic and to the OpenAI / Anthropic SDKs.
load_dotenv()


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
class TelemetryConfig:
    enabled: bool = True
    endpoint: str = "http://localhost:4317"
    service_name: str = "travel-agent-pro"


@dataclass(frozen=True)
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    llm_overrides: dict[str, LLMConfig] = field(default_factory=dict)
    api_keys: ApiKeysConfig = field(default_factory=ApiKeysConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    data_dir: str = "./data"
    max_retries: int = 3
    context_compression_threshold: float = 0.5


def _resolve_env(value: object) -> str:
    """Replace ${ENV_VAR} with actual environment variable value.

    Only supports values that are entirely a single ${VAR} reference.
    Non-string values from YAML (int, bool, etc.) are converted to str.
    """
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        env_name = value[2:-1]
        return os.environ.get(env_name, "")
    return str(value) if not isinstance(value, str) else value


def _build_llm_config(llm_raw: dict) -> LLMConfig:
    """Build LLMConfig from YAML values, then override with env vars.

    Priority: env var > yaml > dataclass default.

    Env var mapping:
        DEFAULT_PROVIDER  → provider
        OPENAI_MODEL      → model  (when provider == "openai")
        ANTHROPIC_MODEL   → model  (when provider == "anthropic")
    """
    provider = os.environ.get("DEFAULT_PROVIDER", llm_raw.get("provider", "openai"))

    if provider == "anthropic":
        default_model = "claude-sonnet-4-20250514"
        model_env = "ANTHROPIC_MODEL"
    else:
        default_model = "gpt-4o"
        model_env = "OPENAI_MODEL"

    model = os.environ.get(model_env, llm_raw.get("model", default_model))

    return LLMConfig(
        provider=provider,
        model=model,
        temperature=float(llm_raw.get("temperature", 0.7)),
        max_tokens=int(llm_raw.get("max_tokens", 4096)),
    )


def _build_api_keys(api_raw: dict) -> ApiKeysConfig:
    """Build ApiKeysConfig from YAML values, falling back to env vars.

    Priority: yaml ${VAR} resolved value > direct env var > empty string.
    """

    def _get(yaml_key: str, env_key: str) -> str:
        yaml_val = _resolve_env(api_raw.get(yaml_key, ""))
        return yaml_val or os.environ.get(env_key, "")

    return ApiKeysConfig(
        google_maps=_get("google_maps", "GOOGLE_MAPS_API_KEY"),
        amadeus_key=_get("amadeus_key", "AMADEUS_API_KEY"),
        amadeus_secret=_get("amadeus_secret", "AMADEUS_API_SECRET"),
        openweather=_get("openweather", "OPENWEATHER_API_KEY"),
    )


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    path = Path(path)
    if not path.exists():
        # No YAML — build entirely from env vars / defaults
        return AppConfig(
            llm=_build_llm_config({}),
            api_keys=_build_api_keys({}),
            telemetry=TelemetryConfig(),
        )

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    llm = _build_llm_config(raw.get("llm", {}))

    overrides: dict[str, LLMConfig] = {}
    for key, val in raw.get("llm_overrides", {}).items():
        overrides[key] = LLMConfig(
            **{k: v for k, v in val.items() if k in LLMConfig.__dataclass_fields__}
        )

    api_keys = _build_api_keys(raw.get("api_keys", {}))

    tel_raw = raw.get("telemetry", {})
    telemetry = TelemetryConfig(
        enabled=tel_raw.get("enabled", True),
        endpoint=tel_raw.get("endpoint", "http://localhost:4317"),
        service_name=tel_raw.get("service_name", "travel-agent-pro"),
    )

    return AppConfig(
        llm=llm,
        llm_overrides=overrides,
        api_keys=api_keys,
        telemetry=telemetry,
        data_dir=raw.get("data_dir", "./data"),
        max_retries=raw.get("max_retries", 3),
        context_compression_threshold=raw.get("context_compression_threshold", 0.5),
    )
