# Recall Candidate Unification Phase C Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Stage 3 的 Profile / EpisodeSlice 召回结果统一成 `RecallCandidate` 输出 contract，同时保持现有检索与排序逻辑尽量不变，为后续 Reranker 提供统一输入。

**Architecture:** 新增 `memory.retrieval_candidates` 模块负责 candidate 数据结构和共享正规化逻辑；`symbolic_recall.py` 继续保留 source-specific 匹配与排序，但返回值改为 `RecallCandidate[]`；`manager.py`、`formatter.py`、trace/stats 提取逻辑统一消费 candidate，而不再分别处理 profile tuple / slice tuple 两套输出。

**Tech Stack:** Python 3.12、FastAPI SSE、async/await、pytest + pytest-asyncio、现有 symbolic recall 规则召回器。

---

## File Map

- Create: `backend/memory/retrieval_candidates.py`
  - `RecallCandidate`
  - `build_profile_candidates(...)`
  - `build_episode_slice_candidates(...)`
  - score / reason / summary 正规化逻辑
- Modify: `backend/memory/symbolic_recall.py`
  - `rank_profile_items()` 返回 `RecallCandidate[]`
  - `rank_episode_slices()` 返回 `RecallCandidate[]`
- Modify: `backend/memory/manager.py`
  - 改为消费统一 candidate
  - telemetry 从 candidate 提取 `profile_ids` / `slice_ids` / `matched_reasons`
- Modify: `backend/memory/formatter.py`
  - 接收统一 `recall_candidates`
  - 按 `source` 渲染 profile / slice 差异
- Create: `backend/tests/test_retrieval_candidates.py`
  - profile / slice -> candidate builder 单测
- Modify: `backend/tests/test_symbolic_recall.py`
  - symbolic recall 返回值改为 candidate 后的单测
- Modify: `backend/tests/test_memory_manager.py`
  - manager 合并 profile + slice candidates 的单测
- Modify: `backend/tests/test_memory_formatter.py`
  - formatter 接统一 candidate 的单测
- Modify: `backend/tests/test_memory_integration.py`
  - recall block 中 profile / slice 仍同时可见的集成测试
- Modify: `backend/tests/test_trace_api.py`
  - trace / stats 在统一 candidate 后仍能正确提取 ids / reasons
- Modify: `PROJECT_OVERVIEW.md`
  - 更新 Memory System 对统一 candidate 输出的描述

---

### Task 1: 锁定 RecallCandidate contract 与 builder 输出

**Files:**
- Create: `backend/memory/retrieval_candidates.py`
- Create: `backend/tests/test_retrieval_candidates.py`

- [ ] **Step 1: 先写失败测试，锁定 profile candidate 的字段语义**

```python
from memory.retrieval_candidates import RecallCandidate, build_profile_candidates
from memory.v3_models import MemoryProfileItem


def test_build_profile_candidates_normalizes_profile_tuple_output():
    item = MemoryProfileItem(
        id="constraints:flight:avoid_red_eye",
        domain="flight",
        key="avoid_red_eye",
        value=True,
        polarity="avoid",
        stability="explicit_declared",
        confidence=0.95,
        status="active",
        context={},
        applicability="适用于所有旅行。",
        recall_hints={"domains": ["flight"], "keywords": ["红眼航班"]},
        source_refs=[],
        created_at="2026-04-19T00:00:00",
        updated_at="2026-04-19T00:00:00",
    )

    candidates = build_profile_candidates([
        ("constraints", item, "exact domain match on flight; keyword match on 红眼航班; bucket=constraints")
    ])

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source == "profile"
    assert candidate.item_id == item.id
    assert candidate.bucket == "constraints"
    assert candidate.domains == ["flight"]
    assert candidate.applicability == "适用于所有旅行。"
    assert candidate.content_summary
    assert candidate.score > 0
    assert candidate.matched_reason
```

- [ ] **Step 2: 运行 builder 测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_retrieval_candidates.py -k profile_candidates -v`
Expected: FAIL，因为 `memory.retrieval_candidates` 文件和 builder 还不存在。

- [ ] **Step 3: 再写失败测试，锁定 episode slice candidate 的字段语义**

```python
from memory.retrieval_candidates import build_episode_slice_candidates
from memory.v3_models import EpisodeSlice


def test_build_episode_slice_candidates_normalizes_slice_tuple_output():
    slice_ = EpisodeSlice(
        id="slice_ep_kyoto_01",
        user_id="u1",
        source_episode_id="ep_kyoto",
        source_trip_id="trip_1",
        slice_type="accommodation_decision",
        domains=["hotel", "accommodation"],
        entities={"destination": "京都"},
        keywords=["住宿", "酒店"],
        content="上次京都住四条附近的町屋。",
        applicability="仅供住宿选择参考。",
        created_at="2026-04-19T00:00:00",
    )

    candidates = build_episode_slice_candidates([
        (slice_, "exact destination match on 京都; domain match on hotel")
    ])

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source == "episode_slice"
    assert candidate.item_id == slice_.id
    assert candidate.bucket == "accommodation_decision"
    assert candidate.domains == ["hotel", "accommodation"]
    assert candidate.applicability == "仅供住宿选择参考。"
    assert candidate.content_summary == "上次京都住四条附近的町屋。"
