# Recall Reranker Stage 4 Phase A+B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Stage 3 已有的 `evidence_by_id` 正式接入 Stage 4 reranker，并在保持默认排序行为稳定的前提下，补齐结构化 score/telemetry，为后续 evidence-aware rerank 放量做准备。

**Architecture:** 这次实现不新增第二套 embedding reranker，也不改 Stage 3 候选生成边界。主线是：先扩展 `MemoryRerankerConfig` 和 `RecallRerankResult`，再把 `Stage3RecallResult.evidence_by_id` 从 `MemoryManager.generate_context()` 传到 `choose_reranker_path()`，最后在 reranker 内部拆出规则信号、evidence 信号、source-aware normalization 和结构化 telemetry，同时维持默认 config 下的 `selected_item_ids` 与主干 `per_item_reason` 稳定。

**Tech Stack:** Python 3.12、FastAPI、pytest + pytest-asyncio、现有 `fastembed`/ONNX runtime 抽象、TypeScript trace types。

---

## Scope Check

本计划只覆盖更新后 spec 的 **Phase A + Phase B**：

- Phase A：evidence plumbing、config 扩展、结构化 telemetry、默认行为兼容
- Phase B：消费 Stage 3 lexical / semantic / fused evidence 做附加分，默认权重仍为 0

不包含：

- Phase C：动态 source budget
- Phase D：reranker-local embedding

这两部分依赖 Phase A/B 的 telemetry 结果，应该在本计划落地后另开 plan。

## File Map

- Modify: `backend/config.py`
  - 扩展 `MemoryRerankerConfig`
  - 增加 `IntentWeightProfile` / `RerankerEvidenceConfig` / `RerankerDynamicBudgetConfig`
  - 扩展 `_build_memory_config()` 解析 `reranker.evidence`
- Modify: `backend/memory/manager.py`
  - 把 `stage3_result.evidence_by_id` 传给 reranker
  - 补齐 `reranker_per_item_scores` / `reranker_intent_label` / `reranker_selection_metrics`
- Modify: `backend/memory/recall_reranker.py`
  - 扩展 `RecallRerankResult`
  - 增加 `SignalScoreDetail`
  - 增加 evidence-aware scoring 与 source-aware normalization helper
  - 维持默认 config 下的排序与 `per_item_reason` 主干稳定
- Modify: `backend/memory/formatter.py`
  - 扩展 `MemoryRecallTelemetry`
- Modify: `backend/main.py`
  - 扩展 internal task payload 与 recall telemetry record 映射
- Modify: `backend/telemetry/stats.py`
  - 扩展 `RecallTelemetryRecord`
- Modify: `backend/api/trace.py`
  - 暴露新的 reranker telemetry 字段
- Modify: `frontend/src/types/trace.ts`
  - 为 trace viewer 暴露新字段类型
- Modify: `frontend/src/types/plan.ts`
  - 为 SSE `memory_recall` 事件暴露新字段类型
- Modify: `frontend/src/components/TraceViewer.tsx`
  - 最小展示 `reranker_intent_label`
- Modify: `backend/tests/test_stage3_config.py`
  - 增加 reranker config 解析测试
- Modify: `backend/tests/test_recall_reranker.py`
  - 增加 Phase A/Phase B 核心单测
- Modify: `backend/tests/test_memory_manager.py`
  - 验证 evidence plumbing、missing-key fallback、默认稳定性
- Modify: `backend/tests/test_memory_formatter.py`
  - 验证 telemetry `to_dict()` 新字段
- Modify: `backend/tests/test_trace_api.py`
  - 验证 trace API 暴露新字段
- Modify: `backend/tests/test_stats.py`
  - 验证 `last_memory_recall` stats 字段
- Modify: `backend/tests/test_memory_integration.py`
  - 验证 chat SSE `memory_recall` 事件包含新字段
- Modify: `PROJECT_OVERVIEW.md`
  - 更新 Memory System / reranker telemetry 描述

---

### Task 1: 扩展 reranker config 与结果 contract

**Files:**
- Modify: `backend/config.py`
- Modify: `backend/memory/recall_reranker.py`
- Modify: `backend/tests/test_stage3_config.py`
- Modify: `backend/tests/test_recall_reranker.py`

- [ ] **Step 1: 先写 config 失败测试，锁定 reranker 新默认值**

```python
from config import MemoryRetrievalConfig


def test_memory_retrieval_config_reranker_defaults_include_evidence_blocks():
    cfg = MemoryRetrievalConfig()

    assert cfg.reranker.small_candidate_set_threshold == 3
    assert cfg.reranker.evidence.symbolic_hit_weight == 0.0
    assert cfg.reranker.evidence.lexical_hit_weight == 0.0
    assert cfg.reranker.evidence.semantic_hit_weight == 0.0
    assert cfg.reranker.evidence.lane_fused_weight == 0.0
    assert cfg.reranker.dynamic_budget.enabled is False
    assert dict(cfg.reranker.intent_weights)["profile"].profile_source_prior == 1.0
```

- [ ] **Step 2: 再写 YAML 解析失败测试，锁定缺失 block 回退默认值**

```python
from config import load_config


def test_load_config_reranker_missing_blocks_fall_back_to_defaults(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """\
memory:
  retrieval:
    reranker:
      hybrid_top_n: 5
""",
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_file))

    assert cfg.memory.retrieval.reranker.hybrid_top_n == 5
    assert cfg.memory.retrieval.reranker.evidence.semantic_score_weight == 0.0
    assert cfg.memory.retrieval.reranker.dynamic_budget.enabled is False
```

- [ ] **Step 3: 再写 reranker contract 失败测试，锁定结构化 score 字段**

```python
from memory.recall_reranker import RecallRerankResult, SignalScoreDetail


def test_recall_rerank_result_supports_structured_score_payload():
    result = RecallRerankResult(
        selected_item_ids=["profile_1"],
        final_reason="selected profile memory",
        per_item_reason={"profile_1": "bucket=0.82 domain=1.00 keyword=0.50"},
        per_item_scores={
            "profile_1": SignalScoreDetail(
                bucket_score=0.82,
                domain_exact_score=1.0,
                keyword_exact_score=0.5,
                destination_score=0.0,
                recency_score=0.9,
                applicability_score=0.35,
                conflict_score=0.0,
                rule_score=0.71,
                evidence_score=0.0,
                source_normalized_score=1.0,
                final_score=2.0,
            )
        },
        intent_label="profile",
        selection_metrics={
            "selected_pairwise_similarity_max": None,
            "selected_pairwise_similarity_avg": None,
        },
    )

    assert result.intent_label == "profile"
    assert result.per_item_scores["profile_1"].rule_score == 0.71
```

- [ ] **Step 4: 运行定向测试并确认先失败**

Run: `pytest backend/tests/test_stage3_config.py -k reranker -v`
Expected: FAIL，因为 `MemoryRerankerConfig` 还没有 `evidence` / `dynamic_budget` / `intent_weights` 字段。

Run: `pytest backend/tests/test_recall_reranker.py -k structured_score_payload -v`
Expected: FAIL，因为 `SignalScoreDetail` 和扩展后的 `RecallRerankResult` 还不存在。

- [ ] **Step 5: 实现 config dataclass 与 parser**

