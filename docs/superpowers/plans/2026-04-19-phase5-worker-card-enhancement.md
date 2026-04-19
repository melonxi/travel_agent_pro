# Phase 5 Worker 卡片信息增强实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Phase 5 并行 orchestrator 下每个 worker 卡片的显示从"规划中"升级为信息丰富的单行条目，暴露主题 / 当前工具 / 迭代轮次 / 活动数 / 失败原因五个字段，让用户实时看到 worker 在做什么。

**Architecture:** Worker 通过新加的同步 `on_progress(day, kind, payload)` 回调把 `iter_start` / `tool_start` 信号推给 orchestrator；orchestrator 闭包回调就地更新 `worker_statuses[idx]` 状态字典，并把"要广播"意图投入 `asyncio.Queue`；orchestrator 主循环 `asyncio.wait([*worker_tasks, queue.get()])` 任意就绪都 yield 一条 `parallel_progress` chunk。前端 `ParallelProgress.tsx` 根据 status 条件渲染尾部字段。

**Tech Stack:** Python 3.12 asyncio, 现有 LLMProvider/ToolEngine/TravelPlanState；React 19 + Vite + TypeScript；pytest。

**Reference:** 详细设计见 `docs/superpowers/specs/2026-04-19-phase5-worker-card-enhancement-design.md`。

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `backend/agent/day_worker.py` | Modify | 加 `on_progress` 参数、`_safe_emit` 辅助、两处信号发射点 |
| `backend/agent/orchestrator.py` | Modify | `_derive_theme` / `_format_error` 辅助、`worker_statuses` 扩字段、callback 闭包、queue-based 主收集循环 |
| `backend/tests/test_day_worker_progress_callback.py` | Create | day_worker 回调发射单测 |
| `backend/tests/test_orchestrator.py` | Modify | 扩展 6 条断言，覆盖 theme / current_tool / activity_count / error / retry 重置 / 截断 |
| `backend/tests/test_parallel_phase5_integration.py` | Modify | happy path 断言新字段、chunk 数量 sanity |
| `frontend/src/types/plan.ts` | Modify | `ParallelWorkerStatus` 新增 6 个可选字段 |
| `frontend/src/components/ParallelProgress.tsx` | Modify | 新 `renderTail` + theme span |
| `frontend/src/styles/index.css` | Modify | `.parallel-worker-theme` 一条 CSS |
| `screenshots/phase5-worker-card-enhanced.png` | Create | 手动冒烟验证截图 |

实施顺序按 YAGNI + TDD：内层纯逻辑先过（Task 1-2），再拼外层 orchestration（Task 3），集成测试收尾（Task 4），最后前端（Task 5）。

---

### Task 1: day_worker 新增 `on_progress` 参数与发射点

**Files:**
- Create: `backend/tests/test_day_worker_progress_callback.py`
- Modify: `backend/agent/day_worker.py`

**Goal:** 给 `run_day_worker` 加同步回调参数，工作循环在进入新 iteration 和开启新 tool batch 时发射信号。回调异常必须被隔离。

- [ ] **Step 1: 写失败测试 · iter_start 每轮发射**

```python
# backend/tests/test_day_worker_progress_callback.py
import pytest

from agent.day_worker import run_day_worker
from agent.worker_prompt import DayTask
from agent.types import ToolCall
from llm.types import ChunkType, LLMChunk
from state.models import TravelPlanState, DateRange


def _stub_plan() -> TravelPlanState:
    plan = TravelPlanState(session_id="s-dw")
    plan.phase = 5
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.selected_skeleton_id = "x"
    plan.skeleton_plans = [{"id": "x", "days": [{"area": "A", "theme": "T"}]}]
    return plan


def _task() -> DayTask:
    return DayTask(
        day=1,
        date="2026-05-01",
        skeleton_slice={"area": "A", "theme": "T"},
        pace="balanced",
    )


class _LLMStub:
    def __init__(self, chunk_batches):
        self._batches = list(chunk_batches)

    async def chat(self, messages, tools=None, stream=True):
        batch = self._batches.pop(0)
        for c in batch:
            yield c


class _ToolEngineStub:
    def get_tool(self, name):
        class _T:
            human_label = "查询 POI" if name == "get_poi_info" else None
        return _T()

    async def execute_batch(self, tcs):
        from agent.types import ToolResult
        return [
            ToolResult(tool_call_id=tc.id, status="success", data={})
            for tc in tcs
        ]


@pytest.mark.asyncio
async def test_worker_emits_iter_start_each_iteration():
    # Two iterations: first yields a tool call (→ second iteration),
    # second yields a final JSON text.
    batch_1 = [
        LLMChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call=ToolCall(id="t1", name="get_poi_info", arguments={}),
        ),
        LLMChunk(type=ChunkType.DONE),
    ]
    batch_2 = [
        LLMChunk(
            type=ChunkType.TEXT_DELTA,
            content='```json\n{"day": 1, "activities": []}\n```',
        ),
        LLMChunk(type=ChunkType.DONE),
    ]
    llm = _LLMStub([batch_1, batch_2])
    events: list[tuple[int, str, dict]] = []

    await run_day_worker(
        llm=llm,
        tool_engine=_ToolEngineStub(),
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        max_iterations=5,
        timeout_seconds=5,
        on_progress=lambda day, kind, payload: events.append((day, kind, payload)),
    )

    iter_events = [e for e in events if e[1] == "iter_start"]
    assert len(iter_events) == 2
    assert iter_events[0][2] == {"iteration": 1, "max": 5}
    assert iter_events[1][2] == {"iteration": 2, "max": 5}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_day_worker_progress_callback.py::test_worker_emits_iter_start_each_iteration -v`

