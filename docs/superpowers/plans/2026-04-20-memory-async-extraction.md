# Memory Async Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将记忆提取改为用户发消息后立即后台执行的异步 job，使用 coalescing queue 合并连续消息，并通过独立 internal task SSE 与 chat 主流解耦。

**Architecture:** `chat` 请求在 append 用户消息后立刻向 session 级 memory scheduler 提交 snapshot，主链路继续执行同步 `memory_recall` 和 assistant 回复；后台 worker 先跑短窗口 `memory_extraction_gate`，通过后再跑增量窗口 `memory_extraction`，其生命周期通过新的 `/api/internal-tasks/{session_id}/stream` 推给前端。前端复用现有 internal task 卡片，但把任务 id 映射提升为组件级共享状态，让 chat 结束后仍能持续更新后台 memory 任务。

**Tech Stack:** FastAPI SSE、Python async/await、pytest + pytest-asyncio、React 19、TypeScript、Vite。

---

### Task 1: 锁定新的时序与队列行为

**Files:**
- Modify: `backend/tests/test_memory_integration.py`
- Modify: `backend/tests/test_api.py`

- [ ] **Step 1: 写失败测试，证明 chat 不再等待 memory extraction**

```python
@pytest.mark.asyncio
async def test_chat_done_does_not_wait_for_background_memory_job(app):
    ...
    assert body.index('"type": "done"') < body.index('"kind": "memory_extraction"')
```

- [ ] **Step 2: 运行单测并确认它先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_integration.py -k 'does_not_wait_for_background_memory_job' -v`
Expected: FAIL，因为当前实现仍在 chat 主流尾部执行 memory gate / extraction。

- [ ] **Step 3: 写失败测试，验证 coalescing queue 只保留最新 pending snapshot**

```python
@pytest.mark.asyncio
async def test_memory_scheduler_coalesces_pending_snapshots(app):
    ...
    assert seen_gate_windows == [["第一条"], ["第三条"]]
```

- [ ] **Step 4: 写失败测试，验证 gate 和 extraction 的窗口不同**

```python
@pytest.mark.asyncio
async def test_memory_gate_uses_short_recent_window_and_extraction_uses_incremental_window(app):
    ...
    assert observed["gate_messages"] == ["第二条", "第三条", "第四条"]
    assert observed["extraction_messages"] == ["第一条", "第二条", "第三条", "第四条"]
```

- [ ] **Step 5: 运行这组测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_integration.py -k 'coalesces_pending_snapshots or uses_short_recent_window' -v`
Expected: FAIL，因为调度器和上下文构造尚未实现。

### Task 2: 实现 session 级 memory scheduler 与独立 task stream

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/memory/extraction.py`

- [ ] **Step 1: 增加 memory scheduler 的数据结构**

```python
@dataclass
class MemoryJobSnapshot:
    session_id: str
    user_id: str
    turn_id: str
    user_messages: list[str]
    submitted_user_count: int
    plan_snapshot: TravelPlanState
```

```python
@dataclass
class MemorySchedulerState:
    running_job: asyncio.Task | None = None
    pending_snapshot: MemoryJobSnapshot | None = None
    last_consumed_user_count: int = 0
```

- [ ] **Step 2: 新增发布/订阅 internal task 的 session SSE**

```python
@app.get("/api/internal-tasks/{session_id}/stream")
async def stream_internal_tasks(session_id: str):
    ...
    return EventSourceResponse(event_stream())
```

- [ ] **Step 3: 实现 `submit_memory_snapshot()` 的 latest-wins / coalescing 行为**

```python
def _submit_memory_snapshot(snapshot: MemoryJobSnapshot) -> None:
    if state.running_job is None:
        state.running_job = asyncio.create_task(_run_memory_job(snapshot))
    else:
        state.pending_snapshot = snapshot
```

- [ ] **Step 4: 将 chat 中的同步尾处理改为后台启动**

```python
messages.append(Message(role=Role.USER, content=req.message))
_submit_memory_snapshot(...)

async for event in _run_agent_stream(...):
    yield event
```

- [ ] **Step 5: 从 `_run_agent_stream()` 中移除 chat 尾部 memory extraction**

```python
if run.status == "completed":
    yield json.dumps({"type": "done", ...}, ensure_ascii=False)
```

- [ ] **Step 6: 实现 gate / extraction 的窗口裁剪**

```python
def _build_gate_user_window(all_user_messages: list[str]) -> list[str]:
    return _clip_user_messages(all_user_messages[-3:], max_chars=1200)

def _build_extraction_user_window(snapshot: MemoryJobSnapshot, consumed: int) -> list[str]:
    return _clip_user_messages(
        snapshot.user_messages[consumed:snapshot.submitted_user_count][-8:],
        max_chars=3000,
    )
```

- [ ] **Step 7: 运行目标测试，确认红变绿**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_integration.py -k 'memory_extraction or memory_recall or coalesces_pending_snapshots or uses_short_recent_window' -v`
Expected: PASS

### Task 3: 接入前端独立 memory task SSE

**Files:**
- Modify: `frontend/src/hooks/useSSE.ts`
- Modify: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/types/plan.ts`

- [ ] **Step 1: 扩展 SSE hook，支持独立订阅 GET SSE**

```ts
const subscribe = useCallback((url: string, onEvent: (event: SSEEvent) => void) => {
  const controller = new AbortController()
  void streamSSE(url, { method: 'GET' }, onEvent, controller)
  return () => controller.abort()
}, [])
```

- [ ] **Step 2: 把 `internalTaskMessageIds` 提升为组件级 ref**

```ts
const internalTaskMessageIdsRef = useRef(new Map<string, string>())
```

- [ ] **Step 3: 在 `sessionId` 生命周期里订阅 `/api/internal-tasks/{sessionId}/stream`**

```ts
useEffect(() => {
  return subscribe(`/api/internal-tasks/${sessionId}/stream`, handleBackgroundEvent)
}, [sessionId, subscribe])
```

- [ ] **Step 4: 让后台 internal task 更新在 chat 结束后仍能更新原卡片**

```ts
const taskMessageIds = internalTaskMessageIdsRef.current
```

- [ ] **Step 5: 验证输入框解锁不再依赖 memory extraction 完成**

Run: `cd frontend && npm run build`
Expected: PASS

### Task 4: 文档同步与回归

**Files:**
- Modify: `PROJECT_OVERVIEW.md`
- Modify: `docs/superpowers/specs/2026-04-20-memory-async-extraction-design.md`
- Modify: `docs/superpowers/plans/2026-04-20-memory-async-extraction.md`

- [ ] **Step 1: 更新项目全景图中的 Memory System 与 Internal Task Stream 描述**

```markdown
- `memory_recall` 仍在回答前同步执行；
- `memory_extraction_gate` / `memory_extraction` 改为用户消息进入后立即后台启动，经独立 internal task SSE 推送；
- 连续消息通过 session 级 coalescing queue 合并。
```

- [ ] **Step 2: 跑完整相关验证**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_extraction.py tests/test_memory_integration.py tests/test_api.py -k 'memory_extraction or memory_recall or internal_task'`
Expected: PASS

- [ ] **Step 3: 跑前端构建验证**

Run: `cd frontend && npm run build`
Expected: PASS
