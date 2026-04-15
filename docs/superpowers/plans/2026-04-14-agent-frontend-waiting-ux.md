# Agent 前端等待体验与状态同步 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 SSE 协议上新增 2 个事件（`phase_transition`、`agent_status`）+ 1 个字段（`tool_call.human_label`），分 4 个 PR 消除后端/UI 状态错位与等待焦虑，不影响现有 retry/continue 机制。

**Architecture:** 后端在 `LLMChunk` 扩展两种新 chunk type 并在 `agent/loop.py` 精准触发；`main.py` 把 chunk 翻译成 SSE 事件。前端在 `App.tsx` 引入 `phaseOverride` hoist state 做乐观同步，新增 `ThinkingBubble` 组件和 `RoundSummaryBar`，扩展 `MessageBubble` 的工具卡显示 `human_label`+计时器。每个 PR 独立可合入，依赖关系清晰：PR2/3/4 依赖 PR1 建立的新 chunk 基础设施。

**Tech Stack:** Python 3.12（FastAPI / pytest / asyncio），TypeScript / React 19，CSS（Solstice 设计 token），Playwright E2E。

**Spec:** `docs/superpowers/specs/2026-04-14-agent-frontend-waiting-ux-design.md`

**Testing 约定：** 本仓库前端**没有 Vitest/Jest 单测**，所有前端行为验证通过 Playwright E2E（mock SSE 事件）完成；后端单测走 pytest。

---

## File Structure

| 文件 | 操作 | PR | 责任 |
|---|---|---|---|
| `backend/llm/types.py` | 修改 | PR1 | 扩展 `ChunkType` 枚举与 `LLMChunk` 字段 |
| `backend/main.py` | 修改 | PR1 / PR3 | SSE 事件翻译；on_validate hook 检测 phase3_step；keepalive 节奏 |
| `backend/agent/loop.py` | 修改 | PR1 / PR2 / PR3 | phase_transition yield；agent_status yield；summarizing 追踪 flag |
| `backend/tools/base.py` | 修改 | PR2 | `@tool` 装饰器新增 `human_label` 参数 |
| `backend/tools/*.py`（14 个工具） | 修改 | PR2 | 每个工具补 `human_label` 文案 |
| `backend/context/manager.py` | 修改 | PR3 | `compact_messages_for_prompt` 暴露判定函数供预告使用 |
| `backend/agent/narration.py` | 创建 | PR4 | 规则式 narration 合成函数 |
| `backend/tests/test_phase_transition_event.py` | 创建 | PR1 | phase_transition 四种触发路径 |
| `backend/tests/test_agent_status_event.py` | 创建 | PR2 / PR3 | agent_status 三种 stage |
| `backend/tests/test_tool_human_label.py` | 创建 | PR2 | human_label 序列化 + 14 工具覆盖 |
| `backend/tests/test_narration.py` | 创建 | PR4 | narration 规则覆盖 |
| `frontend/src/types/plan.ts` | 修改 | PR1 / PR2 | `SSEEvent` 新增 `phase_transition` / `agent_status` |
| `frontend/src/hooks/useSSE.ts` | 修改 | PR1 | 解析新事件（透传，无特殊处理） |
| `frontend/src/App.tsx` | 修改 | PR1 | `phaseOverride` state + 透传 |
| `frontend/src/components/PhaseIndicator.tsx` | 修改 | PR1 | override 优先 + 切换动画 |
| `frontend/src/components/Phase3Workbench.tsx` | 修改 | PR1 | override 优先 |
| `frontend/src/components/MessageBubble.tsx` | 修改 | PR1 / PR2 | PhaseTransitionCard variant；工具副标题与计时器 |
| `frontend/src/components/ChatPanel.tsx` | 修改 | PR1–PR4 | 处理新事件；ThinkingBubble 生命周期；staleness；RoundSummaryBar；memory_recall chip |
| `frontend/src/components/ThinkingBubble.tsx` | 创建 | PR2 | 思考气泡 + 2s 兜底 + hint 支持 |
| `frontend/src/components/RoundSummaryBar.tsx` | 创建 | PR3 | done 事件收尾条 |
| `frontend/src/styles/index.css` | 修改 | PR1–PR4 | 所有新样式（Solstice token） |
| `e2e-waiting-experience.spec.ts` | 创建 | PR2–PR4 | 等待体验专项 E2E |
| `playwright.waiting.config.ts` | 创建 | PR2 | 只跑等待体验专项 |
| `e2e-test.spec.ts` | 修改 | PR1 | 断言 phase tab 在 state_update 到达前已切换 |
| `PROJECT_OVERVIEW.md` | 修改 | 每个 PR 合入时 | 同步 SSE 协议段与前端架构段 |

---

## PR1 — Phase 同步（P0）

**范围**：新增 `phase_transition` 事件 + `ChunkType` / `LLMChunk` 扩展 + 前端 `phaseOverride` 机制。

### Task 1: 扩展 ChunkType / LLMChunk

**Files:**
- Modify: `backend/llm/types.py`
- Test: `backend/tests/test_phase_transition_event.py`（新建）

- [ ] **Step 1：写失败测试**

创建 `backend/tests/test_phase_transition_event.py`：

```python
import pytest
from llm.types import ChunkType, LLMChunk


def test_chunk_type_has_phase_transition_and_agent_status():
    assert ChunkType.PHASE_TRANSITION.value == "phase_transition"
    assert ChunkType.AGENT_STATUS.value == "agent_status"


def test_llm_chunk_accepts_phase_info_and_agent_status():
    chunk = LLMChunk(
        type=ChunkType.PHASE_TRANSITION,
        phase_info={"from_phase": 1, "to_phase": 3, "from_step": None, "to_step": "brief"},
    )
    assert chunk.phase_info["to_phase"] == 3

    chunk2 = LLMChunk(
        type=ChunkType.AGENT_STATUS,
        agent_status={"stage": "thinking", "iteration": 0},
    )
    assert chunk2.agent_status["stage"] == "thinking"
```

- [ ] **Step 2：运行测试确认失败**

```bash
cd backend && pytest tests/test_phase_transition_event.py -v
```

Expected: FAIL（`AttributeError: PHASE_TRANSITION` 或 `unexpected keyword argument 'phase_info'`）

- [ ] **Step 3：实现**

修改 `backend/llm/types.py`：

```python
class ChunkType(str, Enum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_RESULT = "tool_result"
    CONTEXT_COMPRESSION = "context_compression"
    KEEPALIVE = "keepalive"
    USAGE = "usage"
    DONE = "done"
    PHASE_TRANSITION = "phase_transition"
    AGENT_STATUS = "agent_status"


@dataclass
class LLMChunk:
    type: ChunkType
    content: str | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    compression_info: dict | None = None
    usage_info: dict | None = None
    phase_info: dict | None = None
    agent_status: dict | None = None
```

- [ ] **Step 4：运行测试确认通过**

```bash
cd backend && pytest tests/test_phase_transition_event.py -v
```

Expected: 2 passed

- [ ] **Step 5：提交**

```bash
git add backend/llm/types.py backend/tests/test_phase_transition_event.py
git commit -m "feat(llm): extend ChunkType with phase_transition and agent_status"
```

---

### Task 2: main.py SSE 翻译分支

**Files:**
- Modify: `backend/main.py`（附近 line 1494-1500 的 event_type 分派）
- Test: `backend/tests/test_phase_transition_event.py`

- [ ] **Step 1：写失败测试**

追加到 `backend/tests/test_phase_transition_event.py`：

```python
import json
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from llm.types import ChunkType, LLMChunk


@pytest.mark.asyncio
async def test_sse_emits_phase_transition_event(app, sessions, session_id):
    async def fake_agent_run(*args, **kwargs):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="hi")
        yield LLMChunk(
            type=ChunkType.PHASE_TRANSITION,
            phase_info={"from_phase": 1, "to_phase": 3, "from_step": None, "to_step": "brief", "reason": "check"},
        )
        yield LLMChunk(type=ChunkType.DONE)

    from agent.loop import AgentLoop
    AgentLoop.run = AsyncMock(side_effect=fake_agent_run)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/api/chat/{session_id}", json={"message": "去成都", "user_id": "u1"})
    assert '"type": "phase_transition"' in resp.text
    assert '"to_phase": 3' in resp.text
```

复用仓库已有的 `app` / `sessions` / `session_id` fixtures（参考 `test_api.py`）。

- [ ] **Step 2：运行确认失败**

```bash
cd backend && pytest tests/test_phase_transition_event.py::test_sse_emits_phase_transition_event -v
```

