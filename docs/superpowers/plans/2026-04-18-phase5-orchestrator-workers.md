# Phase 5 Orchestrator-Workers 并行架构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Phase 5 的逐日行程生成从单 Agent 串行改为 Orchestrator-Workers 并行，通过上下文隔离和并发执行解决 token 膨胀、"只承诺不动手"和串行延迟三大问题。

**Architecture:** Python Orchestrator（纯代码调度器）并行 spawn N 个 Day Worker（轻量 LLM Agent），每个 Worker 独立上下文处理单天行程。Worker 共享相同的 system prompt prefix 以最大化 KV-Cache 命中率。Orchestrator 收集结果后做全局验证并写入状态。

**Tech Stack:** Python asyncio, 现有 LLMProvider 接口, 现有 ToolEngine, 现有 TravelPlanState

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `backend/agent/day_worker.py` | 新增 | Day Worker：构建 prompt、执行 LLM+工具循环、提取 DayPlan JSON |
| `backend/agent/orchestrator.py` | 新增 | Orchestrator：骨架切分、并发调度 Workers、全局验证、结果写入 |
| `backend/agent/worker_prompt.py` | 新增 | Worker system prompt 模板和构建函数 |
| `backend/agent/loop.py` | 修改 | Phase 5 入口分流：并行 or 串行 |
| `backend/context/manager.py` | 修改 | 新增 `build_worker_context` 方法 |
| `config.yaml` | 修改 | 新增 `phase5.parallel` 配置段 |
| `backend/config.py` | 修改 | 新增 `Phase5ParallelConfig` dataclass |
| `backend/tests/test_worker_prompt.py` | 新增 | Worker prompt 构建测试 |
| `backend/tests/test_day_worker.py` | 新增 | Day Worker 单元测试 |
| `backend/tests/test_orchestrator.py` | 新增 | Orchestrator 单元和集成测试 |

---

### Task 1: Phase5ParallelConfig 配置

**Files:**
- Modify: `backend/config.py`
- Modify: `config.yaml`
- Test: `backend/tests/test_config_parallel.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_config_parallel.py
from config import load_config, Phase5ParallelConfig


def test_phase5_parallel_defaults():
    """默认配置应启用并行模式。"""
    cfg = load_config()
    assert isinstance(cfg.phase5_parallel, Phase5ParallelConfig)
    assert cfg.phase5_parallel.enabled is True
    assert cfg.phase5_parallel.max_workers == 5
    assert cfg.phase5_parallel.worker_max_iterations == 5
    assert cfg.phase5_parallel.worker_timeout_seconds == 60
    assert cfg.phase5_parallel.fallback_to_serial is True


def test_phase5_parallel_disabled():
    """配置文件中可以禁用并行模式。"""
    cfg = load_config()
    # 此测试验证 Phase5ParallelConfig 可被构造为 disabled
    disabled = Phase5ParallelConfig(enabled=False)
    assert disabled.enabled is False
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest backend/tests/test_config_parallel.py -v`
Expected: FAIL with "cannot import name 'Phase5ParallelConfig'"

- [ ] **Step 3: 实现 Phase5ParallelConfig**

在 `backend/config.py` 中，在 `LLMConfig` 之后新增：

```python
@dataclass(frozen=True)
class Phase5ParallelConfig:
    enabled: bool = True
    max_workers: int = 5
    worker_max_iterations: int = 5
    worker_timeout_seconds: int = 60
    fallback_to_serial: bool = True
```

在 `AppConfig` 类中新增字段：

```python
phase5_parallel: Phase5ParallelConfig = field(default_factory=Phase5ParallelConfig)
```

在 `load_config` 的 `_build_app_config` 逻辑中，解析 `phase5.parallel` 段：

```python
def _build_phase5_parallel_config(raw: dict) -> Phase5ParallelConfig:
    p5 = raw.get("phase5", {}).get("parallel", {})
    return Phase5ParallelConfig(
        enabled=p5.get("enabled", True),
        max_workers=p5.get("max_workers", 5),
        worker_max_iterations=p5.get("worker_max_iterations", 5),
        worker_timeout_seconds=p5.get("worker_timeout_seconds", 60),
        fallback_to_serial=p5.get("fallback_to_serial", True),
    )
```

- [ ] **Step 4: 更新 config.yaml**

在 `config.yaml` 末尾新增：

```yaml
phase5:
  parallel:
    enabled: true
    max_workers: 5
    worker_max_iterations: 5
    worker_timeout_seconds: 60
    fallback_to_serial: true
```

- [ ] **Step 5: 运行测试验证通过**

Run: `pytest backend/tests/test_config_parallel.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/config.py config.yaml backend/tests/test_config_parallel.py
git commit -m "feat(config): add Phase5ParallelConfig for orchestrator-workers mode"
```

---

### Task 2: Worker Prompt 模板

**Files:**
- Create: `backend/agent/worker_prompt.py`
- Modify: `backend/context/manager.py`
- Test: `backend/tests/test_worker_prompt.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_worker_prompt.py
import pytest

from agent.worker_prompt import build_shared_prefix, build_day_suffix, DayTask
from state.models import (
    TravelPlanState,
    DateRange,
    Travelers,
    Accommodation,
    Preference,
    Constraint,
)


def _make_plan() -> TravelPlanState:
    plan = TravelPlanState()
    plan.destination = "东京"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-05")
    plan.travelers = Travelers(adults=2, children=0)
    plan.trip_brief = {
        "goal": "文化探索",
        "pace": "balanced",
        "departure_city": "上海",
    }
    plan.accommodation = Accommodation(area="新宿", hotel="新宿华盛顿酒店")
    plan.preferences = [Preference(key="must_do", value="浅草寺")]
    plan.constraints = [Constraint(type="hard", description="不去迪士尼")]
    return plan


def test_build_shared_prefix_contains_destination():
    plan = _make_plan()
    prefix = build_shared_prefix(plan)
    assert "东京" in prefix
    assert "新宿" in prefix
    assert "balanced" in prefix
    assert "浅草寺" in prefix
    assert "不去迪士尼" in prefix


def test_build_shared_prefix_stable_across_calls():
    """共享 prefix 应在多次调用间完全相同（KV-Cache 友好）。"""
    plan = _make_plan()
    prefix1 = build_shared_prefix(plan)
    prefix2 = build_shared_prefix(plan)
    assert prefix1 == prefix2


def test_build_day_suffix():
    task = DayTask(
        day=3,
        date="2026-05-03",
        skeleton_slice={
            "area": "浅草/上野",
            "theme": "文化体验",
            "core_activities": ["浅草寺", "上野公园"],
            "fatigue": "中等",
        },
        pace="balanced",
    )
    suffix = build_day_suffix(task)
    assert "第 3 天" in suffix
    assert "2026-05-03" in suffix
    assert "浅草/上野" in suffix
    assert "浅草寺" in suffix


def test_day_suffix_differs_per_day():
    """不同天的后缀必须不同。"""
    task_a = DayTask(day=1, date="2026-05-01", skeleton_slice={"area": "新宿"}, pace="balanced")
    task_b = DayTask(day=2, date="2026-05-02", skeleton_slice={"area": "浅草"}, pace="balanced")
    suffix_a = build_day_suffix(task_a)
    suffix_b = build_day_suffix(task_b)
    assert suffix_a != suffix_b
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest backend/tests/test_worker_prompt.py -v`
Expected: FAIL with "No module named 'agent.worker_prompt'"

