# Phase 5 Worker 卡片信息增强设计

日期：2026-04-19
关联前序工作：
- `docs/superpowers/specs/2026-04-18-phase5-orchestrator-workers-design.md`（Phase 5 并行架构）
- `docs/superpowers/specs/2026-04-19-phase5-max-retries-guard-design.md`（边界守卫）
- `frontend/src/components/ParallelProgress.tsx:14-21`（当前卡片只有 `status` 文案 4 种）

---

## 1. 背景

Phase 5 并行模式下，前端通过 `agent_status` SSE 事件接收 `{stage: "parallel_progress", workers: [...]}` 数据，逐日渲染 worker 卡片。

当前 `ParallelWorkerStatus` 只有 `day` 和 `status` 两个字段，前端对用户暴露的信息极其有限：

```
⏳ 第 1 天  规划中
⏳ 第 2 天  规划中
⏳ 第 3 天  规划中
```

用户观察到的体感：5 个"规划中"齐刷刷卡在那里，10 秒没反应会开始怀疑是不是挂了。用户真正想知道的：

1. **这一天我要安排什么主题**：主区域 + 主题（在 skeleton 里已经决定了）。
2. **现在具体在做什么**：调哪个工具、到第几轮迭代。
3. **完成后有多少内容**：生成了几个活动。
4. **失败了为什么失败**：超时？JSON 解析失败？耗尽迭代？

当前实现对这些信号全部不透出——`run_day_worker` 是个黑盒，只返回 `DayWorkerResult` 不 yield 任何中间状态；orchestrator 的 `worker_statuses` 只跟踪 `status` 字段。

---

## 2. 目标

1. 在不破坏 Phase 5 并行 orchestrator 既有契约的前提下，把 worker 的 **主题 / 当前工具 / 迭代轮次 / 完成活动数 / 失败原因** 这 5 个信号暴露到前端。
2. 维持"立即广播"的实时性：每次 worker 开启新迭代或发起新工具调用，前端能尽快看到。
3. 保持对旧 SSE 客户端的向后兼容——所有新字段可选。
4. 改动只限在 **并行 orchestrator 路径**，串行 Phase 5（`should_use_parallel_phase5=False`）无关。

### 非目标

- 不改动 `DayPlan` / `Activity` 数据模型。
- 不改动 Phase 5 工具集（`get_poi_info` / `optimize_day_route` 等）。
- 不加"悬停看完整 error 堆栈"的交互——error 截断到 80 字已足够，完整 trace 可去 backend log 查。
- 不做"同一 worker 一批 tool_call 全部显示"——只显示第一个 tool，视觉简洁优先。
- 不做串行 Phase 5 的增强，那条路没有 worker 概念。

---

## 3. 架构

### 3.1 信号管线

```
DayWorker.loop
    │
    ├── iter_start  ─┐
    ├── tool_start  ─┤
    └── return result┘
                     │
                     ▼ on_progress(day, kind, payload)
        ┌─────────────────────────────────────┐
        │ Orchestrator 闭包回调                │
        │ 1. 更新 worker_statuses[idx] dict    │
        │ 2. progress_queue.put_nowait(...)   │
        └──────┬──────────────────────────────┘
               │
               ▼
        ┌─────────────────────────────────────┐
        │ Orchestrator.run() 主循环            │
        │ asyncio.wait([*worker_tasks,        │
        │              queue.get()])          │
        │ 任意一个 ready → yield parallel_progress │
        └──────┬──────────────────────────────┘
               │
               ▼  SSE → ChatPanel → ParallelProgress.tsx
```

### 3.2 为什么 callback 而不是 AsyncIterator

选项 B（worker 变成 `AsyncIterator[WorkerEvent]`）和 C（独立 queue-per-worker）都能工作，但在当前规模下过度抽象：

- 信号类型只有 2 种（`iter_start` / `tool_start`），状态机平坦。
- orchestrator 本来就在维护 `worker_statuses: list[dict]`，callback 直接往 dict 里写是最顺的延续。
- callback 是同步的，避免 async iterator 聚合时需要的 `asyncio.gather(*[drain(gen) for gen in ...])` 样板代码。

B 和 C 在 worker 发射事件数量增长到 10+ 种 / worker 之间需要协调（如全局速率限制）时才值得升级。

### 3.3 为什么 queue 仍有必要