```python
@dataclass(frozen=True)
class IntentWeightProfile:
    profile_source_prior: float
    slice_source_prior: float
    bucket_weight: float
    domain_weight: float
    keyword_weight: float
    destination_weight: float
    recency_weight: float
    applicability_weight: float
    conflict_weight: float


@dataclass(frozen=True)
class RerankerEvidenceConfig:
    symbolic_hit_weight: float = 0.0
    lexical_hit_weight: float = 0.0
    semantic_hit_weight: float = 0.0
    lane_fused_weight: float = 0.0
    lexical_score_weight: float = 0.0
    semantic_score_weight: float = 0.0
    destination_match_type_weight: float = 0.0


@dataclass(frozen=True)
class RerankerDynamicBudgetConfig:
    enabled: bool = False


@dataclass(frozen=True)
class MemoryRerankerConfig:
    small_candidate_set_threshold: int = 3
    profile_top_n: int = 4
    slice_top_n: int = 3
    hybrid_top_n: int = 4
    hybrid_profile_top_n: int = 2
    hybrid_slice_top_n: int = 2
    recency_half_life_days: int = 180
    intent_weights: tuple[tuple[str, IntentWeightProfile], ...] = (
        ("profile", IntentWeightProfile(1.0, 0.62, 0.34, 0.24, 0.18, 0.08, 0.06, 0.10, 1.4)),
        ("episode_slice", IntentWeightProfile(0.62, 1.0, 0.16, 0.22, 0.18, 0.24, 0.14, 0.08, 1.0)),
        ("recommend", IntentWeightProfile(0.90, 0.90, 0.22, 0.22, 0.20, 0.18, 0.10, 0.14, 1.2)),
        ("default", IntentWeightProfile(0.84, 0.84, 0.24, 0.22, 0.18, 0.14, 0.08, 0.12, 1.2)),
    )
    evidence: RerankerEvidenceConfig = field(default_factory=RerankerEvidenceConfig)
    dynamic_budget: RerankerDynamicBudgetConfig = field(default_factory=RerankerDynamicBudgetConfig)
```

```python
evidence_raw = reranker_raw.get("evidence", {})
dynamic_budget_raw = reranker_raw.get("dynamic_budget", {})

reranker = MemoryRerankerConfig(
    small_candidate_set_threshold=int(
        reranker_raw.get("small_candidate_set_threshold", 3)
    ),
    profile_top_n=int(reranker_raw.get("profile_top_n", 4)),
    slice_top_n=int(reranker_raw.get("slice_top_n", 3)),
    hybrid_top_n=int(reranker_raw.get("hybrid_top_n", 4)),
    hybrid_profile_top_n=int(reranker_raw.get("hybrid_profile_top_n", 2)),
    hybrid_slice_top_n=int(reranker_raw.get("hybrid_slice_top_n", 2)),
    recency_half_life_days=int(reranker_raw.get("recency_half_life_days", 180)),
    # Phase A/B keeps intent_weights as code-only defaults on purpose.
    intent_weights=MemoryRerankerConfig().intent_weights,
    evidence=RerankerEvidenceConfig(
        symbolic_hit_weight=float(evidence_raw.get("symbolic_hit_weight", 0.0)),
        lexical_hit_weight=float(evidence_raw.get("lexical_hit_weight", 0.0)),
        semantic_hit_weight=float(evidence_raw.get("semantic_hit_weight", 0.0)),
        lane_fused_weight=float(evidence_raw.get("lane_fused_weight", 0.0)),
        lexical_score_weight=float(evidence_raw.get("lexical_score_weight", 0.0)),
        semantic_score_weight=float(evidence_raw.get("semantic_score_weight", 0.0)),
        destination_match_type_weight=float(
            evidence_raw.get("destination_match_type_weight", 0.0)
        ),
    ),
    dynamic_budget=RerankerDynamicBudgetConfig(
        enabled=_as_bool(dynamic_budget_raw.get("enabled"), False),
    ),
)
```

- [ ] **Step 6: 实现 reranker 结果 dataclass**

```python
@dataclass
class SignalScoreDetail:
    bucket_score: float
    domain_exact_score: float
    keyword_exact_score: float
    destination_score: float
    recency_score: float
    applicability_score: float
    conflict_score: float
    symbolic_hit: float = 0.0
    lexical_hit: float = 0.0
    semantic_hit: float = 0.0
    lane_fused_score: float = 0.0
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    destination_match_type_score: float = 0.0
    rule_score: float = 0.0
    evidence_score: float = 0.0
    source_normalized_score: float = 0.0
    final_score: float = 0.0
    hard_filter: str = ""


@dataclass
class RecallRerankResult:
    selected_item_ids: list[str]
    final_reason: str
    per_item_reason: dict[str, str]
    fallback_used: str = "none"
    per_item_scores: dict[str, SignalScoreDetail] = field(default_factory=dict)
    intent_label: str = ""
    selection_metrics: dict[str, float | None] = field(default_factory=dict)
```

- [ ] **Step 7: 运行测试并确认通过**

Run: `pytest backend/tests/test_stage3_config.py backend/tests/test_recall_reranker.py -k "reranker or structured_score_payload" -v`
Expected: PASS，证明 config 解析与新 dataclass contract 已稳定。

- [ ] **Step 8: 提交**

```bash
git add backend/config.py backend/memory/recall_reranker.py backend/tests/test_stage3_config.py backend/tests/test_recall_reranker.py
git commit -m "feat: extend reranker config and score contract"
```

### Task 2: 把 Stage 3 evidence 传入 Stage 4 reranker

**Files:**
- Modify: `backend/memory/manager.py`
- Modify: `backend/tests/test_memory_manager.py`

- [ ] **Step 1: 先写失败测试，锁定 manager 会把 `evidence_by_id` 传给 reranker**

```python
@pytest.mark.asyncio
async def test_generate_context_passes_stage3_evidence_to_reranker(tmp_path, monkeypatch):
    manager = MemoryManager(data_dir=str(tmp_path))
    seen = {}

    def fake_retrieve_recall_candidates(**kwargs):
        candidate = RecallCandidate(
            source="profile",
            item_id="profile_1",
            bucket="stable_preferences",
            score=1.0,
            matched_reason=["exact domain match on hotel"],
            content_summary="hotel:preferred_area=京都四条",
            domains=["hotel"],
            applicability="适用于大多数住宿选择。",
        )
        evidence = RetrievalEvidence(
            item_id="profile_1",
            source="profile",
            lanes=["symbolic"],
            fused_score=0.8,
        )
        return Stage3RecallResult(
            candidates=[candidate],
            evidence_by_id={"profile_1": evidence},
            telemetry=Stage3Telemetry(lanes_attempted=["symbolic"], lanes_succeeded=["symbolic"]),
        )

    async def fake_select_recall_candidates(**kwargs):
        seen["evidence_by_id"] = kwargs["evidence_by_id"]
        return kwargs["candidates"], RecallRerankResult(
            selected_item_ids=["profile_1"],
            final_reason="fake",
            per_item_reason={"profile_1": "bucket=0.82 domain=1.00 keyword=0.00 destination=0.00 recency=1.00 applicability=0.35 conflict=0.00"},
        )

    monkeypatch.setattr("memory.manager.retrieve_recall_candidates", fake_retrieve_recall_candidates)
    monkeypatch.setattr("memory.manager.select_recall_candidates", fake_select_recall_candidates)

    await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_now"),
        user_message="住宿按我习惯",
        recall_gate=True,
        retrieval_plan=RecallRetrievalPlan(
            source="profile",
            buckets=["stable_preferences"],
            domains=["hotel"],
            destination="",
            keywords=["住宿"],
            top_k=5,
            reason="test",
        ),
    )

    assert "profile_1" in seen["evidence_by_id"]
```

- [ ] **Step 2: 再写失败测试，锁定 `select_recall_candidates()` 会把 evidence 继续透传给 `choose_reranker_path()`**

