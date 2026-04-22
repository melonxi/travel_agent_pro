# Profile Recall Query Tool Phase B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Phase A 的 recall gate 之后引入 `Recall Query Tool`，用结构化 retrieval plan 替换现有规则 `build_recall_query()`，同时通过 adapter 兼容现有规则召回器，避免本阶段重写 Stage 3。

**Architecture:** 新增 `memory.recall_query` 模块承载 Stage 2 的 tool schema、prompt、parser 和 fallback plan；新增 `memory.recall_query_adapter` 模块把 `RecallRetrievalPlan` 翻译成现有规则召回器可消费的兼容查询对象。`backend/main.py` 负责在 gate 放行后调用 query tool 并处理 timeout/error/invalid payload fallback，`MemoryManager.generate_context()` 负责优先消费 adapter 产出的 legacy query，同时保留旧路径以兼容未迁移调用。

**Tech Stack:** Python 3.12、FastAPI SSE、async/await、pytest + pytest-asyncio、现有 forced tool call 基础设施、现有 symbolic recall 规则召回器。

---

## File Map

- Create: `backend/memory/recall_query.py`
  - `RecallRetrievalPlan`
  - `build_recall_query_tool()`
  - `build_recall_query_prompt()`
  - `parse_recall_query_tool_arguments()`
  - `fallback_retrieval_plan()`
- Create: `backend/memory/recall_query_adapter.py`
  - `LegacyRecallQueryAdapterResult`
  - `plan_to_legacy_recall_query()`
- Modify: `backend/memory/symbolic_recall.py`
  - 最小扩展 `RecallQuery` 或规则召回器入口，支持 `allowed_buckets` / `strictness` 兼容字段
- Modify: `backend/memory/manager.py`
  - 接收 `retrieval_plan` / adapter query
  - 优先走新 query tool 路径
- Modify: `backend/main.py`
  - gate 放行后调用 Stage 2 query tool
  - timeout/error/invalid payload 时落到 fallback retrieval plan
  - telemetry 记录 query plan 摘要与 query fallback 来源
- Modify: `backend/memory/formatter.py`
  - 若需要，为 telemetry 增加 query plan 摘要字段
- Create: `backend/tests/test_recall_query.py`
  - tool schema、parser、fallback plan 单测
- Create: `backend/tests/test_recall_query_adapter.py`
  - adapter 映射单测
- Modify: `backend/tests/test_memory_manager.py`
  - manager 优先走新 plan / fallback plan 的路由测试
- Modify: `backend/tests/test_memory_integration.py`
  - query tool 成功路径、fallback 路径集成测试
- Modify: `PROJECT_OVERVIEW.md`
  - 更新 Memory System 中对 Stage 2 的描述

---

### Task 1: 锁定 Recall Query Tool 契约与 fallback 规则

**Files:**
- Create: `backend/memory/recall_query.py`
- Create: `backend/tests/test_recall_query.py`

- [ ] **Step 1: 先写失败测试，锁定 `RecallRetrievalPlan` 的合法解析路径**

```python
from memory.recall_query import parse_recall_query_tool_arguments


def test_parse_recall_query_tool_arguments_honors_schema_fields():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences", "constraints"],
            "domains": ["hotel", "accommodation"],
            "keywords": ["住宿", "酒店"],
            "aliases": ["住哪里", "住宿偏好"],
            "strictness": "soft",
            "top_k": 8,
            "reason": "user wants to reuse accommodation preference",
        }
    )

    assert plan.source == "profile"
    assert plan.buckets == ["stable_preferences", "constraints"]
    assert plan.domains == ["hotel", "accommodation"]
    assert plan.aliases == ["住哪里", "住宿偏好"]
    assert plan.strictness == "soft"
    assert plan.top_k == 8
```

- [ ] **Step 2: 运行解析测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_recall_query.py -k honors_schema_fields -v`
Expected: FAIL，因为 `memory.recall_query` 模块和 parser 还不存在。

- [ ] **Step 3: 再写失败测试，锁定非法 payload 与 fallback plan**

```python
from memory.recall_query import fallback_retrieval_plan, parse_recall_query_tool_arguments


