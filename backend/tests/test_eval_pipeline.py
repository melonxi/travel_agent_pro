"""Tests for eval pipeline models and runner."""
from __future__ import annotations
import json
from pathlib import Path

import pytest
import yaml

from evals.models import Assertion, AssertionType, EvalExecution, GoldenCase, SuiteResult
from evals.runner import (
    evaluate_assertion,
    load_golden_cases,
    run_case,
    run_suite,
    run_case_offline,
    run_suite_offline,
    save_report,
)


class TestAssertionEvaluation:
    def test_phase_reached_pass(self):
        a = Assertion(type=AssertionType.PHASE_REACHED, target="", value=3)
        ok, _ = evaluate_assertion(a, {"phase": 5}, [], [])
        assert ok

    def test_phase_reached_fail(self):
        a = Assertion(type=AssertionType.PHASE_REACHED, target="", value=5)
        ok, reason = evaluate_assertion(a, {"phase": 3}, [], [])
        assert not ok
        assert "phase" in reason

    def test_state_field_set_pass(self):
        a = Assertion(type=AssertionType.STATE_FIELD_SET, target="destination", value=None)
        ok, _ = evaluate_assertion(a, {"destination": "东京"}, [], [])
        assert ok

    def test_state_field_set_fail(self):
        a = Assertion(type=AssertionType.STATE_FIELD_SET, target="destination", value=None)
        ok, _ = evaluate_assertion(a, {}, [], [])
        assert not ok

    def test_state_field_value_match(self):
        a = Assertion(type=AssertionType.STATE_FIELD_SET, target="destination", value="东京")
        ok, _ = evaluate_assertion(a, {"destination": "东京"}, [], [])
        assert ok

    def test_tool_called_pass(self):
        a = Assertion(type=AssertionType.TOOL_CALLED, target="search_flights")
        ok, _ = evaluate_assertion(a, {}, ["search_flights", "search_hotels"], [])
        assert ok

    def test_tool_not_called_pass(self):
        a = Assertion(type=AssertionType.TOOL_NOT_CALLED, target="book_flight")
        ok, _ = evaluate_assertion(a, {}, ["search_flights"], [])
        assert ok

    def test_contains_text_pass(self):
        a = Assertion(type=AssertionType.CONTAINS_TEXT, target="东京")
        ok, _ = evaluate_assertion(a, {}, [], ["推荐去东京旅行"])
        assert ok

    def test_budget_within_pass(self):
        a = Assertion(type=AssertionType.BUDGET_WITHIN, target="", value=1.1)
        ok, _ = evaluate_assertion(a, {"total_cost": 9000, "budget_total": 10000}, [], [])
        assert ok


class TestGoldenCaseLoader:
    def test_load_yaml_cases(self, tmp_path):
        case_data = {
            "id": "test-001",
            "name": "Basic Tokyo Trip",
            "description": "Test basic trip",
            "difficulty": "easy",
            "messages": [{"role": "user", "content": "我想去东京"}],
            "assertions": [
                {"type": "phase_reached", "target": "", "value": 3},
                {"type": "state_field_set", "target": "destination"},
            ],
            "tags": ["basic"],
        }
        yaml_file = tmp_path / "test-001.yaml"
        with open(yaml_file, "w") as f:
            yaml.dump(case_data, f, allow_unicode=True)

        cases = load_golden_cases(tmp_path)
        assert len(cases) == 1
        assert cases[0].id == "test-001"
        assert len(cases[0].assertions) == 2
        assert cases[0].difficulty == "easy"

    def test_empty_directory(self, tmp_path):
        cases = load_golden_cases(tmp_path)
        assert cases == []

    def test_golden_cases_use_registered_tool_names(self):
        cases = load_golden_cases(Path("evals/golden_cases"))
        known_tools = {
            "update_plan_state",
            "search_flights",
            "search_trains",
            "ai_travel_search",
            "search_accommodations",
            "get_poi_info",
            "calculate_route",
            "assemble_day_plan",
            "check_availability",
            "check_weather",
            "generate_summary",
            "quick_travel_search",
            "search_travel_services",
            "web_search",
            "xiaohongshu_search",
        }
        bad_targets = [
            (case.id, assertion.target)
            for case in cases
            for assertion in case.assertions
            if assertion.type in {AssertionType.TOOL_CALLED, AssertionType.TOOL_NOT_CALLED}
            and assertion.target not in known_tools
        ]

        assert bad_targets == []


