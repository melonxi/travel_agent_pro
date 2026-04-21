# Profile Recall Gate Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为同步 `memory_recall` 主路径引入 `Stage 0 硬规则短路 + Stage 1 单层 LLM recall gate`，先替换当前纯触发词判定，同时保留现有 `build_recall_query()` 与规则召回逻辑不变。

**Architecture:** 新增一个专用 `memory.recall_gate` 模块，承载短路规则、gate tool schema、prompt 和 tool 参数解析；`backend/main.py` 负责 LLM gate 编排与失败降级，`MemoryManager.generate_context()` 继续负责固定注入和 query-aware recall，只是把“是否允许进入 query-aware recall”的决策改为接收外部 routing decision。`MemoryRecallTelemetry` 扩展为始终记录短路决策、gate 决策与最终 recall 路由，chat SSE 无论是否命中记忆都发出结构化 `memory_recall` 遥测事件。

**Tech Stack:** Python 3.12、FastAPI SSE、async/await、pytest + pytest-asyncio、现有 LLM forced tool call 基础设施。

---

## File Map

- Create: `backend/memory/recall_gate.py`
  - `RecallShortCircuitDecision`
  - `RecallGateDecision`
  - `apply_recall_short_circuit()`
  - `build_recall_gate_prompt()`
  - `build_recall_gate_tool()`
  - `parse_recall_gate_tool_arguments()`
- Modify: `backend/main.py`
  - 新增 `_decide_memory_recall()`
  - 在 chat SSE 的同步 memory recall 段接入 short-circuit + LLM gate
  - 始终发出 `memory_recall` 遥测事件
- Modify: `backend/memory/manager.py`
  - `generate_context()` 接收 routing decision
  - 只在允许时运行现有 query-aware recall
  - telemetry 中写入 gate / short-circuit 字段
- Modify: `backend/memory/formatter.py`
  - 为 `MemoryRecallTelemetry` 增加 gate 相关字段与 `to_dict()` 输出
- Modify: `backend/config.py`
  - 为 `memory.retrieval` 增加 recall gate 开关、模型回退和超时配置
- Modify: `backend/tests/test_memory_manager.py`
  - 配置映射测试
  - manager 路由测试
- Modify: `backend/tests/test_memory_formatter.py`
  - telemetry 序列化测试
- Create: `backend/tests/test_recall_gate.py`
  - 短路规则 / gate schema 解析单测
- Modify: `backend/tests/test_memory_integration.py`
  - chat 同步 recall 遥测与 gate 降级集成测试
- Modify: `PROJECT_OVERVIEW.md`
  - 更新 Memory System 描述

---

### Task 1: 锁定 recall gate 配置与 telemetry 契约

**Files:**
- Modify: `backend/config.py`
- Modify: `backend/memory/formatter.py`
- Modify: `backend/tests/test_memory_manager.py`
- Modify: `backend/tests/test_memory_formatter.py`

- [ ] **Step 1: 先写失败测试，要求 `memory.retrieval` 暴露 recall gate 配置**

```python
def test_memory_config_maps_recall_gate_fields(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
memory:
  enabled: "true"
  retrieval:
    core_limit: 5
    phase_limit: 3
    include_pending: "true"
    recall_gate_enabled: "true"
    recall_gate_model: "gpt-4o-mini"
    recall_gate_timeout_seconds: 6.5
""",
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_file))

    assert cfg.memory.retrieval.recall_gate_enabled is True
    assert cfg.memory.retrieval.recall_gate_model == "gpt-4o-mini"
    assert cfg.memory.retrieval.recall_gate_timeout_seconds == 6.5
```