def test_parse_recall_query_tool_arguments_rejects_non_profile_source():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "episode_slice",
            "buckets": ["stable_preferences"],
            "domains": [],
            "keywords": [],
            "aliases": [],
            "strictness": "soft",
            "top_k": 5,
            "reason": "bad source",
        }
    )

    assert plan.fallback_used == "invalid_query_plan"
    assert plan.reason == "invalid_query_plan"


def test_fallback_retrieval_plan_is_conservative():
    plan = fallback_retrieval_plan()

    assert plan.source == "profile"
    assert plan.buckets == ["constraints", "rejections", "stable_preferences"]
    assert plan.strictness == "soft"
    assert plan.top_k == 5
```

- [ ] **Step 4: 运行 fallback 测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_recall_query.py -k 'non_profile_source or fallback_retrieval_plan' -v`
Expected: FAIL，因为 fallback plan 和非法 payload 语义尚未实现。

- [ ] **Step 5: 实现 `backend/memory/recall_query.py` 的最小闭环**

```python
@dataclass
class RecallRetrievalPlan:
    source: str
    buckets: list[str]
    domains: list[str]
    keywords: list[str]
    aliases: list[str]
    strictness: str
    top_k: int
    reason: str
    fallback_used: str = "none"


def fallback_retrieval_plan() -> RecallRetrievalPlan:
    return RecallRetrievalPlan(
        source="profile",
        buckets=["constraints", "rejections", "stable_preferences"],
        domains=[],
        keywords=[],
        aliases=[],
        strictness="soft",
        top_k=5,
        reason="fallback_default_plan",
        fallback_used="fallback_default_plan",
    )
```
```
def parse_recall_query_tool_arguments(payload: dict[str, Any] | None) -> RecallRetrievalPlan:
    ...
    if source != "profile":
        return RecallRetrievalPlan(
            ...,
            reason="invalid_query_plan",
            fallback_used="invalid_query_plan",
        )
```

- [ ] **Step 6: 运行 query tool 单测，确认从红变绿**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_recall_query.py -v`
Expected: PASS

- [ ] **Step 7: 提交这一小步**

```bash
git add backend/memory/recall_query.py backend/tests/test_recall_query.py
git commit -m "feat(memory): add recall query tool contract"
```

### Task 2: 实现 Plan Adapter，并最小扩展现有规则召回入口

**Files:**
- Create: `backend/memory/recall_query_adapter.py`
- Modify: `backend/memory/symbolic_recall.py`
- Create: `backend/tests/test_recall_query_adapter.py`
- Modify: `backend/tests/test_symbolic_recall.py`

- [ ] **Step 1: 写失败测试，锁定 adapter 对 `keywords + aliases` 与 `allowed_buckets` 的映射**

```python
from memory.recall_query import RecallRetrievalPlan
from memory.recall_query_adapter import plan_to_legacy_recall_query


def test_plan_to_legacy_recall_query_merges_keywords_and_aliases():
    plan = RecallRetrievalPlan(
        source="profile",
        buckets=["stable_preferences", "constraints"],
        domains=["hotel"],
        keywords=["住宿"],
        aliases=["住哪里", "住宿偏好"],
        strictness="soft",
        top_k=8,
        reason="reuse accommodation preference",
    )

    query = plan_to_legacy_recall_query(plan)

    assert query.include_profile is True
    assert query.include_slices is False
    assert query.allowed_buckets == ["stable_preferences", "constraints"]
    assert query.strictness == "soft"
    assert query.keywords == ["住宿", "住哪里", "住宿偏好"]
```

- [ ] **Step 2: 运行 adapter 测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_recall_query_adapter.py -k merges_keywords_and_aliases -v`
Expected: FAIL，因为 adapter 模块和兼容 query 结构还不存在。

- [ ] **Step 3: 再写失败测试，要求 `rank_profile_items()` 支持 `allowed_buckets` 过滤**

```python
def test_rank_profile_items_respects_allowed_buckets():
    query = RecallQuery(
        needs_memory=True,
        domains=["hotel"],
        entities={},
        keywords=["青旅"],
        include_profile=True,
        include_slices=False,
        include_working_memory=False,
        matched_reason="adapter generated",
        allowed_buckets=["rejections"],
        strictness="strict",
    )
    ...
    assert [bucket for bucket, _, _ in ranked] == ["rejections"]
```