callback 是同步的，无法在内部直接 `yield`——`yield` 只能出现在 async generator 体内（`run()` 本身）。queue 的作用是把"触发广播"的意图从 callback 回传到 `run()` 的 yield 点，让 runner 边界仍然是唯一的 yield 源。

队列只承担"唤醒主循环"职责（put_nowait 即触发一次广播），不承担业务数据，也不做去重——状态已经就地写入了 `worker_statuses`，每次 get() 都 yield 一份最新全量 snapshot。两条相邻信号最多产生两条相邻 SSE（可能内容完全相同），可接受。

### 3.4 推送节流策略

**不节流**：每条回调都立刻 yield 一条 `parallel_progress`。

理由：
- 5 个 worker × ~5 轮 × ~3 tool call = 单次 run 最多 ~75 条 SSE。折合 ~5 QPS，本地网络和前端 re-render 压力可忽略。
- 引入 `asyncio.sleep(0.5)` flush 循环会让测试变得时间敏感（flaky 风险）。
- 用户对"实时感"的价值 > 带宽成本。

如果未来 worker 数扩到 15+/天，且信号触发更频繁，可以再引入"250ms 合批"——届时接入点在 queue 消费端，不需要动 worker 或 callback。

---

## 4. 数据契约

### 4.1 后端 chunk 扩展

`parallel_progress` 事件的 `workers[]` 每个元素：

```python
{
    "day": int,
    "status": "running" | "done" | "failed" | "retrying",

    # 以下为本次新增字段（全部可选，老前端忽略）
    "theme": str | None,              # "浅草 · 传统文化" 或 None
    "iteration": int | None,          # 1-based；running/retrying 反映当前轮；done/failed 保留最终轮
    "max_iterations": int | None,     # 配置上限（通常 5）
    "current_tool": str | None,       # tool 的 human_label，缺则原始 name；iter_start 时重置为 None
    "activity_count": int | None,     # 仅 done 状态填充 = len(dayplan["activities"])
    "error": str | None,              # 仅 failed 状态填充，截断到 80 字
}
```

### 4.2 字段填充规则

| 状态 | theme | iteration | current_tool | activity_count | error |
|------|-------|-----------|--------------|----------------|-------|
| running | 固定（init 时算好） | 1..N，随 iter_start 变化 | 随 tool_start 变化 | null | null |
| done | 固定 | 最终轮 | null | len(dayplan.activities) | null |
| failed | 固定 | 最终轮 | null | null | error[:80] |
| retrying | 固定 | null（重置） | null（重置） | null（新一轮会覆盖） | null（新一轮会覆盖） |

### 4.3 theme 派生逻辑

```python
def _derive_theme(slice_: dict) -> str | None:
    area = (slice_.get("area") or "").strip()
    theme = (slice_.get("theme") or "").strip()
    if area and theme:
        return f"{area} · {theme}"
    return area or theme or None
```

### 4.4 error 截断

```python
def _format_error(raw: str | None) -> str | None:
    if not raw:
        return None
    return raw[:77] + "..." if len(raw) > 80 else raw
```

不做语义化转写（如 "TimeoutError" → "超时"）——worker 已经发的就是中文 + 技术名混合的字符串（`"Worker 超时 (60s)"`、`"Worker 耗尽 5 轮迭代未输出 DayPlan"`），透传最省事，debug 最直接。

### 4.5 前端 TS 类型

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

---

## 5. 组件设计

### 5.1 Worker 发射协议（`backend/agent/day_worker.py`）

签名扩展：

```python
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
    ...
```

发射点：

- **`iter_start`**：每次进入 `for iteration in range(max_iterations)` 首行时。
  - payload: `{"iteration": iteration + 1, "max": max_iterations}`
- **`tool_start`**：收集完 `tool_calls` 列表、`execute_batch` 之前。**展示粒度**：批量内只为 `tool_calls[0]` 发射一次；`execute_batch` 仍并发执行全部 tool，此决策只影响 UI 字段的精简显示，不影响执行时序。
  - payload: `{"tool": tc.name, "human_label": tool_def.human_label if tool_def else tc.name}`

调用 safety：

```python
def _safe_emit(kind: str, payload: dict) -> None:
    if on_progress is None:
        return
    try:
        on_progress(task.day, kind, payload)
    except Exception as exc:
        logger.warning("on_progress callback failed: %s", exc)
```

### 5.2 Orchestrator 装配（`backend/agent/orchestrator.py`）