Expected: FAIL（事件未在响应中出现）

- [ ] **Step 3：实现**

修改 `backend/main.py` 的流式处理块（在现有 `CONTEXT_COMPRESSION` 分支之后、`event_type = ...` 之前）：

```python
if chunk.type == ChunkType.PHASE_TRANSITION and chunk.phase_info:
    yield json.dumps(
        {"type": "phase_transition", **chunk.phase_info},
        ensure_ascii=False,
    )
    continue
if chunk.type == ChunkType.AGENT_STATUS and chunk.agent_status:
    yield json.dumps(
        {"type": "agent_status", **chunk.agent_status},
        ensure_ascii=False,
    )
    continue
```

- [ ] **Step 4：运行确认通过**

```bash
cd backend && pytest tests/test_phase_transition_event.py -v
```

Expected: 3 passed

- [ ] **Step 5：提交**

```bash
git add backend/main.py backend/tests/test_phase_transition_event.py
git commit -m "feat(api): translate phase_transition / agent_status chunks to SSE events"
```

---

### Task 3: loop.py — `check_and_apply_transition` 后 yield

**Files:**
- Modify: `backend/agent/loop.py` (around line 404-426)
- Test: `backend/tests/test_phase_transition_event.py`

- [ ] **Step 1：写失败测试**

追加：

```python
@pytest.mark.asyncio
async def test_loop_yields_phase_transition_on_check_and_apply(agent_with_router, plan_phase1):
    """When check_and_apply_transition promotes phase 1 -> 3, loop yields a
    phase_transition chunk before re-entering the loop."""
    agent, mock_router = agent_with_router
    mock_router.check_and_apply_transition.side_effect = _promote_phase(plan_phase1, to_phase=3)

    chunks = [c async for c in agent.run([], phase=1)]
    phase_chunks = [c for c in chunks if c.type == ChunkType.PHASE_TRANSITION]
    assert len(phase_chunks) == 1
    assert phase_chunks[0].phase_info["from_phase"] == 1
    assert phase_chunks[0].phase_info["to_phase"] == 3
```

`_promote_phase` 和 `agent_with_router` fixture 放到测试文件头部（参考 `test_agent_loop.py` 已有 fixtures）。

- [ ] **Step 2：运行确认失败**

```bash
cd backend && pytest tests/test_phase_transition_event.py::test_loop_yields_phase_transition_on_check_and_apply -v
```

Expected: FAIL

- [ ] **Step 3：实现**

`backend/agent/loop.py` line 410 附近，`if phase_changed:` 分支 `continue` 之前：

```python
if phase_changed:
    yield LLMChunk(
        type=ChunkType.PHASE_TRANSITION,
        phase_info={
            "from_phase": phase_before_batch,
            "to_phase": self.plan.phase,
            "from_step": phase3_step_before_batch,
            "to_step": getattr(self.plan, "phase3_step", None),
            "reason": "check_and_apply_transition",
        },
    )
    messages[:] = await self._rebuild_messages_for_phase_change(...)
    current_phase = phase_after_batch
    tools = self.tool_engine.get_tools_for_phase(...)
    continue
```

- [ ] **Step 4：运行确认通过**

```bash
cd backend && pytest tests/test_phase_transition_event.py -v
```

Expected: 4 passed

- [ ] **Step 5：提交**

```bash
git add backend/agent/loop.py backend/tests/test_phase_transition_event.py
git commit -m "feat(loop): yield phase_transition chunk after check_and_apply_transition"
```

---

### Task 4: loop.py — 显式 phase 变化路径

**Files:**
- Modify: `backend/agent/loop.py` (around line 381)
- Test: `backend/tests/test_phase_transition_event.py`

- [ ] **Step 1：写失败测试**

```python
@pytest.mark.asyncio
async def test_loop_yields_phase_transition_on_explicit_path(agent_with_tool_that_writes_phase):
    """When update_plan_state directly changes plan.phase (not via check_and_apply),
    loop yields phase_transition in the 'phase_after_batch != phase_before_batch' branch."""
    agent = agent_with_tool_that_writes_phase
    chunks = [c async for c in agent.run([], phase=1)]
    phase_chunks = [c for c in chunks if c.type == ChunkType.PHASE_TRANSITION]
    assert any(c.phase_info["to_phase"] == 3 for c in phase_chunks)
```

- [ ] **Step 2：运行确认失败**

Expected: FAIL

- [ ] **Step 3：实现**

`backend/agent/loop.py` line 381 的 `if phase_after_batch != phase_before_batch:` 分支内，`continue` 之前：

```python
if phase_after_batch != phase_before_batch:
    yield LLMChunk(
        type=ChunkType.PHASE_TRANSITION,
        phase_info={
            "from_phase": phase_before_batch,
            "to_phase": phase_after_batch,
            "from_step": phase3_step_before_batch,
            "to_step": getattr(self.plan, "phase3_step", None),
            "reason": "update_plan_state_direct",
        },
    )
    messages[:] = await self._rebuild_messages_for_phase_change(...)
    current_phase = phase_after_batch
    tools = self.tool_engine.get_tools_for_phase(...)
    continue
```

- [ ] **Step 4：运行确认通过**

```bash
cd backend && pytest tests/test_phase_transition_event.py -v
```

Expected: 5 passed

- [ ] **Step 5：提交**

```bash
git add backend/agent/loop.py backend/tests/test_phase_transition_event.py
git commit -m "feat(loop): yield phase_transition on explicit phase change path"
```

---

### Task 5: loop.py — Backtrack 反向迁移

**Files:**
- Modify: `backend/agent/loop.py` (`_is_backtrack_result` 分支, around line 335-356)
- Test: `backend/tests/test_phase_transition_event.py`

- [ ] **Step 1：写失败测试**

```python
@pytest.mark.asyncio
async def test_loop_yields_phase_transition_on_backtrack(agent_with_backtrack_tool):
    chunks = [c async for c in agent_with_backtrack_tool.run([], phase=5)]
    phase_chunks = [c for c in chunks if c.type == ChunkType.PHASE_TRANSITION]
    assert any(
        c.phase_info["from_phase"] > c.phase_info["to_phase"]
        for c in phase_chunks
    )
```

- [ ] **Step 2：运行确认失败**

Expected: FAIL

- [ ] **Step 3：实现**

`backend/agent/loop.py` `needs_rebuild = True; break` 之后、在 `if needs_rebuild:` 分支内 `messages[:] = await self._rebuild_messages_for_phase_change(...)` 之前补：

```python
if needs_rebuild:
    phase_after_batch = (
        self.plan.phase if self.plan is not None else current_phase
    )
    yield LLMChunk(
        type=ChunkType.PHASE_TRANSITION,
        phase_info={
            "from_phase": phase_before_batch,
            "to_phase": phase_after_batch,
            "from_step": phase3_step_before_batch,
            "to_step": getattr(self.plan, "phase3_step", None),
            "reason": "backtrack",
        },
    )
    messages[:] = await self._rebuild_messages_for_phase_change(...)
    # ... 原有逻辑
```

- [ ] **Step 4：运行确认通过**

```bash
cd backend && pytest tests/test_phase_transition_event.py -v
```

Expected: 6 passed

- [ ] **Step 5：提交**

```bash
git add backend/agent/loop.py backend/tests/test_phase_transition_event.py
git commit -m "feat(loop): yield reverse phase_transition on backtrack"
```

---

### Task 6: `on_validate` hook 检测 phase3_step 变化

**Files:**
- Modify: `backend/main.py` (around line 424 `on_validate` hook)
- Test: `backend/tests/test_phase_transition_event.py`

- [ ] **Step 1：写失败测试**

```python
@pytest.mark.asyncio
async def test_phase3_step_change_emits_phase_transition(app, sessions, session_id_with_phase3):
    """Writing phase3_step via update_plan_state emits a phase_transition with
    from_phase == to_phase but different steps."""
    async def fake_agent_run(*args, **kwargs):
        tool_call = ToolCall(id="1", name="update_plan_state", arguments={"field": "phase3_step", "value": "candidate"})
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=tool_call)
        yield LLMChunk(type=ChunkType.TOOL_RESULT, tool_result=ToolResult(
            tool_call_id="1", status="success",
            data={"updated_field": "phase3_step", "previous_value": "brief", "new_value": "candidate"},
        ))
        yield LLMChunk(type=ChunkType.DONE)

    # ... mock + assert phase_transition in SSE with to_step=candidate
```

- [ ] **Step 2：运行确认失败**

Expected: FAIL

- [ ] **Step 3：实现**

