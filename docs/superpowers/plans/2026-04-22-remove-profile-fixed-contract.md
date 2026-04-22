# Remove Fixed Profile Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 recall-first 之后遗留的 `fixed profile` 空链路和 `profile_fixed` 对外 contract，同时保持现有记忆召回主路径行为不变。

**Architecture:** 这次改动只做 contract 收口，不重做 recall 架构。后端删除 `fixed_profile_items` 与 `sources.profile_fixed`，formatter 只渲染 working memory 和最终命中的 recall candidates；前端、trace、stats、测试和文档同步对齐到新的来源语义。

**Tech Stack:** Python 3.12, FastAPI backend, React + TypeScript frontend, pytest, existing memory v3 manager/formatter/stats pipeline.

---

### Task 1: 删除后端 fixed profile 空链路

**Files:**
- Modify: `backend/memory/manager.py`
- Modify: `backend/memory/formatter.py`
- Test: `backend/tests/test_memory_manager.py`
- Test: `backend/tests/test_memory_formatter.py`

- [ ] **Step 1: 写失败测试，锁定 formatter 不再接受 fixed profile 输入**

```python
def test_format_v3_memory_context_renders_only_working_and_recall_sections():
    from memory.formatter import format_v3_memory_context

    text = format_v3_memory_context(
        working_items=[make_working_memory_item()],
        recall_candidates=[
            RecallCandidate(
                source="episode_slice",
                item_id="slice-1",
                bucket="accommodation_decision",
                score=1.0,
                matched_reason=["exact destination match on 京都"],
                content_summary="上次京都选择町屋。",
                domains=["hotel"],
                applicability="仅供住宿偏好参考。",
            )
        ],
    )

    assert "## 长期用户画像" not in text
    assert "## 当前会话工作记忆" in text
    assert "## 本轮请求命中的历史记忆" in text
```

- [ ] **Step 2: 运行单测确认当前会失败**

Run: `cd backend && pytest tests/test_memory_formatter.py -k "renders_only_working_and_recall_sections" -v`
Expected: FAIL，因为 `format_v3_memory_context()` 当前仍要求 `profile_items` 参数。

- [ ] **Step 3: 修改 formatter，删除 fixed profile 渲染入口**

```python
def format_v3_memory_context(
    working_items: list[WorkingMemoryItem],
    recall_candidates: list[RecallCandidate],
) -> str:
    sections: list[str] = []

    if working_items:
        lines = ["## 当前会话工作记忆"]
        for item in working_items:
            lines.append(_format_v3_working_memory_item(item))
        sections.append("\n".join(lines))

    history_lines = [_format_recall_candidate(candidate) for candidate in recall_candidates]
    if history_lines:
        sections.append("\n".join(["## 本轮请求命中的历史记忆", *history_lines]))

    return "\n\n".join(sections) if sections else "暂无相关用户记忆"
```

- [ ] **Step 4: 修改 manager，删除 `fixed_profile_items` 透传与聚合**

```python
async def generate_context(...):
    profile = await self.v3_store.load_profile(user_id)
    working_memory = await self.v3_store.load_working_memory(...)
    working_items = self._active_working_memory_items(working_memory.items)
    ...
    telemetry = self._build_v3_telemetry(
        working_items,
        selected_candidates,
    )
    ...
    context = format_v3_memory_context(
        working_items=working_items,
        recall_candidates=selected_candidates,
    )
    return context, telemetry


def _build_v3_telemetry(
    self,
    working_items: list[WorkingMemoryItem],
    recall_candidates: list[RecallCandidate],
) -> MemoryRecallTelemetry:
    query_profile_ids = self._dedupe_ids(
        [candidate.item_id for candidate in recall_candidates if candidate.source == "profile"]
    )
    ...
    return MemoryRecallTelemetry(
        sources={
            "query_profile": len(query_profile_ids),
            "working_memory": len(working_memory_ids),
            "episode_slice": len(slice_ids),
        },
        profile_ids=query_profile_ids,
        ...,
    )
```

- [ ] **Step 5: 更新 manager / formatter 测试断言**

```python
assert "## 长期用户画像" not in text
assert "profile_fixed" not in recall.sources
assert recall.sources["query_profile"] == 0
assert recall.profile_ids == []
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && pytest tests/test_memory_manager.py tests/test_memory_formatter.py -v`
Expected: PASS，且不再有任何 `profile_fixed` 相关断言。