- [ ] **Step 3: 实现 worker_prompt.py**

```python
# backend/agent/worker_prompt.py
"""Worker system prompt templates for Phase 5 parallel mode.

Design goal: maximize shared prefix across all Day Workers to achieve
high KV-Cache hit rates (Manus / Claude Code fork sub-agent pattern).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from state.models import TravelPlanState


@dataclass
class DayTask:
    """A single day's task extracted from the skeleton."""
    day: int
    date: str
    skeleton_slice: dict[str, Any]
    pace: str


_SOUL_PATH = Path("backend/context/soul.md")

_WORKER_ROLE = """## 角色

你是单日行程落地规划师。你的任务是为指定的一天生成完整的可执行 DayPlan。

## 硬法则

- 严格基于骨架安排展开，不要偷偷替换区域或主题。
- 区域连续性优先于景点密度——同一天的活动应在地理上聚拢。
- 时间安排必须留出现实缓冲（交通延误、排队、休息），不要把活动首尾无缝拼死。
- 用 get_poi_info 补齐缺失的坐标、票价、开放时间。
- 用 optimize_day_route 优化活动顺序。
- 用 calculate_route 验证关键移动是否可行。
- 餐饮可作为活动（category="food"），安排在合理时段。"""

_DAYPLAN_SCHEMA = """## DayPlan 严格 JSON 结构

完成规划后，你的**最后一条消息**必须包含一个 JSON 代码块，格式如下：

```json
{
  "day": <天数>,
  "date": "<YYYY-MM-DD>",
  "notes": "<当天补充说明>",
  "activities": [
    {
      "name": "<活动名称>",
      "location": {"name": "<地点名>", "lat": <纬度>, "lng": <经度>},
      "start_time": "HH:MM",
      "end_time": "HH:MM",
      "category": "<类别>",
      "cost": <人民币数字>,
      "transport_from_prev": "<从上一地点的交通方式>",
      "transport_duration_min": <分钟数>,
      "notes": "<可选备注>"
    }
  ]
}
```

硬约束：
- location 必须是 dict（含 name, lat, lng），不能是字符串
- start_time / end_time 必须是 "HH:MM" 格式
- cost 是数字（人民币），没有时填 0
- category 必须提供（shrine, museum, food, transport, activity, shopping, park 等）"""


def _load_soul() -> str:
    if _SOUL_PATH.exists():
        return _SOUL_PATH.read_text(encoding="utf-8")
    return "你是一个旅行规划 Agent。"


def build_shared_prefix(plan: TravelPlanState) -> str:
    """Build the shared prefix for all Day Workers.

    This prefix is identical across all workers to maximize KV-Cache hit rate.
    Do NOT include any per-day information here.
    """
    parts = [_load_soul()]

    # 旅行上下文（只读）
    parts.append("\n---\n\n## 旅行上下文\n")
    if plan.destination:
        parts.append(f"- 目的地：{plan.destination}")
    if plan.dates:
        parts.append(
            f"- 日期范围：{plan.dates.start} 至 {plan.dates.end}"
            f"（{plan.dates.total_days} 天）"
        )
    if plan.travelers:
        line = f"- 出行人数：{plan.travelers.adults} 成人"
        if plan.travelers.children:
            line += f"、{plan.travelers.children} 儿童"
        parts.append(line)
    if plan.trip_brief:
        parts.append("- 旅行画像：")
        for key, val in plan.trip_brief.items():
            if key in ("dates", "total_days"):
                continue
            parts.append(f"  - {key}: {val}")
    if plan.accommodation:
        parts.append(f"- 住宿区域：{plan.accommodation.area}")
        if plan.accommodation.hotel:
            parts.append(f"- 住宿酒店：{plan.accommodation.hotel}")
    if plan.budget:
        parts.append(
            f"- 总预算：{plan.budget.total} {plan.budget.currency}"
        )
    if plan.preferences:
        pref_strs = [f"{p.key}: {p.value}" for p in plan.preferences if p.key]
        if pref_strs:
            parts.append(f"- 用户偏好：{'; '.join(pref_strs)}")
    if plan.constraints:
        cons_strs = [f"[{c.type}] {c.description}" for c in plan.constraints]
        if cons_strs:
            parts.append(f"- 用户约束：{'; '.join(cons_strs)}")

    # 角色和规则
    parts.append("\n---\n")
    parts.append(_WORKER_ROLE)
    parts.append("\n---\n")
    parts.append(_DAYPLAN_SCHEMA)

    return "\n".join(parts)


def build_day_suffix(task: DayTask) -> str:
    """Build the per-day suffix that differs across workers."""
    parts = [f"\n---\n\n## 你的任务：第 {task.day} 天（{task.date}）\n"]

    sk = task.skeleton_slice
    parts.append("骨架安排：")
    if "area" in sk:
        parts.append(f"- 主区域：{sk['area']}")
    if "theme" in sk:
        parts.append(f"- 主题：{sk['theme']}")
    if "core_activities" in sk:
        activities = sk["core_activities"]
        if isinstance(activities, list):
            parts.append(f"- 核心活动：{'、'.join(str(a) for a in activities)}")
        else:
            parts.append(f"- 核心活动：{activities}")
    if "fatigue" in sk:
        parts.append(f"- 疲劳等级：{sk['fatigue']}")
    if "budget_level" in sk:
        parts.append(f"- 预算等级：{sk['budget_level']}")

    # 节奏 → 活动数量范围
    pace = task.pace
    if pace == "relaxed":
        count_range = "2-3"
    elif pace == "intensive":
        count_range = "4-5"
    else:
        count_range = "3-4"
    parts.append(f"\n节奏要求：{pace} → 本天 {count_range} 个核心活动")
    parts.append(
        "\n请为这一天生成完整的 DayPlan JSON。"
        "先用工具补齐信息和优化路线，最后输出 JSON。"
    )

    return "\n".join(parts)


def split_skeleton_to_day_tasks(
    skeleton: dict[str, Any],
    plan: TravelPlanState,
) -> list[DayTask]:
    """Split a selected skeleton into per-day tasks."""
    from datetime import date as dt_date, timedelta

    days_data = skeleton.get("days", [])
    start = dt_date.fromisoformat(plan.dates.start) if plan.dates else None
    pace = plan.trip_brief.get("pace", "balanced") if plan.trip_brief else "balanced"

    tasks: list[DayTask] = []
    for i, day_skeleton in enumerate(days_data):
        day_num = i + 1
        if start:
            day_date = (start + timedelta(days=i)).isoformat()
        else:
            day_date = f"day-{day_num}"
        tasks.append(
            DayTask(
                day=day_num,
                date=day_date,
                skeleton_slice=day_skeleton if isinstance(day_skeleton, dict) else {},
                pace=pace,
            )
        )
    return tasks
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest backend/tests/test_worker_prompt.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/agent/worker_prompt.py backend/tests/test_worker_prompt.py
git commit -m "feat(worker-prompt): add shared prefix and day suffix templates for parallel Phase 5"
```

