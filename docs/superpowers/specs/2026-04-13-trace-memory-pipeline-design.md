# Trace & Memory 数据管道补全设计

> **日期**：2026-04-13
> **目标**：补全 P1 评估中识别的 7 个缺口，让 TraceViewer 和 MemoryCenter 的数据通道完全打通。

---

## 1. 问题总结

| # | 缺口 | 根因 |
|---|------|------|
| 1 | TraceViewer state_changes 始终为空 | `ToolCallRecord` 不记录字段变更，`build_trace` 硬编码 `[]` |
| 2 | Memory Hit 全链路未实现 | `generate_context` 返回纯文本，丢弃了命中的 MemoryItem ID |
| 3 | Validator/Judge 结果未接入 Trace | 钩子执行了但结果未写入 Stats |
| 4 | Compression Event 未打通 | `session["compression_events"]` 有数据但 `build_trace` 不读取 |
| 5 | 工具并行/顺序标记缺失 | `loop.py` 知道并行关系但不记录到 `ToolCallRecord` |
| 6 | MemoryCenter "本轮命中" 未实现 | 没有 SSE 事件透传命中记忆 ID |
| 7 | 住宿 schema price vs price_per_night | `_REQUIRED_RESULT_FIELDS` 只认 `price` |

**核心根因**：SessionStats 只记录 "谁调用了什么"，不记录 "发生了什么变化"。

---

## 2. 设计原则

- **"丰富 Stats 层，Trace 层只做读取"**：记录点离数据产生点最近，准确度最高
- `build_trace` 保持为纯数据组装函数，不引入业务逻辑
- 前端类型已预留 `state_changes` / `compression_event` 字段，最小化前端改动
- 所有新增字段使用 `| None` 默认值，保持向后兼容

---

## 3. 后端 Stats 层扩展

### 3.1 `ToolCallRecord` 新增字段（`backend/telemetry/stats.py`）

```python
@dataclass
class ToolCallRecord:
    tool_name: str
    duration_ms: float
    status: str
    error_code: str | None = None
    phase: int | None = None
    timestamp: float = 0.0
    # --- 新增 ---
    state_changes: list[dict] | None = None      # [{field, before, after}]
    parallel_group: int | None = None             # 同组工具并行执行
    validation_errors: list[str] | None = None    # 验证钩子产生的错误
    judge_scores: dict | None = None              # {pace, geography, coherence, personalization}
```

### 3.2 新增 `MemoryHitRecord`（`backend/telemetry/stats.py`）

```python
@dataclass
class MemoryHitRecord:
    item_ids: list[str]
    core_count: int
    trip_count: int
    phase_count: int
    timestamp: float = 0.0
```

### 3.3 `SessionStats` 新增字段

```python
@dataclass
class SessionStats:
    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    # --- 新增 ---
    memory_hits: list[MemoryHitRecord] = field(default_factory=list)
```

注意：`compression_events` 已存在于 `session` 字典中（`main.py:1127`），不在 Stats 中重复存储，由 `build_trace` 直接从 session 字典读取。

### 3.4 `to_dict` 扩展

`SessionStats.to_dict()` 需要序列化新增的 `memory_hits` 列表。`ToolCallRecord` 的新字段在 `to_dict` 的 `by_tool` 聚合中可忽略（它们只被 trace 消费）。

---

## 4. 数据采集点改动

### 4.1 state_changes — `main.py` 的 `on_validate` 钩子

**当前**：`on_validate` 在 `update_plan_state` 后调用 `validate_incremental`，但不记录字段变更。

**改动**：在 `on_validate` 中，用 `update_plan_state` 返回的 `field` 和 `value` 构建 state_change 记录，写入最近一条 `ToolCallRecord`。

```python
async def on_validate(**kwargs):
    if kwargs.get("tool_name") == "update_plan_state":
        field = arguments.get("field", "")
        value = arguments.get("value")
        
        # 获取旧值（在工具执行前需要快照）
        old_value = _get_plan_field(plan_before_tool, field)
        
        # 写入最近的 ToolCallRecord
        if stats.tool_calls:
            stats.tool_calls[-1].state_changes = [
                {"field": field, "before": old_value, "after": value}
            ]
        
        # 验证逻辑不变...
        errors = validate_incremental(plan, field, value)
        if errors and stats.tool_calls:
            stats.tool_calls[-1].validation_errors = errors
```