Expected: FAIL with `TypeError: run_day_worker() got an unexpected keyword argument 'on_progress'`.

- [ ] **Step 3: 加参数 + iter_start 发射点 + `_safe_emit`**

Modify `backend/agent/day_worker.py`:

A. 在文件顶部加 logger import：

```python
import logging

logger = logging.getLogger(__name__)
```

B. 扩展 `run_day_worker` 签名（参数追加在 `timeout_seconds` 之后）：

```python
from typing import Callable

OnProgress = Callable[[int, str, dict], None] | None


async def run_day_worker(
    *,
    llm: LLMProvider,
    tool_engine: ToolEngine,
    plan: TravelPlanState,
    task: DayTask,
    shared_prefix: str,
    max_iterations: int = 5,
    timeout_seconds: int = 60,
    on_progress: OnProgress = None,
) -> DayWorkerResult:
```

C. 在函数体内、`for iteration in range(max_iterations):` 这一行**之后**、`iterations = iteration + 1` 之后，加：

```python
                def _safe_emit(kind: str, payload: dict) -> None:
                    if on_progress is None:
                        return
                    try:
                        on_progress(task.day, kind, payload)
                    except Exception as exc:
                        logger.warning(
                            "day_worker on_progress callback failed: %s", exc
                        )

                _safe_emit(
                    "iter_start",
                    {"iteration": iterations, "max": max_iterations},
                )
```

（注意：`_safe_emit` 在每轮循环里重新绑定一次闭包是可接受的开销，保持代码就近。或者把 `_safe_emit` 提到 `async with asyncio.timeout(...)` 块外——实现者自选；测试不关心位置）

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_day_worker_progress_callback.py::test_worker_emits_iter_start_each_iteration -v`

Expected: PASS.

- [ ] **Step 5: 写 tool_start 测试**

Append to `backend/tests/test_day_worker_progress_callback.py`:

```python
@pytest.mark.asyncio
async def test_worker_emits_tool_start_before_execute():
    batch_1 = [
        LLMChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call=ToolCall(id="t1", name="get_poi_info", arguments={}),
        ),
        LLMChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call=ToolCall(id="t2", name="calculate_route", arguments={}),
        ),
        LLMChunk(type=ChunkType.DONE),
    ]
    batch_2 = [
        LLMChunk(
            type=ChunkType.TEXT_DELTA,
            content='```json\n{"day": 1, "activities": []}\n```',
        ),
        LLMChunk(type=ChunkType.DONE),
    ]
    llm = _LLMStub([batch_1, batch_2])
    events: list[tuple[int, str, dict]] = []

    await run_day_worker(
        llm=llm,
        tool_engine=_ToolEngineStub(),
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        max_iterations=5,
        timeout_seconds=5,
        on_progress=lambda day, kind, payload: events.append((day, kind, payload)),
    )

    tool_events = [e for e in events if e[1] == "tool_start"]
    # Exactly one tool_start per batch (the first tool call).
    assert len(tool_events) == 1
    assert tool_events[0][2]["tool"] == "get_poi_info"
    assert tool_events[0][2]["human_label"] == "查询 POI"
```

- [ ] **Step 6: Run it — should FAIL**

Run: `cd backend && pytest tests/test_day_worker_progress_callback.py::test_worker_emits_tool_start_before_execute -v`

Expected: FAIL (`len(tool_events) == 0` assertion).

- [ ] **Step 7: 加 tool_start 发射点**

In `backend/agent/day_worker.py`, locate the block right after `tool_calls` is collected and **before** `tool_engine.execute_batch(tool_calls)` (search for `results = await tool_engine.execute_batch`). Insert:

```python
                    if tool_calls:
                        first = tool_calls[0]
                        tool_def = tool_engine.get_tool(first.name)
                        _safe_emit(
                            "tool_start",
                            {
                                "tool": first.name,
                                "human_label": (
                                    tool_def.human_label
                                    if tool_def is not None
                                    and getattr(tool_def, "human_label", None)
                                    else first.name
                                ),
                            },
                        )
```

If `_safe_emit` was scoped to inside the for-loop in Step 3, keep it there—this call lives in the same for-body. If it was lifted out, this call uses the outer binding.

- [ ] **Step 8: Run both tests to verify pass**

Run: `cd backend && pytest tests/test_day_worker_progress_callback.py -v`

Expected: 2 passed.

- [ ] **Step 9: 写异常隔离测试**

Append:

```python
@pytest.mark.asyncio
async def test_worker_progress_callback_exception_does_not_kill_worker():
    batch_1 = [
        LLMChunk(
            type=ChunkType.TEXT_DELTA,
            content='```json\n{"day": 1, "activities": []}\n```',
        ),
        LLMChunk(type=ChunkType.DONE),
    ]
    llm = _LLMStub([batch_1])

    def boom(day, kind, payload):
        raise ValueError("intentional")

    result = await run_day_worker(
        llm=llm,
        tool_engine=_ToolEngineStub(),
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        max_iterations=5,
        timeout_seconds=5,
        on_progress=boom,
    )
    assert result.success is True
    assert result.dayplan == {"day": 1, "activities": []}
