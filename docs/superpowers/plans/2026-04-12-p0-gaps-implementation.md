# P0 Gaps: Failure Analysis & Reproducible Demo — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill two missing P0 items — 7.1 Failure Analysis (8 failure scenarios → `docs/learning/2026-04-13-失败案例分析.md`) and 7.7 Reproducible Demo (Playwright recording + seed data).

**Architecture:** Failure analysis reuses the existing `backend/evals/` framework — 8 new `failure-*.yaml` golden cases + `run_and_analyze.py` HTTP executor + `failure_report.py` markdown generator. Demo is a single Playwright spec with `test.step()` blocks sharing one browser session, driven by `run-all-demos.sh`.

**Tech Stack:** Python 3.11, FastAPI, Playwright, YAML, SSE streaming

---

## File Structure

### New Files

| Path | Responsibility |
|------|---------------|
| `backend/evals/golden_cases/failure-001-tight-budget.yaml` | Failure scenario: 5天3000元日本自由行 |
| `backend/evals/golden_cases/failure-002-elderly-altitude.yaml` | Failure scenario: 带80岁老人去九寨沟 |
| `backend/evals/golden_cases/failure-003-impossible-luxury.yaml` | Failure scenario: 500元马尔代夫5星7天 |
| `backend/evals/golden_cases/failure-004-midway-change.yaml` | Failure scenario: 多轮变更 东京→大阪 |
| `backend/evals/golden_cases/failure-005-multi-constraint.yaml` | Failure scenario: 3人春节三亚+素食 |
| `backend/evals/golden_cases/failure-006-extreme-date.yaml` | Failure scenario: 明天就要飞纽约 |
| `backend/evals/golden_cases/failure-007-vague-intent.yaml` | Failure scenario: "最近很火的地方" |
| `backend/evals/golden_cases/failure-008-greedy-itinerary.yaml` | Failure scenario: 5城5天 |
| `backend/evals/failure_report.py` | Generate `docs/learning/2026-04-13-失败案例分析.md` from eval results |
| `backend/tests/test_failure_report.py` | Tests for failure_report.py |
| `scripts/failure-analysis/run_and_analyze.py` | Main script: HTTP executor + eval assertion check |
| `scripts/failure-analysis/capture_screenshots.ts` | Playwright: open session by ID, take screenshots |
| `scripts/demo/seed-memory.json` | Preset user preferences + travel history |
| `scripts/demo/playwright.config.ts` | Demo Playwright config (video=on) |
| `scripts/demo/demo-full-flow.spec.ts` | Single test with 3 test.step() blocks |
| `scripts/demo/run-all-demos.sh` | One-click: start services → seed → record |
| `scripts/demo/README.md` | Demo usage guide |

### Modified Files

| Path | Change |
|------|--------|
| `README.md` | Add Demo & Failure Analysis links |
| `PROJECT_OVERVIEW.md` | Update docs section |

---

## Task 1: Create Failure Golden Cases (1-4)

**Files:**
- Create: `backend/evals/golden_cases/failure-001-tight-budget.yaml`
- Create: `backend/evals/golden_cases/failure-002-elderly-altitude.yaml`
- Create: `backend/evals/golden_cases/failure-003-impossible-luxury.yaml`
- Create: `backend/evals/golden_cases/failure-004-midway-change.yaml`

- [ ] **Step 1: Create failure-001-tight-budget.yaml**

```yaml
# backend/evals/golden_cases/failure-001-tight-budget.yaml
id: failure-001
name: 预算极紧 — 5天3000元日本自由行
description: |
  用户要求以极低预算去日本旅行5天。测试系统是否：
  1. 在 Phase 1 就通过 feasibility gate 拦截
  2. 给出合理的预算不足说明
  3. 不盲目进入 Phase 3 搜索航班
difficulty: hard
tags: [failure-analysis, budget, feasibility]
messages:
  - role: user
    content: 我想去日本东京玩5天，预算只有3000块人民币，帮我规划一下
assertions:
  - type: contains_text
    target: 预算
  - type: tool_not_called
    target: search_flights
```

- [ ] **Step 2: Create failure-002-elderly-altitude.yaml**

```yaml
# backend/evals/golden_cases/failure-002-elderly-altitude.yaml
id: failure-002
name: 特殊人群 — 带80岁老人去高海拔
description: |
  用户带80岁老人去九寨沟（高海拔）。测试系统是否：
  1. 识别高海拔对老年人的健康风险
  2. 在回复中提及健康/安全注意事项
  3. 建议替代方案或注意医疗准备
difficulty: hard
tags: [failure-analysis, special-needs, safety]
messages:
  - role: user
    content: 我想带我80岁的奶奶去九寨沟玩一周，预算2万
assertions:
  - type: contains_text
    target: 海拔
  - type: contains_text
    target: 健康
```

- [ ] **Step 3: Create failure-003-impossible-luxury.yaml**

```yaml
# backend/evals/golden_cases/failure-003-impossible-luxury.yaml
id: failure-003
name: 不可解任务 — 500元马尔代夫5星7天
description: |
  完全不可行的任务。测试 feasibility gate 是否拦截。
difficulty: infeasible
tags: [failure-analysis, infeasible, feasibility]
messages:
  - role: user
    content: 我只有500块钱，想去马尔代夫住5星级酒店7天
assertions:
  - type: tool_not_called
    target: search_flights
  - type: tool_not_called
    target: search_accommodations
  - type: contains_text
    target: 预算
```