---

### Task 3: Day Worker 执行引擎

**Files:**
- Create: `backend/agent/day_worker.py`
- Test: `backend/tests/test_day_worker.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_day_worker.py
import json
import pytest

from agent.day_worker import extract_dayplan_json, DayWorkerResult


def test_extract_dayplan_json_from_code_block():
    text = '''我来为你规划第 3 天的行程。

```json
{
  "day": 3,
  "date": "2026-05-03",
  "notes": "浅草-上野文化区",
  "activities": [
    {
      "name": "浅草寺",
      "location": {"name": "浅草寺", "lat": 35.7148, "lng": 139.7967},
      "start_time": "09:00",
      "end_time": "10:30",
      "category": "shrine",
      "cost": 0,
      "transport_from_prev": "地铁",
      "transport_duration_min": 20,
      "notes": ""
    }
  ]
}
```'''
    result = extract_dayplan_json(text)
    assert result is not None
    assert result["day"] == 3
    assert len(result["activities"]) == 1
    assert result["activities"][0]["name"] == "浅草寺"


def test_extract_dayplan_json_bare_json():
    """Worker 可能直接输出 JSON 不带代码块。"""
    data = {
        "day": 1,
        "date": "2026-05-01",
        "notes": "",
        "activities": [],
    }
    text = json.dumps(data, ensure_ascii=False)
    result = extract_dayplan_json(text)
    assert result is not None
    assert result["day"] == 1


def test_extract_dayplan_json_no_json():
    text = "我正在规划行程，请稍等..."
    result = extract_dayplan_json(text)
    assert result is None


def test_day_worker_result_success():
    r = DayWorkerResult(
        day=1,
        date="2026-05-01",
        success=True,
        dayplan={"day": 1, "date": "2026-05-01", "activities": []},
        error=None,
    )
    assert r.success is True
    assert r.dayplan is not None


def test_day_worker_result_failure():
    r = DayWorkerResult(
        day=2,
        date="2026-05-02",
        success=False,
        dayplan=None,
        error="LLM timeout",
    )
    assert r.success is False
    assert "timeout" in r.error
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest backend/tests/test_day_worker.py -v`
Expected: FAIL with "No module named 'agent.day_worker'"

- [ ] **Step 3: 实现 day_worker.py**