- [ ] **Step 2: 运行配置测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_manager.py -k recall_gate_fields -v`
Expected: FAIL，因为 `MemoryRetrievalConfig` 还没有 recall gate 相关字段。

- [ ] **Step 3: 再写失败测试，要求 `MemoryRecallTelemetry` 保留 gate 决策字段**

```python
def test_memory_recall_telemetry_to_dict_includes_gate_fields():
    telemetry = MemoryRecallTelemetry(
        sources={"profile_fixed": 1, "query_profile": 0, "working_memory": 0, "episode_slice": 0},
        profile_ids=["profile-1"],
        matched_reasons=["fixed profile injection"],
        stage0_decision="force_recall",
        stage0_reason="history_phrase",
        gate_needs_recall=True,
        gate_intent_type="profile_preference_recall",
        gate_confidence=0.88,
        gate_reason="user asks to reuse prior preference",
        final_recall_decision="query_recall_enabled",
        fallback_used="none",
    )

    assert telemetry.to_dict()["stage0_decision"] == "force_recall"
    assert telemetry.to_dict()["gate_needs_recall"] is True
    assert telemetry.to_dict()["final_recall_decision"] == "query_recall_enabled"
```

- [ ] **Step 4: 运行 telemetry 测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_formatter.py -k gate_fields -v`
Expected: FAIL，因为 `MemoryRecallTelemetry` 目前只序列化 sources / ids / matched_reasons。

- [ ] **Step 5: 最小实现配置与 telemetry 字段**

```python
@dataclass(frozen=True)
class MemoryRetrievalConfig:
    core_limit: int = 10
    phase_limit: int = 8
    include_pending: bool = False
    recall_gate_enabled: bool = True
    recall_gate_model: str = ""
    recall_gate_timeout_seconds: float = 6.0
```
```
@dataclass
class MemoryRecallTelemetry:
    sources: dict[str, int] = field(...)
    profile_ids: list[str] = field(default_factory=list)
    working_memory_ids: list[str] = field(default_factory=list)
    slice_ids: list[str] = field(default_factory=list)
    matched_reasons: list[str] = field(default_factory=list)
    stage0_decision: str = "undecided"
    stage0_reason: str = ""
    gate_needs_recall: bool | None = None
    gate_intent_type: str = ""
    gate_confidence: float | None = None
    gate_reason: str = ""
    final_recall_decision: str = ""
    fallback_used: str = "none"
```

- [ ] **Step 6: 运行目标测试，确认从红变绿**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_manager.py -k recall_gate_fields -v && pytest tests/test_memory_formatter.py -k gate_fields -v`
Expected: PASS

- [ ] **Step 7: 提交这一小步**

```bash
git add backend/config.py backend/memory/formatter.py backend/tests/test_memory_manager.py backend/tests/test_memory_formatter.py
git commit -m "feat: add recall gate config and telemetry fields"
```

### Task 2: 实现纯函数 recall gate 模块与短路规则

**Files:**
- Create: `backend/memory/recall_gate.py`
- Create: `backend/tests/test_recall_gate.py`

- [ ] **Step 1: 写失败测试，锁定 Stage 0 三态短路行为**

```python
from memory.recall_gate import apply_recall_short_circuit


def test_short_circuit_skips_current_trip_fact_question():
    decision = apply_recall_short_circuit("这次预算多少？")

    assert decision.decision == "skip_recall"
    assert decision.reason == "current_trip_fact_question"


def test_short_circuit_forces_obvious_history_question():
    decision = apply_recall_short_circuit("我是不是说过不坐红眼航班？")

    assert decision.decision == "force_recall"
    assert decision.reason == "explicit_profile_history_query"


def test_short_circuit_leaves_ambiguous_message_to_gate():
    decision = apply_recall_short_circuit("还是按我常规偏好来")

    assert decision.decision == "undecided"
    assert decision.reason == "needs_llm_gate"
```

- [ ] **Step 2: 运行短路规则测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_recall_gate.py -k short_circuit -v`
Expected: FAIL，因为 `memory.recall_gate` 文件和对应函数还不存在。

- [ ] **Step 3: 再写失败测试，锁定 gate tool 输出解析与默认值**

```python
from memory.recall_gate import parse_recall_gate_tool_arguments


def test_parse_recall_gate_tool_arguments_honors_schema_fields():
    decision = parse_recall_gate_tool_arguments(
        {
            "needs_recall": True,
            "intent_type": "profile_preference_recall",
            "reason": "user asks to apply prior preference",
            "confidence": 0.81,
        }
    )

    assert decision.needs_recall is True
    assert decision.intent_type == "profile_preference_recall"
    assert decision.reason == "user asks to apply prior preference"
    assert decision.confidence == 0.81


def test_parse_recall_gate_tool_arguments_defaults_invalid_payload_to_safe_false():
    decision = parse_recall_gate_tool_arguments({"confidence": "oops"})

    assert decision.needs_recall is False
    assert decision.intent_type == "no_recall_needed"
    assert decision.reason == "invalid_tool_payload"
    assert decision.confidence == 0.0
```

