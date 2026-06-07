# backend/telemetry/stats.py
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


# Per-1M-token pricing (USD)
_DEEPSEEK_V4_FLASH_PRICING = {
    "input": 0.14,
    "output": 0.28,
    "cache_hit_input": 0.0028,
    "cache_miss_input": 0.14,
}

_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-4-1": {"input": 2.00, "output": 8.00},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "o1": {"input": 15.00, "output": 60.00},
    "o3-mini": {"input": 1.10, "output": 4.40},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00},
    "claude-opus-4": {"input": 15.00, "output": 75.00},
    "claude-haiku-4": {"input": 0.80, "output": 4.00},
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.00},
    "deepseek-v4-flash": _DEEPSEEK_V4_FLASH_PRICING,
    "deepseek-chat": _DEEPSEEK_V4_FLASH_PRICING,
    "deepseek-reasoner": _DEEPSEEK_V4_FLASH_PRICING,
    "deepseek-v4-pro": {
        "input": 0.435,
        "output": 0.87,
        "cache_hit_input": 0.003625,
        "cache_miss_input": 0.435,
    },
    "deepseek-r1": {"input": 0.55, "output": 2.19},
}


def _model_candidates(model: str) -> list[str]:
    model_lower = model.lower()
    candidates = [model_lower]
    if "/" in model_lower:
        candidates.append(model_lower.rsplit("/", 1)[-1])
    return candidates


def _lookup_pricing(model: str) -> dict[str, float] | None:
    candidates = _model_candidates(model)
    for prefix, pricing in _PRICING.items():
        for candidate in candidates:
            if candidate.startswith(prefix):
                return pricing
    return None


def lookup_pricing(model: str) -> dict[str, float] | None:
    """Public API for model pricing lookup."""
    return _lookup_pricing(model)


def _metadata_int(metadata: dict[str, Any] | None, key: str) -> int | None:
    if not metadata:
        return None
    value = metadata.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def estimate_llm_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    metadata: dict[str, Any] | None = None,
) -> float:
    pricing = _lookup_pricing(model)
    if not pricing:
        return 0.0

    cache_hit = _metadata_int(metadata, "prompt_cache_hit_tokens")
    cache_miss = _metadata_int(metadata, "prompt_cache_miss_tokens")
    has_cache_pricing = "cache_hit_input" in pricing and "cache_miss_input" in pricing
    if has_cache_pricing and (cache_hit is not None or cache_miss is not None):
        hit_tokens = cache_hit or 0
        miss_tokens = cache_miss or 0
        priced_input_tokens = hit_tokens + miss_tokens
        unbucketed_tokens = max(input_tokens - priced_input_tokens, 0)
        input_cost = (hit_tokens / 1_000_000) * pricing["cache_hit_input"]
        input_cost += (miss_tokens / 1_000_000) * pricing["cache_miss_input"]
        input_cost += (unbucketed_tokens / 1_000_000) * pricing["input"]
    else:
        input_cost = (input_tokens / 1_000_000) * pricing["input"]

    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return input_cost + output_cost


LLM_CACHE_USAGE_KEYS = (
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "prompt_cache_hit_rate",
)


def llm_cache_usage_metadata(usage_info: dict[str, Any] | None) -> dict[str, Any]:
    if not usage_info:
        return {}

    metadata: dict[str, Any] = {}
    for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        value = usage_info.get(key)
        if value is None:
            continue
        try:
            metadata[key] = int(value)
        except (TypeError, ValueError):
            continue

    rate = usage_info.get("prompt_cache_hit_rate")
    if rate is not None:
        try:
            metadata["prompt_cache_hit_rate"] = float(rate)
        except (TypeError, ValueError):
            pass
    else:
        hit = metadata.get("prompt_cache_hit_tokens")
        miss = metadata.get("prompt_cache_miss_tokens")
        if hit is not None and miss is not None and hit + miss > 0:
            metadata["prompt_cache_hit_rate"] = hit / (hit + miss)

    return metadata


