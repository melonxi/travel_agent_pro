# Travel Memory Stage 4 Reranker Upgrade v2

## 背景

当前旅行 Agent 的记忆召回主链路已经稳定为 recall-first：

1. Stage 0/1 做 short-circuit + recall gate，判断本轮是否值得查历史记忆。
2. Stage 2 产出 `RecallRetrievalPlan`。
3. Stage 3 生成 `RecallCandidate[]`，并附带 `evidence_by_id`。
4. Stage 4 reranker 对候选做过滤、去重、排序、裁剪，再注入 prompt 与 telemetry。

本 spec 只讨论 Stage 4。它的目标不是扩大候选池，也不是重写 memory 写入与归档流程，而是让 reranker 和当前已经落地的 Stage 3 mixed retrieval 实现真正接上：在保持默认行为可回滚的前提下，逐步从“纯规则排序器”升级为“规则主干 + Stage 3 evidence 感知 + 结构化 telemetry”的精排层。

## 当前系统事实

这份 spec 必须以当前仓库实现为准，而不是假设一个未落地的理想系统：

### 1. Stage 3 已经不是单纯 symbolic-only 架构

代码中已经存在并测试覆盖：

- `SymbolicLane`
- `LexicalLane`
- `SemanticLane`
- `Stage3RecallResult.evidence_by_id`
- `RetrievalEvidence.semantic_score / lexical_score / lane_scores / lane_ranks / retrieval_reason`

对应实现位于：

- `backend/memory/recall_stage3.py`
- `backend/memory/recall_stage3_lanes.py`
- `backend/memory/recall_stage3_fusion.py`
- `backend/memory/recall_stage3_models.py`

### 2. 当前默认生产行为仍然是 symbolic-first compatibility mode

`config.yaml` 没有显式开启 `memory.retrieval.stage3` 或 `memory.retrieval.reranker` 的高级配置，因此当前默认行为仍然接近：

- Stage 3: symbolic lane enabled
- lexical / semantic / entity / temporal: disabled
- destination normalization: disabled
- source widening: disabled
- Stage 4: 旧版规则 reranker

因此任何 Stage 4 升级都必须满足：

- 在 `evidence_by_id` 为空或只有 symbolic evidence 时，默认结果尽量保持现状。
- 只有在显式开启 Stage 3 lexical / semantic lane 之后，Stage 4 才能吃到额外语义证据。

### 3. 当前 embedding runtime 已经存在，Stage 4 不应再发明第二套

项目已经有可复用的 embedding 基础设施：

- `backend/memory/embedding_provider.py`
- `FastEmbedProvider`
- `CachedEmbeddingProvider`
- `NullEmbeddingProvider`
- 默认模型：`BAAI/bge-small-zh-v1.5`
- 默认 runtime：FastEmbed + ONNX Runtime CPU
- 默认 cache：`backend/data/embedding_cache`

这套 runtime 当前由 Stage 3 semantic lane 使用。Stage 4 spec 必须优先复用现有 runtime、config 形状和缓存策略，而不是另起一套 `OnnxEmbeddingProvider` / `embed_texts()` / 独立模型路径协议。

### 4. effective retrieval plan 一致性问题已经修复

`MemoryManager.generate_context()` 当前已经保证：

- 若外部未传入 `retrieval_plan`，内部会生成 `active_plan`
- Stage 3 与 Stage 4 使用同一个 `active_plan`

该项已有回归测试覆盖，不应再作为“当前缺陷”写进待做项。

### 5. 当前真正的断点在 Stage 3 evidence 没有进入 Stage 4

现在 `Stage3RecallResult` 已经返回 `evidence_by_id`，但 `MemoryManager` 只把 `candidates` 传给 reranker，导致：

- Stage 3 lexical / semantic lane 产出的 evidence 无法被 Stage 4 使用
- reranker 仍只能基于 `RecallCandidate` 的扁平字段重新打分
- trace 无法回答“为什么 semantic lane 命中了，但最终没被选中”

