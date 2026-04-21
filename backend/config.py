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
    context_window: int = 200000


@dataclass(frozen=True)
class ApiKeysConfig:
    google_maps: str = ""
    amadeus_key: str = ""
    amadeus_secret: str = ""
    openweather: str = ""
    tavily: str = ""


@dataclass(frozen=True)
class TelemetryConfig:
    enabled: bool = True
    endpoint: str = "http://localhost:4317"
    service_name: str = "travel-agent-pro"


@dataclass(frozen=True)
class FlyAIConfig:
    enabled: bool = True
    cli_timeout: int = 30
    api_key: str | None = None


@dataclass(frozen=True)
class XhsConfig:
    enabled: bool = True
    cli_bin: str = "xhs"
    cli_timeout: int = 30


@dataclass(frozen=True)
class QualityGateConfig:
    threshold: float = 3.5
    max_retries: int = 2


@dataclass(frozen=True)
class MemoryExtractionConfig:
    enabled: bool = True
    model: str = "gpt-4o-mini"


@dataclass(frozen=True)
class MemoryExtractionV2Config:
    enabled: bool = True
    model: str = "gpt-4o-mini"
    trigger: str = "each_turn"
    max_user_messages: int = 8


@dataclass(frozen=True)
class MemoryPolicyConfig:
    auto_save_low_risk: bool = True
    auto_save_medium_risk: bool = False
    require_confirmation_for_high_risk: bool = True


@dataclass(frozen=True)
class MemoryRetrievalConfig:
    core_limit: int = 10
    phase_limit: int = 8
    include_pending: bool = False
    recall_gate_enabled: bool = True
    recall_gate_model: str = ""
    recall_gate_timeout_seconds: float = 6.0


@dataclass(frozen=True)
class MemoryStorageConfig:
    backend: str = "json"


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = True
    extraction: MemoryExtractionV2Config = field(
        default_factory=MemoryExtractionV2Config
    )
    policy: MemoryPolicyConfig = field(default_factory=MemoryPolicyConfig)
    retrieval: MemoryRetrievalConfig = field(default_factory=MemoryRetrievalConfig)
    storage: MemoryStorageConfig = field(default_factory=MemoryStorageConfig)


@dataclass(frozen=True)
class GuardrailsConfig:
    enabled: bool = True
    disabled_rules: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Phase5ParallelConfig:
    enabled: bool = True
    max_workers: int = 5
    worker_max_iterations: int = 60
    worker_timeout_seconds: int = 1200
    fallback_to_serial: bool = True