```python
# backend/agent/day_worker.py
"""Day Worker: executes a single-day planning task in isolated context.

Each worker gets its own LLM conversation and tool execution scope.
It receives a shared prefix + day-specific suffix as system prompt,
runs a mini agent loop (LLM call → tool calls → LLM call → ... → final JSON),
and returns a DayWorkerResult with the parsed DayPlan.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace

from agent.types import Message, Role, ToolCall, ToolResult
from agent.worker_prompt import DayTask, build_shared_prefix, build_day_suffix
from llm.base import LLMProvider
from llm.types import ChunkType
from state.models import TravelPlanState
from tools.engine import ToolEngine


@dataclass
class DayWorkerResult:
    """Result from a single Day Worker execution."""
    day: int
    date: str
    success: bool
    dayplan: dict[str, Any] | None
    error: str | None = None
    iterations: int = 0


# JSON extraction patterns
_JSON_CODE_BLOCK = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)
_JSON_OBJECT = re.compile(r"\{[^{}]*\"day\"[^{}]*\"activities\".*\}", re.DOTALL)


def extract_dayplan_json(text: str) -> dict[str, Any] | None:
    """Extract DayPlan JSON from worker's final message.

    Tries in order:
    1. JSON code block (```json ... ```)
    2. Bare JSON object containing "day" and "activities"
    """
    # Try code block first
    match = _JSON_CODE_BLOCK.search(text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try bare JSON: find outermost { ... } containing "day" and "activities"
    # Use a simple brace-counting approach
    brace_depth = 0
    start_idx = None
    for i, ch in enumerate(text):
        if ch == "{":
            if brace_depth == 0:
                start_idx = i
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
            if brace_depth == 0 and start_idx is not None:
                candidate = text[start_idx : i + 1]
                if '"day"' in candidate and '"activities"' in candidate:
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        pass
                start_idx = None
    return None


async def run_day_worker(
    *,
    llm: LLMProvider,
    tool_engine: ToolEngine,
    plan: TravelPlanState,
    task: DayTask,
    shared_prefix: str,
    max_iterations: int = 5,
    timeout_seconds: int = 60,
) -> DayWorkerResult:
    """Run a single Day Worker agent loop.

    The worker operates in its own isolated context:
    - system message = shared_prefix + day_suffix
    - user message = "请开始规划"
    - loops: LLM call → execute tools → LLM call → ... → extract JSON

    The worker does NOT have write tools. It only uses read tools
    (get_poi_info, optimize_day_route, calculate_route, etc.).
    """
    tracer = trace.get_tracer("day-worker")

    day_suffix = build_day_suffix(task)
    system_content = shared_prefix + day_suffix

    messages: list[Message] = [
        Message(role=Role.SYSTEM, content=system_content),
        Message(role=Role.USER, content="请开始规划这一天的行程。"),
    ]

    # Build tool list: only read tools for Phase 5
    worker_tools = _get_worker_tools(tool_engine)

    iterations = 0

    try:
        async with asyncio.timeout(timeout_seconds):
            with tracer.start_as_current_span(f"day_worker.run.day_{task.day}") as span:
                span.set_attribute("day", task.day)
                span.set_attribute("date", task.date)

                for iteration in range(max_iterations):
                    iterations = iteration + 1

                    # LLM call
                    tool_calls: list[ToolCall] = []
                    text_chunks: list[str] = []

                    async for chunk in llm.chat(
                        messages, tools=worker_tools, stream=True
                    ):
                        if chunk.type == ChunkType.TEXT_DELTA:
                            text_chunks.append(chunk.content or "")
                        elif (
                            chunk.type == ChunkType.TOOL_CALL_START
                            and chunk.tool_call
                        ):
                            tool_calls.append(chunk.tool_call)

                    assistant_text = "".join(text_chunks)

                    # No tool calls → final response, extract JSON
                    if not tool_calls:
                        messages.append(
                            Message(role=Role.ASSISTANT, content=assistant_text)
                        )
                        dayplan = extract_dayplan_json(assistant_text)
                        if dayplan is not None:
                            return DayWorkerResult(
                                day=task.day,
                                date=task.date,
                                success=True,
                                dayplan=dayplan,
                                iterations=iterations,
                            )
                        # No JSON found, but no tools either — worker is stuck
                        return DayWorkerResult(
                            day=task.day,
                            date=task.date,
                            success=False,
                            dayplan=None,
                            error=f"Worker 未输出有效 DayPlan JSON (iteration {iterations})",
                            iterations=iterations,
                        )

                    # Has tool calls → execute them and continue
                    messages.append(
                        Message(
                            role=Role.ASSISTANT,
                            content=assistant_text or None,
                            tool_calls=tool_calls,
                        )
                    )

                    # Execute tools (all read, can be parallel)
                    results = await tool_engine.execute_batch(tool_calls)
                    for tc, result in zip(tool_calls, results):
                        messages.append(
                            Message(role=Role.TOOL, tool_result=result)
                        )

                # Exhausted iterations
                # Try to extract JSON from the last assistant message
                last_text = ""
                for msg in reversed(messages):
                    if msg.role == Role.ASSISTANT and msg.content:
                        last_text = msg.content
                        break
                dayplan = extract_dayplan_json(last_text)
                if dayplan is not None:
                    return DayWorkerResult(
                        day=task.day,
                        date=task.date,
                        success=True,
                        dayplan=dayplan,
                        iterations=iterations,
                    )
                return DayWorkerResult(
                    day=task.day,
                    date=task.date,
                    success=False,
                    dayplan=None,
                    error=f"Worker 耗尽 {max_iterations} 轮迭代未输出 DayPlan",
                    iterations=iterations,
                )

    except TimeoutError:
        return DayWorkerResult(
            day=task.day,
            date=task.date,
            success=False,
            dayplan=None,
            error=f"Worker 超时 ({timeout_seconds}s)",
            iterations=iterations,
        )
    except Exception as e:
        return DayWorkerResult(
            day=task.day,
            date=task.date,
            success=False,
            dayplan=None,
            error=f"Worker 异常: {type(e).__name__}: {e}",
            iterations=iterations,
        )


def _get_worker_tools(tool_engine: ToolEngine) -> list[dict[str, Any]]:
    """Get read-only tools available to Day Workers.

    Workers get Phase 5 tools minus write tools.
    """
    _WORKER_TOOL_NAMES = {
        "get_poi_info",
        "optimize_day_route",
        "calculate_route",
        "check_availability",
        "check_weather",
        "xiaohongshu_search_notes",
        "xiaohongshu_read_note",
        "xiaohongshu_get_comments",
    }
    all_tools = []
    for name in _WORKER_TOOL_NAMES:
        tool_def = tool_engine.get_tool(name)
        if tool_def is not None:
            all_tools.append(tool_def.to_schema())
    return all_tools
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest backend/tests/test_day_worker.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/agent/day_worker.py backend/tests/test_day_worker.py
git commit -m "feat(day-worker): implement single-day LLM agent with isolated context"
```

---

### Task 4: Orchestrator 核心逻辑

**Files:**
- Create: `backend/agent/orchestrator.py`
- Test: `backend/tests/test_orchestrator.py`

- [ ] **Step 1: 写失败测试——骨架切分**

```python
# backend/tests/test_orchestrator.py
import pytest

from agent.orchestrator import Phase5Orchestrator, GlobalValidationIssue
from agent.worker_prompt import DayTask
from state.models import (
    TravelPlanState,
    DateRange,
    Travelers,
    Accommodation,
    Budget,
    DayPlan,
    Activity,
    Location,
)


def _make_plan_with_skeleton() -> TravelPlanState:
    plan = TravelPlanState()
    plan.phase = 5
    plan.destination = "东京"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.travelers = Travelers(adults=2)
    plan.trip_brief = {"goal": "文化探索", "pace": "balanced", "departure_city": "上海"}
    plan.accommodation = Accommodation(area="新宿", hotel="新宿华盛顿酒店")
    plan.budget = Budget(total=30000, currency="CNY")
    plan.selected_skeleton_id = "plan_A"
    plan.skeleton_plans = [
        {
            "id": "plan_A",
            "name": "平衡版",
            "days": [
                {"area": "新宿/原宿", "theme": "潮流文化", "core_activities": ["明治神宫", "竹下通"], "fatigue": "低"},
                {"area": "浅草/上野", "theme": "传统文化", "core_activities": ["浅草寺", "上野公园"], "fatigue": "中等"},
                {"area": "涩谷/银座", "theme": "购物", "core_activities": ["涩谷十字路口", "银座六丁目"], "fatigue": "中等"},
            ],
        }
    ]
    return plan


class TestSplitTasks:
    def test_split_produces_correct_day_count(self):
        plan = _make_plan_with_skeleton()
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        tasks = orch._split_tasks()
        assert len(tasks) == 3

    def test_split_assigns_correct_dates(self):
        plan = _make_plan_with_skeleton()
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        tasks = orch._split_tasks()
        assert tasks[0].date == "2026-05-01"
        assert tasks[1].date == "2026-05-02"
        assert tasks[2].date == "2026-05-03"

    def test_split_preserves_skeleton_data(self):
        plan = _make_plan_with_skeleton()
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        tasks = orch._split_tasks()
        assert tasks[0].skeleton_slice["area"] == "新宿/原宿"
        assert tasks[1].skeleton_slice["area"] == "浅草/上野"

    def test_split_raises_if_no_skeleton(self):
        plan = _make_plan_with_skeleton()
        plan.selected_skeleton_id = None
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        with pytest.raises(ValueError, match="未找到已选骨架"):
            orch._split_tasks()


class TestGlobalValidation:
    def _make_dayplan_dict(self, day: int, date: str, activities: list[dict]) -> dict:
        return {"day": day, "date": date, "notes": "", "activities": activities}

    def _make_activity(self, name: str, cost: float = 0) -> dict:
        return {
            "name": name,
            "location": {"name": name, "lat": 35.0, "lng": 139.0},
            "start_time": "09:00",
            "end_time": "10:00",
            "category": "activity",
            "cost": cost,
        }

    def test_no_issues_when_valid(self):
        plan = _make_plan_with_skeleton()
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [self._make_activity("A", 5000)]),
            self._make_dayplan_dict(2, "2026-05-02", [self._make_activity("B", 5000)]),
            self._make_dayplan_dict(3, "2026-05-03", [self._make_activity("C", 5000)]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        assert len(issues) == 0

    def test_detects_poi_duplicate(self):
        plan = _make_plan_with_skeleton()
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [self._make_activity("浅草寺")]),
            self._make_dayplan_dict(2, "2026-05-02", [self._make_activity("浅草寺")]),
            self._make_dayplan_dict(3, "2026-05-03", [self._make_activity("C")]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        poi_issues = [i for i in issues if i.issue_type == "poi_duplicate"]
        assert len(poi_issues) >= 1

    def test_detects_budget_overrun(self):
        plan = _make_plan_with_skeleton()
        plan.budget = Budget(total=100, currency="CNY")
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [self._make_activity("A", 50)]),
            self._make_dayplan_dict(2, "2026-05-02", [self._make_activity("B", 50)]),
            self._make_dayplan_dict(3, "2026-05-03", [self._make_activity("C", 50)]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        budget_issues = [i for i in issues if i.issue_type == "budget_overrun"]
        assert len(budget_issues) >= 1
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest backend/tests/test_orchestrator.py -v`
Expected: FAIL with "No module named 'agent.orchestrator'"