这才是当前 Stage 4 升级的主问题。

## 当前问题

基于上述现状，Stage 4 的主要短板是：

1. **evidence 断流**：Stage 3 已有 `RetrievalEvidence`，但 Stage 4 没消费。
2. **语义信号浪费**：semantic lane 已经能给候选打 `semantic_score`，但 reranker 不认识它。
3. **规则权重硬编码**：`_intent_weights()` 仍是硬编码分支，缺少配置和结构化输出。
4. **telemetry 粒度不足**：当前只有 `per_item_reason` 文本，不利于 eval 做 signal-level 对比。
5. **trace 透传不完整**：即便 reranker 内部以后计算出结构化分数，当前 trace/stats/schema 也没有对应字段。
6. **来源预算仍是静态配额**：hybrid 场景仍依赖 `hybrid_profile_top_n` / `hybrid_slice_top_n` 固定切片，尚未 evidence-aware。

## 设计原则

1. **Stage 4 只精排，不补召回**：候选扩大仍归 Stage 3。
2. **先接 Stage 3 evidence，再谈 reranker-local 语义**：先把已经存在的数据流接通。
3. **默认行为可回滚**：默认 config 下，结果应尽量维持当前表现。
4. **复用现有 embedding runtime**：若未来 Stage 4 需要直接计算 embedding，必须复用 `embedding_provider.py`，不再造第二套基础设施。
5. **结构化可观测优先**：每个分数信号都要可进入 telemetry / trace / eval。
6. **公共 contract 尽量稳定**：不向 `RecallCandidate` 塞 embedding vector，不改 memory 持久化 schema。

## 非目标

| 不做 | 原因 |
| --- | --- |
| Stage 3 semantic recall 补漏池重设计 | 属于召回层，不属于 Stage 4。 |
| profile / slice 持久化 embedding | 涉及 storage 生命周期，另开设计。 |
| 直接引入 cross-encoder / LLM reranker | 延迟、成本、回滚都不适合当前主链路。 |
| 修改 memory extraction / archive schema | 本 spec 只覆盖 recall rerank。 |
| 把 entity / temporal lane 一并落地 | 当前执行路径尚未启用，先按 absent evidence 兼容。 |
| 默认启用 destination hard filter | 现状只把 destination 当打分信号；hard filter 需要独立 eval。 |

## 目标架构

```text
RecallCandidate[]
+ RecallRetrievalPlan
+ optional RetrievalEvidence sidecar
        │
        ▼
Candidate Enrichment
  - attach evidence_by_id[item_id] when present
  - keep backward-compatible fallback when evidence missing
        │
        ▼
Hard Filters
  - existing conflict hard drop
  - existing weak relevance hard drop
        │
        ▼
Signal Scoring
  - exact rule signals
  - applicability / recency
  - stage3 evidence signals
        │
        ▼
Weighted Merge
  - rule score stays dominant
  - evidence score is additive and configurable
        │
        ▼
Source-aware normalization
        │
        ▼
Source budget + dedupe + final selection
        │
        ▼
RecallRerankResult + trace telemetry
```

核心思路：

- **规则分仍然是主干**，保证默认兼容性。
- **Stage 3 evidence 分是附加信号**，只在对应 lane 开启且 evidence 存在时参与。
- **缺失 evidence 时全部回退到当前规则行为**。

## Stage 4 输入边界

### 当前输入

当前 Stage 4 实际输入只有：

- `user_message`
- `TravelPlanState`
- `RecallRetrievalPlan | None`
- `list[RecallCandidate]`

### 目标输入

升级后应显式支持：

```python
@dataclass(frozen=True)
class RecallRerankInputs:
    user_message: str
    plan: TravelPlanState
    retrieval_plan: RecallRetrievalPlan | None
    candidates: list[RecallCandidate]
    evidence_by_id: dict[str, RetrievalEvidence] = field(default_factory=dict)
```

说明：

