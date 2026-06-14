"""Tests for failure_report.py — markdown generation from eval results."""

import importlib.util
import sys
from pathlib import Path

from evals.failure_report import ScenarioResult, generate_failure_report


ROOT = Path(__file__).resolve().parents[2]


def _load_failure_analysis_script():
    path = ROOT / "scripts" / "failure-analysis" / "run_and_analyze.py"
    spec = importlib.util.spec_from_file_location("failure_analysis_run_and_analyze", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_scenario(
    scenario_id: str,
    name: str,
    user_input: str,
    *,
    passed_assertions: int = 1,
    total_assertions: int = 2,
    failures: list[str] | None = None,
    tool_calls: list[str] | None = None,
    responses: list[str] | None = None,
) -> ScenarioResult:
    return ScenarioResult(
        scenario_id=scenario_id,
        name=name,
        user_input=user_input,
        passed_assertions=passed_assertions,
        total_assertions=total_assertions,
        failures=failures or [],
        tool_calls=tool_calls or ["web_search"],
        responses=responses or ["这是一段测试回复"],
        duration_ms=1234.5,
        stats={},
    )


class TestGenerateReport:
    def test_report_has_title(self):
        scenarios = [_make_scenario("failure-001", "预算极紧", "去日本3000块")]
        md = generate_failure_report(scenarios)
        assert "# Travel Agent Pro 失败案例分析" in md

    def test_report_has_methodology(self):
        scenarios = [_make_scenario("failure-001", "预算极紧", "去日本3000块")]
        md = generate_failure_report(scenarios)
        assert "## 方法论" in md

    def test_report_has_taxonomy_table(self):
        scenarios = [_make_scenario("failure-001", "预算极紧", "去日本3000块")]
        md = generate_failure_report(scenarios)
        assert "tool args" in md
        assert "state write" in md
        assert "quality gate" in md

    def test_report_has_scenario_section(self):
        scenarios = [_make_scenario("failure-001", "预算极紧", "去日本3000块")]
        md = generate_failure_report(scenarios)
        assert "### 场景 1: 预算极紧" in md
        assert "去日本3000块" in md

    def test_report_has_overview_table(self):
        scenarios = [
            _make_scenario(
                "failure-001",
                "预算极紧",
                "去日本3000块",
                passed_assertions=2,
                total_assertions=2,
            ),
            _make_scenario(
                "failure-002",
                "高海拔",
                "带老人去九寨沟",
                passed_assertions=0,
                total_assertions=2,
                failures=["fail"],
            ),
        ]
        md = generate_failure_report(scenarios)
        assert "## 场景总览" in md
        assert "✅" in md
        assert "❌" in md

    def test_report_multiple_scenarios(self):
        scenarios = [
            _make_scenario(f"failure-{i:03d}", f"场景{i}", f"输入{i}")
            for i in range(1, 9)
        ]
        md = generate_failure_report(scenarios)
        assert "### 场景 8:" in md

    def test_report_includes_tool_calls(self):
        scenarios = [
            _make_scenario(
                "failure-001",
                "预算极紧",
                "去日本3000块",
                tool_calls=["web_search", "update_trip_basics"],
            )
        ]
        md = generate_failure_report(scenarios)
        assert "web_search" in md

    def test_report_includes_failure_details(self):
        scenarios = [
            _make_scenario(
                "failure-001",
                "预算极紧",
                "去日本3000块",
                passed_assertions=0,
                total_assertions=2,
                failures=["[tool_not_called] tool search_flights was called"],
            )
        ]
        md = generate_failure_report(scenarios)
        assert "search_flights" in md

    def test_report_includes_trace_evidence(self):
        scenario = _make_scenario(
            "failure-001",
            "预算极紧",
            "去日本3000块",
            failures=["failed"],
        )
        scenario.stats = {
            "trace_grade_failures": [
                {
                    "rubric_id": "state_write_has_diff",
                    "reason": "missing diff",
                    "evidence_event_ids": ["evt-1"],
                }
            ],
            "top_failing_event_ids": ["evt-1"],
            "top_failing_events": [
                {
                    "event_id": "evt-1",
                    "event_type": "tool_result",
                    "tool_name": "update_trip_basics",
                    "status": "success",
                }
            ],
        }
        md = generate_failure_report([scenario])
        assert "Trace Evidence" in md
        assert "state_write_has_diff" in md
        assert "evt-1 tool_result update_trip_basics success" in md

    def test_failure_analysis_json_redacts_sensitive_trace_tokens(self):
        module = _load_failure_analysis_script()
        scenario = _make_scenario(
            "failure-001",
            "敏感 token",
            "https://example.test/path?xsec_token=secret123",
            responses=["结果 https://example.test/path?xsec_token=secret456"],
        )
        scenario.stats = {
            "top_failing_events": [
                {
                    "event_id": "evt-1",
                    "payload": {
                        "url": "https://example.test/path?a=1&xsec_token=secret789"
                    },
                }
            ]
        }

        payload = module.result_to_json(scenario)
        serialized = str(payload)

        assert "secret123" not in serialized
        assert "secret456" not in serialized
        assert "secret789" not in serialized
        assert "xsec_token=<redacted>" in serialized
