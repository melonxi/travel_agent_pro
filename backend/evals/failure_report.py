"""Generate docs/failure-analysis.md from structured scenario results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    user_input: str
    passed_assertions: int
    total_assertions: int
    failures: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return len(self.failures) == 0

    @property
    def result_emoji(self) -> str:
        if self.passed:
            return "✅ 成功"
        if self.passed_assertions > 0:
            return "⚠️ 部分成功"
        return "❌ 失败"


_TAXONOMY = [
    ("LLM 推理", "模型理解/推理能力不足", "无法识别特殊人群需求"),
    ("工具数据", "外部 API 返回数据不足或异常", "无航班搜索结果"),
    ("状态机", "阶段转换/回退逻辑缺陷", "backtrack 未清理下游"),
    ("约束传递", "用户约束未被传递到下游决策", "饮食约束未进入行程"),
    ("设计边界", "系统设计本身的合理限制", "不支持多人差异化行程"),
]


def generate_failure_report(
    scenarios: list[ScenarioResult],
    *,
    timestamp: str | None = None,
    model_info: str = "GPT-4o + Claude Sonnet 4",
) -> str:
    ts = timestamp or datetime.now().strftime("%Y-%m-%d")
    lines: list[str] = []

    lines.append("# Travel Agent Pro 失败案例分析\n")

    lines.append("## 方法论\n")
    lines.append(f"- 测试环境：生产配置（{model_info}）")
    lines.append("- 测试方式：真实 API 调用，非 mock")
    lines.append(f"- 测试时间：{ts}")
    lines.append("- 运行元数据：model、token、cost、latency stats 已记录\n")

    lines.append("## 失败模式分类法\n")
    lines.append("| 失败类别 | 含义 | 示例 |")
    lines.append("|---------|------|------|")
    for category, meaning, example in _TAXONOMY:
        lines.append(f"| {category} | {meaning} | {example} |")
    lines.append("")

    lines.append("## 场景总览\n")
    lines.append("| # | 场景 | 结果 | 断言通过率 | 关键发现 |")
    lines.append("|---|------|------|-----------|---------|")
    for index, scenario in enumerate(scenarios, 1):
        rate = f"{scenario.passed_assertions}/{scenario.total_assertions}"
        finding = scenario.failures[0] if scenario.failures else "所有断言通过"
        lines.append(f"| {index} | {scenario.name} | {scenario.result_emoji} | {rate} | {finding} |")
    lines.append("")

    lines.append("## 详细分析\n")
    for index, scenario in enumerate(scenarios, 1):
        lines.append(f"### 场景 {index}: {scenario.name}\n")
        lines.append(f"**输入**: {scenario.user_input}\n")
        lines.append(f"**结果**: {scenario.result_emoji}\n")
        lines.append(
            f"**断言**: {scenario.passed_assertions}/{scenario.total_assertions} 通过\n"
        )

        if scenario.tool_calls:
            lines.append(f"**工具调用**: {', '.join(scenario.tool_calls)}\n")

        if scenario.failures:
            lines.append("**失败详情**:\n")
            for failure in scenario.failures:
                lines.append(f"- {failure}")
            lines.append("")

        if scenario.responses:
            preview = scenario.responses[-1][:200]
            lines.append(f"**Agent 回复摘要**: {preview}...\n")

        lines.append(
            "**失败类别**: <!-- 人工填写: LLM推理 / 工具数据 / 状态机 / 约束传递 / 设计边界 -->\n"
        )
        lines.append("**根因分析**: <!-- 人工填写: 指向代码位置 -->\n")
        lines.append("**修复状态**: <!-- 已修复 / 待修复 / 设计权衡 -->\n")
        lines.append("**面试话术**: <!-- 一句话描述这个案例的工程价值 -->\n")
        lines.append("---\n")

    lines.append("## 失败模式归类\n")
    lines.append("<!-- 按类别统计分布，展示系统边界认知 -->\n")
    lines.append("## 改进路线图\n")
    lines.append("<!-- 基于分析结果的后续优化方向 -->\n")

    return "\n".join(lines)


def save_failure_report(
    scenarios: list[ScenarioResult],
    output_path: str = "docs/failure-analysis.md",
    **kwargs: Any,
) -> str:
    markdown = generate_failure_report(scenarios, **kwargs)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return str(path)
