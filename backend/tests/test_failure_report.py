"""Tests for failure_report.py — markdown generation from eval results."""

from pathlib import Path

from evals.models import CaseResult
from evals.failure_report import (
    ScenarioResult,
    generate_failure_report,
    save_failure_report,
)


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
        assert "LLM 推理" in md
        assert "工具数据" in md
        assert "状态机" in md

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
                tool_calls=["web_search", "update_plan_state"],
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

    def test_report_escapes_markdown_table_cells(self):
        scenarios = [
            _make_scenario(
                "failure-001",
                "预算|紧张",
                "去日本3000块",
                passed_assertions=0,
                total_assertions=2,
                failures=["bad|failure\nnext line"],
            )
        ]
        md = generate_failure_report(scenarios)
        assert "预算\\|紧张" in md
        assert "bad\\|failure<br>next line" in md

    def test_save_failure_report_writes_file(self):
        scenarios = [_make_scenario("failure-001", "预算极紧", "去日本3000块")]
        output_path = Path("tests/_generated/failure-report.md")
        try:
            saved = save_failure_report(scenarios, output_path=str(output_path))
            assert saved == str(output_path)
            assert output_path.exists()
            assert "# Travel Agent Pro 失败案例分析" in output_path.read_text(encoding="utf-8")
        finally:
            if output_path.exists():
                output_path.unlink()
            if output_path.parent.exists():
                output_path.parent.rmdir()

    def test_case_result_adapter_maps_existing_eval_fields(self):
        case_result = CaseResult(
            case_id="failure-001",
            passed=False,
            assertions_passed=1,
            assertions_total=2,
            failures=["tool missing"],
            error=None,
        )
        scenario = ScenarioResult.from_case_result(
            case_result,
            name="预算极紧",
            user_input="去日本3000块",
            tool_calls=["web_search"],
            responses=["测试回复"],
        )
        assert scenario.scenario_id == "failure-001"
        assert scenario.passed_assertions == 1
        assert scenario.total_assertions == 2
        assert scenario.failures == ["tool missing"]

    def test_error_case_is_rendered_as_failure(self):
        case_result = CaseResult(
            case_id="failure-002",
            passed=False,
            assertions_passed=0,
            assertions_total=2,
            failures=[],
            error="TimeoutError: upstream timeout",
        )
        scenario = ScenarioResult.from_case_result(
            case_result,
            name="高海拔",
            user_input="带老人去九寨沟",
        )
        md = generate_failure_report([scenario])
        assert "❌ 失败" in md
        assert "TimeoutError: upstream timeout" in md