```python
@pytest.mark.asyncio
async def test_select_recall_candidates_forwards_evidence_to_choose_reranker_path(monkeypatch):
    seen = {}

    def fake_choose_reranker_path(**kwargs):
        seen["evidence_by_id"] = kwargs["evidence_by_id"]
        return RecallRerankPath(
            selected_candidates=kwargs["candidates"],
            result=RecallRerankResult(
                selected_item_ids=[candidate.item_id for candidate in kwargs["candidates"]],
                final_reason="fake",
                per_item_reason={},
            ),
        )

    monkeypatch.setattr("memory.manager.choose_reranker_path", fake_choose_reranker_path)

    candidates = [make_candidate(item_id="profile_1")]
    evidence_by_id = {
        "profile_1": RetrievalEvidence(
            item_id="profile_1",
            source="profile",
            lanes=["symbolic"],
            fused_score=0.8,
        )
    }

    selected, result = await select_recall_candidates(
        user_message="住宿按我习惯",
        plan=TravelPlanState(session_id="s1", trip_id="trip_now"),
        retrieval_plan=RecallRetrievalPlan(
            source="profile",
            buckets=["stable_preferences"],
            domains=["hotel"],
            destination="",
            keywords=["住宿"],
            top_k=5,
            reason="test",
        ),
        candidates=candidates,
        evidence_by_id=evidence_by_id,
    )

    assert [candidate.item_id for candidate in selected] == ["profile_1"]
    assert result.selected_item_ids == ["profile_1"]
    assert seen["evidence_by_id"] == evidence_by_id
```

- [ ] **Step 3: 再写失败测试，锁定 candidate 缺 evidence key 时不会报错**

```python
def test_choose_reranker_path_treats_missing_evidence_as_empty():
    candidate = make_candidate(item_id="profile_missing_evidence")

    path = choose_reranker_path(
        candidates=[candidate],
        user_message="住宿按我习惯",
        plan=TravelPlanState(session_id="s1", trip_id="trip_now"),
        retrieval_plan=RecallRetrievalPlan(
            source="profile",
            buckets=["stable_preferences"],
            domains=["hotel"],
            destination="",
            keywords=["住宿"],
            top_k=5,
            reason="test",
        ),
        evidence_by_id={},
    )

    assert path.result.selected_item_ids == ["profile_missing_evidence"]
```

- [ ] **Step 4: 运行定向测试并确认先失败**

Run: `pytest backend/tests/test_memory_manager.py -k stage3_evidence -v`
Expected: FAIL，因为 `select_recall_candidates()` 还没有 `evidence_by_id` 参数。

Run: `pytest backend/tests/test_recall_reranker.py -k "missing_evidence or forwards_evidence" -v`
Expected: FAIL，因为 reranker 还没有 empty-evidence fallback 逻辑，且 helper 还没有完整透传。

- [ ] **Step 5: 扩展 manager 与 reranker 调用签名**

```python
async def select_recall_candidates(
    *,
    user_message: str,
    plan: TravelPlanState,
    retrieval_plan: RecallRetrievalPlan | None,
    candidates: list[RecallCandidate],
    evidence_by_id: dict[str, RetrievalEvidence] | None = None,
    reranker_config: MemoryRerankerConfig | None = None,
) -> tuple[list[RecallCandidate], RecallRerankResult]:
    if not candidates:
        # Keep this payload byte-for-byte aligned with Task 3B `_empty_rerank_result()`
        # until the shared factory replaces this inline branch.
        return [], RecallRerankResult(
            selected_item_ids=[],
            final_reason="",
            per_item_reason={},
            selection_metrics={
                "selected_pairwise_similarity_max": None,
                "selected_pairwise_similarity_avg": None,
            },
        )
    path = choose_reranker_path(
        candidates=candidates,
        user_message=user_message,
        plan=plan,
        retrieval_plan=retrieval_plan,
        evidence_by_id=evidence_by_id or {},
        config=reranker_config,
    )
    return list(path.selected_candidates), path.result
```

```python
from memory.recall_stage3_models import RetrievalEvidence

stage3_evidence_by_id = stage3_result.evidence_by_id if stage3_result is not None else {}

selected_candidates, rerank_result = await select_recall_candidates(
    user_message=user_message,
    plan=plan,
    retrieval_plan=active_plan,
    candidates=recall_candidates,
    evidence_by_id=stage3_evidence_by_id,
    reranker_config=self.retrieval_config.reranker,
)
```

- [ ] **Step 6: 运行测试并确认通过**

Run: `pytest backend/tests/test_memory_manager.py backend/tests/test_recall_reranker.py -k "stage3_evidence or missing_evidence or forwards_evidence" -v`
Expected: PASS，证明 evidence plumbing 与 missing-key fallback 生效。

- [ ] **Step 7: 提交**

```bash
git add backend/memory/manager.py backend/memory/recall_reranker.py backend/tests/test_memory_manager.py backend/tests/test_recall_reranker.py
git commit -m "feat: pass stage3 evidence into reranker"
```

### Task 3A: 提取 rule signals 与 intent profile，保持默认行为不变

**Files:**
- Modify: `backend/memory/recall_reranker.py`
- Modify: `backend/tests/test_recall_reranker.py`

- [ ] **Step 1: 先跑 baseline，记录当前 reason 文本的精确格式**

Run:

```bash
python - <<'PY'
from memory.recall_reranker import choose_reranker_path
from memory.recall_stage3_models import RecallCandidate, RecallRetrievalPlan
from models.state import TravelPlanState

candidate = RecallCandidate(
    source="profile",
    item_id="profile_kyoto_area",
    bucket="stable_preferences",
    score=1.0,
    matched_reason=["exact domain match on hotel", "keyword match on 住宿"],
    content_summary="hotel:preferred_area=京都四条",
    domains=["hotel"],
    applicability="适用于京都住宿选择。",
)
path = choose_reranker_path(
    candidates=[candidate],
    user_message="推荐这次京都住哪里",
    plan=TravelPlanState(session_id="s1", trip_id="trip_now", destination="京都"),
    retrieval_plan=RecallRetrievalPlan(
        source="profile",
        buckets=["stable_preferences"],
        domains=["hotel"],
        destination="京都",
        keywords=["住宿"],
        top_k=5,
        reason="profile",
    ),
)
print(path.result.per_item_reason["profile_kyoto_area"])
PY
```

Expected: 输出一条当前实现生成的 `per_item_reason`，把它原样复制到下一步的 bit-stable 测试里，不要手写猜测空格或分隔符。

- [ ] **Step 2: 再写失败测试，锁定 intent dispatcher 与 reason 稳定性**