**旧值快照**：需要在工具执行前保存 plan 的当前字段值。方案：在 `on_validate` 钩子触发时，`kwargs` 中已有 `arguments`，可以从 `plan` 对象上用 `getattr(plan, field, None)` 获取当前值（此时工具已执行完，plan 已更新）。但我们需要旧值。

具体做法：在 `_record_tool_result_stats` 调用之前（即工具执行前），通过 `before_tool_call` 钩子或在 `on_validate` 中利用工具返回的 `result.data` 中的旧值信息来获取。`update_plan_state` 工具（`backend/tools/update_plan_state.py`）在执行时可以返回旧值。

**推荐实现**：修改 `update_plan_state` 工具，在返回的 `result.data` 中包含 `previous_value` 字段。这是最干净的方式，因为工具本身就是修改的执行者。

### 4.2 parallel_group — `loop.py` 的并行批处理

**当前**：`loop.py:192-248` 扫描连续 read 工具组成 `read_batch` 并行执行，但不标记。

**改动**：在 AgentLoop 上维护一个递增计数器 `_parallel_group_counter`。每组并行工具共享同一个 group ID。

```python
# loop.py
self._parallel_group_counter = 0

# 并行执行时
self._parallel_group_counter += 1
group_id = self._parallel_group_counter
for tc in read_batch:
    # 执行后记录
    record.parallel_group = group_id

# 顺序执行时
record.parallel_group = None  # 或 0 表示单独执行
```

**记录方式**：`loop.py` 目前不直接操作 `SessionStats`，stats 记录在 `main.py` 的 `_record_tool_result_stats` 中完成。因此需要在 `ToolResult` 或 yield 的 `LLMChunk` 中透传 `parallel_group` 信息。

具体做法：`LLMChunk`（`TOOL_RESULT` 类型）的 `metadata` 字段中携带 `parallel_group`，`main.py` 的 `_record_tool_result_stats` 读取后写入 `ToolCallRecord`。

### 4.3 judge_scores — `main.py` 的 `on_soft_judge` 钩子

**当前**：`on_soft_judge`（`main.py:525-553`）调用 judge，但结果只注入到消息中。

**改动**：将评分写入最近触发 judge 的 `ToolCallRecord`。

```python
async def on_soft_judge(**kwargs):
    tool_name = kwargs.get("tool_name")
    if tool_name in ("assemble_day_plan", "generate_summary"):
        # ...现有 judge 调用逻辑...
        scores = parse_judge_response(raw)
        
        # 新增：写入 stats
        if stats.tool_calls:
            stats.tool_calls[-1].judge_scores = {
                "pace": scores.pace,
                "geography": scores.geography,
                "coherence": scores.coherence,
                "personalization": scores.personalization,
            }
```

### 4.4 memory_hits — `main.py` chat 入口 + `loop.py` 阶段切换

**改动 `MemoryManager.generate_context`**：

```python
async def generate_context(self, user_id: str, plan: TravelPlanState) -> tuple[str, list[str]]:
    items = await self.store.list_items(user_id)
    retrieved = RetrievedMemory(
        core=self.retriever.retrieve_core_profile(items),
        trip=self.retriever.retrieve_trip_memory(items, plan),
        phase=self.retriever.retrieve_phase_relevant(items, plan, plan.phase),
    )
    item_ids = [it.id for it in retrieved.core + retrieved.trip + retrieved.phase]
    return format_memory_context(retrieved), item_ids
```

**调用方 `main.py`**：

```python
memory_text, recalled_ids = await memory_mgr.generate_context(user_id, plan)

# 记录到 stats
stats.memory_hits.append(MemoryHitRecord(
    item_ids=recalled_ids,
    core_count=..., trip_count=..., phase_count=...,
    timestamp=time.time(),
))
```

同时需要修改 `loop.py` 中 `_rebuild_messages_for_phase_change` 的调用点。

### 4.5 compression_events — 无需采集改动

数据已存在于 `session["compression_events"]`，只需在 `build_trace` 中消费。