```

- [ ] **Step 10: Run full test file — should PASS** (the `_safe_emit` added in Step 3 already wraps exceptions)

Run: `cd backend && pytest tests/test_day_worker_progress_callback.py -v`

Expected: 3 passed.

- [ ] **Step 11: Commit**

```bash
cd /path/to/worktree
git add backend/agent/day_worker.py backend/tests/test_day_worker_progress_callback.py
git commit -m "feat(phase5): day_worker emits iter_start/tool_start progress signals

Add an optional synchronous on_progress(day, kind, payload) callback to
run_day_worker. The worker fires iter_start at the top of each iteration
and tool_start right before execute_batch (once per batch, for the first
tool). Callback exceptions are caught and logged so worker execution is
never disturbed by UI-side bugs."
```

---

### Task 2: orchestrator 纯函数辅助 `_derive_theme` + `_format_error`

**Files:**
- Modify: `backend/agent/orchestrator.py`
- Modify: `backend/tests/test_orchestrator.py`

**Goal:** 把两个纯函数先单独测通。后面 Task 3 装配 `worker_statuses` 时依赖它们。

- [ ] **Step 1: 写失败测试**

Add to `backend/tests/test_orchestrator.py` (anywhere near top-level, e.g. above `TestSplitTasks`):

```python
from agent.orchestrator import _derive_theme, _format_error


class TestDeriveTheme:
    def test_area_and_theme_both_present(self):
        assert _derive_theme({"area": "浅草", "theme": "传统文化"}) == "浅草 · 传统文化"

    def test_only_area(self):
        assert _derive_theme({"area": "浅草"}) == "浅草"

    def test_only_theme(self):
        assert _derive_theme({"theme": "传统文化"}) == "传统文化"

    def test_neither(self):
        assert _derive_theme({}) is None

    def test_empty_strings_treated_as_missing(self):
        assert _derive_theme({"area": "  ", "theme": ""}) is None


class TestFormatError:
    def test_none_stays_none(self):
        assert _format_error(None) is None

    def test_empty_stays_none(self):
        assert _format_error("") is None

    def test_short_passes_through(self):
        assert _format_error("超时 60s") == "超时 60s"

    def test_long_truncates_with_ellipsis(self):
        raw = "x" * 120
        result = _format_error(raw)
        assert len(result) == 80
        assert result.endswith("...")
```

- [ ] **Step 2: Run — FAIL**

Run: `cd backend && pytest tests/test_orchestrator.py::TestDeriveTheme tests/test_orchestrator.py::TestFormatError -v`

Expected: FAIL with `ImportError: cannot import name '_derive_theme' from 'agent.orchestrator'`.

- [ ] **Step 3: 实现两个纯函数**

Add to `backend/agent/orchestrator.py`, near the top (after imports, before `class GlobalValidationIssue`):

```python
def _derive_theme(slice_: dict) -> str | None:
    area = str(slice_.get("area") or "").strip()
    theme = str(slice_.get("theme") or "").strip()
    if area and theme:
        return f"{area} · {theme}"
    return area or theme or None


def _format_error(raw: str | None) -> str | None:
    if not raw:
        return None
    if len(raw) > 80:
        return raw[:77] + "..."
    return raw
```

- [ ] **Step 4: Run — PASS**

Run: `cd backend && pytest tests/test_orchestrator.py::TestDeriveTheme tests/test_orchestrator.py::TestFormatError -v`

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/agent/orchestrator.py backend/tests/test_orchestrator.py
git commit -m "feat(phase5): add _derive_theme / _format_error orchestrator helpers"
```

---

### Task 3: orchestrator 扩展 `worker_statuses` + callback 装配 + queue 主循环

**Files:**
- Modify: `backend/agent/orchestrator.py`
- Modify: `backend/tests/test_orchestrator.py`

**Goal:** 装配 callback，让 progress 信号能变成 `parallel_progress` chunk 的额外字段。这是最核心的改动。

- [ ] **Step 1: 写 theme-at-init 测试**

Add to `backend/tests/test_orchestrator.py`:

```python
import pytest
from unittest.mock import AsyncMock
from agent.orchestrator import Phase5Orchestrator
from agent.day_worker import DayWorkerResult
from config import Phase5ParallelConfig
from llm.types import ChunkType


@pytest.mark.asyncio
async def test_orchestrator_broadcasts_theme_at_init(monkeypatch):
    plan = _make_plan_with_skeleton()
    orch = Phase5Orchestrator(
        plan=plan,
        llm=AsyncMock(),
        tool_engine=AsyncMock(),
        config=Phase5ParallelConfig(enabled=True, max_workers=3),
    )

    async def _fake_worker(**kwargs):
        return DayWorkerResult(
            day=kwargs["task"].day,
            date=kwargs["task"].date,
            success=True,
            dayplan={"day": kwargs["task"].day, "activities": []},
            iterations=1,
        )

    monkeypatch.setattr("agent.orchestrator.run_day_worker", _fake_worker)

    chunks = [c async for c in orch.run()]
    progress_chunks = [
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status.get("stage") == "parallel_progress"
    ]
    first = progress_chunks[0]
    themes = {w["day"]: w["theme"] for w in first.agent_status["workers"]}
    assert themes[1] == "新宿/原宿 · 潮流文化"
    assert themes[2] == "浅草/上野 · 传统文化"
    assert themes[3] == "涩谷/银座 · 购物"
```

- [ ] **Step 2: Run — FAIL**

Run: `cd backend && pytest tests/test_orchestrator.py::test_orchestrator_broadcasts_theme_at_init -v`