- [ ] **Step 4: 运行 bucket 过滤测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_symbolic_recall.py -k allowed_buckets -v`
Expected: FAIL，因为旧 `RecallQuery` 尚无兼容字段，规则召回器也不会过滤 bucket。

- [ ] **Step 5: 实现 adapter 和最小规则兼容层**

```python
@dataclass
class LegacyRecallQueryAdapterResult:
    domains: list[str]
    keywords: list[str]
    entities: dict[str, str]
    include_profile: bool
    include_slices: bool
    allowed_buckets: list[str]
    strictness: str
    matched_reason: str
```
```
def plan_to_legacy_recall_query(plan: RecallRetrievalPlan) -> RecallQuery:
    merged_keywords = _dedupe(plan.keywords + plan.aliases)
    return RecallQuery(
        needs_memory=True,
        domains=list(plan.domains),
        entities={},
        keywords=merged_keywords,
        include_profile=True,
        include_slices=False,
        include_working_memory=False,
        matched_reason=plan.reason,
        allowed_buckets=list(plan.buckets),
        strictness=plan.strictness,
    )
```
```
@dataclass
class RecallQuery:
    ...
    allowed_buckets: list[str] = field(default_factory=list)
    strictness: str = "soft"
```

- [ ] **Step 6: 运行 adapter / symbolic recall 单测，确认从红变绿**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_recall_query_adapter.py tests/test_symbolic_recall.py -k 'allowed_buckets or adapter' -v`
Expected: PASS

- [ ] **Step 7: 提交这一小步**

```bash
git add backend/memory/recall_query_adapter.py backend/memory/symbolic_recall.py backend/tests/test_recall_query_adapter.py backend/tests/test_symbolic_recall.py
git commit -m "feat(memory): adapt retrieval plans to symbolic recall"
```

### Task 3: 把 Query Tool 接入 recall 主路径并保留 fallback

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/memory/manager.py`
- Modify: `backend/memory/formatter.py`
- Modify: `backend/tests/test_memory_manager.py`
- Modify: `backend/tests/test_memory_integration.py`

- [ ] **Step 1: 写失败测试，要求 gate 放行后优先走 query tool 而不是旧 `build_recall_query()`**

```python
@pytest.mark.asyncio
async def test_generate_context_prefers_retrieval_plan_over_legacy_query(monkeypatch, tmp_path: Path):
    manager = MemoryManager(data_dir=str(tmp_path))

    def fail_build_recall_query(*args, **kwargs):
        raise AssertionError("legacy build_recall_query should not run when retrieval_plan is provided")

    monkeypatch.setattr("memory.manager.build_recall_query", fail_build_recall_query)

    text, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_now"),
        user_message="住宿还是按我常规偏好来",
        recall_gate=True,
        short_circuit="undecided",
        retrieval_plan=RecallRetrievalPlan(
            source="profile",
            buckets=["stable_preferences"],
            domains=["hotel"],
            keywords=["住宿"],
            aliases=["住哪里"],
            strictness="soft",
            top_k=5,
            reason="reuse accommodation preference",
        ),
    )

    assert recall.final_recall_decision == "query_recall_enabled"
```

- [ ] **Step 2: 运行 manager 路由测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_manager.py -k retrieval_plan_over_legacy_query -v`
Expected: FAIL，因为 `generate_context()` 还不接受 `retrieval_plan`。

- [ ] **Step 3: 再写失败测试，要求 query tool 失败时走 fallback plan 且请求成功**

```python
@pytest.mark.asyncio
async def test_chat_stream_falls_back_to_default_retrieval_plan_when_query_tool_invalid(monkeypatch, app):
    ...
    assert resp.status_code == 200
    assert '"type": "memory_recall"' in resp.text
    assert '"query_plan_fallback": "invalid_query_plan"' in resp.text
```

- [ ] **Step 4: 运行 fallback 集成测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_integration.py -k query_tool_invalid -v`
Expected: FAIL，因为主路径尚未调用 recall query tool，也没有 query fallback telemetry。

- [ ] **Step 5: 在 `main.py` 增加 Stage 2 query tool 编排**

```python
async def _build_recall_retrieval_plan(... ) -> RecallRetrievalPlan:
    if not recall_decision.needs_recall:
        return fallback_retrieval_plan()

    try:
        tool_args = await asyncio.wait_for(
            _collect_forced_tool_call_arguments(
                query_llm,
                messages=[Message(role=Role.USER, content=build_recall_query_prompt(...))],
                tool_def=build_recall_query_tool(),
            ),
            timeout=config.memory.retrieval.recall_gate_timeout_seconds,
        )
    except asyncio.TimeoutError:
        plan = fallback_retrieval_plan()
        plan.fallback_used = "query_plan_timeout"
        return plan
