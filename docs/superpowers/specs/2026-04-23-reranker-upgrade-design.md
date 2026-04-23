# Travel Memory Hybrid Reranker 升级设计

## 背景

当前 Stage 4 reranker 是纯规则加权系统，7 维手工权重打分 + 硬编码意图分类。它在确定性、可观测性、延迟方面表现优秀，但有三个核心痛点：

1. **同义词泛化差** — Jaccard/子串匹配无法捕捉 "安静≈清幽"、"亲子≈带孩子" 等语义等价
2. **冲突检测靠固定词表** — 否定词表和正面词表覆盖有限，大量自然表达漏检
3. **意图分类太粗糙** — 4 类硬分支无法表达混合意图，分支间硬切换

行业调研显示主流方案（Mem0、AgentRank、Memento）使用 Cross-Encoder / LLM 做精排，但完全丢弃规则框架。我们的策略是**保留规则骨架的优势，用 embedding 补上语义短板**。

## 设计原则

1. **守住 Stage 4 边界** — reranker 只排序/过滤/裁剪/解释，不扩大候选池
2. **规则保底，语义补缺** — 语义信号和规则信号并行，不替换
3. **渐进升级** — 每个 Phase 可独立 eval，不依赖后续 Phase
4. **可观测性不降级** — per-signal telemetry 贯穿始终

## 架构总览

```
RecallCandidate 列表
        │
        ▼
┌─────────────────────┐
│  Hard Filter        │  conflict ≥ 0.95 → 丢弃
│  (门槛信号)          │  destination hard mismatch → 丢弃或强降权
│                     │  weak relevance → 丢弃
│                     │  profile rejected by user → 丢弃
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Prior Score        │  source_prior × intent_affinity
│  (先验加权)          │  + bucket_score
│                     │  + recency_score
│                     │  + applicability_score
│                     │  - conflict_penalty (soft)
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Semantic RRF       │  content_semantic_score → rank list
│  (语义信号)          │  domain_semantic_score → rank list
│                     │  → weighted RRF fusion
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Score Merge        │  final_score = prior_score + α × rrf_score
│                     │  α 在 Phase B 从 0 开始逐步上调
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Dynamic Budget     │  按 intent_affinity 动态分配 profile/slice 配额
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Dedupe & Select    │  group dedupe（现有逻辑）+ 保留观测点
│                     │  selected_pairwise_similarity_max/avg
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Telemetry          │  per-signal 分数 + intent vector + fallback 标记
└─────────────────────┘
         │
         ▼
   RecallRerankPath
```

## Phase A：Reranker 可观测性与配置化

**目标**：在不引入任何模型的前提下，加固规则骨架、暴露信号级遥测、为后续 Phase 打好接口基础。

### A1：已知链路修复

- 确认 `active_plan` (TravelPlanState) 正确传入 reranker
- 确认所有 RecallCandidate 字段在 symbolic_recall 构建时正确填充

### A2：意图权重配置化

将 `_intent_weights` 的 4 组硬编码魔数抽到 `MemoryRerankerConfig`：

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
class MemoryRerankerConfig:
    # existing fields...
    small_candidate_set_threshold: int = 3
    profile_top_n: int = 4
    slice_top_n: int = 3
    hybrid_top_n: int = 4
    hybrid_profile_top_n: int = 2
    hybrid_slice_top_n: int = 2
    recency_half_life_days: int = 180
    # new
    intent_weights: dict[str, IntentWeightProfile] = field(default_factory=lambda: {
        "profile": IntentWeightProfile(
            profile_source_prior=1.0, slice_source_prior=0.62,
            bucket_weight=0.34, domain_weight=0.24, keyword_weight=0.18,
            destination_weight=0.08, recency_weight=0.06,
            applicability_weight=0.10, conflict_weight=1.4,
        ),
        "slice": IntentWeightProfile(
            profile_source_prior=0.62, slice_source_prior=1.0,
            bucket_weight=0.16, domain_weight=0.22, keyword_weight=0.18,
            destination_weight=0.24, recency_weight=0.14,
            applicability_weight=0.08, conflict_weight=1.0,
        ),
        "recommend": IntentWeightProfile(
            profile_source_prior=0.9, slice_source_prior=0.9,
            bucket_weight=0.22, domain_weight=0.22, keyword_weight=0.20,
            destination_weight=0.18, recency_weight=0.10,
            applicability_weight=0.14, conflict_weight=1.2,
        ),
        "default": IntentWeightProfile(
            profile_source_prior=0.84, slice_source_prior=0.84,
            bucket_weight=0.24, domain_weight=0.22, keyword_weight=0.18,
            destination_weight=0.14, recency_weight=0.08,
            applicability_weight=0.12, conflict_weight=1.2,
        ),
    })
