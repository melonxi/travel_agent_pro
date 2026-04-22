# Recall Reranker Phase D Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有统一 `RecallCandidate` 流水线上插入 Stage 4 reranker，只把最终相关的历史记忆注入 prompt，同时把 reranker 结果补齐到 SSE、trace、stats 和前端可视化。

**Architecture:** 新增 `memory.recall_reranker` 作为独立模块，负责 tool schema、payload 解析、短路条件和 fallback；`MemoryManager.generate_context()` 先拿到统一 `recall_candidates`，再产出 `selected_candidates` 和 reranker telemetry，最后把选中结果交给 formatter；`main.py`、trace/stats、前端 `ChatPanel`/`TraceViewer` 同步消费新增 telemetry 字段，确保 reranker 过程可观测。

**Tech Stack:** Python 3.12、FastAPI SSE、现有 LLM provider/tool schema、pytest + pytest-asyncio、React 19 + TypeScript。

---

## File Map

- Create: `backend/memory/recall_reranker.py`
  - `RecallRerankResult`
  - reranker tool schema / parser
  - short-circuit 与 fallback top N 逻辑
- Modify: `backend/memory/manager.py`
  - 在 `recall_candidates` 和 formatter 之间插入 reranker
  - 记录 `candidate_count` / `reranker_selected_ids` / `reranker_final_reason` / `reranker_fallback`
- Modify: `backend/memory/formatter.py`
  - 扩展 `MemoryRecallTelemetry`
  - 继续渲染统一 candidate，但只消费 `selected_candidates`
- Modify: `backend/main.py`
  - 扩展 `memory_recall` internal task、SSE payload、stats 映射
- Modify: `backend/telemetry/stats.py`
  - 为 reranker 字段提供 trace/stats 落盘 contract
- Modify: `backend/api/trace.py`
  - 扩展 trace 序列化，把 reranker telemetry 暴露给 Trace API
- Modify: `frontend/src/types/trace.ts`
  - 声明 `memory_recall` telemetry 字段，而不是把 reranker 塞进 `memory_hits`
- Modify: `frontend/src/types/plan.ts`
  - 扩展 `memory_recall` SSE event 字段定义，供 `ChatPanel` 消费
- Modify: `frontend/src/components/ChatPanel.tsx`
  - 在记忆召回系统任务卡显示候选数、最终选中数、fallback 摘要
- Modify: `frontend/src/components/TraceViewer.tsx`
  - 展示 `iteration.memory_recall` 中的 reranker 摘要
- Create: `backend/tests/test_recall_reranker.py`
  - schema / parser / short-circuit / fallback 单测
- Modify: `backend/tests/test_memory_manager.py`
  - manager 使用 reranker、短路、fallback 的单测
- Modify: `backend/tests/test_memory_formatter.py`
  - telemetry 字段序列化单测
- Modify: `backend/tests/test_memory_integration.py`
  - chat SSE 中 `memory_recall` 事件包含 reranker 字段
- Modify: `backend/tests/test_trace_api.py`
  - trace API 输出 reranker 字段
- Modify: `PROJECT_OVERVIEW.md`
  - 更新 Memory System 对 reranker 阶段和 telemetry 的描述

---

### Task 1: 锁定 reranker contract 与 fallback 规则

**Files:**
- Create: `backend/memory/recall_reranker.py`
- Create: `backend/tests/test_recall_reranker.py`

- [ ] **Step 1: 先写失败测试，锁定 reranker 解析结果 contract**

```python
from memory.recall_reranker import RecallRerankResult, parse_recall_reranker_arguments


def test_parse_recall_reranker_arguments_returns_selected_ids_and_reasons():
    result = parse_recall_reranker_arguments(
        {
            "selected_item_ids": ["profile_1", "slice_2"],
            "final_reason": "these two items directly answer the user's lodging question",
            "per_item_reason": {
                "profile_1": "long-term lodging preference still applies",
                "slice_2": "past Kyoto lodging experience is directly relevant",
            },
        }
    )

    assert result == RecallRerankResult(
        selected_item_ids=["profile_1", "slice_2"],
        final_reason="these two items directly answer the user's lodging question",
        per_item_reason={
            "profile_1": "long-term lodging preference still applies",
            "slice_2": "past Kyoto lodging experience is directly relevant",
        },
        fallback_used="none",
    )
```

