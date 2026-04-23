"""Eval runner — loads golden cases, executes them, and evaluates assertions."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from evals.models import (
    Assertion,
    AssertionType,
    CaseResult,
    EvalExecution,
    GoldenCase,
    SuiteResult,
)

GoldenCaseExecutor = Callable[[GoldenCase], EvalExecution]


def load_golden_cases(directory: str | Path) -> list[GoldenCase]:
    """Load all .yaml/.yml golden case files from a directory."""
    dirpath = Path(directory)
    cases: list[GoldenCase] = []
    for filepath in sorted(dirpath.glob("*.y*ml")):
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not data:
            continue
        assertions = [
            Assertion(
                type=AssertionType(a["type"]),
                target=a.get("target", ""),
                value=a.get("value"),
            )
            for a in data.get("assertions", [])
        ]
        case = GoldenCase(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            difficulty=data.get("difficulty", "medium"),
            messages=data.get("messages", []),
            assertions=assertions,
            tags=data.get("tags", []),
        )
        cases.append(case)
    return cases


def evaluate_assertion(
    assertion: Assertion,
    state: dict[str, Any],
    tool_calls: list[str],
    responses: list[str],
    stats: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Evaluate a single assertion against collected state.
    
    Returns (passed, reason).
    """
    t = assertion.type
    if t == AssertionType.PHASE_REACHED:
        current = state.get("phase", 0)
        expected = int(assertion.value)
        if current >= expected:
            return True, ""
        return False, f"expected phase>={expected}, got {current}"

    if t == AssertionType.STATE_FIELD_SET:
        val = state.get(assertion.target)
        if assertion.value is not None:
            if str(val) == str(assertion.value):
                return True, ""
            return False, f"{assertion.target}={val}, expected {assertion.value}"
        if val is not None:
            return True, ""
        return False, f"{assertion.target} is not set"

    if t == AssertionType.TOOL_CALLED:
        if assertion.target in tool_calls:
            return True, ""
        return False, f"tool {assertion.target} was not called"

    if t == AssertionType.TOOL_NOT_CALLED:
        if assertion.target not in tool_calls:
            return True, ""
        return False, f"tool {assertion.target} was unexpectedly called"

    if t == AssertionType.CONTAINS_TEXT:
        text = assertion.target
        if any(text in r for r in responses):
            return True, ""
        return False, f"text '{text}' not found in responses"

    if t == AssertionType.NOT_CONTAINS_TEXT:
        text = assertion.target
        offenders = [r for r in responses if text in r]
        if not offenders:
            return True, ""
        return False, f"text '{text}' unexpectedly appeared in response(s)"

    if t == AssertionType.BUDGET_WITHIN:
        total_cost = state.get("total_cost", 0)
        budget = state.get("budget_total", 0)
        margin = float(assertion.value or 1.1)
        if budget > 0 and total_cost <= budget * margin:
            return True, ""
        return False, f"cost {total_cost} exceeds budget {budget}*{margin}"

    if t == AssertionType.MEMORY_RECALL_FIELD:
        recall = (stats or {}).get("last_memory_recall")
        if not isinstance(recall, dict):
            return False, "last_memory_recall is not available"
        val = _get_nested_value(recall, assertion.target)
        if assertion.value is not None:
            if str(val) == str(assertion.value):
                return True, ""
            return False, (
                f"memory_recall.{assertion.target}={val}, "
                f"expected {assertion.value}"
            )
        if val is not None:
            return True, ""
        return False, f"memory_recall.{assertion.target} is not set"

    return False, f"unknown assertion type: {t}"


def _get_nested_value(source: dict[str, Any], path: str) -> Any:
    value: Any = source
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def run_case_offline(
    case: GoldenCase,
    state: dict[str, Any],
    tool_calls: list[str],
    responses: list[str],
    stats: dict[str, Any] | None = None,
) -> CaseResult:
    """Evaluate a golden case against pre-collected execution data."""
    start = time.monotonic()
    passed_count = 0
    failures: list[str] = []
    stats = stats or {}
    for assertion in case.assertions:
        ok, reason = evaluate_assertion(
            assertion, state, tool_calls, responses, stats=stats
        )
        if ok:
            passed_count += 1
        else:
            failures.append(f"[{assertion.type.value}] {reason}")
    elapsed = (time.monotonic() - start) * 1000
    return CaseResult(
        case_id=case.id,
        passed=len(failures) == 0,
        assertions_passed=passed_count,
        assertions_total=len(case.assertions),
        failures=failures,
        duration_ms=elapsed,
        difficulty=case.difficulty,
        tags=list(case.tags),
        stats=stats,
    )


def run_case(case: GoldenCase, executor: GoldenCaseExecutor) -> CaseResult:
    """Execute one golden case and evaluate its assertions."""
    start = time.monotonic()
    try:
        execution = executor(case)
    except Exception as exc:
        elapsed = (time.monotonic() - start) * 1000
        return CaseResult(
            case_id=case.id,
            passed=False,
            assertions_passed=0,
            assertions_total=len(case.assertions),
            duration_ms=elapsed,
            error=f"{type(exc).__name__}: {exc}",
            difficulty=case.difficulty,
            tags=list(case.tags),
        )

    result = run_case_offline(
        case,
        execution.state,
        execution.tool_calls,
        execution.responses,
        stats=execution.stats,
    )
    result.duration_ms = (time.monotonic() - start) * 1000
    return result