```

`_intent_weights` 函数改为从 config 读取，保留关键词匹配逻辑但输出 config key 而非硬编码元组。

### A3：信号级遥测

在 `RecallRerankResult` 中暴露 per-signal 分数，替换当前的整体 reason 文本：

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
    # Phase B 新增字段先占位
    content_semantic_score: float = 0.0
    domain_semantic_score: float = 0.0
    # 维度预留
    profile_affinity: float = 0.0
    slice_affinity: float = 0.0

@dataclass
class RecallRerankResult:
    selected_item_ids: list[str]
    final_reason: str
    per_item_reason: dict[str, str]
    per_item_scores: dict[str, SignalScoreDetail]  # NEW
    intent_label: str                                # NEW: "profile" | "slice" | "recommend" | "default"
    fallback_used: str = "none"
```

per_item_reason 保留（人类可读），per_item_scores 供 eval 和调试消费。

### A4：融合接口边界

为 Phase B/C 预留明确的函数签名，Phase A 只在内部用 `_` 前缀占位：

```python
def _compute_hard_filter_score(candidate: RecallCandidate, user_message: str, ...) -> tuple[bool, str]:
    """返回 (是否通过, 过滤原因)"""

def _compute_prior_score(candidate: RecallCandidate, ...) -> float:
    """返回先验加权分"""

def _compute_semantic_rrf_score(candidates: list[RecallCandidate], ...) -> dict[str, float]:
    """Phase A 返回全 0，Phase B 接入 embedding"""
```

### A 的验收标准

- 所有 11 个现有单元测试 + 18 个 reranker-only eval golden case 通过
- per_item_scores 在 SSE trace 中可见
- intent 权重从 yaml config 可调
- 无模型依赖、无新包引入

---

## Phase B：Embedding 语义分数

**目标**：引入中文 embedding 模型，为已有候选打语义分，与规则分并行，权重从 0 开始上调。

### B1：模型选型与部署

| 选项 | 模型 | 参数量 | ONNX CPU 延迟 | 中文覆盖 |
|------|------|--------|---------------|---------|
| 推荐 | `shibing624/text2vec-base-chinese` | ~100M | ~5-10ms/query | 原生中文 |
| 备选 | `BAAI/bge-small-zh-v1.5` | ~33M | ~3-5ms/query | 中文优化 |
| 备选 | `BAAI/bge-m3` (ONNX) | ~110M | ~8-15ms/query | 中英日韩多语言 |

- 使用 ONNX Runtime 推理，CPU 部署，不依赖 PyTorch
- 模型在服务启动时加载，单次 query embedding 缓存至 session 生命周期结束
- 候选 embedding 在写入/归档时预计算，存储在 RecallCandidate 的内部字段中（Phase B 新增）
- 新增依赖：`onnxruntime`（~30MB）、模型文件（~100-200MB）

### B2：语义分数计算

新增两个信号，与规则分**并行**：

```python
def _content_semantic_score(
    query_embedding: list[float],
    candidate_embedding: list[float],
) -> float:
    """cosine similarity, 归一化到 [0, 1]"""
    return max(0.0, cosine_similarity(query_embedding, candidate_embedding))

def _domain_semantic_score(
    query_embedding: list[float],
    candidate_domains: list[str],
    domain_embeddings: dict[str, list[float]],
) -> float:
    """query 与候选 domain 列表的 max cosine"""
    if not candidate_domains:
        return 0.0
    scores = [
        cosine_similarity(query_embedding, domain_embeddings[d])
        for d in candidate_domains if d in domain_embeddings
    ]
    return max(scores) if scores else 0.0
```

- `query_embedding`：每轮用户消息算一次，session 内缓存
- `candidate_embedding`：在 memory extraction/archive 时预计算并存储
- `domain_embeddings`：预计算的 13 个 ALLOWED_RECALL_DOMAINS 的 embedding 字典，启动时计算一次缓存

### B3：意图连续化

将 `_intent_weights` 从 4 类硬分支升级为基于 `profile_affinity` 和 `slice_affinity` 的 2 维连续混合：