`backend/main.py` `on_validate` hook（line 424）在处理完 `_pending_state_changes` 后追加：

```python
async def on_validate(**kwargs):
    # ... 原有逻辑

    # 新增：检测 phase3_step 变化并暂存待发事件
    result = kwargs.get("result")
    if result and isinstance(result.data, dict):
        updated_field = result.data.get("updated_field")
        if updated_field == "phase3_step":
            session["_pending_phase_step_transition"] = {
                "from_phase": plan.phase,
                "to_phase": plan.phase,
                "from_step": result.data.get("previous_value"),
                "to_step": result.data.get("new_value"),
                "reason": "phase3_step_change",
            }
```

在 SSE 主循环（line 1547 `yield json.dumps(event_data, ...)` 之后）追加：

```python
# 在已有 state_update 发送逻辑之后
_pending_step = session.pop("_pending_phase_step_transition", None)
if _pending_step is not None:
    yield json.dumps(
        {"type": "phase_transition", **_pending_step},
        ensure_ascii=False,
    )
```

- [ ] **Step 4：运行确认通过**

```bash
cd backend && pytest tests/test_phase_transition_event.py -v
```

Expected: 7 passed

- [ ] **Step 5：提交**

```bash
git add backend/main.py backend/tests/test_phase_transition_event.py
git commit -m "feat(api): emit phase_transition on phase3_step change via on_validate hook"
```

---

### Task 7: 前端 SSEEvent 类型扩展 + useSSE 透传

**Files:**
- Modify: `frontend/src/types/plan.ts`
- Modify: `frontend/src/hooks/useSSE.ts`

- [ ] **Step 1：扩展 SSEEvent 类型**

修改 `frontend/src/types/plan.ts`：

```ts
export interface PhaseTransitionEvent {
  from_phase: number
  to_phase: number
  from_step?: string | null
  to_step?: string | null
  reason?: string
}

export interface AgentStatusEvent {
  stage: 'thinking' | 'summarizing' | 'compacting'
  iteration?: number
  hint?: string | null
}

export interface SSEEvent {
  type: 'text_delta' | 'tool_call' | 'tool_result' | 'state_update'
    | 'context_compression' | 'memory_recall' | 'error' | 'done'
    | 'phase_transition' | 'agent_status'
  content?: string
  tool_call?: ToolCallEvent
  tool_result?: ToolResultEvent
  plan?: TravelPlanState
  compression_info?: CompressionInfo
  item_ids?: string[]
  error?: string
  error_code?: string
  message?: string
  retryable?: boolean
  can_continue?: boolean
  failure_phase?: string
  run_id?: string
  run_status?: string
  // phase_transition
  from_phase?: number
  to_phase?: number
  from_step?: string | null
  to_step?: string | null
  reason?: string
  // agent_status
  stage?: 'thinking' | 'summarizing' | 'compacting'
  iteration?: number
  hint?: string | null
}
```

- [ ] **Step 2：确认 useSSE 无需修改**

`useSSE.ts` 当前通过 `JSON.parse` 透传所有事件到 `onEvent` 回调，新 type 自动到达 ChatPanel。本任务只需确认：

```bash
cd frontend && npm run build
```

Expected: 类型检查通过，无编译错误

- [ ] **Step 3：提交**

```bash
git add frontend/src/types/plan.ts
git commit -m "feat(types): add phase_transition and agent_status to SSEEvent"
```

---

### Task 8: App.tsx 引入 phaseOverride state

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/ChatPanel.tsx`（透传回调）

- [ ] **Step 1：App.tsx 新增 state 与回调**

在 `App.tsx` 组件顶部（和 `plan` 相邻处）新增：

```tsx
const [phaseOverride, setPhaseOverride] = useState<{
  phase: number
  step?: string | null
  expiresAt: number
} | null>(null)

const handlePhaseTransition = useCallback((ev: {
  from_phase: number
  to_phase: number
  from_step?: string | null
  to_step?: string | null
}) => {
  // Backtrack 立即清空
  if (ev.from_phase > ev.to_phase) {
    setPhaseOverride(null)
    return
  }
  setPhaseOverride({
    phase: ev.to_phase,
    step: ev.to_step,
    expiresAt: Date.now() + 800,
  })
}, [])

// plan.phase 追平 override 时自动清空
useEffect(() => {
  if (!phaseOverride) return
  if (plan?.phase === phaseOverride.phase
      && (plan?.phase3_step ?? null) === (phaseOverride.step ?? null)) {
    setPhaseOverride(null)
  }
}, [plan, phaseOverride])
```

把 `phaseOverride` 透传到 `<PhaseIndicator>` 和 `<Phase3Workbench>`，把 `onPhaseTransition={handlePhaseTransition}` 透传到 `<ChatPanel>`。

- [ ] **Step 2：ChatPanel 新增事件分派**

`frontend/src/components/ChatPanel.tsx` 的 `createEventHandler` 里，在现有 `if (event.type === 'text_delta')` 之前追加：

```tsx
if (event.type === 'phase_transition' && event.to_phase !== undefined) {
  onPhaseTransition?.({
    from_phase: event.from_phase ?? 0,
    to_phase: event.to_phase,
    from_step: event.from_step,
    to_step: event.to_step,
  })
  return
}
```

Props 接口加 `onPhaseTransition?: (ev: { from_phase: number; to_phase: number; from_step?: string | null; to_step?: string | null }) => void`。

- [ ] **Step 3：类型检查**

```bash
cd frontend && npm run build
```

Expected: 无错误

- [ ] **Step 4：提交**

```bash
git add frontend/src/App.tsx frontend/src/components/ChatPanel.tsx
git commit -m "feat(frontend): introduce phaseOverride state for optimistic phase sync"
```

---

### Task 9: PhaseIndicator 使用 effective phase + 切换动画

**Files:**
- Modify: `frontend/src/components/PhaseIndicator.tsx`
- Modify: `frontend/src/styles/index.css`

- [ ] **Step 1：PhaseIndicator 读 override**

```tsx
interface Props {
  currentPhase: number
  overridePhase?: number | null
}

export default function PhaseIndicator({ currentPhase, overridePhase }: Props) {
  const effectivePhase = overridePhase ?? currentPhase
  // ... 原逻辑，把 currentPhase 替换为 effectivePhase
}
```

App.tsx 传入：`<PhaseIndicator currentPhase={plan.phase} overridePhase={phaseOverride?.phase} />`

- [ ] **Step 2：切换动画 CSS**

`frontend/src/styles/index.css` 追加（`.phase-node.active` 定义附近）：

```css
.phase-node.advancing {
  animation: phaseAdvance 300ms ease-out;
}

@keyframes phaseAdvance {
  0%   { transform: translateY(4px); opacity: 0.6; }
  100% { transform: translateY(0);   opacity: 1; }
}

.phase-node.active .phase-num {
  animation: phaseGlow 2.5s ease-in-out infinite, phasePulse 180ms ease-out;
}

@keyframes phasePulse {
  0%   { box-shadow: 0 0 0 0 rgba(255, 180, 90, 0.6); }
  100% { box-shadow: 0 0 0 12px rgba(255, 180, 90, 0); }
}
```

PhaseIndicator 通过内部 useEffect 检测 `effectivePhase` 变化，在目标 node 上短时加 `advancing` 类。

- [ ] **Step 3：类型检查**

```bash
cd frontend && npm run build
```

Expected: 无错误

- [ ] **Step 4：提交**

```bash
git add frontend/src/components/PhaseIndicator.tsx frontend/src/styles/index.css
git commit -m "feat(frontend): PhaseIndicator honors overridePhase with advance animation"
```

---

### Task 10: Phase3Workbench 使用 override step

**Files:**
- Modify: `frontend/src/components/Phase3Workbench.tsx`

- [ ] **Step 1：实现**

在现有 `const activeStep = plan.phase3_step ?? 'brief'` 处改为：

```tsx
interface Props {
  plan: TravelPlanState
  overrideStep?: string | null
}

const activeStep = overrideStep ?? plan.phase3_step ?? 'brief'
```

App.tsx 传入 `<Phase3Workbench plan={plan} overrideStep={phaseOverride?.step} />`。

- [ ] **Step 2：类型检查**

```bash
cd frontend && npm run build
```

Expected: 无错误

- [ ] **Step 3：提交**

```bash
git add frontend/src/components/Phase3Workbench.tsx frontend/src/App.tsx
git commit -m "feat(frontend): Phase3Workbench honors overrideStep"
```

---

### Task 11: PhaseTransitionCard 系统消息

**Files:**
- Modify: `frontend/src/components/MessageBubble.tsx`
- Modify: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/styles/index.css`