- `evidence_by_id` 是可选 sidecar，不改 `RecallCandidate`。
- `MemoryManager.generate_context()` 负责把 Stage 3 的 `evidence_by_id` 传给 Stage 4。
- 若调用方没传 `evidence_by_id`，Stage 4 仍必须按旧逻辑工作。
- 若某个 `candidate.item_id` 在 `evidence_by_id` 中缺 key，则按“空 evidence”处理：
  - lane hit 全部为 `0`
  - `lexical_score` / `semantic_score` / `lane_fused_score` 视为缺失
  - `evidence_score = 0`
  - 不报错，不中断 rerank

## 信号模型

### 规则信号

这些仍由 Stage 4 自己计算，保持当前语义：

- `bucket_score`
- `domain_exact_score`
- `keyword_exact_score`
- `destination_score`
- `recency_score`
- `applicability_score`
- `conflict_score`

### Stage 3 evidence 信号

这些优先来自 `RetrievalEvidence`：

- `symbolic_hit`
- `lexical_hit`
- `semantic_hit`
- `lane_fused_score`
- `lexical_score`
- `semantic_score`
- `destination_match_type_score`
- `matched_domain_count`
- `matched_keyword_count`

其中：

- `symbolic_hit` / `lexical_hit` / `semantic_hit` 取值为 `0/1`
- `lane_fused_score` 使用 Stage 3 fusion 后的 `fused_score`
- `lexical_score` / `semantic_score` 直接使用 Stage 3 lane 输出，Stage 4 不重算
- `destination_match_type_score` 由 `destination_match_type` 映射得到

建议映射表：

```python
DESTINATION_MATCH_TYPE_SCORE = {
    "exact": 1.0,
    "alias": 0.85,
    "parent_child": 0.55,
    "region_weak": 0.25,
    "none": 0.0,
    "": 0.0,
}
```

注意：

- 当前 Stage 3 真实产生的 `destination_match_type` 主要是 `exact` 或 `none`。
- 其他枚举是为了和 Stage 3 v2 设计保持前向兼容，当前不要求在主链路里一定出现。

### evidence 缺失值与归一化规则

Stage 3 evidence 字段存在 `Optional[float]`，因此 Stage 4 必须明确定义缺失值行为：

1. `semantic_score` / `lexical_score` / `lane_fused_score` 为 `None` 或 key 缺失时，先记为“缺失值”，不是原始 `0.0`。
2. 归一化参与集合只包含“该信号实际有值”的候选；缺失值不参与 `min/max` 计算。
3. 归一化完成后：
   - 有值的候选得到 `normalized_*`
   - 缺失值候选的 `normalized_* = 0.0`
4. 当参与归一化的候选数为 `0` 时，所有候选该信号的 `normalized_* = 0.0`
5. 当参与归一化的候选数为 `1` 时，唯一命中的候选该信号 `normalized_* = 1.0`，其余候选为 `0.0`

这样定义的目的只有一个：

- evidence 只能给命中的候选加分
- 不会因为其他候选缺失某个 lane 而对其产生隐性减分
- 在权重较低时，Phase B 仍保持“evidence_score 只加不减”的可解释性

## 评分公式

建议把 Stage 4 总分拆成三层：

```text
rule_score =
  bucket_weight * bucket_score
+ domain_weight * domain_exact_score
+ keyword_weight * keyword_exact_score
+ destination_weight * destination_score
+ recency_weight * recency_score
+ applicability_weight * applicability_score
- conflict_weight * conflict_score

evidence_score =
  symbolic_hit_weight * symbolic_hit
+ lexical_hit_weight * lexical_hit
+ semantic_hit_weight * semantic_hit
+ lane_fused_weight * normalized_lane_fused_score
+ lexical_score_weight * normalized_lexical_score
+ semantic_score_weight * normalized_semantic_score
+ destination_match_type_weight * destination_match_type_score

source_score = rule_score + evidence_score
final_score = source_prior + source_normalized_score
```

约束：