---

## 5. Trace API 层改动（`backend/api/trace.py`）

### 5.1 函数签名扩展

```python
def build_trace(stats: SessionStats, session_id: str,
                compression_events: list[dict] | None = None) -> dict:
```

### 5.2 iteration 构建逻辑

```python
# 对每个 iteration:
iteration_dict = {
    "index": idx,
    "phase": llm_rec.phase,
    "llm_call": {...},
    "tool_calls": [
        {
            **existing_fields,
            "parallel_group": tc.parallel_group,
            "validation_errors": tc.validation_errors,
            "judge_scores": tc.judge_scores,
        }
        for tc in matched_tool_calls
    ],
    # 从 update_plan_state 的 ToolCallRecord 聚合
    "state_changes": _collect_state_changes(matched_tool_calls),
    # 按时间戳匹配
    "compression_event": _match_compression_event(llm_rec, compression_events),
    # 按时间戳匹配
    "memory_hits": _match_memory_hits(llm_rec, stats.memory_hits),
}
```

### 5.3 辅助函数

```python
def _collect_state_changes(tool_calls: list[ToolCallRecord]) -> list[dict]:
    """从 update_plan_state 的 ToolCallRecord 中提取 state_changes"""
    changes = []
    for tc in tool_calls:
        if tc.state_changes:
            changes.extend(tc.state_changes)
    return changes

def _match_compression_event(llm_rec: LLMCallRecord, 
                              events: list[dict] | None) -> str | None:
    """按时间戳找到紧邻该 LLM 调用之前的压缩事件"""
    if not events:
        return None
    for evt in events:
        if evt["timestamp"] <= llm_rec.timestamp:
            return f"{evt['strategy']}: {evt.get('summary', '')}"
    return None

def _match_memory_hits(llm_rec: LLMCallRecord,
                        hits: list[MemoryHitRecord]) -> dict | None:
    """按时间戳找到该迭代关联的记忆命中"""
    if not hits:
        return None
    for hit in hits:
        if abs(hit.timestamp - llm_rec.timestamp) < 2.0:  # 2 秒容差
            return {
                "item_ids": hit.item_ids,
                "core": hit.core_count,
                "trip": hit.trip_count,
                "phase": hit.phase_count,
            }
    return None
```

### 5.4 API 端点改动（`main.py`）

```python
@app.get("/api/sessions/{session_id}/trace")
async def get_trace(session_id: str):
    session = sessions.get(session_id)
    trace = build_trace(
        session["stats"],
        session_id,
        compression_events=session.get("compression_events"),
    )
    return trace
```

---

## 6. 前端改动

### 6.1 类型扩展（`frontend/src/types/trace.ts`）

```typescript
export interface TraceToolCall {
  name: string
  duration_ms: number
  status: 'success' | 'error' | 'skipped'
  side_effect: 'read' | 'write'
  arguments_preview: string
  result_preview: string
  // --- 新增 ---
  parallel_group: number | null
  validation_errors: string[] | null
  judge_scores: Record<string, number> | null
}

export interface MemoryHit {
  item_ids: string[]
  core: number
  trip: number
  phase: number
}

export interface TraceIteration {
  index: number
  phase: number
  llm_call: { ... } | null
  tool_calls: TraceToolCall[]
  state_changes: StateChange[]
  compression_event: string | null
  // --- 新增 ---
  memory_hits: MemoryHit | null
}
```

### 6.2 TraceViewer 渲染扩展（`TraceViewer.tsx`）

| 新增渲染 | 组件位置 | 展示方式 |
|---------|---------|---------|
| state_changes | `StateDiffPanel`（已有，现在有数据了） | 绿色=新增（before 为 null），琥珀色=修改，显示 before→after |
| compression_event | `IterationRow` 头部 | 压缩图标 + 策略文字 |
| validation_errors | `ToolCallRow` 内部 | 红色警告条，列出每条错误 |
| judge_scores | `ToolCallRow` 内部 | 四个分数标签：节奏/地理/连贯/个性 各 1-5 |
| memory_hits | `IterationRow` 底部 | "命中 N 条记忆（core X / trip Y / phase Z）" 信息条 |
| parallel_group | `ToolCallRow` | 同组工具用 "⚡并行" 徽章，不同组的用竖线分隔 |