- [x] **Step 1：MessageBubble 新增 variant**

在 `MessageBubble.tsx` 的 `if (role === 'system' && stateChanges...)` 分支之前追加：

```tsx
if (role === 'system' && phaseTransition) {
  return (
    <div className="message system-phase-transition">
      <div className="phase-transition-card">
        <span className="phase-transition-icon">🚀</span>
        <span className="phase-transition-text">
          已进入{PHASE_LABELS[phaseTransition.to_phase] ?? `Phase ${phaseTransition.to_phase}`}
          {phaseTransition.to_step && ` · ${STEP_LABELS[phaseTransition.to_step] ?? phaseTransition.to_step}`}
        </span>
      </div>
    </div>
  )
}
```

`PHASE_LABELS` / `STEP_LABELS` 常量定义在文件头。Props 加 `phaseTransition?: { to_phase: number; to_step?: string | null }`。

- [x] **Step 2：ChatPanel 在 phase_transition 事件时插入卡片**

Task 8 中的分派改为：

```tsx
if (event.type === 'phase_transition' && event.to_phase !== undefined) {
  onPhaseTransition?.({ ... })
  if (event.from_phase !== event.to_phase) {  // 只在主 phase 变化时插入卡片
    setMessages((prev) => [...prev, {
      id: createMessageId(),
      role: 'system',
      content: '',
      phaseTransition: { to_phase: event.to_phase!, to_step: event.to_step },
    }])
  }
  return
}
```

`ChatMessage` 接口加 `phaseTransition?: { to_phase: number; to_step?: string | null }`。

- [x] **Step 3：CSS**

`frontend/src/styles/index.css`：

```css
.system-phase-transition { margin: 6px auto; }
.phase-transition-card {
  display: inline-flex; gap: 8px;
  padding: 4px 10px;
  background: rgba(255, 180, 90, 0.08);
  border: 1px solid rgba(255, 180, 90, 0.2);
  border-radius: 999px;
  color: var(--color-text-muted);
  font-size: 12px;
}
```

- [ ] **Step 4：类型检查**

```bash
cd frontend && npm run build
```

Expected: 无错误

- [ ] **Step 5：提交**

```bash
git add frontend/src/components/MessageBubble.tsx frontend/src/components/ChatPanel.tsx frontend/src/styles/index.css
git commit -m "feat(frontend): add PhaseTransitionCard for phase change announcements"
```

---

### Task 12: E2E — 验证 phase tab 早于 state_update 切换

**Files:**
- Modify: `e2e-test.spec.ts`

- [x] **Step 1：扩展 demo spec**

在 Phase 1 → Phase 3 的断言点追加：

```ts
test('phase_transition event updates tab before state_update', async ({ page }) => {
  await page.goto('/')
  // ... 触发一次会导致 phase 切换的消息

  // 断言：在 state_update 事件到达前（等 200ms），PhaseIndicator 已切到 Phase 3
  await page.waitForTimeout(200)
  const activeTab = await page.locator('.phase-node.active').textContent()
  expect(activeTab).toContain('方案设计')  // Phase 3 label

  // 断言：有 PhaseTransitionCard 插入
  await expect(page.locator('.phase-transition-card')).toBeVisible()
})
```

复用 `scripts/demo/demo-scripted-session.json` 的 mock 机制，确保事件顺序 mock：先 `phase_transition`，再 `state_update`。

- [ ] **Step 2：运行 E2E**

```bash
npx playwright test e2e-test.spec.ts
```

Expected: 全部通过

- [x] **Step 3：PROJECT_OVERVIEW.md 同步**

在 SSE 协议段添加 `phase_transition` 事件说明，在"关键组件"段说明 `phaseOverride` 机制。

- [ ] **Step 4：提交 + PR1 收尾**

```bash
git add e2e-test.spec.ts PROJECT_OVERVIEW.md
git commit -m "test(e2e): verify phase tab switches before state_update arrival"
```

---

## PR2 — 思考气泡 + 工具信息增强（P1）

**范围**：`agent_status` 事件触发点 + `tool_call.human_label` + ThinkingBubble 组件 + 工具卡副标题与计时器。

### Task 13: backend — `before_llm_call` yield `agent_status(thinking)`

**Files:**
- Modify: `backend/agent/loop.py`
- Test: `backend/tests/test_agent_status_event.py`（新建）

- [ ] **Step 1：写失败测试**

```python
import pytest
from llm.types import ChunkType
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_agent_status_thinking_emitted_before_each_llm_call(agent_with_two_iterations):
    chunks = [c async for c in agent_with_two_iterations.run([], phase=1)]
    thinking_chunks = [
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS and c.agent_status["stage"] == "thinking"
    ]
    assert len(thinking_chunks) == 2
    assert thinking_chunks[0].agent_status["iteration"] == 0
    assert thinking_chunks[1].agent_status["iteration"] == 1
```

- [ ] **Step 2：运行确认失败**

```bash
cd backend && pytest tests/test_agent_status_event.py -v
```

Expected: FAIL

- [ ] **Step 3：实现**

`backend/agent/loop.py` 在每次 iteration 开始、LLM 调用之前（在 `ContextManager.build_system_message` 之后、`self.llm.chat(...)` 之前）：

```python
iteration_idx = 0  # 在循环外初始化
# ... 循环内:
yield LLMChunk(
    type=ChunkType.AGENT_STATUS,
    agent_status={"stage": "thinking", "iteration": iteration_idx},
)
iteration_idx += 1
# ... 原 LLM 调用
```

- [ ] **Step 4：运行确认通过**

```bash
cd backend && pytest tests/test_agent_status_event.py -v
```

Expected: 1 passed

- [ ] **Step 5：提交**

```bash
git add backend/agent/loop.py backend/tests/test_agent_status_event.py
git commit -m "feat(loop): emit agent_status(thinking) before each LLM call"
```

---

### Task 14: backend — summarizing 追踪 flag

**Files:**
- Modify: `backend/agent/loop.py`
- Test: `backend/tests/test_agent_status_event.py`

- [ ] **Step 1：写失败测试**

```python
@pytest.mark.asyncio
async def test_agent_status_summarizing_after_tool_batch(agent_iteration_with_tools_then_text):
    """Agent: iter 0 (tool_call) → iter 1 (only text). Second thinking should be summarizing."""
    chunks = [c async for c in agent_iteration_with_tools_then_text.run([], phase=1)]
    statuses = [c.agent_status["stage"] for c in chunks if c.type == ChunkType.AGENT_STATUS]
    assert statuses == ["thinking", "summarizing"]
```

- [ ] **Step 2：运行确认失败**

Expected: FAIL

- [ ] **Step 3：实现**

`backend/agent/loop.py` 维护一个 `prev_iteration_had_tools: bool`，每次工具批执行完毕后置为 True，进入新 iteration 时：

```python
# 循环体开始
stage = "summarizing" if prev_iteration_had_tools and not phase_changed_in_prev else "thinking"
yield LLMChunk(
    type=ChunkType.AGENT_STATUS,
    agent_status={"stage": stage, "iteration": iteration_idx},
)
iteration_idx += 1
prev_iteration_had_tools = False  # 本轮还没执行工具

# 工具批后:
if tool_calls_executed > 0:
    prev_iteration_had_tools = True
```

- [ ] **Step 4：运行确认通过**

```bash
cd backend && pytest tests/test_agent_status_event.py -v
```

Expected: 2 passed

- [ ] **Step 5：提交**

```bash
git add backend/agent/loop.py backend/tests/test_agent_status_event.py
git commit -m "feat(loop): distinguish summarizing stage after tool batch with no phase change"
```

---

### Task 15: `@tool` 装饰器新增 `human_label`

**Files:**
- Modify: `backend/tools/base.py`
- Test: `backend/tests/test_tool_human_label.py`（新建）

- [ ] **Step 1：写失败测试**

```python
from tools.base import tool, ToolDef


def test_tool_decorator_accepts_human_label():
    @tool(name="demo", description="", phases=[1], parameters={}, human_label="测试动作")
    async def demo_tool():
        return {}

    assert isinstance(demo_tool, ToolDef)
    assert demo_tool.human_label == "测试动作"


def test_tool_decorator_human_label_optional_defaults_none():
    @tool(name="demo2", description="", phases=[1], parameters={})
    async def demo_tool():
        return {}

    assert demo_tool.human_label is None
```

- [ ] **Step 2：运行确认失败**

```bash
cd backend && pytest tests/test_tool_human_label.py -v
```

Expected: FAIL

- [ ] **Step 3：实现**

