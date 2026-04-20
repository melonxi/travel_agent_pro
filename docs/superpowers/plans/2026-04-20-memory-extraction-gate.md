# Memory Extraction Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每轮 chat 在 assistant 回复后先进行一次轻量 gate 判定，只有 gate 明确需要时才在当前 SSE 流内执行 memory extraction，并在同一轮把内部任务状态回传给前端。

**Architecture:** 保留 `memory_recall` 的同步入口不变，把 `memory_extraction` 从“后台排队任务”改为“当前轮尾部串行内部任务”。后端新增一个轻量 gate 模型调用用于判断是否值得进入正式提取，`done` 事件延后到 gate 和 extraction 都完成之后；前端继续复用现有 `internal_task` 合并逻辑，只验证同轮尾部事件能正常收口。

**Tech Stack:** FastAPI SSE、Python async/await、pytest + pytest-asyncio、React 19 + TypeScript。

---

### Task 1: 锁定当前轮事件流行为

**Files:**
- Modify: `backend/tests/test_memory_integration.py`
- Test: `backend/tests/test_memory_integration.py`

- [ ] **Step 1: 写失败测试，描述 gate 拦截时的同轮事件流**

```python
@pytest.mark.asyncio
async def test_memory_extraction_gate_skips_in_same_stream(app):
    observed = {"extraction_called": False}

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="助手回复")
        yield LLMChunk(type=ChunkType.DONE)

    async def fake_gate(*args, **kwargs):
        return MemoryExtractionGateDecision(
            should_extract=False,
            reason="no_reusable_memory_signal",
            message="本轮未发现可复用记忆信号",
        )

    async def fake_extract(*args, **kwargs):
        observed["extraction_called"] = True
        return MemoryExtractionOutcome(
            status="success",
            message="should not run",
            item_ids=[],
            reason="saved",
        )
```

- [ ] **Step 2: 运行单测并确认它先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_integration.py -k gate_skips_in_same_stream -v`
Expected: FAIL，因为后端还没有 gate 判定对象和同轮尾部事件流。

- [ ] **Step 3: 再写失败测试，描述 gate 放行时 extraction 在当前轮完成**

```python
@pytest.mark.asyncio
async def test_memory_extraction_runs_before_done_in_same_stream(app):
    events = []

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="助手回复")
        yield LLMChunk(type=ChunkType.DONE)

    async def fake_gate(*args, **kwargs):
        return MemoryExtractionGateDecision(
            should_extract=True,
            reason="explicit_preference_signal",
            message="检测到可复用偏好信号",
        )
```

- [ ] **Step 4: 运行单测并确认它先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_integration.py -k 'runs_before_done_in_same_stream' -v`
Expected: FAIL，因为当前实现仍依赖 `_background_internal_tasks` 并在下一轮才展示 extraction 结果。

- [ ] **Step 5: 补一条 API 级回归测试，确保首轮响应本身就包含 memory_extraction 状态**

```python
@pytest.mark.asyncio
async def test_chat_stream_includes_same_turn_memory_extraction_event(monkeypatch, tmp_path):
    ...
    assert '"kind": "memory_extraction"' in resp.text
    assert '"type": "done"' in resp.text
    assert resp.text.index('"kind": "memory_extraction"') < resp.text.rindex('"type": "done"')
```

- [ ] **Step 6: 运行 API 测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_api.py -k same_turn_memory_extraction -v`
Expected: FAIL，因为现在 `done` 会在 extraction 之前结束当前轮。

### Task 2: 实现后端 gate + 同轮尾部 extraction

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/memory/extraction.py`
- Test: `backend/tests/test_memory_integration.py`

- [ ] **Step 1: 增加 gate 判定数据结构与轻量调用函数**

```python
@dataclass
class MemoryExtractionGateDecision:
    should_extract: bool
    reason: str
    message: str
```

```python
async def _decide_memory_extraction(... ) -> MemoryExtractionGateDecision:
    if not user_messages:
        return MemoryExtractionGateDecision(
            should_extract=False,
            reason="no_user_messages",
            message="本轮没有可提取的用户消息",
        )
```

- [ ] **Step 2: 用工具调用 schema 定义 gate 输出，约束模型只返回是否值得提取**

```python
def build_v3_extraction_gate_tool() -> dict[str, Any]:
    return {
        "name": "decide_memory_extraction",
        "description": "Decide whether the current turn is worth running memory extraction.",
        ...
    }
```

- [ ] **Step 3: 把 `_schedule_memory_extraction` 改为流内尾部执行器**

```python
async def _yield_memory_extraction_tasks(...):
    gate_task_id = f"memory_extraction_gate:{session_id}:{int(time.time())}"
    yield json.dumps({"type": "internal_task", "task": InternalTask(..., status="pending")...})
    decision = await _decide_memory_extraction(...)
    yield json.dumps({"type": "internal_task", "task": InternalTask(..., status="success" if decision.should_extract else "skipped")...})
    if not decision.should_extract:
        return
    ...
```

- [ ] **Step 4: 在 chat SSE 中把 `done` 延后到 gate / extraction 结束之后**

```python
async for chunk in _run_agent_stream(...):
    yield chunk

if run_completed_successfully:
    async for internal_chunk in _yield_memory_extraction_tasks(...):
        yield internal_chunk

yield json.dumps({"type": "done"}, ensure_ascii=False)
```

- [ ] **Step 5: 删除 memory extraction 对 `_background_internal_tasks` 的依赖**

```python
for task in session.pop("_background_internal_tasks", []):
    if task.kind != "memory_extraction":
        yield ...
```

- [ ] **Step 6: 运行目标测试，确认从红变绿**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_integration.py -k 'gate_skips_in_same_stream or runs_before_done_in_same_stream or timeout_is_emitted_as_warning or success_when_auto_saved_items_written' -v`
Expected: PASS

### Task 3: 验证前端同轮收口与状态展示

**Files:**
- Modify: `frontend/src/components/ChatPanel.tsx`（仅在需要时）
- Test: 现有手工验证 + 前端类型检查

- [ ] **Step 1: 确认 `internal_task` 在 assistant 之后、`done` 之前到达时不会提前结束回合**

```ts
} else if (event.type === 'done') {
  state.completed = true
  ...
}
```

- [ ] **Step 2: 若需要，补一处防御性处理，避免尾部 internal task 因 assistant 已创建而被插入错误位置**

```ts
return insertBeforeAssistant(prev, state.currentAssistantId, {
  id: messageId,
  role: 'system',
  ...
})
```

- [ ] **Step 3: 运行前端检查**

Run: `cd frontend && npm test -- --runInBand`
Expected: PASS 或输出仓库当前已有失败信息。

### Task 4: 文档同步与最终验证

**Files:**
- Modify: `PROJECT_OVERVIEW.md`
- Modify: `docs/superpowers/plans/2026-04-20-memory-extraction-gate.md`

- [ ] **Step 1: 更新项目总览中的 Memory System 描述**

```markdown
- Memory System：`memory_recall` 仍在回答前同步检索；`memory_extraction_gate` 与按需 `memory_extraction` 在同一轮 SSE 尾部执行并即时展示，只有 gate 放行时才触发重提取。
```

- [ ] **Step 2: 运行最终回归**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_integration.py tests/test_api.py -k 'memory_extraction or memory_recall or same_turn_memory_extraction'`
Expected: PASS

- [ ] **Step 3: 记录任何未覆盖风险并准备交付说明**

```text
风险重点：gate 是否误判、超时是否仍拖慢当前轮、前端是否正确展示 skipped/success/warning。
```