- [ ] **Step 2: 运行解析测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_recall_reranker.py -k parse_recall_reranker_arguments -v`
Expected: FAIL，因为 `memory.recall_reranker` 模块与 parser 尚不存在。

- [ ] **Step 3: 再写失败测试，锁定小候选集短路与 fallback top N 行为**

```python
from memory.recall_reranker import choose_reranker_path
from memory.retrieval_candidates import RecallCandidate


def test_choose_reranker_path_skips_llm_when_candidate_count_is_small():
    candidates = [
        RecallCandidate(
            source="profile",
            item_id="profile_1",
            bucket="stable_preferences",
            score=1.0,
            matched_reason=["domain=hotel"],
            content_summary="hotel:preferred_area=京都四条",
            domains=["hotel"],
            applicability="适用于大多数住宿选择。",
        ),
        RecallCandidate(
            source="episode_slice",
            item_id="slice_1",
            bucket="accommodation_decision",
            score=0.5,
            matched_reason=["destination=京都"],
            content_summary="上次京都住四条附近的町屋。",
            domains=["hotel"],
            applicability="仅供住宿选择参考。",
        ),
    ]

    path = choose_reranker_path(candidates, rerank_threshold=3, fallback_top_n=3)

    assert path.should_call_llm is False
    assert [candidate.item_id for candidate in path.selected_candidates] == ["profile_1", "slice_1"]
    assert path.fallback_used == "skipped_small_candidate_set"
```

- [ ] **Step 4: 实现 `backend/memory/recall_reranker.py` 的最小闭环**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from memory.retrieval_candidates import RecallCandidate


@dataclass
class RecallRerankResult:
    selected_item_ids: list[str]
    final_reason: str
    per_item_reason: dict[str, str]
    fallback_used: str = "none"


@dataclass
class RecallRerankPath:
    should_call_llm: bool
    selected_candidates: list[RecallCandidate]
    fallback_used: str


def choose_reranker_path(
    candidates: list[RecallCandidate], rerank_threshold: int = 3, fallback_top_n: int = 3
) -> RecallRerankPath:
    if len(candidates) <= rerank_threshold:
        return RecallRerankPath(
            should_call_llm=False,
            selected_candidates=candidates,
            fallback_used="skipped_small_candidate_set",
        )
    return RecallRerankPath(
        should_call_llm=True,
        selected_candidates=candidates[:fallback_top_n],
        fallback_used="none",
    )


def parse_recall_reranker_arguments(payload: dict[str, Any] | None) -> RecallRerankResult:
    if not isinstance(payload, dict):
        return RecallRerankResult([], "invalid_reranker_payload", {}, "invalid_reranker_payload")
    selected_item_ids = payload.get("selected_item_ids")
    final_reason = payload.get("final_reason")
    per_item_reason = payload.get("per_item_reason")
    if (
        isinstance(selected_item_ids, list)
        and all(isinstance(item, str) for item in selected_item_ids)
        and isinstance(final_reason, str)
        and isinstance(per_item_reason, dict)
        and all(isinstance(key, str) and isinstance(value, str) for key, value in per_item_reason.items())
    ):
        return RecallRerankResult(selected_item_ids, final_reason, per_item_reason)
    return RecallRerankResult([], "invalid_reranker_payload", {}, "invalid_reranker_payload")
```

