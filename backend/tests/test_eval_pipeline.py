"""Tests for eval pipeline models and runner."""

from __future__ import annotations
import json
from pathlib import Path

import pytest
import yaml

from evals.models import (
    Assertion,
    AssertionType,
    EvalExecution,
    GoldenCase,
    SuiteResult,
)
from evals.runner import (
    evaluate_assertion,
    load_golden_cases,
    run_case,
    run_suite,
    run_case_offline,
    run_suite_offline,
    save_report,
)
from tools.plan_tools import PLAN_WRITER_TOOL_NAMES

GOLDEN_CASES_DIR = Path(__file__).resolve().parents[1] / "evals/golden_cases"


def golden_cases_dir() -> Path:
    return GOLDEN_CASES_DIR


GOLDEN_CASES_DIR = Path(__file__).resolve().parents[1] / "evals/golden_cases"


def golden_cases_dir() -> Path:
    return GOLDEN_CASES_DIR


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
        a = Assertion(
            type=AssertionType.STATE_FIELD_SET, target="destination", value=None
        )
        ok, _ = evaluate_assertion(a, {"destination": "东京"}, [], [])
        assert ok

    def test_state_field_set_fail(self):
        a = Assertion(
            type=AssertionType.STATE_FIELD_SET, target="destination", value=None
        )
        ok, _ = evaluate_assertion(a, {}, [], [])
        assert not ok

    def test_state_field_value_match(self):
        a = Assertion(
            type=AssertionType.STATE_FIELD_SET, target="destination", value="东京"
        )
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
        ok, _ = evaluate_assertion(
            a, {"total_cost": 9000, "budget_total": 10000}, [], []
        )
        assert ok

    def test_memory_recall_field_assertion_reads_last_recall_stats(self):
        a = Assertion(
            type=AssertionType.MEMORY_RECALL_FIELD,
            target="final_recall_decision",
            value="query_recall_enabled",
        )
        ok, reason = evaluate_assertion(
            a,
            {},
            [],
            [],
            stats={
                "last_memory_recall": {
                    "final_recall_decision": "query_recall_enabled"
                }
            },
        )

        assert ok, reason


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
        cases = load_golden_cases(golden_cases_dir())
        known_tools = PLAN_WRITER_TOOL_NAMES | {
            "search_flights",
            "search_trains",
            "ai_travel_search",
            "search_accommodations",
            "get_poi_info",
            "calculate_route",
            "assemble_day_plan",
            "optimize_day_route",
            "check_availability",
            "check_weather",
            "generate_summary",
            "quick_travel_search",
            "search_travel_services",
            "web_search",
            "xiaohongshu_search_notes",
            "xiaohongshu_read_note",
            "xiaohongshu_get_comments",
        }
        bad_targets = [
            (case.id, assertion.target)
            for case in cases
            for assertion in case.assertions
            if assertion.type
            in {AssertionType.TOOL_CALLED, AssertionType.TOOL_NOT_CALLED}
            and assertion.target not in known_tools
        ]

        assert bad_targets == []

    def test_failure_005_protects_constraint_path(self):
        """Finding 3: failure-005 should protect dietary constraint with correct split tool."""
        cases = load_golden_cases(golden_cases_dir())
        failure_005 = next((c for c in cases if c.id == "failure-005"), None)
        assert failure_005 is not None, "failure-005 case not found"

        # Should still assert trip-basics write
        trip_basics_assertions = [
            a
            for a in failure_005.assertions
            if a.type == AssertionType.TOOL_CALLED and a.target == "update_trip_basics"
        ]
        assert len(trip_basics_assertions) == 1, (
            "Should have update_trip_basics assertion"
        )

        # Should also protect dietary constraint path with the correct split tool
        constraint_assertions = [
            a
            for a in failure_005.assertions
            if a.type == AssertionType.TOOL_CALLED
            and a.target in {"add_constraints", "add_constraint"}
        ]
        assert len(constraint_assertions) >= 1, (
            "Should have add_constraints assertion for dietary constraint"
        )

    def test_loads_memory_recall_golden_cases(self):
        cases = load_golden_cases(golden_cases_dir())
        recall_cases = [case for case in cases if "memory_recall" in case.tags]

        assert {case.id for case in recall_cases} >= {
            "recall-001-style-force-query-fallback",
            "recall-002-current-trip-fact-skip",
            "recall-003-gate-failure-profile-cue",
            "recall-004-ack-preference-force",
            "recall-005-negated-profile-signal",
            "recall-006-recommend-fallback",
        }
        assert all(
            any(
                assertion.type == AssertionType.MEMORY_RECALL_FIELD
                for assertion in case.assertions
            )
            for case in recall_cases
        )


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
                Assertion(
                    type=AssertionType.STATE_FIELD_SET,
                    target="destination",
                    value="东京",
                ),
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
                assertions=[
                    Assertion(type=AssertionType.STATE_FIELD_SET, target="destination")
                ],
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

    def test_run_suite_aggregates_memory_recall_metrics(self):
        cases = [
            GoldenCase(
                id="recall-pass",
                name="Recall expected",
                description="",
                difficulty="memory",
                messages=[],
                tags=["memory_recall", "expect_recall"],
                assertions=[],
            ),
            GoldenCase(
                id="skip-pass",
                name="Skip expected",
                description="",
                difficulty="memory",
                messages=[],
                tags=["memory_recall", "expect_skip"],
                assertions=[],
            ),
            GoldenCase(
                id="zero-hit",
                name="Recall zero hit",
                description="",
                difficulty="memory",
                messages=[],
                tags=["memory_recall", "expect_recall"],
                assertions=[],
            ),
        ]

        def executor(case: GoldenCase) -> EvalExecution:
            payloads = {
                "recall-pass": {
                    "final_recall_decision": "query_recall_enabled",
                    "candidate_count": 2,
                    "recall_attempted_but_zero_hit": False,
                },
                "skip-pass": {
                    "final_recall_decision": "no_recall_applied",
                    "candidate_count": 0,
                    "recall_attempted_but_zero_hit": False,
                },
                "zero-hit": {
                    "final_recall_decision": "query_recall_enabled",
                    "candidate_count": 0,
                    "recall_attempted_but_zero_hit": True,
                },
            }
            return EvalExecution(
                state={},
                tool_calls=[],
                responses=[],
                stats={"last_memory_recall": payloads[case.id]},
            )

        suite = run_suite(cases, executor)
        metrics = suite.metrics["memory_recall"]

        assert metrics["total"] == 3
        assert metrics["expected_recall"] == 2
        assert metrics["expected_skip"] == 1
        assert metrics["false_skip_rate"] == 0.0
        assert metrics["false_recall_rate"] == 0.0
        assert metrics["hit_rate_when_recall_enabled"] == 0.5
        assert metrics["recall_attempted_but_zero_hit_rate"] == 0.5

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

        report_path = save_report(
            suite, output_dir=tmp_path, timestamp="20260412-120000"
        )

        assert report_path == tmp_path / "eval-20260412-120000.json"
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert data["summary"]["total"] == 1
        assert data["summary"]["passed"] == 1
        assert data["metrics"]["stats"]["total_input_tokens"] == 7
        assert data["results"][0]["case_id"] == "report-001"