### 6.3 MemoryCenter "本轮命中"（`MemoryCenter.tsx`）

**SSE 新增事件类型**：`memory_recall`

```typescript
// ChatPanel.tsx 中处理
if (event.type === 'memory_recall') {
  setRecalledIds(event.data.item_ids)
}
```

**MemoryCenter 高亮逻辑**：在活跃 Tab 中，命中的记忆卡片加特殊左边框颜色 + "本轮命中" 徽章。不新增独立 Tab。

Props 传递：`ChatPanel` → `App` → `MemoryCenter`（通过 state lifting 或 context）。

---

## 7. 住宿 Schema 修复（`backend/harness/guardrail.py`）

将 `_REQUIRED_RESULT_FIELDS` 中 `search_accommodations` 的 `price` 改为 price 兼容逻辑：

```python
_REQUIRED_RESULT_FIELDS: dict[str, list[str]] = {
    "search_flights": ["price", "departure_time", "arrival_time", "airline"],
    "search_accommodations": ["name", "location"],  # price 单独兼容处理
    "search_trains": ["price", "departure_time", "arrival_time"],
}

_PRICE_ALIASES = {"price", "price_per_night"}  # 住宿价格字段别名
```

验证逻辑中，住宿的 price 检查改为：任一别名存在即通过。

---

## 8. 文件改动清单

| 文件 | 改动类型 | 改动内容 |
|------|---------|---------|
| `backend/telemetry/stats.py` | 扩展 | ToolCallRecord 新增 4 字段；新增 MemoryHitRecord；SessionStats 新增 memory_hits；to_dict 扩展 |
| `backend/api/trace.py` | 重构 | 消费新字段；接收 compression_events 参数；3 个辅助函数 |
| `backend/main.py` | 扩展 | on_validate 记录 state_changes + validation_errors；on_soft_judge 记录 judge_scores；chat 入口记录 memory_hits；trace 端点传入 compression_events；新增 memory_recall SSE 事件；update_plan_state 旧值快照 |
| `backend/agent/loop.py` | 扩展 | 并行组计数器；LLMChunk metadata 携带 parallel_group |
| `backend/memory/manager.py` | 扩展 | generate_context 返回 tuple[str, list[str]] |
| `backend/tools/update_plan_state.py` | 扩展 | 返回 previous_value |
| `backend/harness/guardrail.py` | 修复 | 住宿 price 别名兼容 |
| `frontend/src/types/trace.ts` | 扩展 | TraceToolCall 新增 3 字段；新增 MemoryHit；TraceIteration 新增 memory_hits |
| `frontend/src/components/TraceViewer.tsx` | 扩展 | 6 个新增渲染区域 |
| `frontend/src/components/MemoryCenter.tsx` | 扩展 | 命中记忆高亮 |
| `frontend/src/components/ChatPanel.tsx` | 扩展 | 处理 memory_recall SSE 事件 |
| `frontend/src/styles/trace-viewer.css` | 扩展 | 新增样式（validation/judge/parallel/memory/compression） |

**预估新增代码量**：~600 行（后端 ~350 行，前端 ~250 行）

---

## 9. 测试策略

| 测试范围 | 方式 |
|---------|------|
| ToolCallRecord 新字段序列化 | 单元测试（stats.py 已有测试文件） |
| build_trace 新数据组装 | 扩展 test_trace_api.py |
| state_changes 采集 | 扩展 test_realtime_validation_hook.py |
| memory_hits 记录 | 新增测试或扩展 memory 相关测试 |
| 住宿 schema 兼容 | 扩展 test_guardrail.py |
| generate_context 返回值 | 扩展 memory manager 测试 |
| parallel_group 透传 | 扩展 loop 相关测试 |

---

## 10. 不做的事情

- 不改 `SessionStats` 的持久化方式（仍为内存，不写数据库）
- 不新增 API 端点（复用现有 `/trace` 和 SSE）
- 不改 TraceViewer 的整体布局结构（只在现有组件内扩展）
- 不在 MemoryCenter 新增独立 Tab（在活跃 Tab 内高亮）