```python
def test_resolve_intent_label_preserves_current_heuristic_branches():
    assert _resolve_intent_label(
        "按我偏好选机票",
        RecallRetrievalPlan(
            source="profile",
            buckets=["constraints"],
            domains=["flight"],
            destination="",
            keywords=["机票"],
            top_k=5,
            reason="profile_constraint_recall",
        ),
    ) == "profile"

    assert _resolve_intent_label(
        "沿用上次大阪行程的节奏",
        RecallRetrievalPlan(
            source="episode_slice",
            buckets=["day_rhythm"],
            domains=["itinerary"],
            destination="大阪",
            keywords=["行程"],
            top_k=5,
            reason="past_trip_slice_lookup",
        ),
    ) == "episode_slice"

    assert _resolve_intent_label(
        "推荐这次京都住哪里比较好",
        RecallRetrievalPlan(
            source="hybrid_history",
            buckets=["stable_preferences"],
            domains=["hotel"],
            destination="京都",
            keywords=["住宿"],
            top_k=5,
            reason="recommend",
        ),
    ) == "recommend"

    assert _resolve_intent_label(
        "查查巴黎餐厅",
        RecallRetrievalPlan(
            source="hybrid_history",
            buckets=["poi_preferences"],
            domains=["food"],
            destination="巴黎",
            keywords=["餐厅"],
            top_k=5,
            reason="lookup",
        ),
    ) == "default"


def test_default_config_per_item_reason_bit_stable():
    candidate = make_candidate(
        item_id="profile_kyoto_area",
        matched_reason=["exact domain match on hotel", "keyword match on 住宿"],
        content_summary="hotel:preferred_area=京都四条",
        domains=["hotel"],
        applicability="适用于京都住宿选择。",
    )

    path = choose_reranker_path(
        candidates=[candidate],
        user_message="推荐这次京都住哪里",
        plan=TravelPlanState(session_id="s1", trip_id="trip_now", destination="京都"),
        retrieval_plan=RecallRetrievalPlan(
            source="profile",
            buckets=["stable_preferences"],
            domains=["hotel"],
            destination="京都",
            keywords=["住宿"],
            top_k=5,
            reason="profile",
        ),
        evidence_by_id={},
        config=DummyRerankerConfig(small_candidate_set_threshold=0),
    )

    assert path.result.per_item_reason["profile_kyoto_area"] == (
        "exact domain match on hotel | keyword match on 住宿 | "
        "bucket=0.82 domain=1.00 keyword=0.50 destination=1.00 "
        "recency=1.00 applicability=0.65 conflict=0.00"
    )
```

- [ ] **Step 3: 再写失败测试，锁定当前 conflict hard-drop 条件可达**

```python
def test_conflict_score_fixture_for_hard_drop_is_reachable():
    candidate = make_candidate(
        item_id="constraint_avoid_red_eye",
        bucket="constraints",
        polarity="avoid",
        matched_reason=["exact domain match on flight", "keyword match on 红眼"],
        content_summary="flight:avoid_red_eye=true",
        domains=["flight"],
        applicability="适用于所有旅行。",
    )

    assert _conflict_score(candidate, "这次可以坐红眼航班") == 1.0
```

- [ ] **Step 4: 运行定向测试并确认先失败**

Run: `pytest backend/tests/test_recall_reranker.py -k "resolve_intent_label or bit_stable or hard_drop_is_reachable" -v`
Expected: FAIL，因为 `_resolve_intent_label()` 还不存在，且 reason stability 还没有显式测试保护。

- [ ] **Step 5: 实现 intent dispatcher、rule signal 计算与 hard-filter 写入策略**

```python
def _resolve_intent_label(
    user_message: str,
    retrieval_plan: RecallRetrievalPlan | None,
) -> str:
    text = user_message or ""
    reason = (retrieval_plan.reason if retrieval_plan is not None else "").lower()
    source = retrieval_plan.source if retrieval_plan is not None else ""
    if source == "profile" or "profile_" in reason:
        return "profile"
    if source == "episode_slice" or "past_trip" in reason:
        return "episode_slice"
    if any(word in text for word in ("推荐", "比较好", "适合我", "怎么安排")):
        return "recommend"
    return "default"


def _resolve_intent_profile(
    user_message: str,
    retrieval_plan: RecallRetrievalPlan | None,
    config: MemoryRerankerConfig,
) -> IntentWeightProfile:
    intent_label = _resolve_intent_label(user_message, retrieval_plan)
    profiles = dict(config.intent_weights)
    return profiles.get(intent_label, profiles["default"])
```

```python
def _compute_rule_signals(
    candidate: RecallCandidate,
    user_message: str,
    plan: TravelPlanState,
    retrieval_plan: RecallRetrievalPlan | None,
    reranker_config: MemoryRerankerConfig,
) -> SignalScoreDetail:
    bucket_score = _bucket_prior(candidate)
    domain_score = _jaccard(
        set(retrieval_plan.domains if retrieval_plan is not None else []),
        set(candidate.domains),
    )
    keyword_score = _keyword_overlap(candidate, retrieval_plan)
    destination_score = _destination_match(candidate, plan, retrieval_plan)
    recency_score = _recency_score(candidate, reranker_config.recency_half_life_days)
    applicability_score = _applicability_score(candidate, plan, user_message)
    conflict_score = _conflict_score(candidate, user_message)
    return SignalScoreDetail(
        bucket_score=bucket_score,
        domain_exact_score=domain_score,
        keyword_exact_score=keyword_score,
        destination_score=destination_score,
        recency_score=recency_score,
        applicability_score=applicability_score,
        conflict_score=conflict_score,
    )
```

```python
def _build_reason_text(candidate: RecallCandidate, detail: SignalScoreDetail) -> str:
    return (
        f"{_matched_reason_text(candidate)} | bucket={detail.bucket_score:.2f} "
        f"domain={detail.domain_exact_score:.2f} keyword={detail.keyword_exact_score:.2f} "
        f"destination={detail.destination_score:.2f} recency={detail.recency_score:.2f} "
        f"applicability={detail.applicability_score:.2f} conflict={detail.conflict_score:.2f}"
    )


def _passes_hard_filter(detail: SignalScoreDetail) -> tuple[bool, str]:
    # Current _conflict_score is binary {0.0, 1.0}; 0.95 keeps a safety margin
    # if conflict becomes continuous later.
    if detail.conflict_score >= 0.95:
        return False, "conflict"
    weak_relevance = (
        detail.domain_exact_score <= 0.0
        and detail.keyword_exact_score <= 0.0
        and detail.destination_score <= 0.0
        and detail.applicability_score <= 0.35
    )
    if weak_relevance:
        return False, "weak_relevance"
    return True, ""
```

```python
per_item_scores[candidate.item_id] = detail
keep, hard_filter = _passes_hard_filter(detail)
detail.hard_filter = hard_filter
if not keep:
    suffix = "dropped as conflict" if hard_filter == "conflict" else "dropped as weak relevance"
    per_item_reason[candidate.item_id] = f"{reason} | {suffix}"
    continue
```

- [ ] **Step 6: 运行测试并确认通过**

Run: `pytest backend/tests/test_recall_reranker.py -k "resolve_intent_label or bit_stable or hard_drop_is_reachable" -v`
Expected: PASS，intent 分支、reason 文本与 hard-filter 可达性都被锁定。

- [ ] **Step 7: 提交**

```bash
git add backend/memory/recall_reranker.py backend/tests/test_recall_reranker.py
git commit -m "refactor: extract rule signals for reranker"
```

### Task 3B: 接入 evidence signals，并保持零权重下排序不变

**Files:**
- Modify: `backend/memory/recall_reranker.py`
- Modify: `backend/tests/test_recall_reranker.py`

- [ ] **Step 1: 先写失败测试，锁定 evidence 归一化、placeholder 与零权重不变性**

```python
def test_normalize_optional_scores_ignores_missing_values():
    values = {"a": 0.4, "b": None, "c": 0.9}

    normalized = _normalize_optional_scores(values)

    assert normalized["c"] == 1.0
    assert normalized["a"] == 0.0
    assert normalized["b"] == 0.0


def test_normalize_optional_scores_single_value_returns_one():
    normalized = _normalize_optional_scores({"a": None, "b": 0.55})

    assert normalized["a"] == 0.0
    assert normalized["b"] == 1.0


def test_selection_metrics_placeholder_is_always_present():
    result = _empty_rerank_result()

    assert result.selection_metrics == {
        "selected_pairwise_similarity_max": None,
        "selected_pairwise_similarity_avg": None,
    }


def test_destination_match_type_score_mapping_covers_supported_labels():
    assert DESTINATION_MATCH_TYPE_SCORE == {
        "exact": 1.0,
        "alias": 0.8,
        "parent_child": 0.6,
        "region_weak": 0.3,
        "none": 0.0,
        "": 0.0,
    }
```