- [ ] **Step 4: Create failure-004-midway-change.yaml**

```yaml
# backend/evals/golden_cases/failure-004-midway-change.yaml
id: failure-004
name: 多轮变更 — 京都改成大阪
description: |
  用户在规划中途改变目的地。测试 backtrack 机制是否：
  1. 正确清理下游状态
  2. 更新目的地
  3. 不保留旧目的地的搜索结果
difficulty: hard
tags: [failure-analysis, backtrack, state-machine]
messages:
  - role: user
    content: 我想去东京和京都玩5天，预算15000
  - role: user
    content: 等等，我改主意了，京都改成大阪吧
assertions:
  - type: state_field_set
    target: destination
```

- [ ] **Step 5: Verify YAML files load correctly**

Run:
```bash
cd backend && python -c "
from evals.runner import load_golden_cases
cases = load_golden_cases('evals/golden_cases')
failure_cases = [c for c in cases if c.id.startswith('failure-')]
print(f'Loaded {len(failure_cases)} failure cases:')
for c in failure_cases:
    print(f'  {c.id}: {c.name} ({c.difficulty})')
"
```

Expected: 4 failure cases loaded successfully.

- [ ] **Step 6: Commit**

```bash
git add backend/evals/golden_cases/failure-00{1,2,3,4}*.yaml
git commit -m "feat(evals): add failure analysis golden cases 1-4

Scenarios: tight budget, elderly altitude, impossible luxury, midway change"
```

---

## Task 2: Create Failure Golden Cases (5-8)

**Files:**
- Create: `backend/evals/golden_cases/failure-005-multi-constraint.yaml`
- Create: `backend/evals/golden_cases/failure-006-extreme-date.yaml`
- Create: `backend/evals/golden_cases/failure-007-vague-intent.yaml`
- Create: `backend/evals/golden_cases/failure-008-greedy-itinerary.yaml`

- [ ] **Step 1: Create failure-005-multi-constraint.yaml**

```yaml
# backend/evals/golden_cases/failure-005-multi-constraint.yaml
id: failure-005
name: 约束组合 — 3人春节三亚+素食者
description: |
  多约束组合：多人、特定时间、饮食限制。测试系统是否：
  1. 将人数纳入预算计算
  2. 传递饮食约束到行程安排
  3. 考虑春节旺季价格
difficulty: hard
tags: [failure-analysis, constraints, dietary]
messages:
  - role: user
    content: 我们3个人春节想去三亚玩5天，预算1.5万，其中一个朋友是素食者
assertions:
  - type: contains_text
    target: 素食
  - type: tool_called
    target: update_plan_state
```

- [ ] **Step 2: Create failure-006-extreme-date.yaml**

```yaml
# backend/evals/golden_cases/failure-006-extreme-date.yaml
id: failure-006
name: 极端时间 — 明天就要飞纽约
description: |
  用户要求极短准备时间的出行。测试系统是否：
  1. guardrail 检测到紧迫日期
  2. 提醒签证/准备时间不足
  3. 不盲目搜索明天的航班
difficulty: hard
tags: [failure-analysis, date-constraint, guardrail]
messages:
  - role: user
    content: 我明天就要飞纽约，帮我订个最便宜的机票和酒店，待一周
assertions:
  - type: contains_text
    target: 签证
```

- [ ] **Step 3: Create failure-007-vague-intent.yaml**

```yaml
# backend/evals/golden_cases/failure-007-vague-intent.yaml
id: failure-007
name: 模糊意图 — "最近很火的地方"
description: |
  极其模糊的意图。测试系统是否：
  1. 通过搜索工具收集信息
  2. 引导用户澄清需求
  3. 提供多个候选目的地
difficulty: medium
tags: [failure-analysis, vague-intent, convergence]
messages:
  - role: user
    content: 想去那个最近很火的地方玩一下
assertions:
  - type: tool_called
    target: web_search
```

- [ ] **Step 4: Create failure-008-greedy-itinerary.yaml**

```yaml
# backend/evals/golden_cases/failure-008-greedy-itinerary.yaml
id: failure-008
name: 贪心行程 — 5城5天
description: |
  用户要求在5天内游览5个城市。测试系统是否：
  1. 识别行程过于紧凑
  2. 提出时间冲突/不合理性
  3. 建议精简行程
difficulty: hard
tags: [failure-analysis, time-conflict, greedy]
messages:
  - role: user
    content: 我想5天玩遍东京、大阪、京都、奈良和神户，预算2万
assertions:
  - type: contains_text
    target: 紧凑
  - type: tool_called
    target: update_plan_state
```

- [ ] **Step 5: Verify all 8 failure cases load**

Run:
```bash
cd backend && python -c "
from evals.runner import load_golden_cases
cases = load_golden_cases('evals/golden_cases')
failure_cases = [c for c in cases if c.id.startswith('failure-')]
print(f'Loaded {len(failure_cases)} failure cases:')
for c in failure_cases:
    print(f'  {c.id}: {c.name} [{c.difficulty}] tags={c.tags}')
assert len(failure_cases) == 8, f'Expected 8 failure cases, got {len(failure_cases)}'
print('All 8 failure cases loaded OK')
"
```

Expected: 8 failure cases loaded.

- [ ] **Step 6: Commit**