@dataclass
class LLMCallRecord:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: float
    phase: int
    iteration: int
    metadata: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class ToolCallRecord:
    tool_name: str
    duration_ms: float
    status: str
    error_code: str | None
    phase: int
    timestamp: float = field(default_factory=time.time)
    arguments_preview: str = ""
    result_preview: str = ""
    state_changes: list[dict] | None = None
    parallel_group: int | None = None
    validation_errors: list[str] | None = None
    judge_scores: dict | None = None
    suggestion: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class MemoryHitRecord:
    sources: dict[str, int] = field(default_factory=dict)
    profile_ids: list[str] = field(default_factory=list)
    working_memory_ids: list[str] = field(default_factory=list)
    slice_ids: list[str] = field(default_factory=list)
    matched_reasons: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "sources": dict(self.sources),
            "profile_ids": list(self.profile_ids),
            "working_memory_ids": list(self.working_memory_ids),
            "slice_ids": list(self.slice_ids),
            "matched_reasons": list(self.matched_reasons),
            "timestamp": self.timestamp,
        }


@dataclass
class RecallTelemetryRecord:
    stage0_decision: str = "undecided"
    stage0_reason: str = ""
    stage0_matched_rule: str = ""
    stage0_signals: dict[str, list[str]] = field(default_factory=dict)
    gate_needs_recall: bool | None = None
    gate_intent_type: str = ""
    final_recall_decision: str = ""
    fallback_used: str = "none"
    recall_skip_source: str = ""
    query_plan_source: str = ""
    candidate_count: int = 0
    recall_attempted_but_zero_hit: bool = False
    reranker_selected_ids: list[str] = field(default_factory=list)
    reranker_final_reason: str = ""
    reranker_fallback: str = "none"
    reranker_per_item_reason: dict[str, str] = field(default_factory=dict)
    reranker_per_item_scores: dict[str, dict[str, float | str | None]] = field(
        default_factory=dict
    )
    reranker_intent_label: str = ""
    reranker_selection_metrics: dict[str, float | None] = field(default_factory=dict)
    profile_reranker_selected_ids: list[str] = field(default_factory=list)
    episode_reranker_selected_ids: list[str] = field(default_factory=list)
    profile_reranker_final_reason: str = ""
    episode_reranker_final_reason: str = ""
    profile_reranker_per_item_scores: dict[str, dict[str, float | str | None]] = field(
        default_factory=dict
    )
    episode_reranker_per_item_scores: dict[str, dict[str, float | str | None]] = field(
        default_factory=dict
    )
    dual_recall_plan: dict = field(default_factory=dict)
    stage3_profile: dict = field(default_factory=dict)
    stage3_episode: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "stage0_decision": self.stage0_decision,
            "stage0_reason": self.stage0_reason,
            "stage0_matched_rule": self.stage0_matched_rule,
            "stage0_signals": {
                name: list(hits) for name, hits in self.stage0_signals.items()
            },
            "gate_needs_recall": self.gate_needs_recall,
            "gate_intent_type": self.gate_intent_type,
            "final_recall_decision": self.final_recall_decision,
            "fallback_used": self.fallback_used,
            "recall_skip_source": self.recall_skip_source,
            "query_plan_source": self.query_plan_source,
            "candidate_count": self.candidate_count,
            "recall_attempted_but_zero_hit": self.recall_attempted_but_zero_hit,
            "reranker_selected_ids": list(self.reranker_selected_ids),
            "reranker_final_reason": self.reranker_final_reason,
            "reranker_fallback": self.reranker_fallback,
            "reranker_per_item_reason": dict(self.reranker_per_item_reason),
            "reranker_per_item_scores": {
                item_id: dict(scores)
                for item_id, scores in self.reranker_per_item_scores.items()
            },
            "reranker_intent_label": self.reranker_intent_label,
            "reranker_selection_metrics": dict(self.reranker_selection_metrics),
            "profile_reranker_selected_ids": list(
                self.profile_reranker_selected_ids
            ),
            "episode_reranker_selected_ids": list(
                self.episode_reranker_selected_ids
            ),
            "profile_reranker_final_reason": self.profile_reranker_final_reason,
            "episode_reranker_final_reason": self.episode_reranker_final_reason,
            "profile_reranker_per_item_scores": {
                item_id: dict(scores)
                for item_id, scores in self.profile_reranker_per_item_scores.items()
            },
            "episode_reranker_per_item_scores": {
                item_id: dict(scores)
                for item_id, scores in self.episode_reranker_per_item_scores.items()
            },
            "dual_recall_plan": dict(self.dual_recall_plan),
            "stage3_profile": dict(self.stage3_profile),
            "stage3_episode": dict(self.stage3_episode),
            "timestamp": self.timestamp,
        }