- [ ] **Step 4: 运行解析测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_recall_gate.py -k parse_recall_gate_tool_arguments -v`
Expected: FAIL，因为解析器、decision 数据结构和 schema 还不存在。

- [ ] **Step 5: 实现 `backend/memory/recall_gate.py` 的最小闭环**

```python
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RecallShortCircuitDecision:
    decision: str
    reason: str


@dataclass
class RecallGateDecision:
    needs_recall: bool
    intent_type: str
    reason: str
    confidence: float = 0.0
    fallback_used: str = "none"


def apply_recall_short_circuit(message: str) -> RecallShortCircuitDecision:
    text = (message or "").strip()
    if not text:
        return RecallShortCircuitDecision("undecided", "needs_llm_gate")
    if any(token in text for token in ("我是不是说过", "按我的习惯", "上次", "之前", "以前")):
        return RecallShortCircuitDecision("force_recall", "explicit_profile_history_query")
    if any(token in text for token in ("这次", "本次", "当前")) and any(
        token in text for token in ("预算", "几号", "出发", "骨架", "安排")
    ):
        return RecallShortCircuitDecision("skip_recall", "current_trip_fact_question")
    return RecallShortCircuitDecision("undecided", "needs_llm_gate")


def build_recall_gate_tool() -> dict[str, Any]:
    return {
        "name": "decide_memory_recall",
        "description": "Decide whether the current user message needs profile recall.",
        "parameters": {
            "type": "object",
            "properties": {
                "needs_recall": {"type": "boolean"},
                "intent_type": {
                    "type": "string",
                    "enum": [
                        "current_trip_fact",
                        "profile_preference_recall",
                        "profile_constraint_recall",
                        "past_trip_experience_recall",
                        "mixed_or_ambiguous",
                        "no_recall_needed",
                    ],
                },
                "reason": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["needs_recall", "intent_type", "reason", "confidence"],
        },
    }
```

- [ ] **Step 6: 运行 recall gate 单测，确认从红变绿**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_recall_gate.py -v`
Expected: PASS

- [ ] **Step 7: 提交这一小步**

```bash
git add backend/memory/recall_gate.py backend/tests/test_recall_gate.py
git commit -m "feat: add profile recall short circuit and gate schema"
```

### Task 3: 把 recall gate 接入同步 memory_recall 主路径

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/memory/manager.py`
- Modify: `backend/tests/test_memory_manager.py`
- Modify: `backend/tests/test_memory_integration.py`

- [ ] **Step 1: 写失败测试，要求 manager 在 gate 关闭 query-aware recall 时仍保留固定注入**

```python
@pytest.mark.asyncio
async def test_generate_context_respects_gate_false_but_keeps_fixed_profile(tmp_path: Path):
    manager = MemoryManager(data_dir=str(tmp_path))
    await manager.v3_store.upsert_profile_item(
        "u1",
        "constraints",
        MemoryProfileItem(
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
        ),
    )

    text, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_now"),
        user_message="还是按我常规偏好来",
        recall_gate=RecallGateDecision(
            needs_recall=False,
            intent_type="no_recall_needed",
            reason="new_preference_signal_not_recall",
            confidence=0.42,
        ),
        short_circuit=RecallShortCircuitDecision("undecided", "needs_llm_gate"),
    )

    assert "## 长期用户画像" in text
    assert "## 本轮请求命中的历史记忆" not in text
    assert recall.sources["profile_fixed"] == 1
    assert recall.sources["query_profile"] == 0
    assert recall.gate_needs_recall is False
    assert recall.final_recall_decision == "query_recall_skipped_by_gate"
```

- [ ] **Step 2: 运行 manager 路由测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_manager.py -k gate_false_but_keeps_fixed_profile -v`
Expected: FAIL，因为 `generate_context()` 还不能接收 `recall_gate` 和 `short_circuit` 参数。

- [ ] **Step 3: 再写失败测试，要求 chat SSE 即使 recall 结果为空也输出 gate 遥测**