- [ ] **Step 5: 运行 reranker 单测并确认通过**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_recall_reranker.py -v`
Expected: PASS，覆盖 parser、短路、fallback top N。

- [ ] **Step 6: 提交这一小步**

```bash
git add backend/memory/recall_reranker.py backend/tests/test_recall_reranker.py
git commit -m "feat: add recall reranker contract"
```

### Task 2: 把 reranker 插入 MemoryManager 主链路

**Files:**
- Modify: `backend/memory/manager.py`
- Modify: `backend/tests/test_memory_manager.py`

- [ ] **Step 1: 先写失败测试，锁定 manager 只格式化选中 candidates**

```python
@pytest.mark.asyncio
async def test_generate_context_formats_selected_candidates_only(tmp_path, monkeypatch):
    manager = MemoryManager(data_dir=str(tmp_path))

    selected_ids = []

    def fake_select_candidates(*args, **kwargs):
        candidates = kwargs["candidates"]
        selected_ids.extend(candidate.item_id for candidate in candidates[:1])
        return candidates[:1], "selected_by_test", "none"

    monkeypatch.setattr("memory.manager.select_recall_candidates", fake_select_candidates)

    text, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_now"),
        user_message="我上次去京都住哪里？",
    )

    assert selected_ids
    assert recall.candidate_count >= len(selected_ids)
    assert recall.reranker_selected_ids == selected_ids
```

- [ ] **Step 2: 运行 manager 定向测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_manager.py -k selected_candidates_only -v`
Expected: FAIL，因为 `generate_context()` 还没有 reranker 插入点和 telemetry 字段。

- [ ] **Step 3: 在 `manager.py` 中插入 reranker 选择点**

```python
selected_candidates = recall_candidates
rerank_result = RecallRerankResult(
    selected_item_ids=[],
    final_reason="not_needed",
    per_item_reason={},
    fallback_used="none",
)
if recall_candidates:
    selected_candidates, rerank_result = await select_recall_candidates(
        user_message=user_message,
        plan=plan,
        retrieval_plan=retrieval_plan,
        candidates=recall_candidates,
    )

telemetry.candidate_count = len(recall_candidates)
telemetry.reranker_selected_ids = [candidate.item_id for candidate in selected_candidates]
telemetry.reranker_final_reason = rerank_result.final_reason
telemetry.reranker_fallback = rerank_result.fallback_used

context = format_v3_memory_context(
    profile_items=fixed_profile_items,
    working_items=working_items,
    recall_candidates=selected_candidates,
)
```

- [ ] **Step 4: 补充 manager 的 fallback 与零候选测试**

```python
@pytest.mark.asyncio
async def test_generate_context_keeps_empty_reranker_fields_when_no_candidates(tmp_path):
    manager = MemoryManager(data_dir=str(tmp_path))

    _, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_now"),
        user_message="这次预算多少？",
    )

    assert recall.candidate_count == 0
    assert recall.reranker_selected_ids == []
    assert recall.reranker_final_reason == ""
```

- [ ] **Step 5: 运行 manager 测试并确认通过**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_manager.py -k "selected_candidates_only or reranker_fields" -v`
Expected: PASS，manager 能记录 candidate_count、selected ids 和 fallback。

- [ ] **Step 6: 提交这一小步**

```bash
git add backend/memory/manager.py backend/tests/test_memory_manager.py
git commit -m "feat: insert reranker into memory recall flow"
```

### Task 3: 扩展 formatter、SSE、trace/stats 的 telemetry contract

**Files:**
- Modify: `backend/memory/formatter.py`
- Modify: `backend/main.py`
- Modify: `backend/telemetry/stats.py`
- Modify: `backend/api/trace.py`
- Modify: `backend/tests/test_memory_formatter.py`
- Modify: `backend/tests/test_memory_integration.py`
- Modify: `backend/tests/test_trace_api.py`

- [ ] **Step 1: 先写失败测试，锁定 telemetry 序列化字段**

```python
def test_memory_recall_telemetry_to_dict_includes_reranker_fields():
    telemetry = MemoryRecallTelemetry(
        candidate_count=4,
        reranker_selected_ids=["profile_1", "slice_2"],
        reranker_final_reason="two items directly answer the user's question",
        reranker_fallback="none",
    )

    payload = telemetry.to_dict()

    assert payload["candidate_count"] == 4
    assert payload["reranker_selected_ids"] == ["profile_1", "slice_2"]
    assert payload["reranker_final_reason"] == "two items directly answer the user's question"
    assert payload["reranker_fallback"] == "none"