@dataclass
class SessionStats:
    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    memory_hits: list[MemoryHitRecord] = field(default_factory=list)
    recall_telemetry: list[RecallTelemetryRecord] = field(default_factory=list)

    def record_llm_call(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: float,
        phase: int,
        iteration: int,
        metadata: dict | None = None,
    ) -> None:
        self.llm_calls.append(
            LLMCallRecord(
                provider=provider,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
                phase=phase,
                iteration=iteration,
                metadata=dict(metadata or {}),
            )
        )

    def record_tool_call(
        self,
        *,
        tool_name: str,
        duration_ms: float,
        status: str,
        error_code: str | None,
        phase: int,
        parallel_group: int | None = None,
        arguments_preview: str = "",
        result_preview: str = "",
        suggestion: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.tool_calls.append(
            ToolCallRecord(
                tool_name=tool_name,
                duration_ms=duration_ms,
                status=status,
                error_code=error_code,
                phase=phase,
                arguments_preview=arguments_preview,
                result_preview=result_preview,
                parallel_group=parallel_group,
                suggestion=suggestion,
                metadata=dict(metadata or {}),
            )
        )

    @property
    def total_input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.llm_calls)

    @property
    def total_output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.llm_calls)

    @property
    def total_llm_duration_ms(self) -> float:
        return sum(r.duration_ms for r in self.llm_calls)

    @property
    def total_tool_duration_ms(self) -> float:
        return sum(r.duration_ms for r in self.tool_calls)

    @property
    def estimated_cost_usd(self) -> float:
        total = 0.0
        for r in self.llm_calls:
            total += estimate_llm_cost_usd(
                r.model,
                r.input_tokens,
                r.output_tokens,
                r.metadata,
            )
        return total

    def to_dict(self) -> dict:
        by_model: dict[str, dict] = defaultdict(
            lambda: {
                "input_tokens": 0,
                "output_tokens": 0,
                "calls": 0,
                "duration_ms": 0.0,
            }
        )
        for r in self.llm_calls:
            entry = by_model[r.model]
            entry["input_tokens"] += r.input_tokens
            entry["output_tokens"] += r.output_tokens
            entry["calls"] += 1
            entry["duration_ms"] += r.duration_ms
            for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
                if key in r.metadata:
                    entry[key] = entry.get(key, 0) + int(r.metadata.get(key) or 0)

        for entry in by_model.values():
            hit = entry.get("prompt_cache_hit_tokens")
            miss = entry.get("prompt_cache_miss_tokens")
            if hit is not None and miss is not None and hit + miss > 0:
                entry["prompt_cache_hit_rate"] = hit / (hit + miss)

        by_tool: dict[str, dict] = defaultdict(
            lambda: {"calls": 0, "duration_ms": 0.0, "errors": 0}
        )
        for r in self.tool_calls:
            entry = by_tool[r.tool_name]
            entry["calls"] += 1
            entry["duration_ms"] += r.duration_ms
            if r.status == "error":
                entry["errors"] += 1

        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_llm_duration_ms": round(self.total_llm_duration_ms, 1),
            "total_tool_duration_ms": round(self.total_tool_duration_ms, 1),
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "llm_call_count": len(self.llm_calls),
            "tool_call_count": len(self.tool_calls),
            "memory_hit_count": len(self.memory_hits),
            "last_memory_recall": (
                self.recall_telemetry[-1].to_dict() if self.recall_telemetry else None
            ),
            "by_model": dict(by_model),
            "by_tool": dict(by_tool),
        }