```python
@pytest.mark.asyncio
async def test_chat_stream_emits_memory_recall_telemetry_without_hits(monkeypatch, app):
    async def fake_generate_context(user_id, plan, user_message="", recall_gate=None, short_circuit=None):
        return (
            "暂无相关用户记忆",
            MemoryRecallTelemetry(
                stage0_decision="undecided",
                stage0_reason="needs_llm_gate",
                gate_needs_recall=False,
                gate_intent_type="no_recall_needed",
                gate_confidence=0.33,
                gate_reason="new_preference_signal_not_recall",
                final_recall_decision="query_recall_skipped_by_gate",
                fallback_used="none",
            ),
        )

    ...
    assert '"type": "memory_recall"' in resp.text
    assert '"gate_needs_recall": false' in resp.text
    assert '"final_recall_decision": "query_recall_skipped_by_gate"' in resp.text
```

- [ ] **Step 4: 运行集成测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_integration.py -k telemetry_without_hits -v`
Expected: FAIL，因为当前只有命中记忆时才会发 `memory_recall` 遥测事件。

- [ ] **Step 5: 在 `main.py` 增加 recall gate 编排并把 routing decision 传给 manager**

```python
async def _decide_memory_recall(*, session_id: str, user_id: str, user_message: str) -> tuple[RecallShortCircuitDecision, RecallGateDecision]:
    short_circuit = apply_recall_short_circuit(user_message)
    if short_circuit.decision == "force_recall":
        return short_circuit, RecallGateDecision(
            needs_recall=True,
            intent_type="mixed_or_ambiguous",
            reason=short_circuit.reason,
            confidence=1.0,
            fallback_used="stage0_force_recall",
        )
    if short_circuit.decision == "skip_recall":
        return short_circuit, RecallGateDecision(
            needs_recall=False,
            intent_type="current_trip_fact",
            reason=short_circuit.reason,
            confidence=1.0,
            fallback_used="stage0_skip_recall",
        )
    if not config.memory.retrieval.recall_gate_enabled:
        return short_circuit, RecallGateDecision(
            needs_recall=False,
            intent_type="no_recall_needed",
            reason="recall_gate_disabled",
            confidence=0.0,
            fallback_used="gate_disabled",
        )

    model_cfg = replace(
        config.llm,
        model=config.memory.retrieval.recall_gate_model or config.llm.model,
    )
    gate_llm = create_llm_provider(model_cfg)
    tool_args = await asyncio.wait_for(
        _collect_forced_tool_call_arguments(
            gate_llm,
            messages=[Message(role=Role.USER, content=build_recall_gate_prompt(user_message))],
            tool_def=build_recall_gate_tool(),
        ),
        timeout=config.memory.retrieval.recall_gate_timeout_seconds,
    )
    return short_circuit, parse_recall_gate_tool_arguments(tool_args)
```

- [ ] **Step 6: 修改 `MemoryManager.generate_context()`，只在 gate 放行时继续现有 query-aware recall**

```python
async def generate_context(
    self,
    user_id: str,
    plan: TravelPlanState,
    user_message: str = "",
    recall_gate: RecallGateDecision | None = None,
    short_circuit: RecallShortCircuitDecision | None = None,
) -> tuple[str, MemoryRecallTelemetry]:
    ...
    should_run_query_recall = bool(user_message)
    if recall_gate is not None:
        should_run_query_recall = recall_gate.needs_recall
    elif user_message:
        recall_query = build_recall_query(user_message)
        should_run_query_recall = should_trigger_memory_recall(user_message) or recall_query.needs_memory

    if should_run_query_recall:
        recall_query = build_recall_query(user_message)
        ...
```

- [ ] **Step 7: 始终发出 `memory_recall` 遥测事件，并把 gate 字段写入 internal task result**

```python
yield json.dumps(
    {
        "type": "internal_task",
        "task": InternalTask(
            ...,
            result={
                "item_ids": recalled_ids,
                "count": len(recalled_ids),
                "sources": dict(memory_recall.sources),
                "stage0_decision": memory_recall.stage0_decision,
                "gate_needs_recall": memory_recall.gate_needs_recall,
                "final_recall_decision": memory_recall.final_recall_decision,
            },
        ).to_dict(),
    },
    ensure_ascii=False,
)

