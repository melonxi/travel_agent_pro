"""Eval runner — loads golden cases from YAML files and evaluates assertions."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml

from evals.models import (
    Assertion,
    AssertionType,
    CaseResult,
    GoldenCase,
    SuiteResult,
)


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

    if t == AssertionType.BUDGET_WITHIN:
        total_cost = state.get("total_cost", 0)
        budget = state.get("budget_total", 0)
        margin = float(assertion.value or 1.1)
        if budget > 0 and total_cost <= budget * margin:
            return True, ""
        return False, f"cost {total_cost} exceeds budget {budget}*{margin}"

    return False, f"unknown assertion type: {t}"


def run_case_offline(
    case: GoldenCase,
    state: dict[str, Any],
    tool_calls: list[str],
    responses: list[str],
) -> CaseResult:
    """Evaluate a golden case against pre-collected execution data."""
    start = time.monotonic()
    passed_count = 0
    failures: list[str] = []
    for assertion in case.assertions:
        ok, reason = evaluate_assertion(assertion, state, tool_calls, responses)
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
    )


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
    return suite