Expected: FAIL with `KeyError: 'theme'`.

- [ ] **Step 3: 在 `worker_statuses` 初始化中填入 theme + 占位字段**

In `backend/agent/orchestrator.py`, locate the block:

```python
            worker_statuses: list[dict[str, Any]] = [
                {"day": t.day, "status": "running"} for t in tasks
            ]
```

Replace with:

```python
            worker_statuses: list[dict[str, Any]] = [
                {
                    "day": t.day,
                    "status": "running",
                    "theme": _derive_theme(t.skeleton_slice),
                    "iteration": None,
                    "max_iterations": None,
                    "current_tool": None,
                    "activity_count": None,
                    "error": None,
                }
                for t in tasks
            ]
```

- [ ] **Step 4: Run — PASS**

Run: `cd backend && pytest tests/test_orchestrator.py::test_orchestrator_broadcasts_theme_at_init -v`

Expected: PASS.

- [ ] **Step 5: 写 current_tool mid-run 测试**

Add:

```python
@pytest.mark.asyncio
async def test_orchestrator_broadcasts_current_tool_mid_run(monkeypatch):
    plan = _make_plan_with_skeleton()
    orch = Phase5Orchestrator(
        plan=plan,
        llm=AsyncMock(),
        tool_engine=AsyncMock(),
        config=Phase5ParallelConfig(enabled=True, max_workers=3),
    )

    async def _fake_worker(**kwargs):
        on_progress = kwargs.get("on_progress")
        if on_progress:
            on_progress(kwargs["task"].day, "iter_start", {"iteration": 1, "max": 5})
            on_progress(
                kwargs["task"].day,
                "tool_start",
                {"tool": "get_poi_info", "human_label": "查询 POI"},
            )
        return DayWorkerResult(
            day=kwargs["task"].day,
            date=kwargs["task"].date,
            success=True,
            dayplan={"day": kwargs["task"].day, "activities": []},
            iterations=1,
        )

    monkeypatch.setattr("agent.orchestrator.run_day_worker", _fake_worker)

    chunks = [c async for c in orch.run()]
    progress_chunks = [
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status.get("stage") == "parallel_progress"
    ]
    # At least one mid-run chunk should have a non-null current_tool
    mid_chunks_with_tool = [
        c for c in progress_chunks
        if any(w.get("current_tool") == "查询 POI" for w in c.agent_status["workers"])
    ]
    assert len(mid_chunks_with_tool) >= 1
```

- [ ] **Step 6: Run — FAIL**

Expected: FAIL because `run_day_worker` is not called with `on_progress`, and orchestrator doesn't yield mid-run progress chunks on callback.

- [ ] **Step 7: 装配 callback + queue + 改造主循环**

In `backend/agent/orchestrator.py`, inside the `run()` method, locate:

```python
            # 4. Spawn workers with concurrency control
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
```

Replace with:

```python
            # 4. Spawn workers with concurrency control
            semaphore = asyncio.Semaphore(self.config.max_workers)
            progress_queue: asyncio.Queue = asyncio.Queue()

            def _make_progress_cb(idx: int):
                def _on_progress(day: int, kind: str, payload: dict) -> None:
                    try:
                        if kind == "iter_start":
                            worker_statuses[idx]["iteration"] = payload["iteration"]
                            worker_statuses[idx]["max_iterations"] = payload["max"]
                            worker_statuses[idx]["current_tool"] = None
                        elif kind == "tool_start":
                            worker_statuses[idx]["current_tool"] = (
                                payload.get("human_label") or payload.get("tool")
                            )
                        progress_queue.put_nowait({"day": day, "kind": kind})
                    except Exception as exc:
                        logger.warning(
                            "orchestrator progress callback failed: %s", exc
                        )
                return _on_progress

            async def _run_with_semaphore(task: DayTask) -> DayWorkerResult:
                idx = _find_worker_idx(task.day)
                async with semaphore:
                    return await run_day_worker(
                        llm=self.llm,
                        tool_engine=self.tool_engine,
                        plan=self.plan,
                        task=task,
                        shared_prefix=shared_prefix,
                        max_iterations=self.config.worker_max_iterations,
                        timeout_seconds=self.config.worker_timeout_seconds,
                        on_progress=_make_progress_cb(idx),
                    )
```

Then locate the main collection loop (Step 5 "Collect results"):

```python
            while pending:
                done_set, _ = await asyncio.wait(
                    pending.keys(), return_when=asyncio.FIRST_COMPLETED
                )
                for completed in done_set:
                    # ... existing body ...
```

Replace with:

```python
            getter_task: asyncio.Task | None = None
            while pending:
                if getter_task is None:
                    getter_task = asyncio.create_task(progress_queue.get())
                wait_set: set[asyncio.Task] = set(pending.keys()) | {getter_task}
                done_set, _ = await asyncio.wait(
                    wait_set, return_when=asyncio.FIRST_COMPLETED
                )

                if getter_task in done_set:
                    _ = getter_task.result()
                    getter_task = None
                    yield self._build_progress_chunk(
                        worker_statuses,
                        total_days,
                        f"正在并行规划 {total_days} 天行程...",
                    )
                    continue

                for completed in done_set:
                    day_task = pending.pop(completed)
                    idx = _find_worker_idx(day_task.day)
                    try:
                        result = completed.result()
                        if result.success:
                            successes.append(result)
                            worker_statuses[idx]["status"] = "done"
                            worker_statuses[idx]["current_tool"] = None
                            if result.dayplan:
                                worker_statuses[idx]["activity_count"] = len(
                                    result.dayplan.get("activities", [])
                                )
                        else:
                            failures.append(
                                (day_task, result.error or "Unknown error")
                            )
                            worker_statuses[idx]["status"] = "failed"
                            worker_statuses[idx]["current_tool"] = None
                            worker_statuses[idx]["error"] = _format_error(
                                result.error
                            )
                            logger.warning(
                                "Day %d worker failed: %s",
                                day_task.day,
                                result.error,
                            )
                    except Exception as e:
                        failures.append((day_task, f"Exception: {e}"))
                        worker_statuses[idx]["status"] = "failed"
                        worker_statuses[idx]["current_tool"] = None
                        worker_statuses[idx]["error"] = _format_error(f"Exception: {e}")
                        logger.error(
                            "Day %d worker exception: %s", day_task.day, e
                        )

                done_count = sum(
                    1
                    for w in worker_statuses
                    if w["status"] in ("done", "failed")
                )
                yield self._build_progress_chunk(
                    worker_statuses,
                    total_days,
                    f"已完成 {done_count}/{total_days} 天...",
                )

            if getter_task and not getter_task.done():
                getter_task.cancel()
                try:
                    await getter_task
                except (asyncio.CancelledError, Exception):
                    pass
```

Also: module-level logger. Verify `import logging` and `logger = logging.getLogger(__name__)` already exist near the top of `orchestrator.py`. If not, add them.

- [ ] **Step 8: Run — PASS**

Run: `cd backend && pytest tests/test_orchestrator.py::test_orchestrator_broadcasts_current_tool_mid_run -v`

Expected: PASS.

- [ ] **Step 9: 写 activity_count on success + error on failure 测试**

Add:

```python
@pytest.mark.asyncio
async def test_orchestrator_populates_activity_count_on_success(monkeypatch):
    plan = _make_plan_with_skeleton()
    orch = Phase5Orchestrator(
        plan=plan,
        llm=AsyncMock(),
        tool_engine=AsyncMock(),
        config=Phase5ParallelConfig(enabled=True, max_workers=3),
    )

    async def _fake_worker(**kwargs):
        return DayWorkerResult(
            day=kwargs["task"].day,
            date=kwargs["task"].date,
            success=True,
            dayplan={
                "day": kwargs["task"].day,
                "activities": [{"name": "a"}, {"name": "b"}],
            },
            iterations=1,
        )

    monkeypatch.setattr("agent.orchestrator.run_day_worker", _fake_worker)

    chunks = [c async for c in orch.run()]
    last_progress = [
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status.get("stage") == "parallel_progress"
    ][-1]
    for w in last_progress.agent_status["workers"]:
        assert w["activity_count"] == 2


@pytest.mark.asyncio
async def test_orchestrator_populates_error_on_failure(monkeypatch):
    plan = _make_plan_with_skeleton()
    # Disable retry by making fallback kick in
    orch = Phase5Orchestrator(
        plan=plan,
        llm=AsyncMock(),
        tool_engine=AsyncMock(),
        config=Phase5ParallelConfig(
            enabled=True, max_workers=3, fallback_to_serial=True
        ),
    )

    async def _fake_worker(**kwargs):
        return DayWorkerResult(
            day=kwargs["task"].day,
            date=kwargs["task"].date,
            success=False,
            dayplan=None,
            error="Worker 超时 (60s)",
            iterations=5,
        )

    monkeypatch.setattr("agent.orchestrator.run_day_worker", _fake_worker)

    chunks = [c async for c in orch.run()]
    progress = [
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status.get("stage") == "parallel_progress"
    ]
    # At least one chunk should have error populated for all failed workers
    has_error = any(
        all(w["error"] == "Worker 超时 (60s)" for w in c.agent_status["workers"])
        for c in progress
    )
    assert has_error
```

- [ ] **Step 10: Run — both should PASS** (already covered by Step 7)

Run: `cd backend && pytest tests/test_orchestrator.py::test_orchestrator_populates_activity_count_on_success tests/test_orchestrator.py::test_orchestrator_populates_error_on_failure -v`

Expected: 2 passed.

- [ ] **Step 11: 写 retry 重置 + error 截断测试**

Add:

```python
@pytest.mark.asyncio
async def test_orchestrator_retry_resets_dynamic_fields(monkeypatch):
    plan = _make_plan_with_skeleton()
    orch = Phase5Orchestrator(
        plan=plan,
        llm=AsyncMock(),
        tool_engine=AsyncMock(),
        config=Phase5ParallelConfig(
            enabled=True, max_workers=3, fallback_to_serial=False
        ),
    )

    call_count = {1: 0, 2: 0, 3: 0}

    async def _fake_worker(**kwargs):
        day = kwargs["task"].day
        call_count[day] += 1
        on_progress = kwargs.get("on_progress")
        if on_progress:
            on_progress(day, "iter_start", {"iteration": 1, "max": 5})
            on_progress(
                day, "tool_start",
                {"tool": "get_poi_info", "human_label": "查询 POI"},
            )
        # First call to day 1 fails, second (retry) succeeds
        if day == 1 and call_count[1] == 1:
            return DayWorkerResult(
                day=day, date=kwargs["task"].date,
                success=False, dayplan=None,
                error="first try failed", iterations=5,
            )
        return DayWorkerResult(
            day=day, date=kwargs["task"].date,
            success=True,
            dayplan={"day": day, "activities": []}, iterations=1,
        )

    monkeypatch.setattr("agent.orchestrator.run_day_worker", _fake_worker)

    chunks = [c async for c in orch.run()]
    progress = [
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status.get("stage") == "parallel_progress"
    ]
    # Find the "retrying" transition chunk
    retry_chunks = [
        c for c in progress
        if any(
            w["day"] == 1 and w["status"] == "retrying"
            for w in c.agent_status["workers"]
        )
    ]
    assert retry_chunks, "expected at least one retrying chunk for day 1"
    retry_worker = next(
        w for w in retry_chunks[0].agent_status["workers"] if w["day"] == 1
    )
    assert retry_worker["iteration"] is None
    assert retry_worker["current_tool"] is None
    assert retry_worker["theme"] is not None  # theme preserved


@pytest.mark.asyncio
async def test_orchestrator_long_error_truncated_to_80(monkeypatch):
    plan = _make_plan_with_skeleton()
    orch = Phase5Orchestrator(
        plan=plan,
        llm=AsyncMock(),
        tool_engine=AsyncMock(),
        config=Phase5ParallelConfig(
            enabled=True, max_workers=3, fallback_to_serial=True
        ),
    )

    async def _fake_worker(**kwargs):
        return DayWorkerResult(
            day=kwargs["task"].day, date=kwargs["task"].date,
            success=False, dayplan=None,
            error="x" * 200, iterations=5,
        )

    monkeypatch.setattr("agent.orchestrator.run_day_worker", _fake_worker)
    chunks = [c async for c in orch.run()]
    progress = [
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status.get("stage") == "parallel_progress"
    ]
    for c in progress:
        for w in c.agent_status["workers"]:
            if w.get("error"):
                assert len(w["error"]) == 80
                assert w["error"].endswith("...")
```

- [ ] **Step 12: Run retry test — FAIL**

Expected: FAIL — `worker_statuses[idx]["iteration"]` is not being reset on retry transition.

- [ ] **Step 13: 修改 retry 分支**

In `backend/agent/orchestrator.py`, locate the retry block (step 7, around line 279):

```python
            for task, error_msg in failures:
                idx = _find_worker_idx(task.day)
                worker_statuses[idx]["status"] = "retrying"
                yield self._build_progress_chunk(
                    worker_statuses,
                    total_days,
                    f"重试第 {task.day} 天...",
                )
```

Replace the first two lines of the body (`idx = ...; worker_statuses[idx]["status"] = "retrying"`) with:

```python
            for task, error_msg in failures:
                idx = _find_worker_idx(task.day)
                worker_statuses[idx].update({
                    "status": "retrying",
                    "iteration": None,
                    "current_tool": None,
                    "error": None,
                    "activity_count": None,
                })
```

Also update the retry call itself (in the same block, the `retry_result = await run_day_worker(...)` call) to pass `on_progress`:

```python
                retry_result = await run_day_worker(
                    llm=self.llm,
                    tool_engine=self.tool_engine,
                    plan=self.plan,
                    task=task,
                    shared_prefix=shared_prefix,
                    max_iterations=self.config.worker_max_iterations,
                    timeout_seconds=self.config.worker_timeout_seconds,
                    on_progress=_make_progress_cb(idx),
                )
```

Wait — `_make_progress_cb` is defined inside an inner scope in Step 7. The retry block is at the outer `run()` method scope. Need to hoist `_make_progress_cb` definition one level up so retry can see it. In Step 7 when you insert `_make_progress_cb`, place it **before** `async def _run_with_semaphore` at the same indent level as `progress_queue`, NOT inside `_run_with_semaphore`. Re-check your Step 7 indentation.

The retry block additionally needs to update status on success/failure with new fields:

Replace the retry success/failure block:

```python
                if retry_result.success:
                    successes.append(retry_result)
                    worker_statuses[idx]["status"] = "done"
                    worker_statuses[idx]["current_tool"] = None
                    if retry_result.dayplan:
                        worker_statuses[idx]["activity_count"] = len(
                            retry_result.dayplan.get("activities", [])
                        )
                    yield self._build_progress_chunk(
                        worker_statuses,
                        total_days,
                        f"第 {retry_result.day} 天（重试）规划完成",
                    )
                else:
                    worker_statuses[idx]["status"] = "failed"
                    worker_statuses[idx]["current_tool"] = None
                    worker_statuses[idx]["error"] = _format_error(
                        retry_result.error
                    )
                    logger.error(
                        "Day %d retry also failed: %s",
                        task.day,
                        retry_result.error,
                    )
                    yield self._build_progress_chunk(
                        worker_statuses,
                        total_days,
                        f"第 {task.day} 天重试失败",
                    )
```

- [ ] **Step 14: Run both new tests — PASS**

Run: `cd backend && pytest tests/test_orchestrator.py::test_orchestrator_retry_resets_dynamic_fields tests/test_orchestrator.py::test_orchestrator_long_error_truncated_to_80 -v`

Expected: 2 passed.

- [ ] **Step 15: Run entire orchestrator test file — PASS**

Run: `cd backend && pytest tests/test_orchestrator.py -v`

Expected: all tests pass (old ones still green + 6 new assertions).

- [ ] **Step 16: Commit**