```

- [ ] **Step 2: 修改 `MemoryRecallTelemetry`、`main.py`、stats record 与 trace 序列化**

```python
@dataclass
class MemoryRecallTelemetry:
    candidate_count: int = 0
    reranker_selected_ids: list[str] = field(default_factory=list)
    reranker_final_reason: str = ""
    reranker_fallback: str = "none"
```

```python
result={
    "item_ids": recalled_ids,
    "count": len(recalled_ids),
    "sources": dict(memory_recall.sources),
    "candidate_count": memory_recall.candidate_count,
    "reranker_selected_ids": list(memory_recall.reranker_selected_ids),
    "reranker_final_reason": memory_recall.reranker_final_reason,
    "reranker_fallback": memory_recall.reranker_fallback,
}
```

```python
@dataclass
class RecallTelemetryRecord:
    stage0_decision: str = "undecided"
    stage0_reason: str = ""
    gate_needs_recall: bool | None = None
    gate_intent_type: str = ""
    final_recall_decision: str = ""
    fallback_used: str = "none"
    candidate_count: int = 0
    reranker_selected_ids: list[str] = field(default_factory=list)
    reranker_final_reason: str = ""
    reranker_fallback: str = "none"
```

```python
def _recall_telemetry_record_from_recall(
    memory_recall: MemoryRecallTelemetry,
):
    return RecallTelemetryRecord(
        stage0_decision=memory_recall.stage0_decision,
        stage0_reason=memory_recall.stage0_reason,
        gate_needs_recall=memory_recall.gate_needs_recall,
        gate_intent_type=memory_recall.gate_intent_type,
        final_recall_decision=memory_recall.final_recall_decision,
        fallback_used=memory_recall.fallback_used,
        candidate_count=memory_recall.candidate_count,
        reranker_selected_ids=list(memory_recall.reranker_selected_ids),
        reranker_final_reason=memory_recall.reranker_final_reason,
        reranker_fallback=memory_recall.reranker_fallback,
    )
```

```python
def _serialize_recall_telemetry(hit: RecallTelemetryRecord) -> dict:
    return {
        "stage0_decision": hit.stage0_decision,
        "stage0_reason": hit.stage0_reason,
        "gate_needs_recall": hit.gate_needs_recall,
        "gate_intent_type": hit.gate_intent_type,
        "final_recall_decision": hit.final_recall_decision,
        "fallback_used": hit.fallback_used,
        "candidate_count": hit.candidate_count,
        "reranker_selected_ids": list(hit.reranker_selected_ids),
        "reranker_final_reason": hit.reranker_final_reason,
        "reranker_fallback": hit.reranker_fallback,
    }
```

- [ ] **Step 3: 写 SSE / trace 集成测试，锁定 API 输出**

```python
assert '"candidate_count": 4' in resp.text
assert '"reranker_selected_ids": ["profile_1", "slice_2"]' in resp.text
assert '"reranker_final_reason": "two items directly answer the user\\'s question"' in resp.text
assert '"reranker_fallback": "none"' in resp.text
assert data["iterations"][0]["memory_recall"]["candidate_count"] == 4
assert data["iterations"][0]["memory_recall"]["reranker_selected_ids"] == ["profile_1", "slice_2"]
```

- [ ] **Step 4: 运行 telemetry 相关测试并确认通过**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_formatter.py tests/test_memory_integration.py tests/test_trace_api.py -v`
Expected: PASS，`memory_recall` payload、trace API、stats 都包含 reranker 字段。

- [ ] **Step 5: 提交这一小步**

```bash
git add backend/memory/formatter.py backend/main.py backend/telemetry/stats.py backend/api/trace.py backend/tests/test_memory_formatter.py backend/tests/test_memory_integration.py backend/tests/test_trace_api.py
git commit -m "feat: expose reranker recall telemetry"
```

### Task 4: 前端展示 reranker 结果并更新项目总览