- [ ] **Step 3: 实现 orchestrator.py**

```python
# backend/agent/orchestrator.py
"""Phase 5 Orchestrator: parallel Day Worker dispatch and result collection.

The orchestrator is pure Python (not an LLM agent). It:
1. Splits the selected skeleton into per-day tasks
2. Builds a shared prompt prefix (maximizing KV-Cache hits)
3. Spawns N Day Workers in parallel via asyncio
4. Collects results and performs global validation
5. Writes validated DayPlans to state
6. Retries or falls back to serial on failures
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from opentelemetry import trace

from agent.day_worker import DayWorkerResult, run_day_worker
from agent.worker_prompt import (
    DayTask,
    build_shared_prefix,
    split_skeleton_to_day_tasks,
)
from config import Phase5ParallelConfig
from llm.base import LLMProvider
from llm.types import ChunkType, LLMChunk
from state.models import TravelPlanState
from state.plan_writers import append_one_day_plan, replace_all_daily_plans
from tools.engine import ToolEngine

logger = logging.getLogger(__name__)


@dataclass
class GlobalValidationIssue:
    issue_type: str  # "poi_duplicate" | "budget_overrun" | "coverage_gap"
    description: str
    affected_days: list[int] = field(default_factory=list)


class Phase5Orchestrator:
    def __init__(
        self,
        *,
        plan: TravelPlanState,
        llm: LLMProvider | None,
        tool_engine: ToolEngine | None,
        config: Phase5ParallelConfig | None,
    ):
        self.plan = plan
        self.llm = llm
        self.tool_engine = tool_engine
        self.config = config or Phase5ParallelConfig()

    def _find_selected_skeleton(self) -> dict[str, Any] | None:
        if not self.plan.selected_skeleton_id or not self.plan.skeleton_plans:
            return None
        sid = self.plan.selected_skeleton_id
        for skeleton in self.plan.skeleton_plans:
            if not isinstance(skeleton, dict):
                continue
            if skeleton.get("id") == sid or skeleton.get("name") == sid:
                return skeleton
        valid = [s for s in self.plan.skeleton_plans if isinstance(s, dict)]
        if len(valid) == 1:
            return valid[0]
        return None

    def _split_tasks(self) -> list[DayTask]:
        skeleton = self._find_selected_skeleton()
        if skeleton is None:
            raise ValueError("未找到已选骨架方案")
        return split_skeleton_to_day_tasks(skeleton, self.plan)

    def _global_validate(
        self, dayplans: list[dict[str, Any]]
    ) -> list[GlobalValidationIssue]:
        issues: list[GlobalValidationIssue] = []

        # 1. POI 去重
        poi_to_days: dict[str, list[int]] = {}
        for dp in dayplans:
            day_num = dp.get("day", 0)
            for act in dp.get("activities", []):
                name = act.get("name", "")
                if name:
                    poi_to_days.setdefault(name, []).append(day_num)
        for poi_name, days in poi_to_days.items():
            if len(days) > 1:
                issues.append(
                    GlobalValidationIssue(
                        issue_type="poi_duplicate",
                        description=f"POI '{poi_name}' 出现在多天: {days}",
                        affected_days=days[1:],  # 后续出现的天需要修复
                    )
                )

        # 2. 预算检查
        if self.plan.budget:
            total_cost = sum(
                act.get("cost", 0)
                for dp in dayplans
                for act in dp.get("activities", [])
            )
            if total_cost > self.plan.budget.total:
                # 找出花费最高的天
                day_costs = []
                for dp in dayplans:
                    day_cost = sum(
                        act.get("cost", 0) for act in dp.get("activities", [])
                    )
                    day_costs.append((dp.get("day", 0), day_cost))
                day_costs.sort(key=lambda x: x[1], reverse=True)
                issues.append(
                    GlobalValidationIssue(
                        issue_type="budget_overrun",
                        description=(
                            f"总花费 {total_cost} 超出预算 "
                            f"{self.plan.budget.total} {self.plan.budget.currency}"
                        ),
                        affected_days=[d for d, _ in day_costs[:2]],
                    )
                )

        # 3. 天数覆盖检查
        if self.plan.dates:
            expected_days = set(range(1, self.plan.dates.total_days + 1))
            actual_days = {dp.get("day", 0) for dp in dayplans}
            missing = expected_days - actual_days
            if missing:
                issues.append(
                    GlobalValidationIssue(
                        issue_type="coverage_gap",
                        description=f"缺少天数: {sorted(missing)}",
                        affected_days=sorted(missing),
                    )
                )

        return issues

    async def run(self) -> AsyncIterator[LLMChunk]:
        """Execute parallel Phase 5 generation.

        Yields LLMChunk events for frontend progress display.
        """
        tracer = trace.get_tracer("phase5-orchestrator")

        with tracer.start_as_current_span("orchestrator.run") as span:
            # 1. Split tasks
            yield LLMChunk(
                type=ChunkType.AGENT_STATUS,
                agent_status={"stage": "planning", "hint": "正在分解行程任务..."},
            )
            tasks = self._split_tasks()
            span.set_attribute("total_days", len(tasks))

            # 2. Build shared prefix
            shared_prefix = build_shared_prefix(self.plan)

            # 3. Spawn workers with concurrency control
            yield LLMChunk(
                type=ChunkType.AGENT_STATUS,
                agent_status={
                    "stage": "thinking",
                    "hint": f"正在并行规划第 1-{len(tasks)} 天的详细行程...",
                },
            )

            semaphore = asyncio.Semaphore(self.config.max_workers)

            async def _run_with_semaphore(task: DayTask) -> DayWorkerResult:
                async with semaphore:
                    return await run_day_worker(
                        llm=self.llm,
                        tool_engine=self.tool_engine,
                        plan=self.plan,
                        task=task,
                        shared_prefix=shared_prefix,
                        max_iterations=self.config.worker_max_iterations,
                        timeout_seconds=self.config.worker_timeout_seconds,
                    )

            raw_results = await asyncio.gather(
                *[_run_with_semaphore(t) for t in tasks],
                return_exceptions=True,
            )

            # 4. Collect results
            successes: list[DayWorkerResult] = []
            failures: list[tuple[DayTask, str]] = []

            for task, result in zip(tasks, raw_results):
                if isinstance(result, Exception):
                    failures.append((task, f"Exception: {result}"))
                    logger.error("Day %d worker exception: %s", task.day, result)
                elif result.success:
                    successes.append(result)
                    yield LLMChunk(
                        type=ChunkType.AGENT_STATUS,
                        agent_status={
                            "stage": "summarizing",
                            "hint": f"第 {result.day} 天规划完成",
                        },
                    )
                else:
                    failures.append((task, result.error or "Unknown error"))
                    logger.warning(
                        "Day %d worker failed: %s", task.day, result.error
                    )

            span.set_attribute("successes", len(successes))
            span.set_attribute("failures", len(failures))

            # 5. Check if we should fall back to serial
            if (
                self.config.fallback_to_serial
                and len(failures) > len(tasks) / 2
            ):
                logger.warning(
                    "Parallel mode failure rate %.0f%%, falling back to serial",
                    len(failures) / len(tasks) * 100,
                )
                yield LLMChunk(
                    type=ChunkType.AGENT_STATUS,
                    agent_status={
                        "stage": "thinking",
                        "hint": "并行模式失败率过高，切换到串行模式...",
                    },
                )
                # Signal caller to fall back
                return

            # 6. Retry failed days (one at a time)
            for task, error_msg in failures:
                logger.info("Retrying day %d (previous error: %s)", task.day, error_msg)
                retry_result = await run_day_worker(
                    llm=self.llm,
                    tool_engine=self.tool_engine,
                    plan=self.plan,
                    task=task,
                    shared_prefix=shared_prefix,
                    max_iterations=self.config.worker_max_iterations,
                    timeout_seconds=self.config.worker_timeout_seconds,
                )
                if retry_result.success:
                    successes.append(retry_result)
                    yield LLMChunk(
                        type=ChunkType.AGENT_STATUS,
                        agent_status={
                            "stage": "summarizing",
                            "hint": f"第 {retry_result.day} 天（重试）规划完成",
                        },
                    )
                else:
                    logger.error(
                        "Day %d retry also failed: %s",
                        task.day,
                        retry_result.error,
                    )

            # 7. Sort and validate
            dayplans = sorted(
                [r.dayplan for r in successes if r.dayplan],
                key=lambda dp: dp.get("day", 0),
            )

            yield LLMChunk(
                type=ChunkType.AGENT_STATUS,
                agent_status={"stage": "summarizing", "hint": "正在做最终验证..."},
            )
            issues = self._global_validate(dayplans)
            for issue in issues:
                logger.warning("Global validation: %s", issue.description)

            # 8. Write results
            if dayplans:
                replace_all_daily_plans(self.plan, dayplans)
                yield LLMChunk(
                    type=ChunkType.AGENT_STATUS,
                    agent_status={
                        "stage": "summarizing",
                        "hint": f"已写入 {len(dayplans)} 天行程",
                    },
                )

            # 9. Generate summary text
            summary_lines = [f"已完成 {len(dayplans)}/{len(tasks)} 天的行程规划。\n"]
            for dp in dayplans:
                day_num = dp.get("day", "?")
                notes = dp.get("notes", "")
                acts = dp.get("activities", [])
                act_names = [a.get("name", "") for a in acts[:5]]
                summary_lines.append(
                    f"**第 {day_num} 天**：{notes or ''}  \n"
                    f"{'→'.join(act_names)}\n"
                )
            if issues:
                summary_lines.append("\n⚠️ 发现以下问题需要关注：")
                for issue in issues:
                    summary_lines.append(f"- {issue.description}")

            summary_text = "\n".join(summary_lines)
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content=summary_text)
            yield LLMChunk(type=ChunkType.DONE)
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest backend/tests/test_orchestrator.py -v`
Expected: PASS (同步测试部分：_split_tasks 和 _global_validate)