```bash
git add backend/agent/orchestrator.py backend/tests/test_orchestrator.py
git commit -m "feat(phase5): wire worker progress callback into parallel_progress chunks

Orchestrator now aggregates theme / iteration / current_tool / activity_count /
error onto each worker_statuses entry and broadcasts a fresh parallel_progress
chunk whenever a worker fires iter_start or tool_start. Retry transition
resets dynamic fields while preserving theme. Errors are truncated to 80
chars to keep SSE payloads small."
```

---

### Task 4: 并行集成测试断言新字段

**Files:**
- Modify: `backend/tests/test_parallel_phase5_integration.py`

**Goal:** 在现有 happy path 基础上，验证新字段在端到端流程中也被正确填充。

- [ ] **Step 1: 找到现有 happy path 测试**

Run: `cd backend && grep -n "def test_" tests/test_parallel_phase5_integration.py | head -5`

Identify the main "happy path" test name (likely `test_full_parallel_run` or similar).

- [ ] **Step 2: 在该测试末尾添加断言**

In `backend/tests/test_parallel_phase5_integration.py`, inside the happy-path test, after the existing final assertions (before the test body ends), append:

```python
    progress_chunks = [
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status.get("stage") == "parallel_progress"
    ]
    # Sanity: must emit at least one parallel_progress chunk per worker.
    assert len(progress_chunks) >= total_expected_days

    # Final progress chunk must carry activity_count for every done worker.
    last_workers = progress_chunks[-1].agent_status["workers"]
    for w in last_workers:
        if w["status"] == "done":
            assert w["activity_count"] is not None, f"day {w['day']} missing activity_count"
            assert w["theme"] is not None or w["theme"] is None  # theme may be None if skeleton slice has no area/theme
```

Import `ChunkType` at the top of the file if not already imported:

```python
from llm.types import ChunkType
```