`backend/tools/base.py`：

```python
@dataclass
class ToolDef:
    name: str
    description: str
    phases: list[int]
    parameters: dict[str, Any]
    _fn: Callable[..., Coroutine[Any, Any, Any]] = field(repr=False)
    side_effect: str = "read"
    human_label: str | None = None

    # ...


def tool(
    name: str,
    description: str,
    phases: list[int],
    parameters: dict[str, Any],
    side_effect: str = "read",
    human_label: str | None = None,
) -> Callable:
    def decorator(fn: Callable) -> ToolDef:
        return ToolDef(
            name=name,
            description=description,
            phases=phases,
            parameters=parameters,
            _fn=fn,
            side_effect=side_effect,
            human_label=human_label,
        )

    return decorator
```

- [ ] **Step 4：运行确认通过**

```bash
cd backend && pytest tests/test_tool_human_label.py -v
```

Expected: 2 passed

- [ ] **Step 5：提交**

```bash
git add backend/tools/base.py backend/tests/test_tool_human_label.py
git commit -m "feat(tools): add optional human_label to @tool decorator and ToolDef"
```

---

### Task 16: ToolEngine 把 human_label 写入 tool_call 事件

**Files:**
- Modify: `backend/tools/engine.py`
- Modify: `backend/agent/loop.py`（yield tool_call 时透传）
- Test: `backend/tests/test_tool_human_label.py`

- [ ] **Step 1：写失败测试**

```python
@pytest.mark.asyncio
async def test_tool_call_event_includes_human_label_when_defined(agent_with_labeled_tool):
    chunks = [c async for c in agent_with_labeled_tool.run([], phase=1)]
    tool_starts = [c for c in chunks if c.type == ChunkType.TOOL_CALL_START]
    assert any(
        getattr(c.tool_call, "human_label", None) == "测试动作"
        for c in tool_starts
    )
```

同时确认 ToolCall 数据类加了 human_label 字段。

- [ ] **Step 2：运行确认失败**

Expected: FAIL

- [ ] **Step 3：实现**

`backend/agent/types.py` 的 `ToolCall` 数据类加 `human_label: str | None = None`。

`backend/agent/loop.py` 构造 `tool_call` 对象时查 ToolEngine 注册表：

```python
tool_def = self.tool_engine.get_tool(tc.name)
tc.human_label = tool_def.human_label if tool_def else None
```

`backend/main.py` SSE 翻译（line 1505-1514）：

```python
if chunk.tool_call:
    event_data["tool_call"] = {
        "id": chunk.tool_call.id,
        "name": chunk.tool_call.name,
        "arguments": chunk.tool_call.arguments,
        "human_label": chunk.tool_call.human_label,
    }
```

- [ ] **Step 4：运行确认通过**

```bash
cd backend && pytest tests/test_tool_human_label.py -v
```

Expected: 3 passed

- [ ] **Step 5：提交**

```bash
git add backend/agent/types.py backend/agent/loop.py backend/main.py backend/tests/test_tool_human_label.py
git commit -m "feat(tools): propagate human_label through ToolCall to SSE event"
```

---

### Task 17: 给 14 个工具补 `human_label` 文案

**Files:**
- Modify: `backend/tools/update_plan_state.py`
- Modify: `backend/tools/xiaohongshu_search.py`
- Modify: `backend/tools/web_search.py`
- Modify: `backend/tools/search_flights.py`
- Modify: `backend/tools/search_trains.py`
- Modify: `backend/tools/search_accommodations.py`
- Modify: `backend/tools/get_poi_info.py`
- Modify: `backend/tools/calculate_route.py`
- Modify: `backend/tools/assemble_day_plan.py`
- Modify: `backend/tools/check_weather.py`
- Modify: `backend/tools/check_availability.py`
- Modify: `backend/tools/check_feasibility.py`
- Modify: `backend/tools/generate_summary.py`
- Modify: `backend/tools/quick_travel_search.py`（若存在）
- Test: `backend/tests/test_tool_human_label.py`

- [ ] **Step 1：写覆盖测试**

```python
def test_all_registered_tools_have_human_label():
    from tools.engine import ToolEngine
    engine = ToolEngine()
    engine.register_defaults()

    missing = [t.name for t in engine.list_tools() if t.human_label is None]
    assert missing == [], f"Tools missing human_label: {missing}"
```

- [ ] **Step 2：运行确认失败**

Expected: FAIL，列出所有无 label 的工具

- [ ] **Step 3：按 spec 附录 A 补齐**

对每个文件，在 `@tool(...)` 装饰器里加 `human_label="..."`（映射表见 `docs/superpowers/specs/2026-04-14-agent-frontend-waiting-ux-design.md` 附录 A）。

示例：

```python
@tool(
    name="xiaohongshu_search",
    description="...",
    phases=[1, 3],
    parameters={...},
    human_label="翻小红书找灵感",
)
async def xiaohongshu_search(...):
    ...
```

- [ ] **Step 4：运行确认通过**

```bash
cd backend && pytest tests/test_tool_human_label.py -v
```

Expected: 4 passed

- [ ] **Step 5：提交**

```bash
git add backend/tools/*.py backend/tests/test_tool_human_label.py
git commit -m "feat(tools): populate human_label for all registered tools"
```

---

### Task 18: 创建 ThinkingBubble 组件

**Files:**
- Create: `frontend/src/components/ThinkingBubble.tsx`
- Modify: `frontend/src/styles/index.css`

- [x] **Step 1：组件骨架**

```tsx
import { useEffect, useState } from 'react'

interface Props {
  createdAt: number
  stage?: 'thinking' | 'summarizing' | 'compacting'
  iteration?: number
  hint?: string | null
  onDismiss?: () => void
}

const STAGE_FALLBACK_TEXT: Record<NonNullable<Props['stage']>, string> = {
  thinking: '思考中…',
  summarizing: '汇总中…',
  compacting: '整理上下文中…',
}

export default function ThinkingBubble({ createdAt, stage = 'thinking', iteration = 0, hint }: Props) {
  const [elapsed, setElapsed] = useState(0)

  useEffect(() => {
    const t = setInterval(() => setElapsed(Date.now() - createdAt), 500)
    return () => clearInterval(t)
  }, [createdAt])

  const text = hint
    ?? (iteration >= 1 ? `继续思考…（第 ${iteration + 1} 轮）` : STAGE_FALLBACK_TEXT[stage])
  const isStale = elapsed >= 2000 && stage === 'thinking' && !hint
  const displayText = isStale ? '正在连接…' : text

  return (
    <div className="thinking-bubble" data-testid="thinking-bubble" data-stage={stage}>
      <span className="thinking-dot" />
      <span className="thinking-text">{displayText}</span>
    </div>
  )
}
```

- [x] **Step 2：CSS**

```css
.thinking-bubble {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 8px 14px;
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.06);
  color: var(--color-text-muted);
  font-size: 13px;
  animation: bubbleIn 200ms ease-out;
}

@keyframes bubbleIn {
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: translateY(0); }
}

.thinking-bubble.fading {
  animation: bubbleOut 200ms ease-out forwards;
}

@keyframes bubbleOut {
  from { opacity: 1; }
  to   { opacity: 0; transform: translateY(-4px); }
}

.thinking-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: rgba(255, 180, 90, 0.7);
  animation: thinkingPulse 1.2s ease-in-out infinite;
}

@keyframes thinkingPulse {
  0%, 100% { opacity: 0.4; transform: scale(0.85); }
  50%      { opacity: 1;   transform: scale(1.15); }
}
```

- [ ] **Step 3：类型检查**

```bash
cd frontend && npm run build
```

Expected: 无错误

- [ ] **Step 4：提交**

```bash
git add frontend/src/components/ThinkingBubble.tsx frontend/src/styles/index.css
git commit -m "feat(frontend): add ThinkingBubble component with stage-aware copy"
```

---

### Task 19: ChatPanel 接入 ThinkingBubble 生命周期

**Files:**
- Modify: `frontend/src/components/ChatPanel.tsx`

- [x] **Step 1：新增 state**

```tsx
const [thinking, setThinking] = useState<{
  createdAt: number
  stage: 'thinking' | 'summarizing' | 'compacting'
  iteration: number
  hint: string | null
} | null>(null)
```

- [x] **Step 2：handleSend 本地立即触发**

```tsx
const handleSend = async () => {
  if (!input.trim() || streaming) return
  // ... 原逻辑
  setThinking({ createdAt: Date.now(), stage: 'thinking', iteration: 0, hint: null })
  // ... 调用 sendMessage
}
```