- [ ] **Step 5: 提交**

```bash
git add backend/agent/orchestrator.py backend/tests/test_orchestrator.py
git commit -m "feat(orchestrator): implement Phase 5 parallel dispatch with global validation"
```

---

### Task 5: AgentLoop 集成——Phase 5 入口分流

**Files:**
- Modify: `backend/agent/loop.py`
- Test: `backend/tests/test_loop_phase5_routing.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_loop_phase5_routing.py
import pytest

from agent.loop import AgentLoop
from config import Phase5ParallelConfig


class TestPhase5Routing:
    def test_should_use_parallel_when_enabled(self):
        """Phase 5 + 并行启用 + daily_plans 为空 → 应使用并行模式。"""
        from state.models import TravelPlanState, DateRange, Accommodation

        plan = TravelPlanState()
        plan.phase = 5
        plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
        plan.selected_skeleton_id = "plan_A"
        plan.skeleton_plans = [{"id": "plan_A", "days": [{}, {}, {}]}]
        plan.accommodation = Accommodation(area="新宿")
        plan.daily_plans = []

        config = Phase5ParallelConfig(enabled=True)
        assert AgentLoop.should_use_parallel_phase5(plan, config) is True

    def test_should_not_use_parallel_when_disabled(self):
        from state.models import TravelPlanState, DateRange, Accommodation

        plan = TravelPlanState()
        plan.phase = 5
        plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
        plan.selected_skeleton_id = "plan_A"
        plan.skeleton_plans = [{"id": "plan_A", "days": [{}, {}, {}]}]
        plan.accommodation = Accommodation(area="新宿")
        plan.daily_plans = []

        config = Phase5ParallelConfig(enabled=False)
        assert AgentLoop.should_use_parallel_phase5(plan, config) is False

    def test_should_not_use_parallel_when_plans_exist(self):
        """daily_plans 已有数据 → 用户在修改，用串行模式。"""
        from state.models import TravelPlanState, DateRange, Accommodation, DayPlan

        plan = TravelPlanState()
        plan.phase = 5
        plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
        plan.selected_skeleton_id = "plan_A"
        plan.skeleton_plans = [{"id": "plan_A", "days": [{}, {}, {}]}]
        plan.accommodation = Accommodation(area="新宿")
        plan.daily_plans = [DayPlan(day=1, date="2026-05-01")]

        config = Phase5ParallelConfig(enabled=True)
        assert AgentLoop.should_use_parallel_phase5(plan, config) is False

    def test_should_not_use_parallel_when_not_phase5(self):
        from state.models import TravelPlanState

        plan = TravelPlanState()
        plan.phase = 3

        config = Phase5ParallelConfig(enabled=True)
        assert AgentLoop.should_use_parallel_phase5(plan, config) is False
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest backend/tests/test_loop_phase5_routing.py -v`
Expected: FAIL with "has no attribute 'should_use_parallel_phase5'"