yield json.dumps({"type": "memory_recall", **memory_recall.to_dict()}, ensure_ascii=False)
```

- [ ] **Step 8: 运行目标测试，确认从红变绿**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_recall_gate.py tests/test_memory_manager.py -k 'gate_false_but_keeps_fixed_profile or recall_gate_fields' -v && pytest tests/test_memory_integration.py -k telemetry_without_hits -v`
Expected: PASS

- [ ] **Step 9: 提交这一小步**

```bash
git add backend/main.py backend/memory/manager.py backend/tests/test_memory_manager.py backend/tests/test_memory_integration.py
git commit -m "feat: route profile recall through short circuit and llm gate"
```

### Task 4: 补齐 gate 失败降级、文档同步与最终回归

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/tests/test_memory_integration.py`
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: 写失败测试，锁定 gate 超时降级为 `needs_recall=false` 但保留 fixed profile 注入**

```python
@pytest.mark.asyncio
async def test_chat_stream_recall_gate_timeout_falls_back_to_skip(monkeypatch, app):
    async def fake_collect(*args, **kwargs):
        raise asyncio.TimeoutError()

    ...
    assert '"type": "memory_recall"' in resp.text
    assert '"fallback_used": "gate_timeout"' in resp.text
    assert '"gate_needs_recall": false' in resp.text
```

- [ ] **Step 2: 运行超时降级测试并确认先失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_memory_integration.py -k gate_timeout_falls_back_to_skip -v`
Expected: FAIL，因为 recall gate 还没有 timeout fallback 和对应 telemetry。

- [ ] **Step 3: 补上主路径降级逻辑与日志文案**

```python
except asyncio.TimeoutError:
    logger.warning(
        "记忆召回 gate 超时 session=%s user=%s timeout_seconds=%s",
        session_id,
        user_id,
        config.memory.retrieval.recall_gate_timeout_seconds,
    )
    return short_circuit, RecallGateDecision(
        needs_recall=False,
        intent_type="no_recall_needed",
        reason="recall_gate_timeout",
        confidence=0.0,
        fallback_used="gate_timeout",
    )
except Exception:
    logger.exception("记忆召回 gate 失败 session=%s user=%s", session_id, user_id)
    return short_circuit, RecallGateDecision(
        needs_recall=False,
        intent_type="no_recall_needed",
        reason="recall_gate_error",
        confidence=0.0,
        fallback_used="gate_error",
    )
```

- [ ] **Step 4: 更新 `PROJECT_OVERVIEW.md` 的 Memory System 描述**

```markdown
- Memory System：`memory_recall` 在回答前同步执行；Profile recall 第一阶段改为“Stage 0 硬规则短路 + Stage 1 LLM gate”，只在 gate 放行时进入 query-aware symbolic recall，固定 profile / working memory 注入保持不变，chat SSE 每轮都会输出 recall telemetry 结果。
```

- [ ] **Step 5: 运行最终回归**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_recall_gate.py tests/test_memory_formatter.py tests/test_memory_manager.py tests/test_memory_integration.py -k 'recall or memory_recall or gate' -v`
Expected: PASS

- [ ] **Step 6: 提交最终文档与收尾修改**

```bash
git add backend/main.py backend/tests/test_memory_integration.py PROJECT_OVERVIEW.md docs/superpowers/plans/2026-04-21-profile-recall-gate-phase-a.md
git commit -m "docs: capture phase a profile recall gate rollout"
```

---

## Spec Coverage Check

- 已覆盖 `Stage 0 硬规则短路`：Task 2
- 已覆盖 `Stage 1 LLM Recall Gate`：Task 2、Task 3、Task 4
- 已覆盖 telemetry 与失败降级：Task 1、Task 3、Task 4
- 已覆盖第一阶段“后续仍复用现有 query / rank”约束：Task 3
- 未纳入本计划：`Stage 2 Recall Query Tool`、`Stage 3 规则召回器统一 candidate 输出`、`Stage 4 LLM Reranker`

这些未纳入项是刻意分期，不是遗漏；后续分别起 Milestone B/C/D 计划。