- [ ] **Step 7: 提交本任务**

```bash
git add backend/memory/manager.py backend/memory/formatter.py backend/tests/test_memory_manager.py backend/tests/test_memory_formatter.py
git commit -m "refactor: remove fixed profile memory context path"
```

### Task 2: 收口 telemetry、trace 和 stats contract

**Files:**
- Modify: `backend/memory/formatter.py`
- Modify: `backend/main.py`
- Modify: `backend/api/trace.py`
- Modify: `backend/telemetry/stats.py`
- Test: `backend/tests/test_trace_api.py`
- Test: `backend/tests/test_stats.py`
- Test: `backend/tests/test_memory_v3_api.py`
- Test: `backend/tests/test_memory_integration.py`

- [ ] **Step 1: 写失败测试，锁定 `sources` 中不再包含 `profile_fixed`**

```python
def test_memory_recall_telemetry_to_dict_omits_profile_fixed_source():
    from memory.formatter import MemoryRecallTelemetry

    telemetry = MemoryRecallTelemetry(
        sources={"query_profile": 1, "working_memory": 1, "episode_slice": 1},
        profile_ids=["profile-1"],
    )

    payload = telemetry.to_dict()
    assert "profile_fixed" not in payload["sources"]
    assert payload["sources"] == {
        "query_profile": 1,
        "working_memory": 1,
        "episode_slice": 1,
    }
```

- [ ] **Step 2: 运行单测确认当前会失败**

Run: `cd backend && pytest tests/test_memory_formatter.py -k "omits_profile_fixed_source" -v`
Expected: FAIL，因为默认 telemetry sources 仍包含 `profile_fixed`。

- [ ] **Step 3: 修改 telemetry 默认结构和序列化逻辑**

```python
@dataclass
class MemoryRecallTelemetry:
    sources: dict[str, int] = field(
        default_factory=lambda: {
            "query_profile": 0,
            "working_memory": 0,
            "episode_slice": 0,
        }
    )
```

- [ ] **Step 4: 修改 trace / stats / API 测试样例数据**

```python
MemoryHitRecord(
    sources={"query_profile": 1, "working_memory": 1, "episode_slice": 1},
    profile_ids=["m1"],
    working_memory_ids=["m2"],
    slice_ids=["slice-1"],
)
...
assert hits["sources"]["query_profile"] == 1
assert "profile_fixed" not in hits["sources"]
```

- [ ] **Step 5: 收口 integration / trace 中旧的 recall 语义断言**

```python
assert recall["final_recall_decision"] != "fixed_only"
assert "profile_fixed" not in recall.get("sources", {})
```

