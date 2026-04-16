"""Tests for failure_report.py — markdown generation from eval results."""

from evals.failure_report import ScenarioResult, generate_failure_report


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
