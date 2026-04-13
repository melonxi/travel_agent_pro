"""Tests for pass@k stability evaluation."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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
from evals.stability import run_stability, run_stability_suite, save_stability_report


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


class TestSaveStabilityReport:
    def _make_suite(self) -> StabilitySuiteResult:
        """Build a minimal suite result for report testing."""
        metrics = StabilityMetrics(
            case_id="easy-001",
            k=3,
            pass_rate=1.0,
            assertion_consistency={
                "state_field_set:destination": 1.0,
                "tool_called:search_flights": 1.0,
            },
            tool_overlap_ratio=1.0,
            cost_stats={"min": 0.08, "max": 0.12, "mean": 0.10, "stddev": 0.02},
            duration_stats={"min": 100.0, "max": 300.0, "mean": 200.0, "stddev": 100.0},
            runs=[],
        )
        unstable_metrics = StabilityMetrics(
            case_id="hard-001",
            k=3,
            pass_rate=0.33,
            assertion_consistency={"tool_called:search_flights": 0.33},
            tool_overlap_ratio=0.5,
            cost_stats={"min": 0.20, "max": 0.30, "mean": 0.25, "stddev": 0.05},
            duration_stats={"min": 500.0, "max": 900.0, "mean": 700.0, "stddev": 200.0},
            runs=[],
        )
        return StabilitySuiteResult(
            total_cases=2,
            k=3,
            results=[metrics, unstable_metrics],
            overall_pass_rate=0.665,
            unstable_cases=["hard-001"],
            highly_unstable_cases=["hard-001"],
        )

    def test_json_report_written(self, tmp_path):
        suite = self._make_suite()
        json_path, md_path = save_stability_report(suite, tmp_path / "report")

        assert json_path.exists()
        assert md_path.exists()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["k"] == 3
        assert data["total_cases"] == 2
        assert data["overall_pass_rate"] == pytest.approx(0.665)
        assert len(data["results"]) == 2
        assert data["results"][0]["case_id"] == "easy-001"

    def test_markdown_report_written(self, tmp_path):
        suite = self._make_suite()
        _, md_path = save_stability_report(suite, tmp_path / "report")

        assert md_path.exists()
        content = md_path.read_text(encoding="utf-8")
        assert "pass@k 稳定性评估报告" in content
        assert "easy-001" in content
        assert "hard-001" in content
        assert "0.33" in content

    def test_markdown_contains_high_variance_section(self, tmp_path):
        suite = self._make_suite()
        _, md_path = save_stability_report(suite, tmp_path / "report")

        content = md_path.read_text(encoding="utf-8")
        assert "高方差断言" in content
        assert "tool_called:search_flights" in content

    def test_creates_parent_directories(self, tmp_path):
        suite = self._make_suite()
        nested = tmp_path / "a" / "b" / "report"
        json_path, md_path = save_stability_report(suite, nested)

        assert json_path.exists()
        assert md_path.exists()


class TestEvalStabilityCli:
    def _script_path(self) -> Path:
        return Path(__file__).resolve().parents[2] / "scripts" / "eval-stability.py"

    def test_help_outputs_usage(self):
        result = subprocess.run(
            [sys.executable, str(self._script_path()), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert "--k" in result.stdout
        assert "--base-url" in result.stdout
        assert "--cases" in result.stdout
        assert "--difficulty" in result.stdout
        assert "--output" in result.stdout

    def test_mock_mode_generates_reports(self, tmp_path):
        output = tmp_path / "stability-report"
        result = subprocess.run(
            [
                sys.executable,
                str(self._script_path()),
                "--mock",
                "--cases",
                "easy-001",
                "--k",
                "2",
                "--output",
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert output.with_suffix(".json").exists()
        assert output.with_suffix(".md").exists()
        data = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
        assert data["k"] == 2
        assert data["total_cases"] == 1
        assert data["results"][0]["case_id"] == "easy-001"