说明：如果某条测试只是在表达“零命中但 recall telemetry 仍可见”，就把断言改成当前真实语义，例如 `no_recall_applied` 或 `query_recall_enabled`，不要继续保留 `fixed_only` 这个过时状态。

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && pytest tests/test_memory_formatter.py tests/test_stats.py tests/test_trace_api.py tests/test_memory_v3_api.py tests/test_memory_integration.py -v`
Expected: PASS，所有序列化 payload 均不再包含 `profile_fixed`。

- [ ] **Step 7: 提交本任务**

```bash
git add backend/memory/formatter.py backend/main.py backend/api/trace.py backend/telemetry/stats.py backend/tests/test_trace_api.py backend/tests/test_stats.py backend/tests/test_memory_v3_api.py backend/tests/test_memory_integration.py
git commit -m "refactor: drop profile_fixed recall telemetry"
```

### Task 3: 收口前端类型与 recall 来源展示

**Files:**
- Modify: `frontend/src/types/trace.ts`
- Modify: `frontend/src/types/plan.ts`
- Modify: `frontend/src/components/TraceViewer.tsx`
- Modify: `frontend/src/components/ChatPanel.tsx`

- [ ] **Step 1: 更新前端类型，移除 `profile_fixed`**

```ts
export interface MemoryHitRecord {
  profile_ids: string[]
  working_memory_ids: string[]
  slice_ids: string[]
  matched_reasons: string[]
  sources?: {
    query_profile?: number
    working_memory?: number
    episode_slice?: number
  }
}
```

- [ ] **Step 2: 修改 `TraceViewer` 的来源文案**

```tsx
{iteration.memory_hits && (
  <div className="trace-memory-hits">
    命中 {memoryHitCount} 条记忆
    （query {memorySources.query_profile ?? 0} / working {memorySources.working_memory ?? 0} / slice {memorySources.episode_slice ?? 0}）
  </div>
)}
```

- [ ] **Step 3: 保持 `ChatPanel` 的 recalled item 计数逻辑，但不再隐含 fixed profile 语义**

```ts
function mergeRecalledIds(event: SSEEvent): string[] {
  const merged = [
    ...(event.profile_ids ?? []),
    ...(event.working_memory_ids ?? []),
    ...(event.slice_ids ?? []),
  ]
  ...
}
```

说明：这里不删除 `profile_ids`，只是确保它被理解为“命中的 profile recall ids”，而不是 fixed profile ids。

- [ ] **Step 4: 如有前端类型错误或测试，更新预期**

```ts
// 所有读取 memorySources.profile_fixed 的地方都改为删除，不做 fallback 保留
```

- [ ] **Step 5: 进行最小静态核对**

Run: `grep -R "profile_fixed" frontend/src backend | cat`
Expected: 不再出现前端运行时代码对 `profile_fixed` 的读取；如果只剩文档或 plan/spec 文件，属于预期。

- [ ] **Step 6: 提交本任务**

```bash
git add frontend/src/types/trace.ts frontend/src/types/plan.ts frontend/src/components/TraceViewer.tsx frontend/src/components/ChatPanel.tsx
git commit -m "refactor: align frontend with recall source cleanup"
```

### Task 4: 同步文档与项目总览

**Files:**
- Modify: `PROJECT_OVERVIEW.md`
- Modify: `docs/TODO.md`

- [ ] **Step 1: 更新 `PROJECT_OVERVIEW.md` 中 memory recall 描述**

```md
Memory System | ... 当前有效 recall 来源为 `query_profile`、`working_memory`、`episode_slice`；`profile_fixed` 固定画像注入链路已移除；`profile_ids` 表示本轮最终命中的 profile recall item ids ...
```

- [ ] **Step 2: 更新 memory recall SSE / trace 字段说明**

```md
| `memory_recall` | ... payload 包含 `sources`、`profile_ids`、`working_memory_ids`、`slice_ids` ... 其中 `sources` 只包含 `query_profile`、`working_memory`、`episode_slice` |
```

- [ ] **Step 3: 更新 `docs/TODO.md`，删除已经完成的 `profile_fixed` 清理待办**

```md
- [x] 移除 `profile_fixed` 空链路与前端/trace/stats 残留语义
```

- [ ] **Step 4: 进行文档一致性检查**

Run: `grep -R "profile_fixed" PROJECT_OVERVIEW.md docs/TODO.md`
Expected: 不再有把 `profile_fixed` 描述为运行时有效来源的内容。

- [ ] **Step 5: 提交本任务**

```bash
git add PROJECT_OVERVIEW.md docs/TODO.md
git commit -m "docs: remove profile_fixed recall contract references"
```

### Task 5: 全局残留扫描与最终验证

**Files:**
- Modify: `backend/...` 按扫描结果补漏
- Modify: `frontend/...` 按扫描结果补漏
- Modify: `PROJECT_OVERVIEW.md` 如扫描发现遗漏则补充

- [ ] **Step 1: 全局扫描残留引用**

Run: `rg "profile_fixed|fixed_profile_items|## 长期用户画像|fixed_only" backend frontend PROJECT_OVERVIEW.md docs/TODO.md`
Expected: 运行时代码与测试里不再出现这些旧语义；spec/plan 文档内出现属于预期。

- [ ] **Step 2: 如扫描命中运行时代码或测试，做最小补漏修改**

```python
# 只改命中的残留引用，不顺手做 unrelated cleanup
```

- [ ] **Step 3: 运行最终后端验证集**

Run: `cd backend && pytest tests/test_memory_manager.py tests/test_memory_formatter.py tests/test_stats.py tests/test_trace_api.py tests/test_memory_v3_api.py tests/test_memory_integration.py -v`
Expected: PASS，相关 contract 全部收口到新语义。

- [ ] **Step 4: 检查 git diff，确认只包含本次 contract 清理**

Run: `git diff -- backend frontend PROJECT_OVERVIEW.md docs/TODO.md`
Expected: 只包含 fixed profile contract 清理相关修改，没有无关改动。

- [ ] **Step 5: 最终提交**

```bash
git add backend frontend PROJECT_OVERVIEW.md docs/TODO.md
git commit -m "refactor: remove fixed profile recall contract"
```
