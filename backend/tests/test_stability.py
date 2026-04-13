"""Tests for pass@k stability evaluation."""
from __future__ import annotations

import math

import pytest

from evals.models import (
    Assertion,
    AssertionType,
    CaseResult,
    EvalExecution,
    GoldenCase,
    StabilityMetrics,
    StabilitySuiteResult,
)
from evals.stability import run_stability, run_stability_suite


def _make_case(
    case_id: str = "test-case",
    difficulty: str = "easy",
    assertions: list[Assertion] | None = None,
) -> GoldenCase:
    return GoldenCase(
        id=case_id,
        name=f"Test {case_id}",
        description="",
        difficulty=difficulty,
        messages=[{"role": "user", "content": "hello"}],
        assertions=assertions
        or [
            Assertion(type=AssertionType.STATE_FIELD_SET, target="destination", value="東京"),
            Assertion(type=AssertionType.TOOL_CALLED, target="search_flights"),
        ],
    )


def _make_executor(executions: list[EvalExecution]):
    """Return an executor that yields pre-defined executions in order."""
    it = iter(executions)
    def executor(case: GoldenCase) -> EvalExecution:
        return next(it)
    return executor


class TestRunStabilityAllPass:
    """k=3, all runs pass all assertions."""

    def test_pass_rate_is_one(self):
        case = _make_case()
        execs = [
            EvalExecution(
                state={"destination": "東京"},
                tool_calls=["search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.10},
            )
            for _ in range(3)
        ]
        result = run_stability(case, _make_executor(execs), k=3)

        assert result.pass_rate == 1.0
        assert result.k == 3
        assert result.case_id == "test-case"

    def test_assertion_consistency_all_one(self):
        case = _make_case()
        execs = [
            EvalExecution(
                state={"destination": "東京"},
                tool_calls=["search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.10},
            )
            for _ in range(3)
        ]
        result = run_stability(case, _make_executor(execs), k=3)

        assert result.assertion_consistency["state_field_set:destination"] == 1.0
        assert result.assertion_consistency["tool_called:search_flights"] == 1.0

    def test_tool_overlap_is_one(self):
        case = _make_case()
        execs = [
            EvalExecution(
                state={"destination": "東京"},
                tool_calls=["search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.10},
            )
            for _ in range(3)
        ]
        result = run_stability(case, _make_executor(execs), k=3)

        assert result.tool_overlap_ratio == 1.0


class TestRunStabilityPartialPass:
    """k=5, 3 pass + 2 fail."""

    def test_pass_rate_is_0_6(self):
        case = _make_case()
        passing = EvalExecution(
            state={"destination": "東京"},
            tool_calls=["search_flights"],
            responses=["ok"],
            stats={"estimated_cost_usd": 0.10},
        )
        failing = EvalExecution(
            state={"destination": "大阪"},
            tool_calls=["search_flights"],
            responses=["ok"],
            stats={"estimated_cost_usd": 0.15},
        )
        execs = [passing, passing, passing, failing, failing]
        result = run_stability(case, _make_executor(execs), k=5)

        assert result.pass_rate == pytest.approx(0.6)

    def test_assertion_consistency_reflects_difference(self):
        case = _make_case()
        passing = EvalExecution(
            state={"destination": "東京"},
            tool_calls=["search_flights"],
            responses=["ok"],
            stats={"estimated_cost_usd": 0.10},
        )
        failing = EvalExecution(
            state={"destination": "大阪"},
            tool_calls=["search_flights"],
            responses=["ok"],
            stats={"estimated_cost_usd": 0.15},
        )
        execs = [passing, passing, passing, failing, failing]
        result = run_stability(case, _make_executor(execs), k=5)

        assert result.assertion_consistency["state_field_set:destination"] == pytest.approx(0.6)
        assert result.assertion_consistency["tool_called:search_flights"] == 1.0


class TestRunStabilitySingleRun:
    """k=1 edge case."""

    def test_k1_pass(self):
        case = _make_case()
        execs = [
            EvalExecution(
                state={"destination": "東京"},
                tool_calls=["search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.10},
            )
        ]
        result = run_stability(case, _make_executor(execs), k=1)

        assert result.pass_rate == 1.0
        assert result.cost_stats["stddev"] == 0.0
        assert result.duration_stats["stddev"] == 0.0

    def test_k1_fail(self):
        case = _make_case()
        execs = [
            EvalExecution(
                state={},
                tool_calls=[],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.05},
            )
        ]
        result = run_stability(case, _make_executor(execs), k=1)

        assert result.pass_rate == 0.0


class TestToolOverlap:
    """Tool overlap ratio edge cases."""

    def test_identical_tools(self):
        case = _make_case()
        execs = [
            EvalExecution(
                state={"destination": "東京"},
                tool_calls=["web_search", "update_plan_state"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.10},
            )
            for _ in range(3)
        ]
        result = run_stability(case, _make_executor(execs), k=3)
        assert result.tool_overlap_ratio == 1.0

    def test_disjoint_tools(self):
        case = _make_case()
        execs = [
            EvalExecution(
                state={"destination": "東京"},
                tool_calls=["web_search"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.10},
            ),
            EvalExecution(
                state={"destination": "東京"},
                tool_calls=["web_search", "search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.12},
            ),
            EvalExecution(
                state={"destination": "東京"},
                tool_calls=["search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.11},
            ),
        ]
        result = run_stability(case, _make_executor(execs), k=3)
        # intersection = {} (no tool is in ALL 3), union = {web_search, search_flights}
        assert result.tool_overlap_ratio == 0.0

    def test_no_tools_any_run(self):
        case = _make_case(assertions=[])
        execs = [
            EvalExecution(state={}, tool_calls=[], responses=["ok"], stats={})
            for _ in range(3)
        ]
        result = run_stability(case, _make_executor(execs), k=3)
        assert result.tool_overlap_ratio == 1.0


class TestCostAndDurationStats:
    def test_cost_stats(self):
        case = _make_case(assertions=[])
        execs = [
            EvalExecution(state={}, tool_calls=[], responses=["ok"], stats={"estimated_cost_usd": 0.1}),
            EvalExecution(state={}, tool_calls=[], responses=["ok"], stats={"estimated_cost_usd": 0.2}),
            EvalExecution(state={}, tool_calls=[], responses=["ok"], stats={"estimated_cost_usd": 0.3}),
        ]
        result = run_stability(case, _make_executor(execs), k=3)

        assert result.cost_stats["min"] == pytest.approx(0.1)
        assert result.cost_stats["max"] == pytest.approx(0.3)
        assert result.cost_stats["mean"] == pytest.approx(0.2)
        assert result.cost_stats["stddev"] == pytest.approx(0.1)

    def test_missing_cost_defaults_to_zero(self):
        case = _make_case(assertions=[])
        execs = [
            EvalExecution(state={}, tool_calls=[], responses=["ok"], stats={}),
            EvalExecution(state={}, tool_calls=[], responses=["ok"], stats={}),
        ]
        result = run_stability(case, _make_executor(execs), k=2)
        assert result.cost_stats["mean"] == 0.0


class TestRunStabilitySuite:
    def test_suite_aggregation(self):
        case1 = _make_case(case_id="stable-case")
        case2 = _make_case(case_id="unstable-case")

        execs1 = [
            EvalExecution(
                state={"destination": "東京"},
                tool_calls=["search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.1},
            )
            for _ in range(3)
        ]
        execs2 = [
            EvalExecution(
                state={"destination": "東京"},
                tool_calls=["search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.1},
            ),
            EvalExecution(
                state={},
                tool_calls=[],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.2},
            ),
            EvalExecution(
                state={},
                tool_calls=[],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.15},
            ),
        ]

        all_execs = execs1 + execs2
        executor = _make_executor(all_execs)

        suite = run_stability_suite([case1, case2], executor, k=3)

        assert suite.total_cases == 2
        assert suite.k == 3
        assert suite.overall_pass_rate == pytest.approx((1.0 + 1 / 3) / 2)
        assert "unstable-case" in suite.unstable_cases
        assert "unstable-case" in suite.highly_unstable_cases
        assert "stable-case" not in suite.unstable_cases

    def test_suite_empty_cases(self):
        suite = run_stability_suite([], _make_executor([]), k=3)
        assert suite.total_cases == 0
        assert suite.overall_pass_rate == 0.0