**Files:**
- Modify: `frontend/src/types/trace.ts`
- Modify: `frontend/src/types/plan.ts`
- Modify: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/components/TraceViewer.tsx`
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: 先写前端类型与渲染变更**

```ts
export interface MemoryRecallTelemetry {
  stage0_decision?: string
  stage0_reason?: string
  gate_needs_recall?: boolean | null
  gate_intent_type?: string
  final_recall_decision?: string
  fallback_used?: string
  candidate_count?: number
  reranker_selected_ids?: string[]
  reranker_final_reason?: string
  reranker_fallback?: string
}

export interface TraceIteration {
  ...
  memory_hits: MemoryHit | null
  memory_recall?: MemoryRecallTelemetry | null
}
```

```ts
interface BaseSSEEvent {
  ...
  candidate_count?: number
  reranker_selected_ids?: string[]
  reranker_final_reason?: string
  reranker_fallback?: string
}
```

```tsx
const rerankerCount = event.reranker_selected_ids?.length ?? 0
const candidateCount = event.candidate_count ?? recallCount
const rerankerSuffix = candidateCount > 0
  ? `候选 ${candidateCount} 条，最终保留 ${rerankerCount} 条`
  : '未找到本轮可用记忆'

const rerankerReason = event.reranker_final_reason
```

- [ ] **Step 2: 在 TraceViewer 中展示 reranker 摘要**

```tsx
{iteration.memory_recall && (
  <div className="trace-memory-recall">
    recall: {iteration.memory_recall.final_recall_decision ?? 'unknown'}
    （候选 {iteration.memory_recall.candidate_count ?? 0} / 最终 {iteration.memory_recall.reranker_selected_ids?.length ?? 0} / fallback {iteration.memory_recall.reranker_fallback ?? 'none'}）
  </div>
)}
```

- [ ] **Step 3: 更新 `PROJECT_OVERVIEW.md` 的 Memory System 描述**

```md
- Memory System：同步 recall 采用 `Stage 0` 硬规则短路 + `Stage 1` recall gate + `Stage 2` retrieval plan + `Stage 3` 统一 `RecallCandidate` 召回；当候选数超过短路阈值时进入 `Stage 4` reranker 做最终筛选，formatter、SSE、trace/stats 统一消费 reranker 选中结果与 fallback telemetry。
```

- [ ] **Step 4: 运行前端和文档相关检查命令**

Run: `cd frontend && npm run build`
Expected: PASS，确保 `types/plan.ts`、`types/trace.ts`、`ChatPanel.tsx`、`TraceViewer.tsx` 的类型与构建链路全部通过。

- [ ] **Step 5: 提交这一小步**

```bash
git add frontend/src/types/trace.ts frontend/src/types/plan.ts frontend/src/components/ChatPanel.tsx frontend/src/components/TraceViewer.tsx PROJECT_OVERVIEW.md
git commit -m "feat: show reranker results in recall telemetry"
```

---

## Self-Review

- 已覆盖 spec 中“统一 candidate 后再插入 reranker”的前置条件：Task 1、Task 2。
- 已覆盖 spec 中“Stage 5 改为消费 selected candidates”与“reranker fallback top N”要求：Task 1、Task 2。
- 已覆盖 spec 中“补齐 SSE / trace / stats / 前端可观测性”要求：Task 3、Task 4。
- 已显式区分三条观测链路：
  - `memory_hits` 继续表示真实命中记忆项
  - chat SSE `memory_recall` 事件承载完整 reranker telemetry
  - Trace API 通过 `iteration.memory_recall` 展示 recall/reranker 摘要
- 本计划未触碰 `EpisodeSlice` retrieval plan 统一化，也未重写 Stage 3 规则召回逻辑，符合最新 spec 的边界约束。

Plan complete and saved to `docs/superpowers/plans/2026-04-22-recall-reranker-phase-d.md`.

Two execution options:

**1. Subagent-Driven (recommended)** - 我按任务分发独立子代理执行，并在任务间做检查

**2. Inline Execution** - 我在当前会话里按计划顺序直接执行

Which approach?