初始化 `worker_statuses` 时一次性填入 `theme`：

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

闭包回调 + queue 唤醒：

```python
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
            logger.warning("orchestrator progress cb failed: %s", exc)
    return _on_progress
```

主收集循环改造（伪代码）：

```python
getter_task: asyncio.Task | None = None
while pending or not progress_queue.empty():
    if getter_task is None:
        getter_task = asyncio.create_task(progress_queue.get())
    wait_set = set(pending.keys()) | {getter_task}
    done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

    if getter_task in done:
        _ = getter_task.result()  # drain
        getter_task = None
        yield self._build_progress_chunk(worker_statuses, total_days, hint)
        continue

    # worker tasks done → existing logic（填 activity_count / error、可能 retry）
    ...

if getter_task and not getter_task.done():
    getter_task.cancel()
```

### 5.3 完成/失败时的字段填充

成功分支：
```python
worker_statuses[idx]["status"] = "done"
worker_statuses[idx]["current_tool"] = None
if result.dayplan:
    worker_statuses[idx]["activity_count"] = len(
        result.dayplan.get("activities", [])
    )
```

失败分支（无重试）：
```python
worker_statuses[idx]["status"] = "failed"
worker_statuses[idx]["current_tool"] = None
worker_statuses[idx]["error"] = _format_error(result.error)
```

重试分支：
```python
worker_statuses[idx].update({
    "status": "retrying",
    "iteration": None,
    "current_tool": None,
    # theme / activity_count / error 在下一次 iter_start 时自动覆盖
})
```

### 5.4 前端组件（`frontend/src/components/ParallelProgress.tsx`）

单行布局，状态感知尾部：

```tsx
function renderTail(w: ParallelWorkerStatus): string {
  if (w.status === 'running') {
    const tool = w.current_tool ? `调用 ${w.current_tool}` : '思考中'
    const iter = w.iteration && w.max_iterations
      ? `${w.iteration}/${w.max_iterations} 轮`
      : ''
    return iter ? `${tool} · ${iter}` : tool
  }
  if (w.status === 'done') {
    return w.activity_count != null ? `完成 · ${w.activity_count} 个活动` : '完成'
  }
  if (w.status === 'failed') {
    return w.error ? `失败 · ${w.error}` : '失败'
  }
  if (w.status === 'retrying') {
    const iter = w.iteration && w.max_iterations
      ? `${w.iteration}/${w.max_iterations} 轮`
      : ''
    return iter ? `重试 · ${iter}` : '重试中'
  }
  return ''
}
```

Row 模板：

```tsx
<div className={`parallel-worker parallel-worker--${w.status}`}>
  <span className="parallel-worker-icon">{STATUS_ICON[w.status]}</span>
  <span className="parallel-worker-label">第 {w.day} 天</span>
  {w.theme && <span className="parallel-worker-theme">{w.theme}</span>}
  <span className="parallel-worker-status">{renderTail(w)}</span>
</div>
```

CSS 新增：`.parallel-worker-theme { color: var(--accent-gold); min-width: 110px; }`。

---

## 6. 错误处理与边界

1. **回调异常不影响 worker**：`_safe_emit` 吞所有异常。
2. **回调异常不影响 orchestrator**：`_on_progress` 同样吞异常。
3. **Queue 无上限**：`put_nowait` 绝不阻塞。
4. **Queue 生命周期**：run() 结束时的 `getter_task.cancel()` 防止 pending 警告。
5. **重试时字段重置**：见 5.3。
6. **theme 缺失**：`_derive_theme` 返回 None，前端条件渲染隐藏 span。
7. **error 过长**：截断到 80 字 + "..."。
8. **旧前端兼容**：所有新字段 `| None`，TS 可选，读不到不报错。

---

## 7. 测试策略

### 后端

1. **`tests/test_day_worker_progress_callback.py`（新增）**
   - `test_worker_emits_iter_start_each_iteration`
   - `test_worker_emits_tool_start_before_execute`
   - `test_worker_progress_callback_exception_does_not_kill_worker`

2. **`tests/test_orchestrator.py`（扩展）**
   - `test_orchestrator_broadcasts_theme_at_init`
   - `test_orchestrator_broadcasts_current_tool_mid_run`
   - `test_orchestrator_populates_activity_count_on_success`
   - `test_orchestrator_populates_error_on_failure`
   - `test_orchestrator_retry_resets_dynamic_fields`
   - `test_orchestrator_long_error_truncated_to_80`