- [x] **Step 3：事件回调中控制生命周期**

```tsx
if (event.type === 'agent_status' && event.stage) {
  setThinking({
    createdAt: Date.now(),
    stage: event.stage,
    iteration: event.iteration ?? 0,
    hint: event.hint ?? null,
  })
  return
}

if (event.type === 'text_delta' || event.type === 'tool_call' || event.type === 'error') {
  setThinking(null)
  // ... 原处理继续
}
```

- [x] **Step 4：渲染**

在 `{messages.map(...)}` 之后、`streaming-cursor` 之前：

```tsx
{thinking && <ThinkingBubble {...thinking} />}
```

并在 `handleStop` / done 收尾处调用 `setThinking(null)`。

- [ ] **Step 5：类型检查**

```bash
cd frontend && npm run build
```

Expected: 无错误

- [ ] **Step 6：提交**

```bash
git add frontend/src/components/ChatPanel.tsx
git commit -m "feat(frontend): wire ThinkingBubble lifecycle to agent_status SSE events"
```

---

### Task 20: MessageBubble — 工具卡副标题与计时器

**Files:**
- Modify: `frontend/src/components/MessageBubble.tsx`
- Modify: `frontend/src/components/ChatPanel.tsx`（新增 startedAt/endedAt 字段）
- Modify: `frontend/src/styles/index.css`

- [x] **Step 1：ChatMessage 加计时字段**

`ChatPanel.tsx` 的 `ChatMessage` 接口：

```tsx
interface ChatMessage {
  // ...
  humanLabel?: string | null
  startedAt?: number
  endedAt?: number
}
```

tool_call 事件到达时 `startedAt: Date.now()`, `humanLabel: event.tool_call.human_label`；tool_result 到达时 `endedAt: Date.now()`。

- [x] **Step 2：MessageBubble 渲染**

```tsx
interface Props {
  // ...
  humanLabel?: string | null
  startedAt?: number
  endedAt?: number
}

// tool 分支内
const elapsedMs = (endedAt ?? Date.now()) - (startedAt ?? Date.now())
const elapsedText = elapsedMs > 0 ? `${(elapsedMs / 1000).toFixed(1)}s` : ''

return (
  <div className={`message tool ${toolStatus ?? 'pending'}`}>
    <div className="tool-card">
      <div className="tool-card-header">
        <span className="tool-badge">{toolName}</span>
        {/* ... 原 actions */}
      </div>
      {(humanLabel || elapsedText) && (
        <div className="tool-subtitle">
          {humanLabel && <span>▸ {humanLabel}</span>}
          {elapsedText && <span className="tool-elapsed">{elapsedText}</span>}
        </div>
      )}
      {/* ... 原详情区 */}
    </div>
  </div>
)
```

- [x] **Step 3：Pending 状态计时器重渲染**

MessageBubble 内用 useEffect + setInterval 每 500ms 强制更新（仅 pending 时）：

```tsx
const [, forceTick] = useState(0)
useEffect(() => {
  if (toolStatus !== 'pending') return
  const t = setInterval(() => forceTick(x => x + 1), 500)
  return () => clearInterval(t)
}, [toolStatus])
```

- [x] **Step 4：CSS**

```css
.tool-subtitle {
  display: flex; justify-content: space-between;
  padding: 2px 12px 6px;
  font-size: 12px;
  color: var(--color-text-muted);
}
.tool-elapsed {
  font-variant-numeric: tabular-nums;
  opacity: 0.7;
}
.tool.pending::before {
  content: '';
  position: absolute; left: 0; top: 0; bottom: 0;
  width: 2px;
  background: linear-gradient(180deg, transparent, rgba(255, 180, 90, 0.5), transparent);
  animation: toolBreath 1.8s ease-in-out infinite;
}
@keyframes toolBreath {
  0%, 100% { opacity: 0.3; }
  50%      { opacity: 1; }
}
```

- [ ] **Step 5：类型检查**

```bash
cd frontend && npm run build
```

Expected: 无错误

- [ ] **Step 6：提交**

```bash
git add frontend/src/components/MessageBubble.tsx frontend/src/components/ChatPanel.tsx frontend/src/styles/index.css
git commit -m "feat(frontend): tool card shows human_label and live elapsed timer"
```

---

### Task 21: 工具 Pending 超 8s 警告

**Files:**
- Modify: `frontend/src/components/MessageBubble.tsx`

- [x] **Step 1：实现**

在 tool 分支内：

```tsx
const longRunning = toolStatus === 'pending' && elapsedMs >= 8000

return (
  // ...
  <div className={`tool-subtitle ${longRunning ? 'long-running' : ''}`}>
    {humanLabel && <span>▸ {humanLabel}{longRunning && '（运行较久，请稍候）'}</span>}
    {elapsedText && <span className="tool-elapsed">{elapsedText}</span>}
  </div>
)
```

CSS：

```css
.tool-subtitle.long-running {
  color: rgba(255, 180, 90, 0.9);
}
```

- [ ] **Step 2：类型检查 + 提交**

```bash
cd frontend && npm run build
git add frontend/src/components/MessageBubble.tsx frontend/src/styles/index.css
git commit -m "feat(frontend): warn long-running tool calls after 8 seconds"
```

---

### Task 22: E2E — 创建 waiting-experience 专项

**Files:**
- Create: `e2e-waiting-experience.spec.ts`
- Create: `playwright.waiting.config.ts`
- Modify: `scripts/demo/demo-scripted-session.json`（追加 waiting scenario 所需事件）

- [x] **Step 1：编写 spec**

```ts
import { test, expect } from '@playwright/test'

test.describe('Agent waiting experience', () => {
  test('ThinkingBubble appears immediately after send', async ({ page }) => {
    await page.goto('/')
    await page.fill('[data-testid=chat-input]', '去成都')
    await page.click('[data-testid=send-btn]')
    await expect(page.locator('[data-testid=thinking-bubble]')).toBeVisible({ timeout: 500 })
  })

  test('ThinkingBubble dismisses on first text_delta', async ({ page }) => {
    // mock scenario where first delta arrives at 800ms
    // ...
    await expect(page.locator('[data-testid=thinking-bubble]')).toBeHidden({ timeout: 1500 })
  })

  test('tool card shows human_label and elapsed timer', async ({ page }) => {
    // trigger scenario with xiaohongshu_search tool
    await expect(page.locator('.tool-subtitle')).toContainText('翻小红书找灵感')
    await expect(page.locator('.tool-elapsed')).toContainText(/^\d+\.\d+s$/)
  })
})
```

- [x] **Step 2：playwright.waiting.config.ts**

参照 `playwright.retry.config.ts` 结构：

```ts
import { defineConfig } from '@playwright/test'
import baseConfig from './playwright.config'

export default defineConfig({
  ...baseConfig,
  testMatch: 'e2e-waiting-experience.spec.ts',
})
```

- [x] **Step 3：运行**

```bash
npx playwright test --config playwright.waiting.config.ts
```

Expected: 全部通过

- [x] **Step 4：PROJECT_OVERVIEW.md 同步**

追加 `agent_status` 事件到 SSE 协议段，在"关键组件"段说明 ThinkingBubble 和工具卡增强。

- [x] **Step 5：提交 + PR2 收尾**

```bash
git add e2e-waiting-experience.spec.ts playwright.waiting.config.ts scripts/demo/demo-scripted-session.json PROJECT_OVERVIEW.md
git commit -m "test(e2e): cover ThinkingBubble lifecycle and tool card enhancements"
```

---

## PR3 — 细颗粒反馈 + 回声收尾（P2）

### Task 23: backend keepalive 15s → 8s

**Files:**
- Modify: `backend/main.py`（`_keepalive_loop` 内 `asyncio.sleep(15)`）
- Test: `backend/tests/test_keepalive_interval.py`（新建）

- [x] **Step 1：写测试**

```python
import asyncio
import pytest


@pytest.mark.asyncio
async def test_keepalive_sends_every_8_seconds(app, sessions, session_id):
    # mock slow agent + fake clock; assert keepalive frame count
    # (use the SSEEvent stream and measure inter-keepalive gap ≈ 8s ± 1s)
    ...
```

- [x] **Step 2：改代码**

```python
async def _keepalive_loop():
    try:
        while True:
            await asyncio.sleep(8)
            await keepalive_queue.put(json.dumps({"type": "keepalive"}))
    except asyncio.CancelledError:
        pass
```

- [x] **Step 3：运行 + 提交**

```bash
cd backend && pytest tests/test_keepalive_interval.py -v
git add backend/main.py backend/tests/test_keepalive_interval.py
git commit -m "feat(api): tighten keepalive cadence from 15s to 8s"
```