```

- [ ] **Step 4: 运行 slice builder 测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_retrieval_candidates.py -k slice_candidates -v`
Expected: FAIL，因为 slice builder 和统一 candidate 结构还不存在。

- [ ] **Step 5: 实现 `backend/memory/retrieval_candidates.py` 的最小闭环**

```python
@dataclass
class RecallCandidate:
    source: str
    item_id: str
    bucket: str
    score: float
    matched_reason: list[str]
    content_summary: str
    domains: list[str]
    applicability: str
```
```
def build_profile_candidates(
    ranked_items: list[tuple[str, MemoryProfileItem, str]]
) -> list[RecallCandidate]:
    ...


def build_episode_slice_candidates(
    ranked_slices: list[tuple[EpisodeSlice, str]]
) -> list[RecallCandidate]:
    ...
```

- [ ] **Step 6: 运行 retrieval candidate 单测，确认从红变绿**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_retrieval_candidates.py -v`
Expected: PASS

- [ ] **Step 7: 提交这一小步**

```bash
git add backend/memory/retrieval_candidates.py backend/tests/test_retrieval_candidates.py
git commit -m "feat(memory): add unified recall candidate builders"
```

### Task 2: 把 symbolic recall 输出统一成 RecallCandidate

**Files:**
- Modify: `backend/memory/symbolic_recall.py`
- Modify: `backend/tests/test_symbolic_recall.py`
- Create: `backend/tests/test_retrieval_candidates.py`（若 Task 1 未覆盖排序正规化）

- [ ] **Step 1: 写失败测试，要求 `rank_profile_items()` 返回 `RecallCandidate[]`**

```python
def test_rank_profile_items_returns_recall_candidates():
    query = build_recall_query("我是不是说过不坐红眼航班？")
    profile = UserMemoryProfile(...)

    ranked = rank_profile_items(query, profile)

    assert ranked
    assert ranked[0].source == "profile"
    assert ranked[0].bucket == "constraints"
    assert ranked[0].item_id == "constraints:flight:avoid_red_eye"
    assert ranked[0].score > 0
```

- [ ] **Step 2: 运行 profile recall 测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_symbolic_recall.py -k returns_recall_candidates -v`
Expected: FAIL，因为当前 `rank_profile_items()` 仍返回 tuple。

- [ ] **Step 3: 再写失败测试，要求 `rank_episode_slices()` 返回 `RecallCandidate[]`**

```python
def test_rank_episode_slices_returns_recall_candidates():
    query = build_recall_query("我上次去京都住哪里？")
    slices = [...]

    ranked = rank_episode_slices(query, slices)

    assert ranked
    assert ranked[0].source == "episode_slice"
    assert ranked[0].bucket == "accommodation_decision"
    assert ranked[0].item_id == "slice_exact"
```

- [ ] **Step 4: 运行 slice recall 测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_symbolic_recall.py -k episode_slices_returns_recall_candidates -v`
Expected: FAIL，因为当前 `rank_episode_slices()` 仍返回 `(slice, reason)`。

- [ ] **Step 5: 最小改造 `symbolic_recall.py` 输出层**

```python
def rank_profile_items(
    query: RecallQuery, profile: UserMemoryProfile
) -> list[RecallCandidate]:
    ...
    ranked.sort(key=lambda entry: entry[0])
    return build_profile_candidates(
        [(bucket, item, reason) for _, bucket, item, reason in ranked]
    )


def rank_episode_slices(
    query: RecallQuery, slices: list[EpisodeSlice]
) -> list[RecallCandidate]:
    ...
    ranked.sort(key=lambda entry: entry[0])
    return build_episode_slice_candidates(
        [(slice_, reason) for _, slice_, reason in ranked]
    )
```

- [ ] **Step 6: 运行 symbolic recall 单测，确认从红变绿**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_symbolic_recall.py -v`
Expected: PASS

- [ ] **Step 7: 提交这一小步**

```bash
git add backend/memory/symbolic_recall.py backend/tests/test_symbolic_recall.py
git commit -m "feat(memory): unify symbolic recall output contract"
```

### Task 3: 让 manager / formatter / telemetry 统一消费 candidate

**Files:**
- Modify: `backend/memory/manager.py`
- Modify: `backend/memory/formatter.py`
- Modify: `backend/tests/test_memory_manager.py`
- Modify: `backend/tests/test_memory_formatter.py`
- Modify: `backend/tests/test_trace_api.py`