- [ ] **Step 2: 再写失败测试，锁定 hard-dropped item 仍进入 `per_item_scores`**

```python
def test_hard_dropped_candidate_still_keeps_score_detail_for_trace():
    candidate = make_candidate(
        item_id="constraint_avoid_red_eye",
        bucket="constraints",
        polarity="avoid",
        matched_reason=["exact domain match on flight", "keyword match on 红眼"],
        content_summary="flight:avoid_red_eye=true",
        domains=["flight"],
        applicability="适用于所有旅行。",
    )

    path = choose_reranker_path(
        candidates=[candidate],
        user_message="这次可以坐红眼航班",
        plan=TravelPlanState(session_id="s1", trip_id="trip_now"),
        retrieval_plan=RecallRetrievalPlan(
            source="profile",
            buckets=["constraints"],
            domains=["flight"],
            destination="",
            keywords=["红眼", "航班"],
            top_k=5,
            reason="profile",
        ),
        evidence_by_id={},
        config=DummyRerankerConfig(small_candidate_set_threshold=0),
    )

    assert path.result.selected_item_ids == []
    assert path.result.per_item_scores["constraint_avoid_red_eye"].hard_filter == "conflict"
```

- [ ] **Step 3: 再写失败测试，锁定零权重 evidence 不改变 hybrid 排序**

```python
def test_evidence_scores_do_not_change_order_when_all_evidence_weights_are_zero():
    candidates = [
        make_candidate(
            item_id="profile_kyoto_area",
            matched_reason=["exact domain match on hotel", "keyword match on 住宿"],
            content_summary="hotel:preferred_area=京都四条",
            domains=["hotel"],
            applicability="适用于京都住宿选择。",
        ),
        make_candidate(
            source="episode_slice",
            item_id="slice_kyoto_machiya",
            bucket="stay_choice",
            matched_reason=["exact destination match on 京都", "keyword match on 住宿"],
            content_summary="上次京都住四条附近的町屋。",
            domains=["hotel"],
            applicability="仅供住宿选择参考。",
            polarity="",
        ),
    ]
    evidence_by_id = {
        "profile_kyoto_area": RetrievalEvidence(
            item_id="profile_kyoto_area",
            source="profile",
            lanes=["symbolic"],
            fused_score=0.5,
        ),
        "slice_kyoto_machiya": RetrievalEvidence(
            item_id="slice_kyoto_machiya",
            source="episode_slice",
            lanes=["symbolic", "semantic"],
            fused_score=0.7,
            semantic_score=0.88,
        ),
    }

    path = choose_reranker_path(
        candidates=candidates,
        user_message="推荐这次京都住哪里",
        plan=TravelPlanState(session_id="s1", trip_id="trip_now", destination="京都"),
        retrieval_plan=RecallRetrievalPlan(
            source="hybrid_history",
            buckets=["stable_preferences"],
            domains=["hotel"],
            destination="京都",
            keywords=["住宿"],
            top_k=5,
            reason="recommend",
        ),
        evidence_by_id=evidence_by_id,
        config=DummyRerankerConfig(small_candidate_set_threshold=0),
    )

    assert path.result.selected_item_ids == ["profile_kyoto_area", "slice_kyoto_machiya"]
    assert path.result.per_item_scores["slice_kyoto_machiya"].semantic_score > 0.0
    assert path.result.per_item_scores["slice_kyoto_machiya"].evidence_score == 0.0
```

- [ ] **Step 4: 实现 evidence signal 计算与 placeholder**

```python
def _empty_rerank_result() -> RecallRerankResult:
    return RecallRerankResult(
        selected_item_ids=[],
        final_reason="",
        per_item_reason={},
        fallback_used="none",
        per_item_scores={},
        intent_label="",
        selection_metrics={
            "selected_pairwise_similarity_max": None,
            "selected_pairwise_similarity_avg": None,
        },
    )


DESTINATION_MATCH_TYPE_SCORE = {
    "exact": 1.0,
    "alias": 0.8,
    "parent_child": 0.6,
    "region_weak": 0.3,
    "none": 0.0,
    "": 0.0,
}


def _normalize_optional_scores(values: dict[str, float | None]) -> dict[str, float]:
    present = {key: value for key, value in values.items() if value is not None}
    if not present:
        return {key: 0.0 for key in values}
    if len(present) == 1:
        only_key = next(iter(present))
        return {key: 1.0 if key == only_key else 0.0 for key in values}
    low = min(present.values())
    high = max(present.values())
    if math.isclose(low, high):
        return {key: (1.0 if value is not None else 0.0) for key, value in values.items()}
    return {
        key: ((value - low) / (high - low) if value is not None else 0.0)
        for key, value in values.items()
    }
```

```python
# Replace the inline empty-result branch from Task 2 with the shared factory here
# so selection_metrics placeholder cannot drift across call sites.
if not candidates:
    return [], _empty_rerank_result()


def _compute_evidence_signals(
    detail: SignalScoreDetail,
    evidence: RetrievalEvidence,
    normalized_fused_score: float,
    normalized_lexical_score: float,
    normalized_semantic_score: float,
    config: MemoryRerankerConfig,
) -> SignalScoreDetail:
    evidence_cfg = config.evidence
    detail.symbolic_hit = 1.0 if "symbolic" in evidence.lanes else 0.0
    detail.lexical_hit = 1.0 if "lexical" in evidence.lanes else 0.0
    detail.semantic_hit = 1.0 if "semantic" in evidence.lanes else 0.0
    detail.lane_fused_score = normalized_fused_score
    detail.lexical_score = normalized_lexical_score
    detail.semantic_score = normalized_semantic_score
    detail.destination_match_type_score = DESTINATION_MATCH_TYPE_SCORE.get(
        evidence.destination_match_type or "",
        0.0,
    )
    detail.evidence_score = (
        evidence_cfg.symbolic_hit_weight * detail.symbolic_hit
        + evidence_cfg.lexical_hit_weight * detail.lexical_hit
        + evidence_cfg.semantic_hit_weight * detail.semantic_hit
        + evidence_cfg.lane_fused_weight * detail.lane_fused_score
        + evidence_cfg.lexical_score_weight * detail.lexical_score
        + evidence_cfg.semantic_score_weight * detail.semantic_score
        + evidence_cfg.destination_match_type_weight * detail.destination_match_type_score
    )
    return detail
```

- [ ] **Step 5: 运行测试并确认通过**

Run: `pytest backend/tests/test_recall_reranker.py -k "normalize_optional_scores or selection_metrics_placeholder or hard_dropped_candidate or all_evidence_weights_are_zero" -v`
Expected: PASS，证明 evidence 归一化、placeholder 和 zero-weight invariance 已落地。注意：默认 `Stage3SemanticConfig.enabled=False`，所以真实默认路径里 `detail.semantic_score` 大多数时候仍为 `0.0`；这个测试通过显式构造 semantic evidence 覆盖该分支。

- [ ] **Step 6: 提交**

```bash
git add backend/memory/recall_reranker.py backend/tests/test_recall_reranker.py
git commit -m "feat: add zero-weight evidence signals to reranker"
```

### Task 3C: 落地 source-aware normalization 与最终选择流程