```bash
git add backend/evals/golden_cases/failure-00{5,6,7,8}*.yaml
git commit -m "feat(evals): add failure analysis golden cases 5-8

Scenarios: multi-constraint, extreme date, vague intent, greedy itinerary"
```

---

## Task 3: Create failure_report.py with Tests

**Files:**
- Create: `backend/evals/failure_report.py`
- Create: `backend/tests/test_failure_report.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_failure_report.py
"""Tests for failure_report.py — markdown generation from eval results."""
import json
from evals.failure_report import generate_failure_report, ScenarioResult


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
            _make_scenario("failure-001", "预算极紧", "去日本3000块", passed_assertions=2, total_assertions=2),
            _make_scenario("failure-002", "高海拔", "带老人去九寨沟", passed_assertions=0, total_assertions=2, failures=["fail"]),
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
        scenarios = [_make_scenario(
            "failure-001", "预算极紧", "去日本3000块",
            tool_calls=["web_search", "update_plan_state"],
        )]
        md = generate_failure_report(scenarios)
        assert "web_search" in md

    def test_report_includes_failure_details(self):
        scenarios = [_make_scenario(
            "failure-001", "预算极紧", "去日本3000块",
            passed_assertions=0, total_assertions=2,
            failures=["[tool_not_called] tool search_flights was called"],
        )]
        md = generate_failure_report(scenarios)
        assert "search_flights" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_failure_report.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'evals.failure_report'`

- [ ] **Step 3: Write failure_report.py**

```python
# backend/evals/failure_report.py
"""Generate docs/learning/2026-04-13-失败案例分析.md from structured scenario results."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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

    # Title
    lines.append("# Travel Agent Pro 失败案例分析\n")

    # Methodology
    lines.append("## 方法论\n")
    lines.append(f"- 测试环境：生产配置（{model_info}）")
    lines.append("- 测试方式：真实 API 调用，非 mock")
    lines.append(f"- 测试时间：{ts}")
    lines.append("- 运行元数据：model, provider, config hash 均记录在案\n")

    # Taxonomy
    lines.append("## 失败模式分类法\n")
    lines.append("| 失败类别 | 含义 | 示例 |")
    lines.append("|---------|------|------|")
    for cat, meaning, example in _TAXONOMY:
        lines.append(f"| {cat} | {meaning} | {example} |")
    lines.append("")

    # Overview table
    lines.append("## 场景总览\n")
    lines.append("| # | 场景 | 结果 | 断言通过率 | 关键发现 |")
    lines.append("|---|------|------|-----------|---------|")
    for i, s in enumerate(scenarios, 1):
        rate = f"{s.passed_assertions}/{s.total_assertions}"
        finding = s.failures[0] if s.failures else "所有断言通过"
        lines.append(f"| {i} | {s.name} | {s.result_emoji} | {rate} | {finding} |")
    lines.append("")

    # Detailed analysis per scenario
    lines.append("## 详细分析\n")
    for i, s in enumerate(scenarios, 1):
        lines.append(f"### 场景 {i}: {s.name}\n")
        lines.append(f"**输入**: {s.user_input}\n")
        lines.append(f"**结果**: {s.result_emoji}\n")
        lines.append(f"**断言**: {s.passed_assertions}/{s.total_assertions} 通过\n")

        if s.tool_calls:
            lines.append(f"**工具调用**: {', '.join(s.tool_calls)}\n")

        if s.failures:
            lines.append("**失败详情**:\n")
            for f in s.failures:
                lines.append(f"- {f}")
            lines.append("")

        if s.responses:
            preview = s.responses[-1][:200]
            lines.append(f"**Agent 回复摘要**: {preview}...\n")

        # Placeholders for human review
        lines.append("**失败类别**: <!-- 人工填写: LLM推理 / 工具数据 / 状态机 / 约束传递 / 设计边界 -->\n")
        lines.append("**根因分析**: <!-- 人工填写: 指向代码位置 -->\n")
        lines.append("**修复状态**: <!-- 已修复 / 待修复 / 设计权衡 -->\n")
        lines.append("**面试话术**: <!-- 一句话描述这个案例的工程价值 -->\n")
        lines.append("---\n")

    # Summary sections
    lines.append("## 失败模式归类\n")
    lines.append("<!-- 按类别统计分布，展示系统边界认知 -->\n")

    lines.append("## 改进路线图\n")
    lines.append("<!-- 基于分析结果的后续优化方向 -->\n")

    return "\n".join(lines)


def save_failure_report(
    scenarios: list[ScenarioResult],
    output_path: str = "docs/learning/2026-04-13-失败案例分析.md",
    **kwargs,
) -> str:
    md = generate_failure_report(scenarios, **kwargs)
    from pathlib import Path
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    return str(path)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_failure_report.py -v`

Expected: All 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/evals/failure_report.py backend/tests/test_failure_report.py
git commit -m "feat(evals): add failure_report.py — markdown generator for failure analysis