1. `rule_score` 必须仍是主干。
2. evidence 相关权重默认都为 `0` 或极小值，先 observation 再放量。
3. `normalized_lane_fused_score / normalized_lexical_score / normalized_semantic_score` 仅在当前候选集内归一化。
4. `conflict_score` 采用“当前实现对齐”的双层语义：
   - `conflict_score >= 0.95`：hard drop
   - `conflict_score < 0.95`：仍保留在 `rule_score` 中做 soft subtract
5. semantic / lexical evidence 不能挽回已触发 hard drop 的候选。

### Source-aware normalization 定义

这里的 `source-aware normalization` 与当前实现保持一致，定义为：

1. 候选先按 `candidate.source` 分为两组：
   - `profile`
   - `episode_slice`
2. 每个 source 组内分别计算：

```text
source_normalized_score =
  1.0                              if max(source_score) == min(source_score)
  (source_score - min) / (max - min)   otherwise
```

3. `source_prior` 取自当前意图对应的 `IntentWeightProfile`：
   - profile 候选使用 `profile_source_prior`
   - slice 候选使用 `slice_source_prior`
4. 组内最终分数为：

```text
final_score = source_prior + source_normalized_score
```

5. Hybrid 场景仍按当前机制工作：
   - 先分别得到两组排序结果
   - 再按 `hybrid_profile_top_n` / `hybrid_slice_top_n` 做静态切片
   - 切片后跨 source 重新按 `final_score` 排序

这意味着 Phase A/B 不改变当前 hybrid selection 的基本骨架，只是在组内打分时引入 evidence-aware signal。

## 数据结构

### SignalScoreDetail

新增结构化分数字段，但不替换 `per_item_reason`：

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
```

### RecallRerankResult

保持现有字段兼容，只增不减：

```python
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

## 配置设计

当前 `MemoryRerankerConfig` 过于薄，下一版建议扩成：

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
        (
            "profile",
            IntentWeightProfile(1.0, 0.62, 0.34, 0.24, 0.18, 0.08, 0.06, 0.10, 1.4),
        ),
        (
            "episode_slice",
            IntentWeightProfile(0.62, 1.0, 0.16, 0.22, 0.18, 0.24, 0.14, 0.08, 1.0),
        ),
        (
            "recommend",
            IntentWeightProfile(0.90, 0.90, 0.22, 0.22, 0.20, 0.18, 0.10, 0.14, 1.2),
        ),
        (
            "default",
            IntentWeightProfile(0.84, 0.84, 0.24, 0.22, 0.18, 0.14, 0.08, 0.12, 1.2),
        ),
    )
    evidence: RerankerEvidenceConfig = field(default_factory=RerankerEvidenceConfig)
    dynamic_budget: RerankerDynamicBudgetConfig = field(
        default_factory=RerankerDynamicBudgetConfig
    )
```

YAML 形状建议：

```yaml
memory:
  retrieval:
    reranker:
      small_candidate_set_threshold: 3
      profile_top_n: 4
      slice_top_n: 3
      hybrid_top_n: 4
      hybrid_profile_top_n: 2
      hybrid_slice_top_n: 2
      recency_half_life_days: 180
      evidence:
        symbolic_hit_weight: 0.0
        lexical_hit_weight: 0.0
        semantic_hit_weight: 0.0
        lane_fused_weight: 0.0
        lexical_score_weight: 0.0
        semantic_score_weight: 0.0
        destination_match_type_weight: 0.0
      dynamic_budget:
        enabled: false
