"""Tests for eval pipeline models and runner."""
from __future__ import annotations
import tempfile
from pathlib import Path

import pytest
import yaml

from evals.models import Assertion, AssertionType, GoldenCase, SuiteResult
from evals.runner import (
    evaluate_assertion,
    load_golden_cases,
    run_case_offline,
    run_suite_offline,
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

    def test_suite_summary(self):
        s = SuiteResult(total=10, passed=8, failed=1, errors=1, duration_ms=1234)
        summary = s.summary()
        assert "8/10" in summary
        assert "80%" in summary