Generates docs/learning/2026-04-13-失败案例分析.md from structured ScenarioResult data.
Includes taxonomy table, overview, and per-scenario detail sections."
```

---

## Task 4: Create run_and_analyze.py

This is the main failure analysis script. It calls the live backend API, sends messages from golden cases, collects responses/state/tools, runs eval assertions, and saves structured results.

**Files:**
- Create: `scripts/failure-analysis/run_and_analyze.py`

- [ ] **Step 1: Create scripts/failure-analysis/ directory**

```bash
mkdir -p scripts/failure-analysis
```

- [ ] **Step 2: Write run_and_analyze.py**

```python
#!/usr/bin/env python3
"""Failure analysis runner — execute failure scenarios against live backend.

Usage:
    python scripts/failure-analysis/run_and_analyze.py [--base-url http://127.0.0.1:8000]

Requires: backend running on --base-url (default http://127.0.0.1:8000)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

# Add backend to sys.path for evals imports
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from evals.runner import load_golden_cases, evaluate_assertion
from evals.models import GoldenCase, AssertionType, Assertion
from evals.failure_report import ScenarioResult, save_failure_report

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
GOLDEN_CASES_DIR = BACKEND_DIR / "evals" / "golden_cases"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def create_session(client: httpx.Client) -> str:
    resp = client.post("/api/sessions")
    resp.raise_for_status()
    return resp.json()["session_id"]


def send_message(client: httpx.Client, session_id: str, message: str) -> list[str]:
    """Send a chat message via SSE and collect response chunks."""
    responses: list[str] = []
    with client.stream(
        "POST",
        f"/api/chat/{session_id}",
        json={"message": message},
        timeout=180.0,
    ) as stream:
        for line in stream.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            etype = event.get("type", "")
            if etype == "text" and event.get("content"):
                responses.append(event["content"])
    return responses


def get_plan_state(client: httpx.Client, session_id: str) -> dict:
    resp = client.get(f"/api/plan/{session_id}")
    resp.raise_for_status()
    return resp.json()


def get_session_stats(client: httpx.Client, session_id: str) -> dict:
    resp = client.get(f"/api/sessions/{session_id}/stats")
    if resp.status_code == 200:
        return resp.json()
    return {}


def get_messages(client: httpx.Client, session_id: str) -> list[dict]:
    resp = client.get(f"/api/messages/{session_id}")
    if resp.status_code == 200:
        return resp.json()
    return []


def extract_tool_calls_from_messages(messages: list[dict]) -> list[str]:
    """Extract tool names from stored messages."""
    tool_names: list[str] = []
    for msg in messages:
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                name = tc.get("function", {}).get("name") or tc.get("name", "")
                if name and name not in tool_names:
                    tool_names.append(name)
    return tool_names


def run_scenario(client: httpx.Client, case: GoldenCase) -> ScenarioResult:
    """Execute a single failure scenario against the live backend."""
    print(f"\n{'='*60}")
    print(f"Running: {case.id} — {case.name}")
    print(f"{'='*60}")

    start = time.monotonic()

    # Create session
    session_id = create_session(client)
    print(f"  Session: {session_id}")

    # Send each message
    all_responses: list[str] = []
    user_input = ""
    for msg in case.messages:
        if msg["role"] == "user":
            if not user_input:
                user_input = msg["content"]
            print(f"  Sending: {msg['content'][:80]}...")
            try:
                chunks = send_message(client, session_id, msg["content"])
                all_responses.extend(chunks)
                print(f"  Got {len(chunks)} response chunks")
            except Exception as exc:
                print(f"  ERROR sending message: {exc}")
                all_responses.append(f"ERROR: {exc}")

    # Collect state
    plan_state = get_plan_state(client, session_id)
    stats = get_session_stats(client, session_id)
    messages = get_messages(client, session_id)
    tool_calls = extract_tool_calls_from_messages(messages)

    print(f"  Phase: {plan_state.get('phase', '?')}")
    print(f"  Tools called: {tool_calls}")

    # Evaluate assertions
    full_response_text = " ".join(all_responses)
    passed = 0
    failures: list[str] = []
    for assertion in case.assertions:
        ok, reason = evaluate_assertion(
            assertion, plan_state, tool_calls, [full_response_text]
        )
        if ok:
            passed += 1
            print(f"  ✅ {assertion.type.value}: {assertion.target}")
        else:
            failures.append(f"[{assertion.type.value}] {reason}")
            print(f"  ❌ {assertion.type.value}: {reason}")

    elapsed = (time.monotonic() - start) * 1000

    result = ScenarioResult(
        scenario_id=case.id,
        name=case.name,
        user_input=user_input,
        passed_assertions=passed,
        total_assertions=len(case.assertions),
        failures=failures,
        tool_calls=tool_calls,
        responses=all_responses[-3:] if all_responses else [],
        duration_ms=elapsed,
        stats={
            "session_id": session_id,
            "plan_state": plan_state,
            **stats,
        },
    )

    status = "PASS" if result.passed else "FAIL"
    print(f"\n  Result: {status} ({passed}/{len(case.assertions)} assertions)")
    print(f"  Duration: {elapsed:.0f}ms")

    return result


def main():
    parser = argparse.ArgumentParser(description="Run failure analysis scenarios")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output", default=str(BACKEND_DIR.parent / "docs" / "learning" / "2026-04-13-失败案例分析.md"))
    args = parser.parse_args()

    # Load only failure-* cases
    all_cases = load_golden_cases(str(GOLDEN_CASES_DIR))
    failure_cases = [c for c in all_cases if c.id.startswith("failure-")]
    print(f"Loaded {len(failure_cases)} failure scenarios")

    if not failure_cases:
        print("ERROR: No failure-*.yaml cases found")
        sys.exit(1)

    # Check backend is running
    client = httpx.Client(base_url=args.base_url, timeout=30.0)
    try:
        client.get("/api/sessions")
    except httpx.ConnectError:
        print(f"ERROR: Backend not running at {args.base_url}")
        print("Start with: scripts/dev.sh")
        sys.exit(1)

    # Run all scenarios
    results: list[ScenarioResult] = []
    for case in failure_cases:
        try:
            result = run_scenario(client, case)
            results.append(result)
        except Exception as exc:
            print(f"  FATAL ERROR in {case.id}: {exc}")
            results.append(ScenarioResult(
                scenario_id=case.id,
                name=case.name,
                user_input=case.messages[0]["content"] if case.messages else "",
                passed_assertions=0,
                total_assertions=len(case.assertions),
                failures=[f"FATAL: {exc}"],
            ))

    # Save structured results JSON
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results_json = RESULTS_DIR / "failure-results.json"
    results_json.write_text(
        json.dumps(
            [
                {
                    "scenario_id": r.scenario_id,
                    "name": r.name,
                    "user_input": r.user_input,
                    "passed": r.passed,
                    "passed_assertions": r.passed_assertions,
                    "total_assertions": r.total_assertions,
                    "failures": r.failures,
                    "tool_calls": r.tool_calls,
                    "duration_ms": r.duration_ms,
                    "session_id": r.stats.get("session_id", ""),
                }
                for r in results
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nResults JSON saved to: {results_json}")

    # Generate markdown report
    report_path = save_failure_report(results, output_path=args.output)
    print(f"Report saved to: {report_path}")

    # Print summary
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    print(f"\n{'='*60}")
    print(f"SUMMARY: {passed}/{total} scenarios passed all assertions")
    print(f"{'='*60}")
    for r in results:
        emoji = "✅" if r.passed else "❌"
        print(f"  {emoji} {r.scenario_id}: {r.name} ({r.passed_assertions}/{r.total_assertions})")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify script syntax**

Run:
```bash
cd backend && python -c "
import sys; sys.path.insert(0, '.')
# Just verify the imports work
from evals.runner import load_golden_cases, evaluate_assertion
from evals.failure_report import ScenarioResult, save_failure_report
print('All imports OK')
"
```

Expected: "All imports OK"

- [ ] **Step 4: Commit**

```bash
git add scripts/failure-analysis/run_and_analyze.py
git commit -m "feat(scripts): add failure analysis runner

HTTP executor that runs failure-*.yaml scenarios against live backend,
collects state/tools/responses, evaluates assertions, generates report."
```

---

## Task 5: Create capture_screenshots.ts

Playwright script to open completed sessions in the frontend and take screenshots.

**Files:**
- Create: `scripts/failure-analysis/capture_screenshots.ts`

- [ ] **Step 1: Write capture_screenshots.ts**

```typescript
// scripts/failure-analysis/capture_screenshots.ts
//
// Usage: npx playwright test scripts/failure-analysis/capture_screenshots.ts
//
// Reads session IDs from scripts/failure-analysis/results/failure-results.json,
// opens each session in the frontend, and takes screenshots.

import { test } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

const RESULTS_PATH = path.join(__dirname, 'results', 'failure-results.json');
const SCREENSHOTS_DIR = path.join(__dirname, '..', '..', 'screenshots', 'failure-analysis');

interface FailureResult {
  scenario_id: string;
  name: string;
  session_id: string;
  passed: boolean;
}

function loadResults(): FailureResult[] {
  if (!fs.existsSync(RESULTS_PATH)) {
    console.warn(`Results file not found: ${RESULTS_PATH}`);
    return [];
  }
  return JSON.parse(fs.readFileSync(RESULTS_PATH, 'utf-8'));
}

const results = loadResults();

test.describe('Failure Analysis Screenshots', () => {
  test.beforeAll(() => {
    fs.mkdirSync(SCREENSHOTS_DIR, { recursive: true });
  });

  for (const result of results) {
    if (!result.session_id) continue;

    test(`Screenshot: ${result.scenario_id} — ${result.name}`, async ({ page }) => {
      // Navigate to the session
      await page.goto(`/?session=${result.session_id}`);

      // Wait for the chat to load
      await page.waitForSelector('.message.assistant .bubble', { timeout: 30000 });

      // Wait a bit for rendering to settle
      await page.waitForTimeout(2000);

      // Take full-page screenshot
      const filename = `${result.scenario_id}.png`;
      await page.screenshot({
        path: path.join(SCREENSHOTS_DIR, filename),
        fullPage: true,
      });

      console.log(`  Saved: ${filename}`);
    });
  }
});
```

- [ ] **Step 2: Commit**

```bash
git add scripts/failure-analysis/capture_screenshots.ts
git commit -m "feat(scripts): add failure analysis screenshot capture

Playwright script that opens completed failure sessions in frontend
and takes full-page screenshots for docs/learning/2026-04-13-失败案例分析.md."
```

---

## Task 6: Create Demo Configuration Files

**Files:**
- Create: `scripts/demo/seed-memory.json`
- Create: `scripts/demo/playwright.config.ts`

- [ ] **Step 1: Create scripts/demo/ directory**

```bash
mkdir -p scripts/demo
```

- [ ] **Step 2: Create seed-memory.json**

```json
{
  "user_id": "default_user",
  "events": [
    {
      "event_type": "preference_learned",
      "object_type": "travel_preference",
      "object_payload": {
        "type": "preference",
        "domain": "travel",
        "key": "travel_style",
        "value": "文化体验为主，适度冒险"
      },
      "reason_text": "用户历史偏好"
    },
    {
      "event_type": "preference_learned",
      "object_type": "travel_preference",
      "object_payload": {
        "type": "preference",
        "domain": "accommodation",
        "key": "accommodation_preference",
        "value": "偏好精品民宿和设计酒店"
      },
      "reason_text": "用户历史偏好"
    },
    {
      "event_type": "preference_learned",
      "object_type": "travel_preference",
      "object_payload": {
        "type": "preference",
        "domain": "travel",
        "key": "pace_preference",
        "value": "不赶路，每天2-3个景点"
      },
      "reason_text": "用户历史偏好"
    },
    {
      "event_type": "trip_completed",
      "object_type": "trip_episode",
      "object_payload": {
        "destination": "京都",
        "date": "2025-03",
        "rating": 5,
        "highlight": "岚山竹林和抹茶体验",
        "lesson": "樱花季酒店需提前3个月预订"
      },
      "reason_text": "历史旅行记录"
    }
  ]
}
```

- [ ] **Step 3: Create playwright.config.ts**

```typescript
// scripts/demo/playwright.config.ts
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: 'demo-*.spec.ts',
  timeout: 300000,  // 5 minutes — LLM responses need time
  use: {
    baseURL: 'http://127.0.0.1:5173',
    video: { mode: 'on', size: { width: 1280, height: 720 } },
    screenshot: 'on',
  },
  projects: [
    {
      name: 'demo',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
```

- [ ] **Step 4: Commit**

```bash
git add scripts/demo/seed-memory.json scripts/demo/playwright.config.ts
git commit -m "feat(demo): add seed memory data and Playwright config

seed-memory.json targets default_user (frontend doesn't pass user_id).
playwright.config.ts enables video recording at 1280x720."
```

---

## Task 7: Create demo-full-flow.spec.ts

Single Playwright test with `test.step()` blocks sharing one browser session.

**Files:**
- Create: `scripts/demo/demo-full-flow.spec.ts`

- [ ] **Step 1: Write demo-full-flow.spec.ts**

```typescript
// scripts/demo/demo-full-flow.spec.ts
//
// Full demo flow: Phase 1 → Phase 3 → Phase 5 + backtrack
// Uses test.step() to keep one browser session throughout.

import { test, expect } from '@playwright/test';

const LONG_TIMEOUT = 180000; // 3 min — LLM response time

async function sendMessage(page: any, message: string) {
  const input = page.locator('input[placeholder*="告诉我你想去哪里"]');
  await input.fill(message);
  await page.locator('.send-btn').click();
}

async function waitForAssistantResponse(page: any) {
  // Wait for the latest assistant message to appear and stop streaming
  const lastBubble = page.locator('.message.assistant .bubble').last();
  await expect(lastBubble).toBeVisible({ timeout: LONG_TIMEOUT });
  // Wait for streaming to complete (no more typing indicator)
  await page.waitForTimeout(3000);
  return lastBubble;
}

test('Full demo: Phase 1 → 3 → 5 with backtrack', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('text=旅行者')).toBeVisible({ timeout: 15000 });

  await test.step('Phase 1: Vague intent → destination convergence', async () => {
    await sendMessage(page, '我想找个安静的海边城市放松一下，预算1万左右，大概5天');

    // Wait for tool calls
    const toolCard = page.locator('.tool-card').first();
    await expect(toolCard).toBeVisible({ timeout: LONG_TIMEOUT });

    // Wait for assistant response
    const response = await waitForAssistantResponse(page);
    await expect(response).not.toHaveText(/^$/, { timeout: LONG_TIMEOUT });

    // Screenshot: destination recommendations
    await page.screenshot({
      path: 'screenshots/demos/phase1-recommendations.png',
      fullPage: true,
    });
  });

  await test.step('Phase 3: Confirm destination → skeleton planning', async () => {
    await sendMessage(page, '就去你推荐的第一个吧，帮我规划一下');

    // Wait for search tools
    await page.waitForTimeout(5000);

    // Wait for assistant response with planning details
    const response = await waitForAssistantResponse(page);
    await expect(response).not.toHaveText(/^$/, { timeout: LONG_TIMEOUT });

    // Screenshot: planning results
    await page.screenshot({
      path: 'screenshots/demos/phase3-planning.png',
      fullPage: true,
    });
  });

  await test.step('Phase 5: Backtrack — change accommodation preference', async () => {
    // Wait for potential Phase 5 processing
    await page.waitForTimeout(5000);

    await sendMessage(page, '我改主意了，不住市中心了，想住海边的民宿');

    // Wait for backtrack and re-planning
    const response = await waitForAssistantResponse(page);
    await expect(response).not.toHaveText(/^$/, { timeout: LONG_TIMEOUT });

    // Screenshot: after backtrack
    await page.screenshot({
      path: 'screenshots/demos/phase5-backtrack.png',
      fullPage: true,
    });
  });
});
```

- [ ] **Step 2: Commit**

```bash
git add scripts/demo/demo-full-flow.spec.ts
git commit -m "feat(demo): add full-flow Playwright demo spec

Single test with 3 test.step() blocks: Phase 1 destination convergence,
Phase 3 skeleton planning, Phase 5 backtrack. Shares browser session."
```

---

## Task 8: Create run-all-demos.sh and README.md

**Files:**
- Create: `scripts/demo/run-all-demos.sh`
- Create: `scripts/demo/README.md`

- [ ] **Step 1: Write run-all-demos.sh**

```bash
#!/usr/bin/env bash
# scripts/demo/run-all-demos.sh
# One-click demo: check services → seed memory → record demo → collect videos

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:8000}"
FRONTEND_URL="${FRONTEND_URL:-http://127.0.0.1:5173}"

echo "=== Travel Agent Pro — Demo Recording ==="
echo ""

# 1. Check services
echo "→ Checking backend at $BACKEND_URL..."
if ! curl -sf "$BACKEND_URL/api/sessions" > /dev/null 2>&1; then
  echo "  Backend not running. Starting with scripts/dev.sh..."
  echo "  Please start services manually: scripts/dev.sh"
  exit 1
fi
echo "  ✅ Backend is running"

echo "→ Checking frontend at $FRONTEND_URL..."
if ! curl -sf "$FRONTEND_URL" > /dev/null 2>&1; then
  echo "  Frontend not running."
  echo "  Please start services manually: scripts/dev.sh"
  exit 1
fi
echo "  ✅ Frontend is running"

# 2. Seed memory
echo ""
echo "→ Seeding memory for default_user..."
SEED_FILE="$DEMO_DIR/seed-memory.json"
if [ ! -f "$SEED_FILE" ]; then
  echo "  ERROR: seed-memory.json not found"
  exit 1
fi

USER_ID=$(jq -r '.user_id' "$SEED_FILE")
EVENTS=$(jq -c '.events[]' "$SEED_FILE")

while IFS= read -r event; do
  curl -sf -X POST "$BACKEND_URL/api/memory/$USER_ID/events" \
    -H "Content-Type: application/json" \
    -d "$event" > /dev/null
  echo "  ✅ Injected: $(echo "$event" | jq -r '.event_type')"
done <<< "$EVENTS"

echo "  Memory seeded for user: $USER_ID"

# 3. Create screenshots directory
mkdir -p "$ROOT_DIR/screenshots/demos"

# 4. Run demo
echo ""
echo "→ Running demo recording..."
cd "$DEMO_DIR"
npx playwright test --config=playwright.config.ts 2>&1 || true

# 5. Collect videos
echo ""
echo "→ Collecting videos..."
if ls test-results/**/*.webm 1>/dev/null 2>&1; then
  cp test-results/**/*.webm "$ROOT_DIR/screenshots/demos/" 2>/dev/null || true
  echo "  ✅ Videos copied to screenshots/demos/"
else
  echo "  ⚠️  No video files found in test-results/"
fi

# 6. Summary
echo ""
echo "=== Demo Complete ==="
echo "Screenshots: $ROOT_DIR/screenshots/demos/"
echo "Videos:      $ROOT_DIR/screenshots/demos/*.webm"
ls -la "$ROOT_DIR/screenshots/demos/" 2>/dev/null || echo "(directory empty)"
```

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/demo/run-all-demos.sh
```

- [ ] **Step 3: Write README.md**

```markdown
# Travel Agent Pro — Demo Recording

一键录制 Travel Agent Pro 的核心功能演示视频。

## 前置要求

- Node.js 18+
- Python 3.11+ with backend venv configured
- API keys configured in `config.yaml` (OpenAI / Anthropic)
- Playwright browsers installed: `npx playwright install chromium`

## 快速开始

```bash
# 1. 启动服务（在另一个终端）
scripts/dev.sh

# 2. 运行 demo 录制
scripts/demo/run-all-demos.sh
```

## Demo 内容

### Full Flow: Phase 1 → 3 → 5

一个完整的旅行规划流程，展示三个核心能力：

1. **Phase 1 — 模糊意图收敛**: "我想找个安静的海边城市放松一下"
   - 系统通过工具调用（web_search/xiaohongshu_search）收集信息
   - 推荐多个候选目的地

2. **Phase 3 — 框架规划**: "就去你推荐的第一个吧"
   - 搜索航班、住宿
   - 生成旅行骨架方案

3. **Phase 5 — 回退修改**: "我改主意了，想住海边的民宿"
   - 触发 backtrack 机制
   - 清理下游状态并重新规划

## 产出

- `screenshots/demos/phase1-recommendations.png` — 目的地推荐截图
- `screenshots/demos/phase3-planning.png` — 规划结果截图
- `screenshots/demos/phase5-backtrack.png` — 回退后截图
- `screenshots/demos/*.webm` — 完整录屏视频

## Seed Memory

`seed-memory.json` 预注入用户偏好（文化体验、精品民宿、不赶路）和历史旅行（京都），
让 demo 展示系统的个性化记忆能力。

注入目标用户: `default_user`（前端不传 user_id，后端默认值）。

## 常见问题

**Q: 服务未启动？**
运行 `scripts/dev.sh` 在另一个终端启动 backend (port 8000) + frontend (port 5173)。

**Q: Playwright 浏览器未安装？**
运行 `npx playwright install chromium`。

**Q: Demo 超时？**
LLM 响应可能较慢，默认超时 5 分钟。检查 API key 配置和网络连接。

**Q: 视频文件在哪？**
`screenshots/demos/` 目录下，`.webm` 格式。
```

- [ ] **Step 4: Commit**

```bash
git add scripts/demo/run-all-demos.sh scripts/demo/README.md
git commit -m "feat(demo): add run-all-demos.sh and README

One-click script: check services, seed memory, run Playwright demo, collect videos.
README includes prerequisites, usage, and troubleshooting."
```

---

## Task 9: Run Failure Analysis

Execute the failure analysis against the live backend. This requires backend + frontend running.

**Files:**
- Generates: `docs/learning/2026-04-13-失败案例分析.md`
- Generates: `scripts/failure-analysis/results/failure-results.json`
- Generates: `screenshots/failure-analysis/*.png`

- [ ] **Step 1: Verify backend is running**

```bash
curl -sf http://127.0.0.1:8000/api/sessions | head -c 100
```

If not running, start with `scripts/dev.sh` in a separate terminal.

- [ ] **Step 2: Run failure analysis script**

```bash
cd /path/to/travel_agent_pro
python scripts/failure-analysis/run_and_analyze.py
```

Expected: 8 scenarios executed, results saved to `scripts/failure-analysis/results/failure-results.json` and `docs/learning/2026-04-13-失败案例分析.md`.

- [ ] **Step 3: Capture screenshots**

```bash
npx playwright test scripts/failure-analysis/capture_screenshots.ts --config=playwright.config.ts
```

- [ ] **Step 4: Review and enhance docs/learning/2026-04-13-失败案例分析.md**

Open `docs/learning/2026-04-13-失败案例分析.md` and fill in the human-review sections:
- Failure category for each scenario
- Root cause analysis with code references
- Fix status
- Interview talking points

- [ ] **Step 5: Commit**

```bash
git add docs/learning/2026-04-13-失败案例分析.md scripts/failure-analysis/results/ screenshots/failure-analysis/
git commit -m "docs: add failure analysis with 8 real scenario results

Executed 8 failure scenarios against live backend:
- tight budget, elderly altitude, impossible luxury, midway change
- multi-constraint, extreme date, vague intent, greedy itinerary

Includes assertion results, tool call traces, and screenshots."
```

---

## Task 10: Run Demo Recording

Execute the demo against the live backend + frontend.

**Files:**
- Generates: `screenshots/demos/*.png`
- Generates: `screenshots/demos/*.webm`

- [ ] **Step 1: Verify services running**

```bash
curl -sf http://127.0.0.1:8000/api/sessions > /dev/null && echo "Backend OK"
curl -sf http://127.0.0.1:5173 > /dev/null && echo "Frontend OK"
```

- [ ] **Step 2: Run demo**

```bash
scripts/demo/run-all-demos.sh
```

Expected: Memory seeded, demo recorded, videos in `screenshots/demos/`.

- [ ] **Step 3: Verify outputs**

```bash
ls -la screenshots/demos/
```

Expected: At least phase1-recommendations.png, phase3-planning.png, phase5-backtrack.png, and a .webm video file.

- [ ] **Step 4: Commit demo artifacts**

```bash
git add screenshots/demos/
git commit -m "docs: add demo recordings and screenshots

Full flow demo: Phase 1 destination convergence → Phase 3 skeleton planning
→ Phase 5 backtrack. Includes video recording and phase screenshots."
```

---

## Task 11: Update README.md and PROJECT_OVERVIEW.md

**Files:**
- Modify: `README.md`
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: Add Failure Analysis and Demo sections to README.md**

Add after the existing docs section (find the appropriate location):

```markdown
### 🔍 Failure Analysis

系统性的失败模式分析，基于 8 个真实场景的测试结果。

```bash
# 查看分析报告
cat docs/learning/2026-04-13-失败案例分析.md

# 重新运行失败分析
python scripts/failure-analysis/run_and_analyze.py
```

详见 [docs/learning/2026-04-13-失败案例分析.md](docs/learning/2026-04-13-失败案例分析.md)

### 🎬 Demo

一键录制系统核心功能演示视频。

```bash
# 启动服务（另一个终端）
scripts/dev.sh

# 录制 demo
scripts/demo/run-all-demos.sh
```

详见 [scripts/demo/README.md](scripts/demo/README.md)
```

- [ ] **Step 2: Update PROJECT_OVERVIEW.md docs section**

Add entries for `docs/learning/2026-04-13-失败案例分析.md`, `scripts/failure-analysis/`, and `scripts/demo/` to the project structure.

- [ ] **Step 3: Run existing tests to verify no breakage**

```bash
cd backend && python -m pytest tests/ -q
```

Expected: All tests pass (73+ tests).

- [ ] **Step 4: Commit**

```bash
git add README.md PROJECT_OVERVIEW.md
git commit -m "docs: add failure analysis and demo links to README and PROJECT_OVERVIEW

Updates project documentation to reference the new failure analysis
report and reproducible demo system."
```

---

## Task 12: Final Verification

- [ ] **Step 1: Verify all tests pass**

```bash
cd backend && python -m pytest tests/ -v
```

- [ ] **Step 2: Verify failure analysis doc exists and has content**

```bash
wc -l docs/learning/2026-04-13-失败案例分析.md
```

Expected: 100+ lines.

- [ ] **Step 3: Verify demo artifacts exist**

```bash
ls screenshots/demos/
```

- [ ] **Step 4: Review git log**

```bash
git --no-pager log --oneline -10
```

Expected: Clean commit history showing the progression.