- [ ] **Step 3: 在 AgentLoop 中添加路由方法**

在 `backend/agent/loop.py` 的 `AgentLoop` 类中添加静态方法：

```python
@staticmethod
def should_use_parallel_phase5(
    plan: TravelPlanState | None,
    config: Phase5ParallelConfig | None,
) -> bool:
    """Determine if Phase 5 should use parallel orchestrator mode.

    Parallel mode is used only for initial full generation:
    - Phase must be 5
    - Parallel config must be enabled
    - daily_plans must be empty (not a modification request)
    - selected_skeleton_id must exist
    """
    if plan is None or config is None:
        return False
    if not config.enabled:
        return False
    if plan.phase != 5:
        return False
    if plan.daily_plans:  # already has plans → user is modifying
        return False
    if not plan.selected_skeleton_id:
        return False
    if not plan.skeleton_plans:
        return False
    return True
```

在 `AgentLoop.__init__` 中新增 `phase5_parallel_config` 参数：

```python
def __init__(
    self,
    ...
    phase5_parallel_config: Phase5ParallelConfig | None = None,
):
    ...
    self.phase5_parallel_config = phase5_parallel_config
```

在 `AgentLoop.run` 方法的开头（`for iteration` 循环之前），添加并行模式分流：

```python
# Phase 5 parallel mode: dispatch orchestrator instead of serial loop
if self.should_use_parallel_phase5(self.plan, self.phase5_parallel_config):
    from agent.orchestrator import Phase5Orchestrator

    orchestrator = Phase5Orchestrator(
        plan=self.plan,
        llm=self.llm,
        tool_engine=self.tool_engine,
        config=self.phase5_parallel_config,
    )
    async for chunk in orchestrator.run():
        yield chunk
    return
```

需要在文件顶部添加 import（或使用延迟 import，已在代码中通过局部 import 实现）。

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest backend/tests/test_loop_phase5_routing.py -v`
Expected: PASS

- [ ] **Step 5: 运行现有 loop 测试确保无回归**

Run: `pytest backend/tests/ -k "test_" --ignore=backend/tests/test_e2e_golden_path.py -v --timeout=30`
Expected: 所有现有测试 PASS

- [ ] **Step 6: 提交**

```bash
git add backend/agent/loop.py backend/tests/test_loop_phase5_routing.py
git commit -m "feat(loop): add Phase 5 parallel mode routing in AgentLoop"
```

---

### Task 6: ContextManager 扩展——build_worker_context

**Files:**
- Modify: `backend/context/manager.py`
- Test: `backend/tests/test_context_manager_worker.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_context_manager_worker.py
from context.manager import ContextManager
from state.models import (
    TravelPlanState,
    DateRange,
    Travelers,
    Accommodation,
    Preference,
    Constraint,
)


def _make_plan() -> TravelPlanState:
    plan = TravelPlanState()
    plan.destination = "东京"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-05")
    plan.travelers = Travelers(adults=2)
    plan.trip_brief = {"goal": "文化探索", "pace": "balanced", "departure_city": "上海"}
    plan.accommodation = Accommodation(area="新宿")
    plan.preferences = [Preference(key="must_do", value="浅草寺")]
    plan.constraints = [Constraint(type="hard", description="不去迪士尼")]
    return plan


def test_build_worker_context_returns_dict():
    cm = ContextManager()
    plan = _make_plan()
    ctx = cm.build_worker_context(plan)
    assert isinstance(ctx, dict)
    assert "destination" in ctx
    assert ctx["destination"] == "东京"
    assert "trip_brief" in ctx
    assert "accommodation_area" in ctx


def test_build_worker_context_excludes_mutable_state():
    """Worker context 不应包含 daily_plans 等可变状态。"""
    cm = ContextManager()
    plan = _make_plan()
    ctx = cm.build_worker_context(plan)
    assert "daily_plans" not in ctx
    assert "skeleton_plans" not in ctx
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest backend/tests/test_context_manager_worker.py -v`
Expected: FAIL with "has no attribute 'build_worker_context'"

- [ ] **Step 3: 在 ContextManager 中添加方法**

在 `backend/context/manager.py` 的 `ContextManager` 类中添加：

```python
def build_worker_context(self, plan: TravelPlanState) -> dict[str, Any]:
    """Build a read-only context dict for Day Workers.

    Contains only the immutable planning context that workers need.
    Excludes mutable state like daily_plans and skeleton_plans.
    """
    ctx: dict[str, Any] = {}
    if plan.destination:
        ctx["destination"] = plan.destination
    if plan.dates:
        ctx["dates_start"] = plan.dates.start
        ctx["dates_end"] = plan.dates.end
        ctx["total_days"] = plan.dates.total_days
    if plan.travelers:
        ctx["adults"] = plan.travelers.adults
        ctx["children"] = plan.travelers.children
    if plan.trip_brief:
        ctx["trip_brief"] = {
            k: v for k, v in plan.trip_brief.items()
            if k not in ("dates", "total_days")
        }
    if plan.accommodation:
        ctx["accommodation_area"] = plan.accommodation.area
        if plan.accommodation.hotel:
            ctx["accommodation_hotel"] = plan.accommodation.hotel
    if plan.budget:
        ctx["budget_total"] = plan.budget.total
        ctx["budget_currency"] = plan.budget.currency
    if plan.preferences:
        ctx["preferences"] = [
            {"key": p.key, "value": p.value}
            for p in plan.preferences if p.key
        ]
    if plan.constraints:
        ctx["constraints"] = [
            {"type": c.type, "description": c.description}
            for c in plan.constraints
        ]
    return ctx
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest backend/tests/test_context_manager_worker.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/context/manager.py backend/tests/test_context_manager_worker.py
git commit -m "feat(context): add build_worker_context for Day Worker read-only context"
```

---

### Task 7: 并行模式集成测试

**Files:**
- Test: `backend/tests/test_parallel_phase5_integration.py`

- [ ] **Step 1: 写集成测试（mock LLM）**

```python
# backend/tests/test_parallel_phase5_integration.py
"""Integration tests for Phase 5 parallel orchestrator mode.

Uses mock LLM that returns pre-built DayPlan JSON to verify
the end-to-end flow: split → spawn → collect → validate → write.
"""
import asyncio
import json
import pytest