```

- [ ] **Step 6: 修改 `MemoryManager.generate_context()`，优先消费 `retrieval_plan` + adapter query**

```python
async def generate_context(..., retrieval_plan: RecallRetrievalPlan | None = None):
    ...
    if should_run_query_recall and retrieval_plan is not None:
        recall_query = plan_to_legacy_recall_query(retrieval_plan)
    elif should_run_query_recall:
        recall_query = build_recall_query(user_message)
```

- [ ] **Step 7: 为 telemetry 增加 query plan 摘要与 fallback 字段**

```python
memory_recall.query_plan = {
    "buckets": retrieval_plan.buckets,
    "domains": retrieval_plan.domains,
    "strictness": retrieval_plan.strictness,
    "top_k": retrieval_plan.top_k,
}
memory_recall.query_plan_fallback = retrieval_plan.fallback_used
```

- [ ] **Step 8: 运行目标测试，确认从红变绿**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_manager.py -k retrieval_plan_over_legacy_query -v && pytest tests/test_memory_integration.py -k 'query_tool_invalid or recall_query' -v`
Expected: PASS

- [ ] **Step 9: 提交这一小步**

```bash
git add backend/main.py backend/memory/manager.py backend/memory/formatter.py backend/tests/test_memory_manager.py backend/tests/test_memory_integration.py
git commit -m "feat(memory): route recall through query tool"
```

### Task 4: 文档同步与最终回归

**Files:**
- Modify: `PROJECT_OVERVIEW.md`
- Modify: `docs/superpowers/plans/2026-04-21-profile-recall-query-tool-phase-b.md`

- [ ] **Step 1: 更新项目总览中的 Memory System 描述**

```markdown
- Memory System：Profile recall 已进入 Phase B：Stage 0 / Stage 1 之后新增 `Recall Query Tool`，用结构化 retrieval plan 替换旧 `build_recall_query()`，再通过 adapter 兼容当前规则召回器；query tool 失败时保守回退到 fallback retrieval plan。
```

- [ ] **Step 2: 运行最终回归**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_recall_query.py tests/test_recall_query_adapter.py tests/test_symbolic_recall.py tests/test_memory_manager.py tests/test_memory_integration.py -k 'recall or query_tool or retrieval_plan' -v`
Expected: PASS

- [ ] **Step 3: 记录未覆盖风险并准备交付说明**

```text
风险重点：query tool 的 domain/bucket 质量仍依赖模型输出；Stage 3 仍是旧规则召回器，strictness 当前只是轻量兼容；EpisodeSlice 仍未并入统一 retrieval plan。
```

- [ ] **Step 4: 提交最终文档与收尾修改**

```bash
git add PROJECT_OVERVIEW.md docs/superpowers/plans/2026-04-21-profile-recall-query-tool-phase-b.md
git commit -m "docs(memory): capture phase-b recall query rollout"
```

---

## Spec Coverage Check

- 已覆盖 Stage 2 Recall Query Tool：Task 1、Task 3
- 已覆盖 plan -> 旧规则召回器 adapter：Task 2、Task 3
- 已覆盖 query tool fallback 语义：Task 1、Task 3
- 已覆盖 Milestone B 仅替换 query builder、不重写 retriever 的边界：Task 2、Task 3
- 未纳入本计划：EpisodeSlice 双 source retrieval plan、Stage 4 reranker、Stage 3 全量重写 candidate 输出

这些未纳入项是刻意延后到后续 Milestone C / D，不是遗漏。

---

## 执行后风险记录

- query tool 的 `domains` / `buckets` 质量仍依赖模型输出，当前回归主要覆盖 fallback 语义，未覆盖召回质量波动。
- Stage 3 仍复用现有规则召回器，`strictness` 只是轻量兼容层，排序与命中质量仍有旧实现边界。
- `EpisodeSlice` 仍未纳入统一 retrieval plan，本轮 Phase B 只覆盖 `profile` source。