**Files:**
- Modify: `backend/memory/recall_reranker.py`
- Modify: `backend/tests/test_recall_reranker.py`

- [ ] **Step 1: 先写失败测试，锁定 source-aware normalization 公式**

```python
def test_normalize_source_scores_uses_per_source_min_max_plus_source_prior():
    scored = [
        _ScoredCandidate(
            candidate=make_candidate(item_id="profile_1"),
            source_score=0.5,
            normalized_score=0.5,
            final_score=0.5,
            duplicate_group="profile:1",
            conflict_score=0.0,
            weak_relevance=False,
            reason="r1",
            score_detail=SignalScoreDetail(
                bucket_score=0.82,
                domain_exact_score=1.0,
                keyword_exact_score=0.5,
                destination_score=0.0,
                recency_score=1.0,
                applicability_score=0.35,
                conflict_score=0.0,
            ),
        ),
        _ScoredCandidate(
            candidate=make_candidate(item_id="profile_2"),
            source_score=0.9,
            normalized_score=0.9,
            final_score=0.9,
            duplicate_group="profile:2",
            conflict_score=0.0,
            weak_relevance=False,
            reason="r2",
            score_detail=SignalScoreDetail(
                bucket_score=0.82,
                domain_exact_score=1.0,
                keyword_exact_score=0.5,
                destination_score=0.0,
                recency_score=1.0,
                applicability_score=0.35,
                conflict_score=0.0,
            ),
        ),
    ]

    normalized = _normalize_source_scores(scored, source_prior=1.0)

    assert normalized[0].candidate.item_id == "profile_2"
    assert normalized[0].score_detail.source_normalized_score == 1.0
    assert normalized[0].score_detail.final_score == 2.0


def test_build_scored_candidate_source_score_equals_rule_plus_evidence():
    detail = SignalScoreDetail(
        bucket_score=0.82,
        domain_exact_score=1.0,
        keyword_exact_score=0.5,
        destination_score=1.0,
        recency_score=1.0,
        applicability_score=0.65,
        conflict_score=0.0,
        rule_score=0.71,
        evidence_score=0.19,
    )

    scored = _build_scored_candidate(
        candidate=make_candidate(item_id="profile_1"),
        detail=detail,
        duplicate_group="profile:1",
        reason="r1",
    )

    assert scored.source_score == pytest.approx(0.90)
```

- [ ] **Step 2: 再写失败测试，锁定 hybrid 默认行为与 selection_metrics placeholder 并存**

```python
def test_choose_reranker_path_hybrid_default_config_keeps_selected_ids_and_placeholder_metrics():
    candidates = [
        make_candidate(item_id="profile_kyoto_area", domains=["hotel"]),
        make_candidate(
            source="episode_slice",
            item_id="slice_kyoto_machiya",
            bucket="stay_choice",
            matched_reason=["exact destination match on 京都", "keyword match on 住宿"],
            content_summary="上次京都住四条附近的町屋。",
            domains=["hotel"],
            applicability="仅供住宿选择参考。",
            polarity="",
        ),
    ]

    path = choose_reranker_path(
        candidates=candidates,
        user_message="推荐这次京都住哪里",
        plan=TravelPlanState(session_id="s1", trip_id="trip_now", destination="京都"),
        retrieval_plan=RecallRetrievalPlan(
            source="hybrid_history",
            buckets=["stable_preferences"],
            domains=["hotel"],
            destination="京都",
            keywords=["住宿"],
            top_k=5,
            reason="recommend",
        ),
        evidence_by_id={},
        config=DummyRerankerConfig(small_candidate_set_threshold=0),
    )

    assert path.result.selected_item_ids == ["profile_kyoto_area", "slice_kyoto_machiya"]
    assert path.result.selection_metrics == {
        "selected_pairwise_similarity_max": None,
        "selected_pairwise_similarity_avg": None,
    }
```

- [ ] **Step 3: 实现最终打分与选择流程**

```python
@dataclass(frozen=True)
class _ScoredCandidate:
    candidate: RecallCandidate
    source_score: float
    normalized_score: float
    final_score: float
    duplicate_group: str
    conflict_score: float
    weak_relevance: bool
    reason: str
    score_detail: SignalScoreDetail
```

```python
def _finalize_source_score(
    detail: SignalScoreDetail,
    weights: IntentWeightProfile,
) -> SignalScoreDetail:
    detail.rule_score = (
        weights.bucket_weight * detail.bucket_score
        + weights.domain_weight * detail.domain_exact_score
        + weights.keyword_weight * detail.keyword_exact_score
        + weights.destination_weight * detail.destination_score
        + weights.recency_weight * detail.recency_score
        + weights.applicability_weight * detail.applicability_score
        - weights.conflict_weight * detail.conflict_score
    )
    return detail
```

```python
def _build_scored_candidate(
    candidate: RecallCandidate,
    detail: SignalScoreDetail,
    *,
    duplicate_group: str,
    reason: str,
) -> _ScoredCandidate:
    source_score = detail.rule_score + detail.evidence_score
    return _ScoredCandidate(
        candidate=candidate,
        source_score=source_score,
        normalized_score=0.0,
        final_score=0.0,
        duplicate_group=duplicate_group,
        conflict_score=detail.conflict_score,
        weak_relevance=False,
        reason=reason,
        score_detail=detail,
    )
```

```python
def _normalize_source_scores(
    scored_candidates: list[_ScoredCandidate],
    *,
    source_prior: float,
) -> list[_ScoredCandidate]:
    if not scored_candidates:
        return []
    values = [candidate.source_score for candidate in scored_candidates]
    max_score = max(values)
    min_score = min(values)
    normalized: list[_ScoredCandidate] = []
    for scored in scored_candidates:
        norm = 1.0 if math.isclose(max_score, min_score) else (
            (scored.source_score - min_score) / (max_score - min_score)
        )
        detail = replace(
            scored.score_detail,
            source_normalized_score=norm,
            final_score=source_prior + norm,
        )
        normalized.append(
            _ScoredCandidate(
                candidate=scored.candidate,
                source_score=scored.source_score,
                normalized_score=norm,
                final_score=detail.final_score,
                duplicate_group=scored.duplicate_group,
                conflict_score=scored.conflict_score,
                weak_relevance=scored.weak_relevance,
                reason=scored.reason,
                score_detail=detail,
            )
        )
    normalized.sort(key=lambda item: (-item.final_score, item.candidate.item_id))
    return normalized
```

