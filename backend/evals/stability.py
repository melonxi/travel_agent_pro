"""pass@k stability evaluation — run cases k times and compute consistency metrics."""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from evals.models import (
    CaseResult,
    EvalExecution,
    GoldenCase,
    StabilityMetrics,
    StabilitySuiteResult,
)
from evals.runner import GoldenCaseExecutor, evaluate_assertion, run_case_offline


def _compute_stats(values: list[float]) -> dict[str, float]:
    """Compute min/max/mean/stddev for a list of floats."""
    if not values:
        return {"min": 0.0, "max": 0.0, "mean": 0.0, "stddev": 0.0}
    return {
        "min": min(values),
        "max": max(values),
        "mean": statistics.mean(values),
        "stddev": statistics.stdev(values) if len(values) >= 2 else 0.0,
    }


def _compute_tool_overlap(tool_call_sets: list[set[str]]) -> float:
    """Compute intersection/union ratio across k runs' tool call sets."""
    if not tool_call_sets:
        return 1.0
    union = set()
    intersection: set[str] | None = None
    for s in tool_call_sets:
        union |= s
        intersection = s if intersection is None else intersection & s
    if not union:
        return 1.0
    return len(intersection or set()) / len(union)


def run_stability(
    case: GoldenCase,
    executor: GoldenCaseExecutor,
    k: int = 3,
) -> StabilityMetrics:
    """Run a single case k times and compute stability metrics."""
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    executions: list[EvalExecution] = []
    case_results: list[CaseResult] = []

    for _ in range(k):
        start = time.monotonic()
        execution = executor(case)
        executions.append(execution)
        result = run_case_offline(
            case,
            execution.state,
            execution.tool_calls,
            execution.responses,
            stats=execution.stats,
        )
        result.duration_ms = (time.monotonic() - start) * 1000
        case_results.append(result)

    # pass_rate
    pass_count = sum(1 for r in case_results if r.passed)
    pass_rate = pass_count / k

    # assertion_consistency — per-assertion pass rate across k runs
    assertion_consistency: dict[str, float] = {}
    for assertion in case.assertions:
        key = f"{assertion.type.value}:{assertion.target}"
        passes = 0
        for exc in executions:
            ok, _ = evaluate_assertion(
                assertion, exc.state, exc.tool_calls, exc.responses
            )
            if ok:
                passes += 1
        assertion_consistency[key] = passes / k

    # tool_overlap_ratio
    tool_sets = [set(exc.tool_calls) for exc in executions]
    tool_overlap_ratio = _compute_tool_overlap(tool_sets)

    # cost_stats and duration_stats
    costs = [r.stats.get("estimated_cost_usd", 0.0) for r in case_results]
    durations = [r.duration_ms for r in case_results]

    return StabilityMetrics(
        case_id=case.id,
        k=k,
        pass_rate=pass_rate,
        assertion_consistency=assertion_consistency,
        tool_overlap_ratio=tool_overlap_ratio,
        cost_stats=_compute_stats(costs),
        duration_stats=_compute_stats(durations),
        runs=case_results,
    )


def run_stability_suite(
    cases: list[GoldenCase],
    executor: GoldenCaseExecutor,
    k: int = 3,
) -> StabilitySuiteResult:
    """Run all cases through stability evaluation and aggregate results."""
    if not cases:
        return StabilitySuiteResult(total_cases=0, k=k)

    results: list[StabilityMetrics] = []
    for case in cases:
        metrics = run_stability(case, executor, k=k)
        results.append(metrics)

    rates = [m.pass_rate for m in results]
    overall = statistics.mean(rates) if rates else 0.0
    unstable = [m.case_id for m in results if m.pass_rate < 1.0]
    highly_unstable = [m.case_id for m in results if m.pass_rate < 0.6]

    return StabilitySuiteResult(
        total_cases=len(cases),
        k=k,
        results=results,
        overall_pass_rate=overall,
        unstable_cases=unstable,
        highly_unstable_cases=highly_unstable,
    )