def run_suite_offline(
    cases: list[GoldenCase],
    results_map: dict[str, tuple[dict, list[str], list[str]]],
) -> SuiteResult:
    """Run all cases against pre-collected data.
    
    results_map: case_id → (state_dict, tool_calls, responses)
    """
    start = time.monotonic()
    suite = SuiteResult(total=len(cases))
    for case in cases:
        if case.id not in results_map:
            suite.errors += 1
            suite.results.append(CaseResult(
                case_id=case.id,
                passed=False,
                assertions_passed=0,
                assertions_total=len(case.assertions),
                error="No execution data found",
                difficulty=case.difficulty,
                tags=list(case.tags),
            ))
            continue
        state, tools, responses = results_map[case.id]
        result = run_case_offline(case, state, tools, responses)
        suite.results.append(result)
        if result.passed:
            suite.passed += 1
        else:
            suite.failed += 1
    suite.duration_ms = (time.monotonic() - start) * 1000
    suite.metrics = build_suite_metrics(suite)
    return suite


def run_suite(cases: list[GoldenCase], executor: GoldenCaseExecutor) -> SuiteResult:
    """Execute and evaluate a suite of golden cases."""
    start = time.monotonic()
    suite = SuiteResult(total=len(cases))
    for case in cases:
        result = run_case(case, executor)
        suite.results.append(result)
        if result.error:
            suite.errors += 1
        elif result.passed:
            suite.passed += 1
        else:
            suite.failed += 1
    suite.duration_ms = (time.monotonic() - start) * 1000
    suite.metrics = build_suite_metrics(suite)
    return suite


def build_suite_metrics(suite: SuiteResult) -> dict[str, Any]:
    """Build deterministic aggregate metrics for reports."""
    by_difficulty: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "passed": 0, "failed": 0, "errors": 0}
    )
    assertion_totals = {"passed": 0, "total": 0}
    stats_totals: dict[str, float] = defaultdict(float)

    for result in suite.results:
        difficulty = result.difficulty or "unknown"
        bucket = by_difficulty[difficulty]
        bucket["total"] += 1
        if result.error:
            bucket["errors"] += 1
        elif result.passed:
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1

        assertion_totals["passed"] += result.assertions_passed
        assertion_totals["total"] += result.assertions_total

        for key in (
            "total_input_tokens",
            "total_output_tokens",
            "total_llm_duration_ms",
            "total_tool_duration_ms",
            "estimated_cost_usd",
            "llm_call_count",
            "tool_call_count",
        ):
            value = result.stats.get(key)
            if isinstance(value, (int, float)):
                stats_totals[key] += value

    infeasible = dict(by_difficulty.get("infeasible", {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
    }))
    assertion_pass_rate = (
        assertion_totals["passed"] / assertion_totals["total"]
        if assertion_totals["total"]
        else 0.0
    )

    return {
        "by_difficulty": dict(by_difficulty),
        "assertions": {
            **assertion_totals,
            "pass_rate": assertion_pass_rate,
        },
        "infeasible": infeasible,
        "memory_recall": _build_memory_recall_metrics(suite.results),
        "stats": dict(stats_totals),
    }


def _build_memory_recall_metrics(results: list[CaseResult]) -> dict[str, Any]:
    recall_results = [result for result in results if "memory_recall" in result.tags]
    expected_recall = 0
    expected_skip = 0
    false_skip = 0
    false_recall = 0
    recall_enabled = 0
    recall_hits = 0
    zero_hits = 0

    for result in recall_results:
        tags = set(result.tags)
        recall = result.stats.get("last_memory_recall")
        if not isinstance(recall, dict):
            recall = {}
        final_decision = recall.get("final_recall_decision")
        did_recall = final_decision == "query_recall_enabled"

        if "expect_recall" in tags:
            expected_recall += 1
            if not did_recall:
                false_skip += 1
        if "expect_skip" in tags:
            expected_skip += 1
            if did_recall:
                false_recall += 1

        if did_recall:
            recall_enabled += 1
            candidate_count = recall.get("candidate_count")
            has_hit = isinstance(candidate_count, (int, float)) and candidate_count > 0
            if has_hit:
                recall_hits += 1
            if recall.get("recall_attempted_but_zero_hit") is True or not has_hit:
                zero_hits += 1

    return {
        "total": len(recall_results),
        "expected_recall": expected_recall,
        "expected_skip": expected_skip,
        "false_skip_rate": false_skip / expected_recall if expected_recall else 0.0,
        "false_recall_rate": false_recall / expected_skip if expected_skip else 0.0,
        "hit_rate_when_recall_enabled": (
            recall_hits / recall_enabled if recall_enabled else 0.0
        ),
        "recall_attempted_but_zero_hit_rate": (
            zero_hits / recall_enabled if recall_enabled else 0.0
        ),
    }


def suite_to_dict(suite: SuiteResult) -> dict[str, Any]:
    """Serialize suite result for JSON reports."""
    return {
        "summary": {
            "total": suite.total,
            "passed": suite.passed,
            "failed": suite.failed,
            "errors": suite.errors,
            "pass_rate": suite.pass_rate,
            "duration_ms": round(suite.duration_ms, 1),
        },
        "metrics": suite.metrics or build_suite_metrics(suite),
        "results": [
            {
                "case_id": result.case_id,
                "difficulty": result.difficulty,
                "passed": result.passed,
                "assertions_passed": result.assertions_passed,
                "assertions_total": result.assertions_total,
                "failures": result.failures,
                "duration_ms": round(result.duration_ms, 1),
                "error": result.error,
                "tags": list(result.tags),
                "stats": result.stats,
            }
            for result in suite.results
        ],
    }


def save_report(
    suite: SuiteResult,
    output_dir: str | Path = Path("evals/reports"),
    timestamp: str | None = None,
) -> Path:
    """Write a JSON eval report and return its path."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    stamp = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = output_path / f"eval-{stamp}.json"
    report_path.write_text(
        json.dumps(suite_to_dict(suite), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report_path