---

### Task 24: backend — `agent_status(compacting)` 预告

**Files:**
- Modify: `backend/context/manager.py`（暴露预判）
- Modify: `backend/agent/loop.py`
- Test: `backend/tests/test_agent_status_event.py`

- [x] **Step 1：写测试**

```python
@pytest.mark.asyncio
async def test_agent_status_compacting_emitted_when_budget_exceeded(agent_over_budget):
    chunks = [c async for c in agent_over_budget.run(long_history_messages(), phase=1)]
    stages = [c.agent_status["stage"] for c in chunks if c.type == ChunkType.AGENT_STATUS]
    assert "compacting" in stages
```

- [x] **Step 2：context/manager.py 暴露预判**

在 `ContextManager` 中新增：

```python
def will_trigger_compaction(self, messages: list[Message], phase: int) -> bool:
    estimated_tokens = self._estimate_tokens(messages)
    budget = self.context_window - self.max_output_tokens - 2000
    return estimated_tokens / budget > 0.60
```

- [x] **Step 3：loop.py 使用预判**

在 yield `thinking` 之前：

```python
if self.context.will_trigger_compaction(messages, phase):
    yield LLMChunk(
        type=ChunkType.AGENT_STATUS,
        agent_status={"stage": "compacting"},
    )
```

- [x] **Step 4：运行 + 提交**

```bash
cd backend && pytest tests/test_agent_status_event.py -v
git add backend/context/manager.py backend/agent/loop.py backend/tests/test_agent_status_event.py
git commit -m "feat(loop): emit agent_status(compacting) pre-announcement when budget triggers compression"
```

---

### Task 25: 前端 staleness + 呼吸小点

**Files:**
- Modify: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/components/ThinkingBubble.tsx`
- Modify: `frontend/src/components/MessageBubble.tsx`
- Modify: `frontend/src/styles/index.css`

- [x] **Step 1：ChatPanel staleness state**

```tsx
const [staleness, setStaleness] = useState<'normal' | 'minor' | 'waiting'>('normal')

useEffect(() => {
  if (!streaming) { setStaleness('normal'); return }
  const t = setInterval(() => {
    const gap = Date.now() - lastEventTimeRef.current
    if (gap < 8000) setStaleness('normal')
    else if (gap < 20000) setStaleness('minor')
    else setStaleness('waiting')
  }, 2000)
  return () => clearInterval(t)
}, [streaming])

// 现有 KEEPALIVE_TIMEOUT_MS 已有的 feedback 升级逻辑改用 staleness === 'waiting'
```

- [x] **Step 2：KEEPALIVE_TIMEOUT_MS 30s → 20s**

```tsx
const KEEPALIVE_TIMEOUT_MS = 20_000
```

- [x] **Step 3：呼吸小点**

ThinkingBubble / MessageBubble tool 分支内读取 `staleness` prop（从 ChatPanel 透传）。若 `staleness === 'minor'`：

```tsx
{staleness === 'minor' && <span className="breath-dot">⋯</span>}
```

CSS：

```css
.breath-dot {
  margin-left: 6px;
  color: rgba(255, 180, 90, 0.6);
  animation: breathDot 1.4s ease-in-out infinite;
}
@keyframes breathDot {
  0%, 100% { opacity: 0.3; }
  50%      { opacity: 1; }
}
```

- [x] **Step 4：类型检查 + 提交**

```bash
cd frontend && npm run build
git add frontend/src/components/ChatPanel.tsx frontend/src/components/ThinkingBubble.tsx frontend/src/components/MessageBubble.tsx frontend/src/styles/index.css
git commit -m "feat(frontend): three-tier staleness indicator with breath dot at 8-20s"
```

---

### Task 26: 前端 RoundSummaryBar on done

**Files:**
- Create: `frontend/src/components/RoundSummaryBar.tsx`
- Modify: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/styles/index.css`

- [x] **Step 1：组件**

```tsx
import { useEffect, useState } from 'react'

interface Props {
  toolCount: number
  durationMs: number
  memoryCount: number
}

export default function RoundSummaryBar({ toolCount, durationMs, memoryCount }: Props) {
  const [visible, setVisible] = useState(true)
  useEffect(() => {
    const t = setTimeout(() => setVisible(false), 2500)
    return () => clearTimeout(t)
  }, [])
  if (!visible) return null
  return (
    <div className="round-summary-bar" role="status">
      ✓ 本轮已完成 · {toolCount} 个工具 · 用时 {(durationMs / 1000).toFixed(1)}s
      {memoryCount > 0 && ` · 命中 ${memoryCount} 条记忆`}
    </div>
  )
}
```

- [x] **Step 2：ChatPanel 集成**

`ChatPanel.tsx`：

```tsx
const [summary, setSummary] = useState<{ toolCount: number; durationMs: number; memoryCount: number } | null>(null)
const roundStateRef = useRef({ toolCount: 0, memoryCount: 0, startedAt: 0 })

// handleSend 开始时
roundStateRef.current = { toolCount: 0, memoryCount: 0, startedAt: Date.now() }

// tool_call 事件
roundStateRef.current.toolCount += 1

// memory_recall 事件
roundStateRef.current.memoryCount = event.item_ids?.length ?? 0

// done 事件
if (event.run_status === 'completed') {
  setSummary({
    toolCount: roundStateRef.current.toolCount,
    durationMs: Date.now() - roundStateRef.current.startedAt,
    memoryCount: roundStateRef.current.memoryCount,
  })
}

// 新一轮开始时 setSummary(null)
```

渲染：`{summary && <RoundSummaryBar {...summary} />}`

- [x] **Step 3：CSS**

```css
.round-summary-bar {
  height: 22px;
  padding: 0 12px;
  font-size: 11px;
  color: var(--color-text-muted);
  opacity: 0;
  animation: summaryFadeInOut 2.5s ease-in-out forwards;
}
@keyframes summaryFadeInOut {
  0%   { opacity: 0; }
  15%  { opacity: 1; }
  85%  { opacity: 1; }
  100% { opacity: 0; }
}
```

- [x] **Step 4：类型检查 + 提交**

```bash
cd frontend && npm run build
git add frontend/src/components/RoundSummaryBar.tsx frontend/src/components/ChatPanel.tsx frontend/src/styles/index.css
git commit -m "feat(frontend): RoundSummaryBar after done event with 2.5s fade"
```

---

### Task 27: 前端 memory_recall 内联 chip

**Files:**
- Modify: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/components/MessageBubble.tsx`
- Modify: `frontend/src/styles/index.css`

- [x] **Step 1：ChatPanel 处理**

在 `memory_recall` 事件回调中新增"本轮首次"判定（利用 `roundStateRef.current.memoryChipInserted` bool）：

```tsx
if (event.type === 'memory_recall' && event.item_ids) {
  onMemoryRecall?.(event.item_ids)
  if (!roundStateRef.current.memoryChipInserted && event.item_ids.length > 0) {
    roundStateRef.current.memoryChipInserted = true
    setMessages((prev) => [...prev, {
      id: createMessageId(),
      role: 'system',
      content: '',
      memoryChip: { count: event.item_ids.length },
    }])
  }
  return
}
```

`handleSend` 开始时重置 `memoryChipInserted = false`。`ChatMessage` 接口加 `memoryChip?: { count: number }`。

- [x] **Step 2：MessageBubble variant**

```tsx
if (role === 'system' && memoryChip) {
  return (
    <button
      type="button"
      className="message system-memory-chip"
      onClick={() => window.dispatchEvent(new CustomEvent('openMemoryCenter'))}
    >
      💭 本轮使用 {memoryChip.count} 条旅行记忆
    </button>
  )
}
```

App.tsx 监听 `openMemoryCenter` 事件打开抽屉（复用已有逻辑）。

- [x] **Step 3：CSS**

```css
.system-memory-chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; margin: 4px auto;
  background: rgba(180, 140, 255, 0.08);
  border: 1px solid rgba(180, 140, 255, 0.2);
  border-radius: 999px;
  color: var(--color-text-muted); font-size: 12px;
  cursor: pointer;
}
.system-memory-chip:hover { background: rgba(180, 140, 255, 0.14); }
```

- [x] **Step 4：类型检查 + 提交 + PR3 收尾**

```bash
cd frontend && npm run build
git add frontend/src/components/ChatPanel.tsx frontend/src/components/MessageBubble.tsx frontend/src/App.tsx frontend/src/styles/index.css PROJECT_OVERVIEW.md
git commit -m "feat(frontend): inline memory_recall chip with MemoryCenter jump"
```

---

## PR4 — 推理旁白探索（P3）

### Task 28: 创建 `backend/agent/narration.py`

**Files:**
- Create: `backend/agent/narration.py`
- Create: `backend/tests/test_narration.py`

- [x] **Step 1：写测试**

```python
from agent.narration import compute_narration
from state.models import TravelPlanState