class TestRunSuiteOffline:
    def test_full_suite_run(self):
        case = GoldenCase(
            id="t1",
            name="Test",
            description="",
            difficulty="easy",
            messages=[],
            assertions=[
                Assertion(type=AssertionType.PHASE_REACHED, target="", value=3),
                Assertion(type=AssertionType.TOOL_CALLED, target="search"),
            ],
        )
        results_map = {
            "t1": ({"phase": 5}, ["search"], []),
        }
        suite = run_suite_offline([case], results_map)
        assert suite.total == 1
        assert suite.passed == 1
        assert suite.pass_rate == 1.0

    def test_missing_execution_data(self):
        case = GoldenCase(
            id="t2",
            name="Missing",
            description="",
            difficulty="easy",
            messages=[],
            assertions=[],
        )
        suite = run_suite_offline([case], {})
        assert suite.errors == 1
        assert suite.results[0].difficulty == "easy"
        assert suite.metrics["by_difficulty"]["easy"]["errors"] == 1

    def test_suite_summary(self):
        s = SuiteResult(total=10, passed=8, failed=1, errors=1, duration_ms=1234)
        summary = s.summary()
        assert "8/10" in summary
        assert "80%" in summary


class TestExecutableRunner:
    def test_run_case_calls_executor_and_evaluates_assertions(self):
        case = GoldenCase(
            id="exec-001",
            name="Executable",
            description="",
            difficulty="easy",
            messages=[
                {"role": "user", "content": "我想去东京"},
                {"role": "user", "content": "预算15000"},
            ],
            assertions=[
                Assertion(type=AssertionType.STATE_FIELD_SET, target="destination", value="东京"),
                Assertion(type=AssertionType.TOOL_CALLED, target="search_flights"),
            ],
        )
        seen: list[str] = []

        def executor(received: GoldenCase) -> EvalExecution:
            seen.extend(message["content"] for message in received.messages)
            return EvalExecution(
                state={"phase": 3, "destination": "东京"},
                tool_calls=["search_flights"],
                responses=["已为你查询东京航班"],
                stats={"total_input_tokens": 100, "estimated_cost_usd": 0.001},
            )

        result = run_case(case, executor)

        assert result.passed is True
        assert result.assertions_passed == 2
        assert result.stats["estimated_cost_usd"] == 0.001
        assert seen == ["我想去东京", "预算15000"]

    def test_run_suite_aggregates_metrics_by_difficulty_and_infeasible(self):
        cases = [
            GoldenCase(
                id="easy-pass",
                name="Easy",
                description="",
                difficulty="easy",
                messages=[],
                assertions=[Assertion(type=AssertionType.STATE_FIELD_SET, target="destination")],
            ),
            GoldenCase(
                id="infeasible-fail",
                name="Impossible",
                description="",
                difficulty="infeasible",
                messages=[],
                assertions=[Assertion(type=AssertionType.CONTAINS_TEXT, target="预算")],
            ),
        ]

        def executor(case: GoldenCase) -> EvalExecution:
            if case.id == "easy-pass":
                return EvalExecution(
                    state={"destination": "东京"},
                    tool_calls=[],
                    responses=["东京行程"],
                    stats={"total_input_tokens": 10, "estimated_cost_usd": 0.01},
                )
            return EvalExecution(
                state={},
                tool_calls=[],
                responses=["请补充信息"],
                stats={"total_input_tokens": 20, "estimated_cost_usd": 0.02},
            )

        suite = run_suite(cases, executor)

        assert suite.total == 2
        assert suite.passed == 1
        assert suite.metrics["by_difficulty"]["easy"]["passed"] == 1
        assert suite.metrics["by_difficulty"]["infeasible"]["failed"] == 1
        assert suite.metrics["infeasible"]["total"] == 1
        assert suite.metrics["infeasible"]["passed"] == 0
        assert suite.metrics["stats"]["total_input_tokens"] == 30
        assert suite.metrics["stats"]["estimated_cost_usd"] == 0.03

    def test_save_report_writes_json_with_metrics_and_results(self, tmp_path):
        case = GoldenCase(
            id="report-001",
            name="Report",
            description="",
            difficulty="easy",
            messages=[],
            assertions=[],
        )

        suite = run_suite(
            [case],
            lambda _: EvalExecution(
                state={},
                tool_calls=[],
                responses=[],
                stats={"total_input_tokens": 7, "estimated_cost_usd": 0.004},
            ),
        )

        report_path = save_report(suite, output_dir=tmp_path, timestamp="20260412-120000")

        assert report_path == tmp_path / "eval-20260412-120000.json"
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert data["summary"]["total"] == 1
        assert data["summary"]["passed"] == 1
        assert data["metrics"]["stats"]["total_input_tokens"] == 7
        assert data["results"][0]["case_id"] == "report-001"