```python
intent_profile = _resolve_intent_profile(user_message, retrieval_plan, reranker_config)
intent_label = _resolve_intent_label(user_message, retrieval_plan)
profile_scored: list[_ScoredCandidate] = []
slice_scored: list[_ScoredCandidate] = []

for candidate in candidates:
    detail = _compute_rule_signals(
        candidate,
        user_message,
        plan,
        retrieval_plan,
        reranker_config,
    )
    detail = _finalize_source_score(detail, intent_profile)
    evidence = _candidate_evidence(candidate, evidence_by_id)
    detail = _compute_evidence_signals(
        detail,
        evidence,
        normalized_fused_scores.get(candidate.item_id, 0.0),
        normalized_lexical_scores.get(candidate.item_id, 0.0),
        normalized_semantic_scores.get(candidate.item_id, 0.0),
        reranker_config,
    )
    reason = _build_reason_text(candidate, detail)
    per_item_scores[candidate.item_id] = detail
    keep, hard_filter = _passes_hard_filter(detail)
    detail.hard_filter = hard_filter
    if not keep:
        suffix = (
            "dropped as conflict"
            if hard_filter == "conflict"
            else "dropped as weak relevance"
        )
        per_item_reason[candidate.item_id] = f"{reason} | {suffix}"
        continue
    scored = _build_scored_candidate(
        candidate,
        detail,
        duplicate_group=_duplicate_group(candidate),
        reason=reason,
    )
    if candidate.source == "profile":
        profile_scored.append(scored)
    else:
        slice_scored.append(scored)

deduped_profile = _dedupe_candidates(profile_scored, per_item_reason)
deduped_slices = _dedupe_candidates(slice_scored, per_item_reason)
normalized_profile = _normalize_source_scores(
    deduped_profile,
    source_prior=intent_profile.profile_source_prior,
)
normalized_slices = _normalize_source_scores(
    deduped_slices,
    source_prior=intent_profile.slice_source_prior,
)
selected = _select_candidates(
    normalized_profile,
    normalized_slices,
    retrieval_plan,
    reranker_config,
)
selected_candidates = [scored.candidate for scored in selected]
profile_count = sum(1 for candidate in selected_candidates if candidate.source == "profile")
slice_count = len(selected_candidates) - profile_count
final_reason = (
    "source-aware weighted rerank selected "
    f"{len(selected_candidates)} items ({profile_count} profile, {slice_count} slice)"
)
result = RecallRerankResult(
    selected_item_ids=[candidate.item_id for candidate in selected_candidates],
    final_reason=final_reason,
    per_item_reason=per_item_reason,
    fallback_used="none",
    per_item_scores={item_id: detail for item_id, detail in per_item_scores.items()},
    intent_label=intent_label,
    selection_metrics={
        "selected_pairwise_similarity_max": None,
        "selected_pairwise_similarity_avg": None,
    },
)
```

- [ ] **Step 4: 先跑 deterministic reranker eval，确认 golden contract 仍只锁定 selected ids / final_reason**

Run: `pytest backend/tests/test_reranker_eval.py -v`
Expected: PASS。当前 deterministic eval 只校验 `selected_item_ids`、`candidate_count` 和 `final_reason`，不对 `per_item_reason` 做 snapshot；本阶段不要把 `per_item_reason` 写入 `backend/evals/reranker_cases/*.yaml`。

- [ ] **Step 5: 再跑核心 reranker 测试**

Run: `pytest backend/tests/test_recall_reranker.py backend/tests/test_reranker_eval.py -v`
Expected: PASS，默认 config 下 `selected_item_ids` 稳定，source-aware normalization 与 placeholder metrics 全部通过。

- [ ] **Step 6: 提交**

```bash
git add backend/memory/recall_reranker.py backend/tests/test_recall_reranker.py
git commit -m "feat: finalize source-aware reranker selection"
```

### Task 4: 扩展 recall telemetry、trace 和前端类型

**Files:**
- Modify: `backend/memory/formatter.py`
- Modify: `backend/main.py`
- Modify: `backend/telemetry/stats.py`
- Modify: `backend/api/trace.py`
- Modify: `frontend/src/types/trace.ts`
- Modify: `frontend/src/types/plan.ts`
- Modify: `frontend/src/components/TraceViewer.tsx`
- Modify: `backend/tests/test_memory_formatter.py`
- Modify: `backend/tests/test_trace_api.py`
- Modify: `backend/tests/test_stats.py`

- [ ] **Step 1: 先写失败测试，锁定 telemetry `to_dict()` 新字段**

```python
def test_memory_recall_telemetry_to_dict_includes_structured_reranker_fields():
    telemetry = MemoryRecallTelemetry(
        candidate_count=2,
        reranker_selected_ids=["profile_1"],
        reranker_final_reason="selected profile memory",
        reranker_fallback="none",
        reranker_per_item_reason={"profile_1": "bucket=0.82 domain=1.00 keyword=0.50"},
        reranker_per_item_scores={
            "profile_1": {
                "rule_score": 0.71,
                "evidence_score": 0.0,
                "final_score": 2.0,
            }
        },
        reranker_intent_label="profile",
        reranker_selection_metrics={
            "selected_pairwise_similarity_max": None,
            "selected_pairwise_similarity_avg": None,
        },
    )

    payload = telemetry.to_dict()

    assert payload["reranker_intent_label"] == "profile"
    assert payload["reranker_per_item_scores"]["profile_1"]["final_score"] == 2.0
```

- [ ] **Step 2: 再写失败测试，锁定 trace/stats 暴露这些字段**

```python
@pytest.mark.asyncio
async def test_trace_recall_telemetry_includes_structured_reranker_fields(app):
    from telemetry.stats import RecallTelemetryRecord

    sessions = _get_sessions(app)
    session_id = "test-reranker-structured-trace"
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        duration_ms=200.0,
        phase=1,
        iteration=1,
    )
    stats.recall_telemetry.append(
        RecallTelemetryRecord(
            stage0_decision="undecided",
            stage0_reason="needs_llm_gate",
            gate_needs_recall=True,
            gate_intent_type="recommend",
            final_recall_decision="query_recall_enabled",
            fallback_used="none",
            query_plan_source="llm",
            candidate_count=2,
            recall_attempted_but_zero_hit=False,
            reranker_selected_ids=["profile_1"],
            reranker_final_reason="selected profile memory",
            reranker_fallback="none",
            reranker_per_item_scores={"profile_1": {"rule_score": 0.71, "final_score": 2.0}},
            reranker_intent_label="recommend",
            reranker_selection_metrics={
                "selected_pairwise_similarity_max": None,
                "selected_pairwise_similarity_avg": None,
            },
            timestamp=stats.llm_calls[-1].timestamp,
        )
    )
    sessions[session_id] = {
        "stats": stats,
        "messages": [],
        "plan": None,
        "compression_events": [],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")

    recall = resp.json()["iterations"][0]["memory_recall"]
    assert recall["reranker_intent_label"] == "recommend"
    assert recall["reranker_per_item_scores"]["profile_1"]["rule_score"] == 0.71
    assert recall["reranker_selection_metrics"]["selected_pairwise_similarity_max"] is None
```

- [ ] **Step 3: 实现 formatter / main / stats / trace 字段扩展**

```python
from dataclasses import asdict, dataclass, field


@dataclass
class MemoryRecallTelemetry:
    sources: dict[str, int] = field(default_factory=dict)
    profile_ids: list[str] = field(default_factory=list)
    working_memory_ids: list[str] = field(default_factory=list)
    slice_ids: list[str] = field(default_factory=list)
    matched_reasons: list[str] = field(default_factory=list)
    stage0_decision: str = ""
    stage0_reason: str = ""
    stage0_matched_rule: str = ""
    stage0_signals: dict[str, list[str]] = field(default_factory=dict)
    gate_needs_recall: bool | None = None
    gate_intent_type: str = ""
    gate_confidence: float | None = None
    gate_reason: str = ""
    final_recall_decision: str = ""
    fallback_used: str = ""
    recall_skip_source: str = ""
    query_plan_source: str = ""
    candidate_count: int = 0
    recall_attempted_but_zero_hit: bool = False
    reranker_selected_ids: list[str] = field(default_factory=list)
    reranker_final_reason: str = ""
    reranker_fallback: str = ""
    reranker_per_item_reason: dict[str, str] = field(default_factory=dict)
    reranker_per_item_scores: dict[str, dict[str, float | str | None]] = field(
        default_factory=dict
    )
    reranker_intent_label: str = ""
    reranker_selection_metrics: dict[str, float | None] = field(default_factory=dict)
```

```python
telemetry.reranker_per_item_scores = {
    item_id: asdict(detail) for item_id, detail in rerank_result.per_item_scores.items()
}
telemetry.reranker_intent_label = rerank_result.intent_label
telemetry.reranker_selection_metrics = dict(rerank_result.selection_metrics)
```

