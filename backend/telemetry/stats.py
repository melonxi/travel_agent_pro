# backend/telemetry/stats.py
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field


# Per-1M-token pricing (USD)
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
    "deepseek-chat": {"input": 0.14, "output": 0.28},
    "deepseek-r1": {"input": 0.55, "output": 2.19},
}


def _lookup_pricing(model: str) -> dict[str, float] | None:
    model_lower = model.lower()
    for prefix, pricing in _PRICING.items():
        if model_lower.startswith(prefix):
            return pricing
    return None


@dataclass
class LLMCallRecord:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: float
    phase: int
    iteration: int
    timestamp: float = field(default_factory=time.time)


@dataclass
class ToolCallRecord:
    tool_name: str
    duration_ms: float
    status: str
    error_code: str | None
    phase: int
    timestamp: float = field(default_factory=time.time)


@dataclass
class SessionStats:
    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)

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
    ) -> None:
        self.llm_calls.append(LLMCallRecord(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            phase=phase,
            iteration=iteration,
        ))

    def record_tool_call(
        self,
        *,
        tool_name: str,
        duration_ms: float,
        status: str,
        error_code: str | None,
        phase: int,
    ) -> None:
        self.tool_calls.append(ToolCallRecord(
            tool_name=tool_name,
            duration_ms=duration_ms,
            status=status,
            error_code=error_code,
            phase=phase,
        ))

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
            pricing = _lookup_pricing(r.model)
            if pricing:
                total += (r.input_tokens / 1_000_000) * pricing["input"]
                total += (r.output_tokens / 1_000_000) * pricing["output"]
        return total

    def to_dict(self) -> dict:
        by_model: dict[str, dict] = defaultdict(lambda: {"input_tokens": 0, "output_tokens": 0, "calls": 0, "duration_ms": 0.0})
        for r in self.llm_calls:
            entry = by_model[r.model]
            entry["input_tokens"] += r.input_tokens
            entry["output_tokens"] += r.output_tokens
            entry["calls"] += 1
            entry["duration_ms"] += r.duration_ms

        by_tool: dict[str, dict] = defaultdict(lambda: {"calls": 0, "duration_ms": 0.0, "errors": 0})
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
            "by_model": dict(by_model),
            "by_tool": dict(by_tool),
        }