```python
@dataclass(frozen=True)
class IntentVector:
    profile_affinity: float  # 0.0 ~ 1.0
    slice_affinity: float    # 0.0 ~ 1.0

def _compute_intent_vector(
    user_message: str,
    retrieval_plan: RecallRetrievalPlan | None,
    config: MemoryRerankerConfig,
) -> IntentVector:
    """从 retrieval_plan.source 和用户消息推断连续意图向量"""
    # Phase B: 仍然用规则生成，但输出是连续值而非硬分类
    # 后续 Phase 可以让 Stage 2 query tool 直接输出
    source = retrieval_plan.source if retrieval_plan else "hybrid_history"
    reason = (retrieval_plan.reason or "").lower()

    if source == "profile" or "profile_" in reason:
        return IntentVector(profile_affinity=1.0, slice_affinity=0.62)
    if source == "episode_slice" or "past_trip" in reason:
        return IntentVector(profile_affinity=0.62, slice_affinity=1.0)
    if any(w in user_message for w in ("推荐", "比较好", "适合我", "怎么安排")):
        return IntentVector(profile_affinity=0.9, slice_affinity=0.9)
    return IntentVector(profile_affinity=0.84, slice_affinity=0.84)
```

`source_prior` 现在从 `IntentVector` 派生：

```python
profile_source_prior = intent.profile_affinity
slice_source_prior = intent.slice_affinity
```

### B4：语义权重渐进上调

Phase B 初始部署时语义权重为 0（行为与 Phase A 完全一致）。通过 config 逐步上调：

```python
@dataclass(frozen=True)
class SemanticConfig:
    enabled: bool = False                          # Phase B 默认关闭
    model_name: str = "text2vec-base-chinese"       # 后续可切换
    content_semantic_weight: float = 0.0             # 从 0 开始，逐步上调到 0.15-0.25
    domain_semantic_weight: float = 0.0              # 从 0 开始，逐步上调到 0.08-0.15
    cache_query_embedding: bool = True                # session 内缓存
```

### B5：RecallCandidate 内部扩展

不污染公共 contract，在 reranker 内部使用扩展 DTO：

```python
@dataclass
class _ScoredCandidateInternal:
    candidate: RecallCandidate
    embedding: list[float] | None = None  # Phase B 填充
    # ... 其他 score 字段同现有
```

候选 embedding 来源：
- profile：从 memory profile 存储读取预计算值
- episode_slice：从 episode archive 读取预计算值
- 如果预计算值不存在：退化为纯规则分（semantic_score = 0.0）

### B 的验收标准

- semantic_weight=0 时，所有现有 eval golden case 行为与 Phase A 完全一致
- semantic_weight > 0 时，中文同义词对测试（安静/清幽、亲子/带孩子） semantic_score > 0.7
- 单次 rerank 延迟增量 < 20ms（CPU, 候选数 ≤ 15）
- embedding 计算失败时 graceful 退化为纯规则分

---

## Phase C：混合融合与动态预算

**目标**：语义信号走 RRF，先验/时间走加权求和，动态分配来源配额。

### C1：混合融合架构

```
prior_score = f(intent, bucket, recency, applicability, conflict)
semantic_rrf_score = weighted_rrf(content_semantic_rank, domain_semantic_rank)
final_score = prior_score + α × semantic_rrf_score
```

其中 RRF 融合：

```python
def weighted_rrf(
    rank_lists: dict[str, list[tuple[str, int]]],
    weights: dict[str, float],
    k: int = 60,
) -> dict[str, float]:
    """
    rank_lists: {"content_semantic": [(item_id, rank), ...], "domain_semantic": [...]}
    weights: {"content_semantic": 0.7, "domain_semantic": 0.3}
    k: RRF 常数，默认 60
    """
    scores: dict[str, float] = {}
    for signal_name, ranked in rank_lists.items():
        w = weights.get(signal_name, 1.0)
        for item_id, rank in ranked:
            scores[item_id] = scores.get(item_id, 0.0) + w / (k + rank)
    return scores
```

### C2：动态来源预算

根据 `IntentVector` 动态计算 profile/slice 配额：

```python
def _compute_source_budget(
    intent: IntentVector,
    config: MemoryRerankerConfig,
) -> SourceBudget:
    """
    profile_affinity=0.9, slice_affinity=0.35 → profile 3, slice 1
    profile_affinity=0.35, slice_affinity=0.9 → profile 1, slice 3
    profile_affinity=0.9, slice_affinity=0.9  → profile 2, slice 2
    """
    total = config.hybrid_top_n
    profile_share = round(total * intent.profile_affinity / (intent.profile_affinity + intent.slice_affinity))
    slice_share = total - profile_share
    # constraints/rejections 最低保留 1 条
    constraint_min = 1 if intent.profile_affinity > 0.5 else 0
    return SourceBudget(
        profile_top_n=max(profile_share, constraint_min),
        slice_top_n=slice_share,
        total_top_n=total,
    )
```