```python
return RecallTelemetryRecord(
    stage0_decision=memory_recall.stage0_decision,
    stage0_reason=memory_recall.stage0_reason,
    stage0_matched_rule=memory_recall.stage0_matched_rule,
    stage0_signals=dict(memory_recall.stage0_signals),
    gate_needs_recall=memory_recall.gate_needs_recall,
    gate_intent_type=memory_recall.gate_intent_type,
    final_recall_decision=memory_recall.final_recall_decision,
    fallback_used=memory_recall.fallback_used,
    recall_skip_source=memory_recall.recall_skip_source,
    query_plan_source=memory_recall.query_plan_source,
    candidate_count=memory_recall.candidate_count,
    recall_attempted_but_zero_hit=memory_recall.recall_attempted_but_zero_hit,
    reranker_selected_ids=list(memory_recall.reranker_selected_ids),
    reranker_final_reason=memory_recall.reranker_final_reason,
    reranker_fallback=memory_recall.reranker_fallback,
    reranker_per_item_reason=dict(memory_recall.reranker_per_item_reason),
    reranker_per_item_scores=dict(memory_recall.reranker_per_item_scores),
    reranker_intent_label=memory_recall.reranker_intent_label,
    reranker_selection_metrics=dict(memory_recall.reranker_selection_metrics),
)
```

- [ ] **Step 4: 扩展前端 trace 类型与最小展示**

```ts
export interface MemoryRecallTelemetry {
  sources?: Record<string, number>
  profile_ids?: string[]
  working_memory_ids?: string[]
  slice_ids?: string[]
  matched_reasons?: string[]
  stage0_decision?: string
  stage0_reason?: string
  stage0_matched_rule?: string
  stage0_signals?: Record<string, string[]>
  gate_needs_recall?: boolean | null
  gate_intent_type?: string
  gate_confidence?: number | null
  gate_reason?: string
  final_recall_decision?: string
  fallback_used?: string
  recall_skip_source?: string
  query_plan_source?: string
  candidate_count?: number
  recall_attempted_but_zero_hit?: boolean
  reranker_selected_ids?: string[]
  reranker_final_reason?: string
  reranker_fallback?: string
  reranker_per_item_reason?: Record<string, string>
  reranker_per_item_scores?: Record<string, Record<string, number | string | null>>
  reranker_intent_label?: string
  reranker_selection_metrics?: Record<string, number | null>
}
```

```tsx
{iteration.memory_recall?.reranker_intent_label && (
  <div className="trace-memory-recall-detail">
    intent {iteration.memory_recall.reranker_intent_label}
  </div>
)}
```

- [ ] **Step 5: 运行测试与前端构建**

Run: `pytest backend/tests/test_memory_formatter.py backend/tests/test_trace_api.py backend/tests/test_stats.py -v`
Expected: PASS，新的 telemetry 字段进入 `to_dict()`、trace API 与 stats。

Run: `cd frontend && npm run build`
Expected: PASS，TypeScript 类型与 `TraceViewer` 渲染通过。

- [ ] **Step 6: 提交**

```bash
git add backend/memory/formatter.py backend/main.py backend/telemetry/stats.py backend/api/trace.py frontend/src/types/trace.ts frontend/src/types/plan.ts frontend/src/components/TraceViewer.tsx backend/tests/test_memory_formatter.py backend/tests/test_trace_api.py backend/tests/test_stats.py
git commit -m "feat: expose structured reranker telemetry"
```

### Task 5: 补齐集成测试、回归测试和项目总览

**Files:**
- Modify: `backend/tests/test_memory_integration.py`
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: 先写 SSE 集成测试，锁定 `memory_recall` 事件带上新字段**

```python
@pytest.mark.asyncio
async def test_chat_stream_emits_structured_reranker_fields(monkeypatch, app):
    memory_mgr = _get_closure_value(app, "memory_mgr")

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        return "暂无相关用户记忆", MemoryRecallTelemetry(
            candidate_count=2,
            reranker_selected_ids=["profile_1"],
            reranker_final_reason="selected profile memory",
            reranker_fallback="none",
            reranker_per_item_reason={
                "profile_1": "bucket=0.82 domain=1.00 keyword=0.50"
            },
            reranker_per_item_scores={
                "profile_1": {
                    "rule_score": 0.71,
                    "evidence_score": 0.0,
                    "final_score": 2.0,
                }
            },
            reranker_intent_label="profile",
            reranker_selection_metrics={
                "selected_pairwise_similarity_max": None,
                "selected_pairwise_similarity_avg": None,
            },
        )

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="继续")
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住宿按我习惯", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"type": "memory_recall"' in resp.text
    assert '"reranker_intent_label": "profile"' in resp.text
    assert '"reranker_per_item_scores"' in resp.text
```

- [ ] **Step 2: 更新 `PROJECT_OVERVIEW.md`**

```md
- Memory System：同步 recall 采用 `Stage 0` 硬规则短路 + `Stage 1` recall gate + `Stage 2` retrieval plan + `Stage 3` candidate generation；Stage 3 会返回 `RecallCandidate[]` 与 `evidence_by_id` sidecar。Stage 4 reranker 在默认配置下保持规则主干，并在 feature flag / 非零 evidence 权重下消费 lexical / semantic / fused evidence；trace 与 stats 会记录 `reranker_selected_ids`、`reranker_per_item_scores`、`reranker_intent_label` 和 `reranker_selection_metrics`。
```

- [ ] **Step 3: 运行完整回归子集**

Run: `pytest backend/tests/test_recall_reranker.py backend/tests/test_memory_manager.py backend/tests/test_memory_formatter.py backend/tests/test_trace_api.py backend/tests/test_stats.py backend/tests/test_memory_integration.py backend/tests/test_stage3_config.py -v`
Expected: PASS，证明 config、manager、reranker、telemetry、trace、SSE 全链路稳定。

- [ ] **Step 4: 提交**

```bash
git add backend/tests/test_memory_integration.py PROJECT_OVERVIEW.md
git commit -m "docs: align overview with reranker evidence pipeline"
```

---

## Self-Review

- **Spec coverage:** 已覆盖 spec 中必须实现的 7 个关键点：
  - evidence 缺 key fallback：Task 2
  - optional evidence score 归一化规则：Task 3B
  - conflict 双层语义：Task 3A + 3B
  - source-aware normalization 公式：Task 3C
  - Phase A 默认兼容性承诺：Task 3A + 3C
  - intent_weights code-default + code-only lookup：Task 1 + Task 3A
  - selection metrics placeholder：Task 3B + Task 4
- **Placeholder scan:** 计划内没有占位步骤或“后续再补”式描述；所有改动都给了明确文件、代码片段和命令。
- **Type consistency:** `evidence_by_id`、`SignalScoreDetail`、`reranker_per_item_scores`、`reranker_intent_label`、`reranker_selection_metrics` 在 config、manager、formatter、stats、trace、前端类型中名称一致。
- **Golden contract:** deterministic reranker eval 继续只锁定 `selected_item_ids`、`candidate_count` 和 `final_reason`；`per_item_reason` 的文本稳定性由 `backend/tests/test_recall_reranker.py` 的 bit-stable 单测保护，不进入 golden YAML。
- **Deferred safety rail:** `evidence_score_cap` 故意未纳入本计划；默认权重为 0，但在 Phase B 正式放量任何非零 `evidence_*_weight` 之前，必须先补这个 safety rail，否则错误配置会直接污染排序。