3. **`tests/test_parallel_phase5_integration.py`（扩展）**
   - 在现有 happy path 上增断言 `workers[*].activity_count` 正确。
   - 粗略验证 chunk 数量 ≥ tasks 数量（sanity check）。

### 前端

4. **手动 UI 冒烟**（前端当前未装组件测试框架，`package.json` 只有 Vite + React；不为本次改动引入 Vitest）
   - `npm run dev` 启后端 + 前端，跑一次 Phase 5 并行路径。
   - 验证：running 行含"调用 &lt;tool&gt; · N/M 轮"、done 行含"完成 · K 个活动"、failed 行含"失败 · &lt;error&gt;"、retrying 行含"重试 · N/M 轮"、theme 为 null 时 span 不渲染。
   - 截图存档到 `screenshots/phase5-worker-card-enhanced.png` 作为 commit evidence。

5. **TS 编译**：`cd frontend && npx tsc --noEmit` 必须通过——新字段全可选，不破坏现有 usage。

### 不测试

- 不测 `asyncio.Queue` 本身。
- 不对 callback 调用次数做过度 spec。
- 不测 SSE 帧编码。

---

## 8. 回归风险

### 8.1 每 SSE 事件负载增大

`workers[]` 每元素多出 6 个字段，序列化体积约翻倍（粗估从 ~40B 到 ~120B / worker）。5 worker × 75 事件 / run ≈ 45KB 额外流量。可忽略。

### 8.2 asyncio.wait 新语义

从 `asyncio.wait([*worker_tasks])` 改为 `asyncio.wait([*worker_tasks, queue_get_task])`。后者有额外控制点：queue_get_task 必须在每次 yield 后重新创建。漏 cancel 会泄漏一个 pending task。设计已显式处理 5.2 末尾的 `getter_task.cancel()`。

### 8.3 callback 时机 vs queue 读取时机

callback 在 worker 协程内执行（用 semaphore 串行了并发），`put_nowait` 入队的顺序是 worker 实际执行顺序。orchestrator 每次 `queue.get()` 拿到一个信号就 yield 一次全量 snapshot——用户会看到连续、非丢失的状态更新。

---

## 9. 变更范围速览

| 文件 | 操作 |
|------|------|
| `backend/agent/day_worker.py` | 加 `on_progress` 参数 + 两个发射点 |
| `backend/agent/orchestrator.py` | `worker_statuses` 扩字段 + callback 装配 + queue 主循环 |
| `backend/tests/test_day_worker_progress_callback.py` | 新增 |
| `backend/tests/test_orchestrator.py` | 扩展 6 条 |
| `backend/tests/test_parallel_phase5_integration.py` | 扩展 2 条断言 |
| `frontend/src/types/plan.ts` | `ParallelWorkerStatus` 加 6 个可选字段 |
| `frontend/src/components/ParallelProgress.tsx` | 新 render 逻辑 + theme span |
| `frontend/src/styles/index.css` | `.parallel-worker-theme` 一条 CSS |
| `screenshots/phase5-worker-card-enhanced.png` | 手动 UI 冒烟验证截图 |

---

## 10. 实施顺序提示

本 spec 完成后进入 writing-plans 阶段产出具体 Task/Step checklist。实施顺序建议（由内向外，便于增量验证）：

1. day_worker `on_progress` 参数 + 单测（内层先跑通）
2. orchestrator `_derive_theme` / `_format_error` 小工具 + 单测（纯函数最好测）
3. orchestrator `worker_statuses` 扩字段 + 回调装配 + queue 主循环 + 单测（核心改动）
4. 集成测试断言扩展
5. 前端 TS 类型 + render 逻辑 + CSS
6. 全量 `pytest` + `npx tsc --noEmit` + 手动打开 web 验证（截图存档）

---

## 11. 外部设计参照

本次"静态主题 + 动态状态"的切分、以及"回调 → 闭包 → runner 边界 yield"的路径，与 Anthropic 《Building Effective Agents》里对 orchestrator-workers 模式的描述吻合：workers 通过状态上报把自身进度暴露给中央 orchestrator，orchestrator 保留唯一的对外流式接口。详见 postmortem `2026-04-19-phase5-parallel-guard-refactor.md` 第 4 节。
