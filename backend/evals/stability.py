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


def _case_result_to_dict(result: CaseResult) -> dict[str, Any]:
    """Serialize one CaseResult for report JSON."""
    return {
        "case_id": result.case_id,
        "passed": result.passed,
        "assertions_passed": result.assertions_passed,
        "assertions_total": result.assertions_total,
        "failures": result.failures,
        "duration_ms": round(result.duration_ms, 1),
        "error": result.error,
        "difficulty": result.difficulty,
        "stats": result.stats,
    }


def _stability_metrics_to_dict(metrics: StabilityMetrics) -> dict[str, Any]:
    """Serialize StabilityMetrics to a JSON-safe dict."""
    return {
        "case_id": metrics.case_id,
        "k": metrics.k,
        "pass_rate": metrics.pass_rate,
        "assertion_consistency": metrics.assertion_consistency,
        "tool_overlap_ratio": metrics.tool_overlap_ratio,
        "cost_stats": metrics.cost_stats,
        "duration_stats": metrics.duration_stats,
        "runs": [_case_result_to_dict(result) for result in metrics.runs],
    }


def _format_unstable_assertions(metrics: StabilityMetrics) -> str:
    unstable = [
        f"{key} ({value:.2f})"
        for key, value in metrics.assertion_consistency.items()
        if value < 1.0
    ]
    return ", ".join(unstable) if unstable else "—"


def _case_difficulty(metrics: StabilityMetrics) -> str:
    if metrics.runs:
        return metrics.runs[0].difficulty
    return ""


def save_stability_report(
    suite: StabilitySuiteResult,
    output_path: str | Path,
) -> tuple[Path, Path]:
    """Write JSON and Markdown stability reports and return their paths."""
    base = Path(output_path)
    base.parent.mkdir(parents=True, exist_ok=True)
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")

    payload = {
        "total_cases": suite.total_cases,
        "k": suite.k,
        "overall_pass_rate": suite.overall_pass_rate,
        "unstable_cases": suite.unstable_cases,
        "highly_unstable_cases": suite.highly_unstable_cases,
        "results": [_stability_metrics_to_dict(metrics) for metrics in suite.results],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = [
        "# pass@k 稳定性评估报告",
        "",
        f"- 运行时间：{now}",
        f"- k = {suite.k}",
        f"- 覆盖 case 数：{suite.total_cases}",
        "",
        "## 总览",
        "",
        "| 指标 | 值 |",
        "|------|-----|",
        f"| 总体 pass@k | {suite.overall_pass_rate:.2f} |",
        f"| 不稳定 case（pass_rate < 1.0） | {len(suite.unstable_cases)} |",
        f"| 高度不稳定 case（pass_rate < 0.6） | {len(suite.highly_unstable_cases)} |",
        "",
        "## 按 Case 详情",
        "",
        f"| Case | 难度 | pass@{suite.k} | 不稳定断言 | 工具一致性 | 平均成本 |",
        "|------|------|--------|-----------|-----------|---------|",
    ]

    for metrics in suite.results:
        pass_count = int(metrics.pass_rate * metrics.k + 0.5)
        cost_mean = metrics.cost_stats.get("mean", 0.0)
        lines.append(
            f"| {metrics.case_id} | {_case_difficulty(metrics)} | {pass_count}/{metrics.k} "
            f"| {_format_unstable_assertions(metrics)} | "
            f"{metrics.tool_overlap_ratio:.2f} | ${cost_mean:.2f} |"
        )

    high_variance: list[tuple[str, str, float]] = []
    for metrics in suite.results:
        for key, value in metrics.assertion_consistency.items():
            if value < 0.6:
                high_variance.append((metrics.case_id, key, value))

    if high_variance:
        lines.extend(
            [
                "",
                "## 高方差断言（一致性 < 0.6）",
                "",
                "| Case | 断言 | 一致性 | 说明 |",
                "|------|------|--------|------|",
            ]
        )
        for case_id, assertion_key, value in high_variance:
            pass_count = int(value * suite.k + 0.5)
            lines.append(
                f"| {case_id} | {assertion_key} | {value:.2f} | "
                f"{pass_count}/{suite.k} 次通过 |"
            )

    lines.extend(
        [
            "",
            "## 成本与延迟统计",
            "",
            "| Case | 成本 min | 成本 max | 成本 stddev | 延迟 mean | 延迟 stddev |",
            "|------|---------|---------|------------|----------|------------|",
        ]
    )
    for metrics in suite.results:
        cost = metrics.cost_stats
        duration = metrics.duration_stats
        lines.append(
            f"| {metrics.case_id} | ${cost.get('min', 0.0):.2f} "
            f"| ${cost.get('max', 0.0):.2f} | ${cost.get('stddev', 0.0):.2f} "
            f"| {duration.get('mean', 0.0):.0f}ms | {duration.get('stddev', 0.0):.0f}ms |"
        )
    lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path