```

说明：

- `evidence` / `dynamic_budget` block 缺失时，必须回落到 dataclass 默认值。
- `intent_weights` 在 Phase A 先按 code-default 落地，不要求第一版开放 YAML 覆盖。
- 若后续需要从 YAML 覆盖 `intent_weights`，必须单独补充 parser 形状与回归测试。

## 分阶段实施

## Phase A：接通 evidence 数据流与结构化 telemetry

目标：不改变默认排序结果，只把接口打通。

### A1. evidence plumbing

改动：

- `MemoryManager.generate_context()` 将 `stage3_result.evidence_by_id` 传给 Stage 4。
- `select_recall_candidates()` / `choose_reranker_path()` 支持接收 `evidence_by_id`。

验收：

- 若不传 `evidence_by_id`，现有测试全部通过。
- 若传入 `evidence_by_id` 但 evidence 权重全为 0，选中结果与当前一致。
- Phase A 默认配置下：
  - `selected_item_ids` 必须与当前实现一致
  - `per_item_reason` 允许追加结构化信息，但必须保留当前主干 token：
    - `bucket=`
    - `domain=`
    - `keyword=`
    - `destination=`
    - `recency=`
    - `applicability=`
    - `conflict=`
  - `final_reason` 若变更，必须同步更新对应 golden / eval 快照

### A2. 权重配置化

把 `_intent_weights()` 的四套硬编码搬进 `MemoryRerankerConfig.intent_weights`，默认值与当前行为一致。

### A3. 结构化 score 输出

将 `_score_candidate()` 拆成：

- `_compute_rule_signals()`
- `_compute_evidence_signals()`
- `_passes_hard_filter()`
- `_compute_final_score()`

并产出 `SignalScoreDetail`。

### A4. telemetry 落地

`MemoryRecallTelemetry` 新增：

- `reranker_per_item_scores`
- `reranker_intent_label`
- `reranker_selection_metrics`

同时要同步更新：

- `backend/main.py`
- `backend/telemetry/stats.py`
- `backend/api/trace.py`

否则字段只会停留在内存对象，无法进 trace。

其中 `reranker_selection_metrics` 在 Phase A 的定位是前向兼容 placeholder：

- Phase A/B 允许其常驻 `null`
- 不要求 Phase A/B 就产生真实数值
- 若后续 Phase D 引入 reranker-local embedding 或其它相似度计算，再填充非空值

### A5. 测试

新增或补强：

- `test_memory_manager_passes_stage3_evidence_to_reranker`
- `test_recall_reranker_default_config_keeps_selected_ids`
- `test_recall_reranker_hybrid_default_config_keeps_selected_ids`
- `test_trace_payload_includes_reranker_per_item_scores`

## Phase B：evidence-aware reranker

目标：让 Stage 4 真正利用已经实现的 lexical / semantic retrieval evidence。

### B1. 证据打分

在 evidence 权重大于 0 时，Stage 4 使用：

- `semantic_score`
- `lexical_score`
- `lane_fused_score`
- `destination_match_type`
- lane presence

做附加分。

### B2. 归一化

仅在当前候选集内归一化：

- `semantic_score`
- `lexical_score`
- `lane_fused_score`

避免把 Stage 3 与 Stage 4 两侧分数当作同尺度原始值直接相加。

### B3. 保守放量

默认推荐：

1. `lexical/semantic` weight 先为 `0`
2. 打开 telemetry，观测 selected-vs-dropped case
3. 小流量把 `lane_fused_weight` / `semantic_score_weight` 从低值开始上调

### B4. 注意事项

Phase B 不新增 reranker-local embedding 计算。原因：

- Stage 3 已有 semantic lane 和 embedding runtime
- 先消费已有证据，复杂度和回滚都更可控
- 若 Stage 3 semantic lane 未启用，Phase B 仍能安全退化为旧逻辑

Phase B 额外安全约束：

- `evidence_score` 设计目标是“附加分”，不是替代 `rule_score`
- 放量初期应保持 evidence 权重总和明显低于 rule 主干权重
- 若实现阶段发现 evidence 多信号叠加会压过 rule 分，再单独加入 `evidence_score_cap`；这一项不作为 Phase A 启动前前置条件

## Phase C：hybrid source budget 动态化

目标：只在有足够 telemetry 之后，再处理 static budget 的局限。

当前问题不是“没预算”，而是预算仍是静态常量：

- `hybrid_profile_top_n`
- `hybrid_slice_top_n`

Phase C 可选方案：

- 仅在 `source == "hybrid_history"` 时启用
- 以 intent label 或 source affinity 估算 profile / slice 配额
- 某一来源候选不足时，由另一来源补齐

但这一步必须放在 Phase A/B 之后，因为没有结构化 telemetry 就无法评估它是否真的提升了质量。

## Phase D：仅在证据不足时再考虑 reranker-local embedding

这不是主线，而是条件触发项。

只有在以下结论同时成立时，才考虑让 Stage 4 自己再算 embedding：

1. Stage 3 semantic lane 已经开启且 evidence 已接入 Stage 4
2. 仍存在大量“候选已召回但排序失真”的语义 case
3. 问题无法通过 evidence 权重、source budget、destination evidence 解释

若真的走到这一步，也必须：

- 复用 `backend/memory/embedding_provider.py`
- 复用 FastEmbed / cache 机制
- 优先共享 Stage 3 provider，而不是重复加载模型

## Telemetry Contract

Stage 4 升级后，trace 里至少要能看到：

```json
{
  "reranker_selected_ids": ["stable_preferences:hotel:quiet_stay", "slice_kyoto_machiya"],
  "reranker_final_reason": "source-aware weighted rerank selected 2 items",
  "reranker_fallback": "none",
  "reranker_per_item_reason": {
    "item_id": "human readable reason"
  },
  "reranker_per_item_scores": {
    "item_id": {
      "bucket_score": 0.82,
      "domain_exact_score": 1.0,
      "keyword_exact_score": 0.5,
      "destination_score": 0.0,
      "recency_score": 0.76,
      "applicability_score": 0.35,
      "conflict_score": 0.0,
      "lexical_score": 0.0,
      "semantic_score": 0.63,
      "lane_fused_score": 0.41,
      "rule_score": 0.71,
      "evidence_score": 0.05,
      "source_normalized_score": 1.0,
      "final_score": 1.76,
      "hard_filter": ""
    }
  },
  "reranker_intent_label": "recommend",
  "reranker_selection_metrics": {
    "selected_pairwise_similarity_max": null,
    "selected_pairwise_similarity_avg": null
  }
}
```

## Evaluation

### Deterministic tests

保留并扩展：

- `backend/tests/test_recall_reranker.py`
- `backend/tests/test_reranker_eval.py`
- `backend/tests/test_memory_manager.py`

新增重点：

- default config invariance
- evidence missing fallback
- evidence present but zero-weight invariance
- evidence present and non-zero weight changes order as expected
- trace/stats payload schema

### Integration cases

新增 Stage 3 + Stage 4 联动 case：

- symbolic only
- lexical hit outranks weak symbolic match
- semantic hit helps same-intent synonym candidate
- semantic lane disabled but evidence fields absent
- destination exact evidence helps historical stay choice
- conflicting profile item still gets hard dropped even if semantic score 很高

### 观察指标

- selected ids exact match
- per-signal score snapshots
- evidence coverage rate
- reranker fallback rate
- semantic evidence usage rate
- hybrid profile/slice selection ratio

## Rollout

1. Phase A 先落地，保证 evidence 能进 reranker 和 trace。
2. Phase A 默认必须保持当前排序结果。
3. Phase B 先以 evidence weight = 0 进入 observation mode。
4. 采样 trace 与 reranker-only eval，确认 evidence 信号方向正确。
5. 小步上调 evidence 权重。
6. 如 hybrid 质量仍受限，再推进 Phase C。
7. Phase D 仅在证据不足时单独立项。

## 最终结论

这版 Stage 4 spec 的核心变化只有一句话：

**Stage 4 的下一步重点不是再造一套 embedding reranker，而是先把当前已经实现的 Stage 3 lexical / semantic evidence 真正接到精排链路里。**

只有这一步完成后，后续关于 source budget、reranker-local semantic observation、甚至更重的 cross-encoder 方案才有证据基础。