### C3：MMR 观测点

不实现 MMR 排序，但在 telemetry 中记录：

```python
@dataclass
class SelectionMetrics:
    selected_pairwise_similarity_max: float = 0.0
    selected_pairwise_similarity_avg: float = 0.0
    # 供 eval 分析 top-N 是否语义高度重叠
```

计算时机：选出最终 top-N 后，两两计算 embedding cosine，取 max 和 avg。

### C4：destination 别名/层级映射

增加目的地归一化层（Phase C 独立小项）：

```python
DESTINATION_ALIAS: dict[str, str] = {
    "京都": "京都", "Kyoto": "京都", "京都市": "京都",
    "东京": "东京", "Tokyo": "东京", "东京都": "东京",
    # ...
}

DESTINATION_HIERARCHY: dict[str, list[str]] = {
    "关东": ["东京", "千叶", "�的�的�埼玉", "神奈川"],
    "关西": ["京都", "大阪", "奈良", "兵库"],
    # ...
}
```

`_destination_match` 从子串匹配升级为：先归一化别名，再检查层级包含。

### C 的验收标准

- 与 Phase A eval 对比，整体准确率不降
- 动态预算在 profile/slice 混合意图下正确分配
- MMR 观测点在 telemetry 中可见
- destination 别名测试覆盖 5+ 个常见日文地名

---

## Phase D：证据驱动增强

**目标**：根据 Phase C 的 eval 数据和 MMR 观测点决定后续动作，不预设实现。

### 可能路径

| 观测结论 | 动作 |
|---------|------|
| top-N 语义重叠严重 | 引入 MMR 排序 |
| Uncertain 样本（top scores 接近）多 | 条件触发 cross-encoder 精排 |
| Stage 3 零命中/false-skip 多 | Stage 3 独立增加 semantic recall 补漏池 |
| 以上都不严重 | 保持在 Phase C，不做 Phase D |

### 条件式 cross-encoder 触发条件（待评估确认后再实现）

如果引入，触发条件：

- candidate_count > 6
- top-3 scores 差值 < 0.05
- 检测到冲突候选
- 用户表达自然/承接上文

触发后使用小型 cross-encoder（ONNX CPU）对 top-K 候选重排，输出与规则分融合而非覆盖。

---

## 不做什么

| 不做 | 原因 |
|------|------|
| Stage 3 semantic recall 补漏池 | 属于召回层改动，不在 reranker 范围 |
| RecallCandidate 加 confidence/supersedes/conflicts_with | 无消费方，属于空中楼阁 |
| 6 维 intent 向量 | 无训练数据支撑，先只升到 2 维 |
| MMR 排序 | Phase C 只观测不实现，有数据证据再引入 |
| LLM reranker | 延迟和成本不可接受，排除 |
| RecallCandidate 公共 contract 加 embedding_vector | 用内部 DTO 隔离，不污染公共接口 |

## 配置演进路线

```yaml
# Phase A: 只暴露意图权重到 config
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
      intent_weights:
        profile:
          profile_source_prior: 1.0
          slice_source_prior: 0.62
          # ... 完整 9 维
        slice: { ... }
        recommend: { ... }
        default: { ... }

# Phase B: 增加语义配置
      semantic:
        enabled: false
        model_name: "text2vec-base-chinese"
        content_semantic_weight: 0.0
        domain_semantic_weight: 0.0
        cache_query_embedding: true

# Phase C: 增加 RRF 和动态预算
      rrf:
        content_semantic_weight: 0.7
        domain_semantic_weight: 0.3
        k: 60
      dynamic_budget: true
```

## 延迟预算

| Phase | P50 延迟增量 | P95 延迟增量 |
|-------|-------------|-------------|
| A | 0ms | 0ms |
| B | ~10ms | ~20ms (embedding 计算) |
| C | ~12ms | ~25ms (RRF + 动态预算) |
| D (如触发 cross-encoder) | ~50ms | ~100ms |

## 依赖演进

| Phase | 新增依赖 | 体积 |
|-------|---------|------|
| A | 无 | 0 |
| B | onnxruntime, 中文 embedding 模型 ONNX 文件 | ~250MB |
| C | 无 | 0 |
| D | 可能增加小型 cross-encoder ONNX | ~400MB |