from agent.orchestrator import Phase5Orchestrator
from agent.day_worker import DayWorkerResult
from config import Phase5ParallelConfig
from llm.types import ChunkType, LLMChunk
from state.models import (
    TravelPlanState,
    DateRange,
    Travelers,
    Accommodation,
    Budget,
)


def _make_plan() -> TravelPlanState:
    plan = TravelPlanState()
    plan.phase = 5
    plan.destination = "东京"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.travelers = Travelers(adults=2)
    plan.trip_brief = {"goal": "文化探索", "pace": "balanced", "departure_city": "上海"}
    plan.accommodation = Accommodation(area="新宿", hotel="新宿华盛顿酒店")
    plan.budget = Budget(total=30000, currency="CNY")
    plan.selected_skeleton_id = "plan_A"
    plan.skeleton_plans = [
        {
            "id": "plan_A",
            "name": "平衡版",
            "days": [
                {"area": "新宿/原宿", "theme": "潮流文化", "core_activities": ["明治神宫"], "fatigue": "低"},
                {"area": "浅草/上野", "theme": "传统文化", "core_activities": ["浅草寺"], "fatigue": "中等"},
                {"area": "涩谷", "theme": "购物", "core_activities": ["涩谷十字路口"], "fatigue": "中等"},
            ],
        }
    ]
    return plan


def _make_dayplan_json(day: int, date: str, name: str) -> str:
    return json.dumps(
        {
            "day": day,
            "date": date,
            "notes": f"Day {day} test",
            "activities": [
                {
                    "name": name,
                    "location": {"name": name, "lat": 35.0, "lng": 139.0},
                    "start_time": "09:00",
                    "end_time": "11:00",
                    "category": "activity",
                    "cost": 1000,
                    "transport_from_prev": "地铁",
                    "transport_duration_min": 15,
                    "notes": "",
                }
            ],
        },
        ensure_ascii=False,
    )


class MockLLM:
    """Mock LLM that returns DayPlan JSON based on the day number in the prompt."""

    def __init__(self, day_responses: dict[int, str]):
        self._day_responses = day_responses

    async def chat(self, messages, **kwargs):
        # Extract day number from system message
        system_msg = messages[0].content or ""
        day_num = 1
        for d in range(1, 20):
            if f"第 {d} 天" in system_msg:
                day_num = d
                break

        text = self._day_responses.get(day_num, '{"day": 0, "date": "", "activities": []}')

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content=text)
        yield LLMChunk(type=ChunkType.DONE)

    async def count_tokens(self, messages):
        return 100

    async def get_context_window(self):
        return 200000


class MockToolEngine:
    """Mock ToolEngine that returns empty results."""

    def get_tool(self, name):
        return None

    async def execute_batch(self, calls):
        from agent.types import ToolResult

        return [
            ToolResult(tool_call_id=tc.id, status="success", data={})
            for tc in calls
        ]


@pytest.mark.asyncio
async def test_parallel_happy_path():
    """All 3 days succeed → daily_plans has 3 entries."""
    plan = _make_plan()
    llm = MockLLM(
        {
            1: _make_dayplan_json(1, "2026-05-01", "明治神宫"),
            2: _make_dayplan_json(2, "2026-05-02", "浅草寺"),
            3: _make_dayplan_json(3, "2026-05-03", "涩谷十字路口"),
        }
    )
    tool_engine = MockToolEngine()
    config = Phase5ParallelConfig(enabled=True, max_workers=3)

    orch = Phase5Orchestrator(
        plan=plan, llm=llm, tool_engine=tool_engine, config=config
    )

    chunks = []
    async for chunk in orch.run():
        chunks.append(chunk)

    # Verify daily_plans were written
    assert len(plan.daily_plans) == 3
    assert plan.daily_plans[0].day == 1
    assert plan.daily_plans[1].day == 2
    assert plan.daily_plans[2].day == 3

    # Verify DONE chunk was emitted
    done_chunks = [c for c in chunks if c.type == ChunkType.DONE]
    assert len(done_chunks) == 1


@pytest.mark.asyncio
async def test_parallel_detects_poi_duplicate():
    """Duplicate POI across days should be detected in global validation."""
    plan = _make_plan()
    llm = MockLLM(
        {
            1: _make_dayplan_json(1, "2026-05-01", "浅草寺"),
            2: _make_dayplan_json(2, "2026-05-02", "浅草寺"),  # duplicate!
            3: _make_dayplan_json(3, "2026-05-03", "涩谷十字路口"),
        }
    )
    tool_engine = MockToolEngine()
    config = Phase5ParallelConfig(enabled=True, max_workers=3)

    orch = Phase5Orchestrator(
        plan=plan, llm=llm, tool_engine=tool_engine, config=config
    )

    chunks = []
    async for chunk in orch.run():
        chunks.append(chunk)

    # Plans still written (validation is advisory for now)
    assert len(plan.daily_plans) == 3

    # Check that summary mentions the issue
    text_chunks = [c for c in chunks if c.type == ChunkType.TEXT_DELTA]
    summary = "".join(c.content or "" for c in text_chunks)
    assert "浅草寺" in summary
```

- [ ] **Step 2: 运行测试验证通过**

Run: `pytest backend/tests/test_parallel_phase5_integration.py -v`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_parallel_phase5_integration.py
git commit -m "test(parallel): add integration tests for Phase 5 orchestrator happy path"
```

---

### Task 8: 运行全量测试 + 更新 PROJECT_OVERVIEW.md

**Files:**
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: 运行全量测试**

Run: `pytest backend/tests/ --ignore=backend/tests/test_e2e_golden_path.py -v --timeout=60`
Expected: 所有测试 PASS，无回归

- [ ] **Step 2: 更新 PROJECT_OVERVIEW.md**

在 Phase 5 相关章节中新增并行模式说明：

- Phase 5 现在支持两种执行模式：串行模式（默认回退）和并行 Orchestrator-Workers 模式
- 并行模式在 `config.yaml` 的 `phase5.parallel` 段控制
- 新增文件：`agent/orchestrator.py`、`agent/day_worker.py`、`agent/worker_prompt.py`

- [ ] **Step 3: 提交**

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: update PROJECT_OVERVIEW with Phase 5 parallel orchestrator architecture"
```