def test_phase1_no_destination_returns_inspiration_hint():
    plan = TravelPlanState(session_id="s", phase=1, destination=None)
    assert compute_narration(plan) == "先搞清楚你想去哪，然后翻点真实游记"


def test_phase3_brief_step():
    plan = TravelPlanState(session_id="s", phase=3, phase3_step="brief")
    assert "画像" in compute_narration(plan)


def test_phase3_candidate_step():
    plan = TravelPlanState(session_id="s", phase=3, phase3_step="candidate")
    assert "候选" in compute_narration(plan)


def test_phase3_skeleton_step():
    plan = TravelPlanState(session_id="s", phase=3, phase3_step="skeleton")
    assert "骨架" in compute_narration(plan)


def test_phase3_lock_step():
    plan = TravelPlanState(session_id="s", phase=3, phase3_step="lock")
    assert "锁定" in compute_narration(plan)


def test_phase5():
    plan = TravelPlanState(session_id="s", phase=5)
    assert "日程" in compute_narration(plan)


def test_unrecognized_state_returns_none():
    plan = TravelPlanState(session_id="s", phase=99)
    assert compute_narration(plan) is None
```

- [x] **Step 2：实现**

```python
from state.models import TravelPlanState


def compute_narration(plan: TravelPlanState) -> str | None:
    if plan.phase == 1 and not plan.destination:
        return "先搞清楚你想去哪，然后翻点真实游记"
    if plan.phase == 1 and plan.destination:
        return "围绕目的地再收几条真实游记，定细节"
    if plan.phase == 3:
        step = getattr(plan, "phase3_step", None)
        if step == "brief":
            return "建立旅行画像，理清你的节奏和偏好"
        if step == "candidate":
            return "挑几个候选景点，看看哪些对你胃口"
        if step == "skeleton":
            return "把候选拼成 2–3 套骨架方案"
        if step == "lock":
            return "锁定交通和住宿，核一下预算"
    if plan.phase == 5:
        return "把骨架展开成日程，核对冲突"
    if plan.phase == 7:
        return "做出发前检查清单"
    return None
```

- [x] **Step 3：运行 + 提交**

```bash
cd backend && pytest tests/test_narration.py -v
# Expected: 7 passed
git add backend/agent/narration.py backend/tests/test_narration.py
git commit -m "feat(agent): rule-based narration hints for each phase/step"
```

---

### Task 29: loop.py 把 narration 注入 agent_status.hint

**Files:**
- Modify: `backend/agent/loop.py`
- Test: `backend/tests/test_agent_status_event.py`

- [x] **Step 1：写测试**

```python
@pytest.mark.asyncio
async def test_agent_status_thinking_includes_narration_hint_for_phase1(agent_phase1):
    chunks = [c async for c in agent_phase1.run([], phase=1)]
    thinking = next(
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS and c.agent_status["stage"] == "thinking"
    )
    assert thinking.agent_status["hint"] == "先搞清楚你想去哪，然后翻点真实游记"
```

- [x] **Step 2：实现**

Task 13/14 的 yield 处：

```python
from agent.narration import compute_narration

hint = compute_narration(self.plan) if self.plan else None
yield LLMChunk(
    type=ChunkType.AGENT_STATUS,
    agent_status={"stage": stage, "iteration": iteration_idx, "hint": hint},
)
```

- [x] **Step 3：运行 + 提交**

```bash
cd backend && pytest tests/test_agent_status_event.py -v
git add backend/agent/loop.py backend/tests/test_agent_status_event.py
git commit -m "feat(loop): inject narration hint into agent_status events"
```

---

### Task 30: 前端 ThinkingBubble hint 支持 + 收起

**Files:**
- Modify: `frontend/src/components/ThinkingBubble.tsx`
- Modify: `frontend/src/styles/index.css`

- [x] **Step 1：收起按钮 + localStorage 持久化**

```tsx
const STORAGE_KEY = 'thinkingBubble.collapsed'

export default function ThinkingBubble({ ... }: Props) {
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try { return localStorage.getItem(STORAGE_KEY) === '1' } catch { return false }
  })

  const handleCollapse = () => {
    setCollapsed(true)
    try { localStorage.setItem(STORAGE_KEY, '1') } catch {}
  }

  const effectiveText = collapsed ? STAGE_FALLBACK_TEXT[stage] : (hint ?? STAGE_FALLBACK_TEXT[stage])

  return (
    <div className="thinking-bubble" data-testid="thinking-bubble">
      <span className="thinking-dot" />
      <span className="thinking-text">{effectiveText}</span>
      {hint && !collapsed && (
        <button className="thinking-collapse" onClick={handleCollapse} aria-label="简化提示">×</button>
      )}
    </div>
  )
}
```

- [x] **Step 2：CSS**

```css
.thinking-collapse {
  margin-left: 4px;
  padding: 0 4px;
  background: none; border: none;
  color: var(--color-text-muted);
  opacity: 0.5; cursor: pointer;
  font-size: 14px;
}
.thinking-collapse:hover { opacity: 1; }
```

- [x] **Step 3：提交**

```bash
cd frontend && npm run build
git add frontend/src/components/ThinkingBubble.tsx frontend/src/styles/index.css
git commit -m "feat(frontend): ThinkingBubble hint display with dismiss preference"
```

---

### Task 31: Track B — spike memo

**Files:**
- Create: `docs/learning/2026-0X-XX-thinking-stream-spike.md`（占位日期待落地时确定）

- [x] **Step 1：调研与文档**

不写代码。产出一份 memo 回答以下问题：

1. `claude-sonnet-4-20250514` / `gpt-4o` 是否支持扩展思考 / reasoning chunk；开启后 token 成本变化
2. Provider 层 `LLMChunk` 改造方案（`ChunkType.REASONING_DELTA`）
3. UI 形态对比（accordion / 侧栏）与可用性 tradeoff
4. 与 compaction 的交互（reasoning 文本是否计入压缩目标）

memo 结尾给出"下一迭代是否启动实施"的建议。

- [x] **Step 2：提交**

```bash
git add docs/learning/*-thinking-stream-spike.md
git commit -m "docs: thinking stream spike memo evaluating reasoning chunk support"
```

---

## Self-Review（写完后自查，直接改）

### Spec coverage
- [x] 新事件 `phase_transition` 的 4 种触发路径 → Task 3/4/5/6
- [x] 新事件 `agent_status`（thinking/summarizing/compacting/hint） → Task 13/14/24/29
- [x] `tool_call.human_label` 字段 + 24 个工具 → Task 15/16/17
- [x] PhaseIndicator override + 动画 → Task 8/9
- [x] Phase3Workbench override → Task 10
- [x] PhaseTransitionCard → Task 11
- [x] ThinkingBubble 组件与生命周期 → Task 18/19/30
- [x] 工具副标题 + 计时器 + 8s 警告 → Task 20/21
- [x] Keepalive 8s 与 staleness 三档 → Task 23/25
- [x] RoundSummaryBar → Task 26
- [x] memory_recall 内联 chip → Task 27
- [x] Track A narration → Task 28/29
- [x] Track B spike memo → Task 31
- [x] E2E 覆盖 → Task 12/22

### 明确非目标
- 真思维链实施不在本计划（Task 31 只做 memo）
- 移动端适配不做
- Trace 面板实时等待态不做

### 类型一致性
- `phaseOverride` 字段名全计划统一（App/PhaseIndicator/Phase3Workbench）
- `human_label` 在 ToolDef / ToolCall / SSE payload / 前端类型四处统一命名
- `agent_status.stage` 三个枚举值一致：thinking / summarizing / compacting
- `phase_transition` payload 字段名：from_phase / to_phase / from_step / to_step / reason

---

## 交付完成后的全局清单

- [ ] 4 个 PR 全部合入 main
- [ ] `PROJECT_OVERVIEW.md` § 9 前端架构 + SSE 协议段同步
- [ ] 所有新增测试纳入 CI 运行
- [ ] Playwright `e2e-waiting-experience.spec.ts` 与现有 `e2e-retry-experience.spec.ts` / `e2e-send-button.spec.ts` 并列，不互相依赖
- [ ] 原 retry-recovery 专项 E2E 不被破坏（强制门禁）