@dataclass(frozen=True)
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    llm_overrides: dict[str, LLMConfig] = field(default_factory=dict)
    api_keys: ApiKeysConfig = field(default_factory=ApiKeysConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    data_dir: str = "./data"
    max_retries: int = 60
    context_compression_threshold: float = 0.5
    flyai: FlyAIConfig = field(default_factory=FlyAIConfig)
    xhs: XhsConfig = field(default_factory=XhsConfig)
    quality_gate: QualityGateConfig = field(default_factory=QualityGateConfig)
    parallel_tool_execution: bool = True
    memory_extraction: MemoryExtractionConfig = field(
        default_factory=MemoryExtractionConfig
    )
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    guardrails: GuardrailsConfig = field(default_factory=GuardrailsConfig)
    phase5_parallel: Phase5ParallelConfig = field(default_factory=Phase5ParallelConfig)


def _resolve_env(value: object) -> str:
    """Replace ${ENV_VAR} with actual environment variable value.

    Only supports values that are entirely a single ${VAR} reference.
    Non-string values from YAML (int, bool, etc.) are converted to str.
    """
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        env_name = value[2:-1]
        return os.environ.get(env_name, "")
    return str(value) if not isinstance(value, str) else value


def _as_bool(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = _resolve_env(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return default


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
        context_window=int(llm_raw.get("context_window", 200000)),
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
        tavily=_get("tavily", "TAVILY_API_KEY"),
    )


def _build_xhs_config(xhs_raw: dict) -> XhsConfig:
    return XhsConfig(
        enabled=_as_bool(xhs_raw.get("enabled"), True),
        cli_bin=os.environ.get(
            "XHS_CLI_BIN", _resolve_env(xhs_raw.get("cli_bin", "xhs")) or "xhs"
        ),
        cli_timeout=int(
            os.environ.get(
                "XHS_CLI_TIMEOUT",
                _resolve_env(xhs_raw.get("cli_timeout", 30)) or "30",
            )
        ),
    )


def _build_quality_gate_config(raw: dict) -> QualityGateConfig:
    return QualityGateConfig(
        threshold=float(raw.get("threshold", 3.5)),
        max_retries=int(raw.get("max_retries", 2)),
    )


def _build_memory_extraction_config(raw: dict) -> MemoryExtractionConfig:
    return MemoryExtractionConfig(
        enabled=_as_bool(raw.get("enabled"), True),
        model=str(raw.get("model", "gpt-4o-mini")),
    )


def _build_memory_config(
    raw: dict, legacy_extraction: MemoryExtractionConfig
) -> MemoryConfig:
    extraction_raw = raw.get("extraction", {})
    policy_raw = raw.get("policy", {})
    retrieval_raw = raw.get("retrieval", {})
    storage_raw = raw.get("storage", {})

    extraction = MemoryExtractionV2Config(
        enabled=_as_bool(extraction_raw.get("enabled"), legacy_extraction.enabled),
        model=str(extraction_raw.get("model", legacy_extraction.model)),
        trigger=str(extraction_raw.get("trigger", "each_turn")),
        max_user_messages=int(extraction_raw.get("max_user_messages", 8)),
    )

    return MemoryConfig(
        enabled=_as_bool(raw.get("enabled"), True),
        extraction=extraction,
        policy=MemoryPolicyConfig(
            auto_save_low_risk=_as_bool(policy_raw.get("auto_save_low_risk"), True),
            auto_save_medium_risk=_as_bool(
                policy_raw.get("auto_save_medium_risk"), False
            ),
            require_confirmation_for_high_risk=_as_bool(
                policy_raw.get("require_confirmation_for_high_risk"), True
            ),
        ),
        retrieval=MemoryRetrievalConfig(
            core_limit=int(retrieval_raw.get("core_limit", 10)),
            phase_limit=int(retrieval_raw.get("phase_limit", 8)),
            include_pending=_as_bool(retrieval_raw.get("include_pending"), False),
            recall_gate_enabled=_as_bool(
                retrieval_raw.get("recall_gate_enabled"), True
            ),
            recall_gate_model=(retrieval_raw.get("recall_gate_model") or ""),
            recall_gate_timeout_seconds=float(
                retrieval_raw.get("recall_gate_timeout_seconds", 6.0)
            ),
        ),
        storage=MemoryStorageConfig(
            backend=str(storage_raw.get("backend", "json")),
        ),
    )


def _build_guardrails_config(raw: dict) -> GuardrailsConfig:
    disabled_rules = raw.get("disabled_rules", [])
    if not isinstance(disabled_rules, list):
        disabled_rules = []
    return GuardrailsConfig(
        enabled=_as_bool(raw.get("enabled"), True),
        disabled_rules=[str(rule) for rule in disabled_rules],
    )


def _build_phase5_parallel_config(raw: dict) -> Phase5ParallelConfig:
    p5 = raw.get("phase5", {}).get("parallel", {})
    return Phase5ParallelConfig(
        enabled=_as_bool(p5.get("enabled"), True),
        max_workers=int(p5.get("max_workers", 5)),
        worker_max_iterations=int(p5.get("worker_max_iterations", 60)),
        worker_timeout_seconds=int(p5.get("worker_timeout_seconds", 1200)),
        fallback_to_serial=_as_bool(p5.get("fallback_to_serial"), True),
    )


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    path = Path(path)
    if not path.is_absolute() and not path.exists():
        repo_relative = Path(__file__).resolve().parent.parent / path
        if repo_relative.exists():
            path = repo_relative
    if not path.exists():
        # No YAML — build entirely from env vars / defaults
        return AppConfig(
            llm=_build_llm_config({}),
            api_keys=_build_api_keys({}),
            telemetry=TelemetryConfig(),
            xhs=_build_xhs_config({}),
            quality_gate=_build_quality_gate_config({}),
            memory_extraction=_build_memory_extraction_config({}),
            memory=_build_memory_config({}, _build_memory_extraction_config({})),
            guardrails=_build_guardrails_config({}),
            phase5_parallel=_build_phase5_parallel_config({}),
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
        enabled=_as_bool(tel_raw.get("enabled"), True),
        endpoint=tel_raw.get("endpoint", "http://localhost:4317"),
        service_name=tel_raw.get("service_name", "travel-agent-pro"),
    )

    # Parse flyai config
    flyai_raw = raw.get("flyai", {})
    flyai = FlyAIConfig(
        enabled=_as_bool(flyai_raw.get("enabled"), True),
        cli_timeout=int(flyai_raw.get("cli_timeout", 30)),
        api_key=_resolve_env(flyai_raw.get("api_key", ""))
        or os.environ.get("FLYAI_API_KEY")
        or None,
    )
    xhs = _build_xhs_config(raw.get("xhs", {}))
    quality_gate = _build_quality_gate_config(raw.get("quality_gate", {}))
    memory_extraction = _build_memory_extraction_config(
        raw.get("memory_extraction", {})
    )
    memory = _build_memory_config(raw.get("memory", {}), memory_extraction)
    guardrails = _build_guardrails_config(raw.get("guardrails", {}))

    return AppConfig(
        llm=llm,
        llm_overrides=overrides,
        api_keys=api_keys,
        telemetry=telemetry,
        data_dir=raw.get("data_dir", "./data"),
        max_retries=raw.get("max_retries", 60),
        context_compression_threshold=raw.get("context_compression_threshold", 0.5),
        flyai=flyai,
        xhs=xhs,
        quality_gate=quality_gate,
        parallel_tool_execution=_as_bool(raw.get("parallel_tool_execution"), True),
        memory_extraction=memory_extraction,
        memory=memory,
        guardrails=guardrails,
        phase5_parallel=_build_phase5_parallel_config(raw),
    )
