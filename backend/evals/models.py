"""Data models for the evaluation pipeline."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AssertionType(Enum):
    PHASE_REACHED = "phase_reached"
    STATE_FIELD_SET = "state_field_set"
    TOOL_CALLED = "tool_called"
    TOOL_NOT_CALLED = "tool_not_called"
    CONTAINS_TEXT = "contains_text"
    NOT_CONTAINS_TEXT = "not_contains_text"
    BUDGET_WITHIN = "budget_within"
    MEMORY_RECALL_FIELD = "memory_recall_field"


@dataclass
class Assertion:
    type: AssertionType
    target: str
    value: Any = None


@dataclass
class GoldenCase:
    id: str
    name: str
    description: str
    difficulty: str  # easy / medium / hard / infeasible
    messages: list[dict[str, str]]  # [{role, content}, ...]
    assertions: list[Assertion]
    tags: list[str] = field(default_factory=list)


@dataclass
class EvalExecution:
    """Execution trace collected from running one golden case."""

    state: dict[str, Any]
    tool_calls: list[str]
    responses: list[str]
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    assertions_passed: int
    assertions_total: int
    failures: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    error: str | None = None
    difficulty: str = ""
    tags: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class SuiteResult:
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    results: list[CaseResult] = field(default_factory=list)
    duration_ms: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total > 0 else 0.0

    def summary(self) -> str:
        return (
            f"Eval: {self.passed}/{self.total} passed "
            f"({self.pass_rate:.0%}), "
            f"{self.failed} failed, {self.errors} errors, "
            f"{self.duration_ms:.0f}ms"
        )


@dataclass
class StabilityMetrics:
    """Aggregated stability metrics for a single case over k runs."""

    case_id: str
    k: int
    pass_rate: float
    assertion_consistency: dict[str, float]
    tool_overlap_ratio: float
    cost_stats: dict[str, float]
    duration_stats: dict[str, float]
    runs: list[CaseResult] = field(default_factory=list)


@dataclass
class StabilitySuiteResult:
    """Stability results for all cases."""

    total_cases: int = 0
    k: int = 3
    results: list[StabilityMetrics] = field(default_factory=list)
    overall_pass_rate: float = 0.0
    unstable_cases: list[str] = field(default_factory=list)
    highly_unstable_cases: list[str] = field(default_factory=list)
