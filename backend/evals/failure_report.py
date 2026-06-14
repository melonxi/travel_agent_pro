"""Generate docs/learning/2026-04-13-失败案例分析.md from structured scenario results."""

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
    ("planning", "规划策略或阶段目标选择错误", "提前做逐日行程"),
    ("tool selection", "工具选择错误或遗漏必要工具", "未搜索就写候选池"),
    ("tool args", "工具参数未贴合用户约束", "住宿搜索漏掉区域偏好"),
    ("tool result quality", "工具结果为空、低置信或外部服务异常", "天气/路线无可用结果"),
    ("state write", "状态写入错误或缺少 diff 证据", "锁定字段来源不明"),
    ("phase transition", "阶段推进/阻塞依据错误", "无 gate 证据就跳阶段"),
    ("memory recall", "记忆跳过、误召回或注入污染", "旧偏好覆盖当前意图"),
    ("context pollution", "上下文压缩/重建引入污染", "历史摘要混入旧状态"),
    ("quality gate", "质量门/软评估未阻断问题", "低分交付物被冻结"),
    ("external service", "第三方 API 不可用或数据缺失", "搜索接口认证失败"),
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
    lines.append("| Root Cause | 含义 | 示例 |")
    lines.append("|-----------|------|------|")
    for category, meaning, example in _TAXONOMY:
        lines.append(f"| {category} | {meaning} | {example} |")
    lines.append("")

    lines.append("## 场景总览\n")
    lines.append("| # | 场景 | 结果 | 断言通过率 | 关键发现 |")
    lines.append("|---|------|------|-----------|---------|")
    for index, scenario in enumerate(scenarios, 1):
        rate = f"{scenario.passed_assertions}/{scenario.total_assertions}"
        finding = scenario.failures[0] if scenario.failures else "所有断言通过"
        lines.append(
            f"| {index} | {scenario.name} | {scenario.result_emoji} | {rate} | {finding} |"
        )
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

        trace_failures = scenario.stats.get("trace_grade_failures") or []
        top_event_ids = scenario.stats.get("top_failing_event_ids") or []
        top_events = scenario.stats.get("top_failing_events") or []
        if trace_failures or top_event_ids:
            lines.append("**Trace Evidence**:\n")
            if trace_failures:
                for grade in trace_failures[:8]:
                    lines.append(
                        "- "
                        f"{grade.get('rubric_id')}: {grade.get('reason')} "
                        f"(events={grade.get('evidence_event_ids') or []})"
                    )
            if top_event_ids:
                lines.append(f"- Top event ids: {', '.join(top_event_ids)}")
            if top_events:
                lines.append("- Top event previews:")
                for event in top_events[:5]:
                    lines.append(
                        "  - "
                        f"{event.get('event_id')} {event.get('event_type')} "
                        f"{event.get('tool_name') or ''} {event.get('status') or ''}"
                    )
            lines.append("")

        if scenario.responses:
            preview = scenario.responses[-1][:200]
            lines.append(f"**Agent 回复摘要**: {preview}...\n")

        lines.append(
            "**Root Cause**: <!-- planning / tool selection / tool args / "
            "tool result quality / state write / phase transition / memory recall / "
            "context pollution / quality gate / external service -->\n"
        )
        lines.append("**根因分析**: <!-- 指向 trace event id 与代码位置 -->\n")
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
    output_path: str = "docs/learning/2026-04-13-失败案例分析.md",
    **kwargs: Any,
) -> str:
    markdown = generate_failure_report(scenarios, **kwargs)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return str(path)