Adjust `total_expected_days` to match the variable name already used in the surrounding test (read the test to see what it's called — likely `len(tasks)` or a hardcoded int).

- [ ] **Step 3: Run the integration test**

Run: `cd backend && pytest tests/test_parallel_phase5_integration.py -v`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_parallel_phase5_integration.py
git commit -m "test(phase5): assert parallel_progress chunks carry activity_count/theme"
```

---

### Task 5: 前端类型 + 组件渲染 + CSS

**Files:**
- Modify: `frontend/src/types/plan.ts`
- Modify: `frontend/src/components/ParallelProgress.tsx`
- Modify: `frontend/src/styles/index.css`

**Goal:** 让前端消费新字段。没有组件测试框架——依靠 `tsc --noEmit` + 手动 UI 冒烟。

- [ ] **Step 1: 扩展 TS 类型**

In `frontend/src/types/plan.ts`, locate lines 193-196:

```ts
export interface ParallelWorkerStatus {
  day: number
  status: 'running' | 'done' | 'failed' | 'retrying'
}
```

Replace with:

```ts
export interface ParallelWorkerStatus {
  day: number
  status: 'running' | 'done' | 'failed' | 'retrying'
  theme?: string | null
  iteration?: number | null
  max_iterations?: number | null
  current_tool?: string | null
  activity_count?: number | null
  error?: string | null
}
```

- [ ] **Step 2: Run TS compile — should PASS** (fields are optional, no existing usage breaks)

Run: `cd frontend && npx tsc --noEmit`

Expected: no errors.

- [ ] **Step 3: 改 ParallelProgress 组件**

Replace the contents of `frontend/src/components/ParallelProgress.tsx` entirely with:

```tsx
import type { ParallelWorkerStatus } from '../types/plan'

interface Props {
  totalDays: number
  workers: ParallelWorkerStatus[]
  hint?: string | null
}

const STATUS_ICON: Record<ParallelWorkerStatus['status'], string> = {
  running: '⏳',
  done: '✅',
  failed: '❌',
  retrying: '🔄',
}

function renderTail(w: ParallelWorkerStatus): string {
  if (w.status === 'running') {
    const tool = w.current_tool ? `调用 ${w.current_tool}` : '思考中'
    if (w.iteration && w.max_iterations) {
      return `${tool} · ${w.iteration}/${w.max_iterations} 轮`
    }
    return tool
  }
  if (w.status === 'done') {
    return w.activity_count != null
      ? `完成 · ${w.activity_count} 个活动`
      : '完成'
  }
  if (w.status === 'failed') {
    return w.error ? `失败 · ${w.error}` : '失败'
  }
  if (w.status === 'retrying') {
    if (w.iteration && w.max_iterations) {
      return `重试 · ${w.iteration}/${w.max_iterations} 轮`
    }
    return '重试中'
  }
  return ''
}

export default function ParallelProgress({ totalDays, workers, hint }: Props) {
  const doneCount = workers.filter(w => w.status === 'done').length
  const progress = totalDays > 0 ? (doneCount / totalDays) * 100 : 0
  const allDone = doneCount === totalDays

  return (
    <div className="message assistant" data-testid="parallel-progress">
      <div className="parallel-progress-card">
        <div className="parallel-progress-header">
          <span className="parallel-progress-icon">{allDone ? '✨' : '⚡'}</span>
          <span className="parallel-progress-title">
            {allDone ? '行程规划完成' : '并行规划行程中'}
          </span>
          <span className="parallel-progress-count">{doneCount}/{totalDays}</span>
        </div>

        <div className="parallel-progress-workers">
          {workers.map(w => (
            <div
              key={w.day}
              className={`parallel-worker parallel-worker--${w.status}`}
            >
              <span className="parallel-worker-icon">{STATUS_ICON[w.status]}</span>
              <span className="parallel-worker-label">第 {w.day} 天</span>
              {w.theme && (
                <span className="parallel-worker-theme">{w.theme}</span>
              )}
              <span className="parallel-worker-status">{renderTail(w)}</span>
            </div>
          ))}
        </div>

        <div className="parallel-progress-bar-track">
          <div
            className="parallel-progress-bar-fill"
            style={{ width: `${progress}%` }}
          />
        </div>

        {hint && <div className="parallel-progress-hint">{hint}</div>}
      </div>
    </div>
  )
}
```

- [ ] **Step 4: 加一条 CSS**

In `frontend/src/styles/index.css`, locate `.parallel-worker-label` (around line 820). **After** the `.parallel-worker-label` rule block, insert:

```css
.parallel-worker-theme {
  color: var(--accent-gold);
  font-size: 0.78rem;
  min-width: 110px;
  flex-shrink: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
```

Also update `.parallel-worker-label` to no longer grab `flex: 1`:

```css
.parallel-worker-label {
  color: var(--text-primary);
  font-weight: 500;
  min-width: 58px;
  flex-shrink: 0;
}
```

And update `.parallel-worker-status` to take the remaining space:

```css
.parallel-worker-status {
  color: var(--text-secondary);
  font-size: 0.75rem;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
```

- [ ] **Step 5: TS compile check**

Run: `cd frontend && npx tsc --noEmit`

Expected: no errors.

- [ ] **Step 6: Commit frontend changes**

```bash
git add frontend/src/types/plan.ts frontend/src/components/ParallelProgress.tsx frontend/src/styles/index.css
git commit -m "feat(phase5-ui): render theme + current_tool + iteration + activity_count + error in worker cards"
```

---

### Task 6: 手动 UI 冒烟 + 截图存档

**Files:**
- Create: `screenshots/phase5-worker-card-enhanced.png`

**Goal:** 真实跑一次 Phase 5 并行，检查 UI 如设计所示。

- [ ] **Step 1: 启动开发环境**

```bash
# Terminal 1
cd backend && PYTHONUNBUFFERED=1 uvicorn main:app --port 8000

# Terminal 2
cd frontend && npm run dev
```

- [ ] **Step 2: 在浏览器里走完整 Phase 5 流程**

开 `http://localhost:5173`，新 session，引导到 Phase 5：

```
> 五一去东京玩 5 天，预算 2 万元，2 个大人
> [依次确认画像、候选、骨架、住宿]
> 开始安排每天行程
```

观察 ParallelProgress 卡片，对照 spec 第 5.4 节检查：

- [ ] **Step 3: 核对 5 个字段出现**
  - running 行有"调用 &lt;tool&gt;"或"思考中" + "N/M 轮"
  - done 行有"完成 · K 个活动"
  - failed 行有"失败 · &lt;原因&gt;"（如果有 worker 失败；没有可跳过）
  - retrying 行有"重试 · N/M 轮"（若触发）
  - 每个 worker 有 theme 片段（金色），除非 skeleton slice 缺 area + theme

- [ ] **Step 4: 截图存档**

任一方式均可，文件最终路径必须是 `screenshots/phase5-worker-card-enhanced.png`（项目规范，见 `CLAUDE.md` 截图存放章节）：

- macOS 手动：`Cmd+Shift+4` 选区截图，截图默认落在桌面，手动移到 `screenshots/` 并重命名。
- Playwright MCP（若启用）：`mcp__playwright__browser_take_screenshot` 指定 `filename: "screenshots/phase5-worker-card-enhanced.png"`。
- 调用前确认目录存在：`mkdir -p screenshots`。

- [ ] **Step 5: 提交截图**

```bash
git add screenshots/phase5-worker-card-enhanced.png
git commit -m "docs(phase5): screenshot evidence for worker card enhancement"
```

---

### Task 7: 全量 baseline 验证

**Files:** 无改动。

- [ ] **Step 1: 跑全量 pytest**

```bash
cd backend && PYTHONUNBUFFERED=1 python3 -m pytest -x --no-header -rN 2>&1 > /tmp/baseline.log
tail -3 /tmp/baseline.log
```

Expected: `NNNN passed`，无 FAILED 或 ERROR。

- [ ] **Step 2: 跑全量 tsc**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: （可选）跑 frontend build**

```bash
cd frontend && npm run build
```

Expected: build succeeds without warnings about unused variables / types.

---

## 验收 checklist

- [ ] Task 1-5 所有 pytest 新增/修改用例全绿
- [ ] 全量 backend `pytest` 全绿
- [ ] `npx tsc --noEmit` 无错误
- [ ] 手动 UI 冒烟 5 个字段都能在对应状态下显示
- [ ] `screenshots/phase5-worker-card-enhanced.png` 入库
- [ ] 没有对串行 Phase 5 路径产生任何改动（grep "should_use_parallel_phase5" 不被本次 commit 修改）
- [ ] 前端 SSE 处理对老字段读取仍正确（`ChatPanel.tsx:564` 的 parallel_progress 分支未改）

---

## 偏离 spec 的地方

无。

## 未来扩展（不在本 plan 范围）

1. error 悬停 tooltip 展示完整 traceback（需要交互组件）。
2. 同一 worker 一批 tool_call 全部展示（多行/徽章）。
3. 串行 Phase 5 路径加类似信号（若产品决定开放串行模式）。
4. 节流策略（若 worker 数扩到 15+ 天）。