- [ ] **Step 1: 写失败测试，要求 manager 能合并 profile + slice candidates**

```python
@pytest.mark.asyncio
async def test_generate_context_merges_profile_and_slice_candidates(tmp_path: Path):
    manager = MemoryManager(data_dir=str(tmp_path))
    ...
    text, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_now"),
        user_message="我上次去京都住哪里？",
    )

    assert "上次京都住四条附近的町屋。" in text
    assert recall.profile_ids
    assert recall.slice_ids
    assert recall.matched_reasons
```

- [ ] **Step 2: 运行 manager 测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_manager.py -k merges_profile_and_slice_candidates -v`
Expected: FAIL，因为 manager 仍按 tuple 路径取值。

- [ ] **Step 3: 再写失败测试，要求 formatter 接统一 candidate 输入**

```python
def test_format_v3_memory_context_renders_unified_recall_candidates():
    text = format_v3_memory_context(
        profile_items=[],
        working_items=[],
        recall_candidates=[
            RecallCandidate(
                source="profile",
                item_id="constraints:flight:avoid_red_eye",
                bucket="constraints",
                score=1.0,
                matched_reason=["domain=flight", "keyword=红眼航班"],
                content_summary="[flight] avoid_red_eye: true",
                domains=["flight"],
                applicability="适用于所有旅行。",
            )
        ],
    )

    assert "## 本轮请求命中的历史记忆" in text
    assert "source=profile" in text
```

- [ ] **Step 4: 运行 formatter 测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_formatter.py -k unified_recall_candidates -v`
Expected: FAIL，因为 formatter 目前仍要求 `query_profile_items/query_slices`。

- [ ] **Step 5: 最小改造 manager / formatter / telemetry 提取逻辑**

```python
recall_candidates = profile_candidates + slice_candidates
```
```
def format_v3_memory_context(
    profile_items: list[tuple[str, MemoryProfileItem]],
    working_items: list[WorkingMemoryItem],
    recall_candidates: list[RecallCandidate],
) -> str:
    ...
```
```
profile_ids = [candidate.item_id for candidate in recall_candidates if candidate.source == "profile"]
slice_ids = [candidate.item_id for candidate in recall_candidates if candidate.source == "episode_slice"]
```

- [ ] **Step 6: 运行 manager / formatter / trace 单测，确认从红变绿**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_manager.py tests/test_memory_formatter.py tests/test_trace_api.py -k 'candidate or unified_recall' -v`
Expected: PASS

- [ ] **Step 7: 提交这一小步**

```bash
git add backend/memory/manager.py backend/memory/formatter.py backend/tests/test_memory_manager.py backend/tests/test_memory_formatter.py backend/tests/test_trace_api.py
git commit -m "feat(memory): consume unified recall candidates"
```

### Task 4: 文档同步与最终回归

**Files:**
- Modify: `PROJECT_OVERVIEW.md`
- Modify: `docs/superpowers/plans/2026-04-22-recall-candidate-unification-phase-c.md`

- [ ] **Step 1: 更新 PROJECT_OVERVIEW 中的 Memory System 描述**

```markdown
- Memory System：Stage 3 已把 Profile / EpisodeSlice 规则召回统一收敛到 `RecallCandidate` 输出 contract；manager、formatter、trace/stats 都消费统一 candidate，而不再区分 profile tuple / slice tuple 两套输出。
```

- [ ] **Step 2: 运行最终回归**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_retrieval_candidates.py tests/test_symbolic_recall.py tests/test_memory_manager.py tests/test_memory_formatter.py tests/test_memory_integration.py tests/test_trace_api.py -k 'recall or candidate or memory_hit' -v`
Expected: PASS

- [ ] **Step 3: 记录未覆盖风险并准备交付说明**

```text
风险重点：Stage 3 仍保留 source-specific 内部排序逻辑；score 目前只是轻量正规化；Reranker 尚未接入，统一 candidate contract 主要服务于后续 Milestone D。
```

- [ ] **Step 4: 提交最终文档与收尾修改**

```bash
git add PROJECT_OVERVIEW.md docs/superpowers/plans/2026-04-22-recall-candidate-unification-phase-c.md
git commit -m "docs(memory): capture phase-c recall candidate rollout"
```

---

## Spec Coverage Check

- 已覆盖 `Profile + EpisodeSlice` 一起统一为 `RecallCandidate`：Task 1、Task 2、Task 3
- 已覆盖 manager / formatter / telemetry 统一消费 candidate：Task 3
- 已覆盖 Milestone C 只统一输出 contract、不重写内部检索：Task 2、Task 3
- 未纳入本计划：Stage 4 Reranker、EpisodeSlice 并入统一 retrieval plan、Stage 3 strategy 重构

这些未纳入项是刻意留给 Milestone D 及后续阶段，不是遗漏。
