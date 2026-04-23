# Travel Memory Stage 4 Reranker Upgrade v2

## 背景

当前旅行 Agent 的记忆召回链路已经收敛为 recall-first：

1. Stage 0/1 判断本轮是否需要历史记忆。
2. Stage 2 生成 `RecallRetrievalPlan`。
3. Stage 3 用 symbolic recall 从 profile / episode slice 产生 `RecallCandidate`。
4. Stage 4 reranker 对候选做过滤、去重、排序、裁剪，并把结果注入 prompt 与 telemetry。

本 spec 只升级 Stage 4。它不扩大候选池，不改变 memory 写入/归档流程，不新增长期 schema 字段。目标是把现有规则 reranker 变成可观测、可配置、可语义增强的混合 reranker，同时保持默认行为可回滚。

## 当前问题

当前 `backend/memory/recall_reranker.py` 的优势是确定性强、延迟低、可解释；主要短板是：

- 同义表达泛化弱：`domain_score` / `keyword_score` 主要依赖 Jaccard、子串和少量硬编码 token。
- 意图来源权重硬切换：`profile`、`episode_slice`、`recommend`、`default` 四套权重直接分支，混合意图表达有限。
- 可观测性不足：`per_item_reason` 是文本，不适合 eval 做 signal-level 对比。
- fallback 路径存在一致性风险：manager 内部生成 `active_plan` 后，reranker 应使用同一个 effective retrieval plan。

冲突检测依然是重要痛点，但普通 embedding 对否定和偏好极性不可靠。本 spec 不承诺在 Phase B/C 解决冲突理解，只保持现有 hard rule，并把更强冲突分类器放到证据驱动的后续阶段。

## 设计原则

1. **Stage 4 边界**：reranker 只消费已有候选，不新增 semantic recall 补漏池。
2. **默认零行为风险**：Phase A/B 默认输出应与当前行为一致，除明确修复的 bug 外不改变选中结果。
3. **规则保底，语义补漏**：规则分仍是主干；embedding 只作为可关闭、可调权重的附加信号。
4. **不污染公共 contract**：不向 `RecallCandidate` 公共 dataclass 添加 embedding vector；语义特征用 reranker 内部 DTO 和 cache 承载。
5. **分数尺度明确**：核心融合使用 calibrated weighted sum。RRF / MMR / cross-encoder 只作为 Phase D 评估项。
6. **可观测优先**：每个 signal 都要进入结构化 telemetry，便于 A/B、回归和失败分析。

## 非目标

| 不做 | 原因 |
| --- | --- |
| Stage 3 semantic recall 补漏池 | 属于召回层，不属于 reranker。 |
| profile / slice 持久化 embedding | 涉及 storage、backfill、模型版本生命周期，单独设计。 |
| `RecallCandidate` 添加 `embedding_vector` | 会污染当前公共候选 contract。 |
| `confidence` / `supersedes` / `conflicts_with` | 目前没有稳定写入来源和消费闭环。 |
| 6 维 intent vector | 没有训练或反馈数据支撑，先只做 source affinity。 |
| 默认启用 LLM / cross-encoder reranker | 延迟和成本不可控，只作为后续条件触发项评估。 |
| 新增 destination hard filter | 当前 destination 是加权信号；hard filter 需要独立 eval 后再开。 |

## 目标架构

```text
RecallCandidate[]
  -> effective RecallRetrievalPlan
  -> hard filters
       - existing conflict hard drop
       - existing weak relevance hard drop
  -> per-candidate signal scoring
       - bucket_score
       - domain_exact_score
       - keyword_exact_score
       - destination_score
       - recency_score
       - applicability_score
       - conflict_score
       - content_semantic_score    optional, default 0
       - domain_semantic_score     optional, default 0
  -> calibrated weighted sum
  -> source-aware normalization
  -> deterministic source budget
  -> existing dedupe
  -> selection metrics
  -> RecallRerankPath + telemetry
```

核心公式：

```text
rule_score =
  bucket_weight * bucket_score
+ domain_weight * domain_exact_score
+ keyword_weight * keyword_exact_score
+ destination_weight * destination_score
+ recency_weight * recency_score
+ applicability_weight * applicability_score
- conflict_weight * conflict_score

semantic_score =
  content_semantic_weight * normalized_content_semantic_score
+ domain_semantic_weight * normalized_domain_semantic_score

source_score = rule_score + semantic_score
final_score = source_prior + source_normalized_score
```

说明：

- Phase B 初始 `content_semantic_weight=0`、`domain_semantic_weight=0`，即语义分被观测但不影响排序。
- semantic score 在当前候选集内 min-max normalize 到 `[0,1]`，避免 cosine 与规则分尺度混用。
- 不使用 RRF 作为主融合方式。当前候选池通常很小，RRF 的 `1/(k+rank)` 尺度需要额外校准，调参成本高于收益。

## 数据结构

### SignalScoreDetail

新增内部/telemetry 结构，不替换 `per_item_reason`。

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
    rule_score: float
    source_normalized_score: float
    final_score: float
    content_semantic_score: float = 0.0
    domain_semantic_score: float = 0.0
    profile_affinity: float = 0.0
    slice_affinity: float = 0.0
    hard_filter: str = ""
```

### RecallRerankResult

保持现有字段兼容，只新增字段。

```python
@dataclass
class RecallRerankResult:
    selected_item_ids: list[str]
    final_reason: str
    per_item_reason: dict[str, str]
    fallback_used: str = "none"
    per_item_scores: dict[str, SignalScoreDetail] = field(default_factory=dict)
    intent_label: str = ""
    selection_metrics: SelectionMetrics = field(default_factory=SelectionMetrics)
```

### SelectionMetrics

语义相似度不可用时用 `None`，不使用 `0.0`，避免被误读为“完全不重复”。

```python
@dataclass
class SelectionMetrics:
    selected_pairwise_similarity_max: float | None = None
    selected_pairwise_similarity_avg: float | None = None
```

## Phase A：行为等价重构与遥测

目标：不引入模型，不改变排序策略；修复已知链路一致性问题，拆出 signal telemetry 和配置。

### A1：effective retrieval plan 一致性修复

`MemoryManager.generate_context()` 当前会在没有外部 `retrieval_plan` 时生成 `active_plan`，Stage 3 召回使用该 plan；reranker 也必须收到同一个 effective plan。

改动：

```python
selected_candidates, rerank_result = await select_recall_candidates(
    user_message=user_message,
    plan=plan,
    retrieval_plan=active_plan,
    candidates=recall_candidates,
    reranker_config=self.retrieval_config.reranker,
)
```

验收：

- 增加 heuristic fallback 回归测试。
- 断言 `reranker_per_item_reason` 中 domain/keyword/destination 分数来自 active plan，而不是 `None` plan。

### A2：权重配置化

把当前 `_intent_weights()` 的四组硬编码数值搬进 `MemoryRerankerConfig`，默认值必须与旧代码逐项一致。

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
    small_candidate_set_threshold: int = 3
    profile_top_n: int = 4
    slice_top_n: int = 3
    hybrid_top_n: int = 4
    hybrid_profile_top_n: int = 2
    hybrid_slice_top_n: int = 2
    recency_half_life_days: int = 180
    intent_weights: tuple[tuple[str, IntentWeightProfile], ...] = (...)
```

配置解析时可从 YAML dict 读入，但 dataclass 内部使用 tuple 存储，避免 `frozen=True` 配合 mutable dict 的隐式可变问题。

### A3：score 计算拆分

把 `_score_candidate()` 拆成可测试的私有函数：

```python
def _compute_signal_scores(...) -> SignalScoreDetail: ...
def _passes_hard_filter(score: SignalScoreDetail) -> tuple[bool, str]: ...
def _compute_rule_score(score: SignalScoreDetail, weights: IntentWeightProfile) -> float: ...
```

Phase A 只重构现有逻辑：

- conflict hard drop 保持 `conflict_score >= 0.95`。
- weak relevance 逻辑保持现状。
- destination 仍是加权信号，不新增 hard mismatch 过滤。
- `per_item_reason` 文本保持现有格式或只做兼容性追加。

### A4：telemetry

`MemoryRecallTelemetry` 增加：

```python
reranker_per_item_scores: dict[str, dict[str, float]]
reranker_intent_label: str
reranker_selection_metrics: dict[str, float | None]
```

旧字段保持：

- `reranker_selected_ids`
- `reranker_final_reason`
- `reranker_fallback`
- `reranker_per_item_reason`

### A5：测试

新增 invariance 测试：

- 用固定 seed 生成 100 个候选场景。
- 对比旧实现 fixture 和新实现。
- `selected_item_ids`、drop/keep 决策、fallback 必须完全一致。
- score 使用 `abs(old-new) < 1e-9`，不要求浮点 bit-exact。

验收命令：

```bash
pytest -q backend/tests/test_recall_reranker.py backend/tests/test_reranker_eval.py
```

Phase A 不新增依赖。

## Phase B：reranker-local embedding 语义分

目标：只对 Stage 3 已召回候选做语义打分，不写入持久化存储。语义权重默认 0，先观测后放量。

### B1：EmbeddingProvider

新增 reranker-local provider：

```python
class EmbeddingProvider(Protocol):
    def enabled(self) -> bool: ...
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...
```

默认实现：

- `NullEmbeddingProvider`：返回空结果，Phase A/B disabled 时使用。
- `OnnxEmbeddingProvider`：加载中文 sentence embedding ONNX 模型，批量 embed query + candidate texts。

运行时策略：

- 模型进程级加载，不是 session 级加载。
- 模型文件由部署镜像提供，不在服务启动时联网下载。
- provider 初始化失败时自动降级到 `NullEmbeddingProvider`，reranker 继续使用规则分。
- LRU cache：key = `(model_name, text_hash)`，限制条目数和总内存。

### B2：候选文本构造

不依赖持久化 embedding。每轮 rerank 从 candidate 构造文本：

```text
candidate_text =
  source + bucket + domains
  + content_summary
  + applicability
  + matched_reason
  + polarity/key
```

query text 使用当前 user message。后续如果要引入最近用户窗口，需要单独评估，避免改变 Stage 2/3 语义。

### B3：语义信号

新增两个语义信号：

```python
content_semantic_score = cosine(query_embedding, candidate_text_embedding)
domain_semantic_score = max cosine(query_embedding, domain_description_embedding)
```

domain 不直接 embed 英文 enum，必须使用中文描述表：

```python
DOMAIN_SEMANTIC_DESCRIPTIONS = {
    "itinerary": ["行程", "路线", "游览计划", "景点安排"],
    "pace": ["节奏", "轻松", "紧凑", "别太累", "慢一点"],
    "food": ["餐饮", "吃饭", "美食", "饭店", "小吃"],
    "hotel": ["酒店", "住宿", "宾馆", "旅馆"],
    "accommodation": ["住宿", "住处", "房源", "酒店"],
    "flight": ["航班", "飞机", "机票", "飞行"],
    "train": ["火车", "高铁", "新干线", "JR"],
    "budget": ["预算", "花费", "价格", "费用", "省钱"],
    "family": ["亲子", "带孩子", "儿童", "家庭出行"],
    "accessibility": ["无障碍", "行动不便", "轮椅", "少走路"],
    "planning_style": ["安排风格", "自由行", "跟团", "计划方式"],
    "documents": ["签证", "护照", "入境", "材料"],
    "general": ["一般", "综合", "其他"],
}
```

domain embedding = 同义词 embedding 的 mean pooling，进程级缓存。

### B4：分数校准

在候选集内对 semantic scores min-max normalize：

```python
normalized = 0.0 if max == min else (score - min) / (max - min)
```

如果 provider disabled、embedding 缺失或计算异常：

- `content_semantic_score = 0.0`
- `domain_semantic_score = 0.0`
- `selection_metrics.* = None`
- `fallback_used` 不改为错误，只在 telemetry 标记 `semantic_fallback=provider_unavailable` 或 `semantic_fallback=embedding_error`

### B5：配置

```yaml
memory:
  retrieval:
    reranker:
      semantic:
        enabled: false
        provider: "onnx"
        model_name: "text2vec-base-chinese"
        model_path: "models/text2vec-base-chinese.onnx"
        content_semantic_weight: 0.0
        domain_semantic_weight: 0.0
        cache_max_items: 10000
        cache_max_mb: 64
        timeout_ms: 25
```

放量策略：

1. `enabled=false`：默认规则行为。
2. `enabled=true` + weights 仍为 0：只记录 telemetry。
3. `content_semantic_weight=0.05`、`domain_semantic_weight=0.02` 小流量 eval。
4. 根据 eval 再上调，不在 spec 中预设最终权重。

### B6：测试

- semantic disabled 时，Phase A invariance 测试继续通过。
- mock embedding provider 下，同义词 case 能让 `content_semantic_score` 排序生效。
- domain semantic 使用中文描述表，不直接 embed 英文 enum。
- provider timeout / exception graceful fallback。
- rerank P95 延迟增量在候选数 <= 15 时满足配置阈值；实际阈值以本机 benchmark 写入 eval 报告。

## Phase C：source affinity 与动态预算

目标：在不引入新的意图理解模型前，把硬分支变成可观测的 source affinity，并用它驱动来源预算。

### C1：IntentVector

新增：

```python
@dataclass(frozen=True)
class IntentVector:
    profile_affinity: float
    slice_affinity: float
```

Phase C 仍由规则生成固定档位，不声称已经实现真正连续 intent：

```python
profile source:       (1.00, 0.62)
episode_slice source: (0.62, 1.00)
recommend text:       (0.90, 0.90)
default:              (0.84, 0.84)
```

该结构的价值是：

- telemetry 可直接暴露 source affinity。
- dynamic budget 可基于同一接口实现。
- 未来 Stage 2 query tool 可输出真实连续值，不再改 reranker 下游接口。

### C2：动态预算算法

只影响 `source == "hybrid_history"` 或 retrieval plan 为空且按 hybrid fallback 处理的场景。`source == "profile"` 和 `source == "episode_slice"` 仍优先遵守来源意图。

```python
def compute_source_budget(intent: IntentVector, total: int) -> SourceBudget:
    p = max(intent.profile_affinity, 0.0)
    s = max(intent.slice_affinity, 0.0)
    if p + s <= 1e-9:
        p = s = 1.0

    raw_profile = total * p / (p + s)
    profile_count = floor(raw_profile)
    remainder = raw_profile - profile_count
    if remainder >= 0.5:
        profile_count += 1

    profile_count = min(max(profile_count, 0), total)
    slice_count = total - profile_count
    return SourceBudget(profile_count, slice_count, total)
```

规则：

- 始终保证 `profile_top_n + slice_top_n == hybrid_top_n`。
- 不在此阶段强制 `constraints/rejections` 最低保留；否则会产生超过 total 的隐式配额。硬约束优先级仍由 bucket prior 和排序保证。
- 如果某来源候选不足，剩余额度可由另一来源补足。

### C3：selection metrics

如果 semantic provider 可用并且最终选中项都有 embedding，记录：

- `selected_pairwise_similarity_max`
- `selected_pairwise_similarity_avg`

否则两者为 `None`。

这些指标只用于判断是否需要后续 MMR，不参与 Phase C 排序。

### C4：测试

- hybrid 场景预算分配可预测，包含 affinity sum 为 0 的防御。
- profile-only / slice-only 场景不被 dynamic budget 改写。
- semantic disabled 时 selection metrics 为 `None`。
- eval 对比 Phase A/B，确认准确率不降。

## Phase D：证据驱动增强

Phase D 不预设实现，只根据 Phase A-C 的 telemetry 和 eval gap 决定是否继续。

| 观测结论 | 可能动作 |
| --- | --- |
| zero-hit / false-skip 多 | 单独设计 Stage 3 semantic recall 补漏池。 |
| top-N 语义重复严重 | 引入 MMR final selection。 |
| high-conflict 样本仍误召 | 条件触发 conflict classifier 或 cross-encoder。 |
| semantic score 提升有限 | 停留在 Phase C 或下调语义权重。 |
| latency 超预算 | 保持 semantic observation only 或换更小模型。 |

### MMR 候选条件

仅当 `selected_pairwise_similarity_max` 长期高于阈值，并且人工检查认为上下文重复造成实际回答质量下降时再做。

### Cross-encoder 候选条件

仅在以下条件同时满足时考虑：

- candidate_count > 6
- top scores 接近
- 当前轮涉及住宿、老人儿童、预算、交通疲劳等高影响约束
- 规则/embedding reranker 的 eval gap 明确来自语义判断不足

## Destination normalization

目的地别名/层级是有价值的，但不进入本 spec 的 Stage 4 主线。原因：

- 它更接近实体归一化 / retrieval plan / memory metadata 的共享能力。
- hard mismatch 会改变现有行为，需要独立 eval。
- 当前 reranker 只保留 destination 作为加权信号。

如果后续要做，单独开 `destination-normalization-design.md`，覆盖：

- alias 数据来源
- region 层级关系
- 中日英别名
- hard mismatch feature flag
- retrieval 与 reranker 共用接口

## Embedding persistence future work

本 spec Phase B 不持久化 embedding。若 runtime embedding 延迟不可接受，再单独设计 embedding 存储生命周期：

- sidecar JSON 还是 SQLite / vector store
- profile 与 episode slice 的 embedding schema
- model version 字段
- backfill job
- 模型切换时的失效与重算
- 写入失败是否阻塞 memory extraction/archive
- 隐私与磁盘体积控制

该能力不与 Stage 4 reranker upgrade 绑定。

## Telemetry contract

新增 telemetry 字段应进入 trace；SSE 是否透传由前端需求决定，默认先进入 trace/stats，不要求前端 UI 展示。

```json
{
  "reranker_selected_ids": ["..."],
  "reranker_final_reason": "...",
  "reranker_fallback": "none",
  "reranker_per_item_reason": {
    "item_id": "human readable reason"
  },
  "reranker_per_item_scores": {
    "item_id": {
      "bucket_score": 1.0,
      "domain_exact_score": 0.5,
      "keyword_exact_score": 0.2,
      "destination_score": 0.0,
      "recency_score": 0.8,
      "applicability_score": 0.35,
      "conflict_score": 0.0,
      "content_semantic_score": 0.0,
      "domain_semantic_score": 0.0,
      "rule_score": 0.71,
      "source_normalized_score": 1.0,
      "final_score": 1.84
    }
  },
  "reranker_intent_label": "recommend",
  "reranker_selection_metrics": {
    "selected_pairwise_similarity_max": null,
    "selected_pairwise_similarity_avg": null
  },
  "reranker_semantic_fallback": "disabled"
}
```

## Evaluation

### Deterministic tests

- `backend/tests/test_recall_reranker.py`
- `backend/tests/test_reranker_eval.py`
- new invariance test for Phase A default config
- new mock embedding tests for Phase B
- new dynamic budget tests for Phase C

### Golden cases

Keep existing reranker-only cases and add:

- same-intent synonym cases:
  - 安静 / 清幽 / 避世 / 不商业
  - 亲子 / 带孩子 / 儿童友好
  - 少走路 / 别太累 / 长辈轻松
- domain semantic cases:
  - 住宿 / 住处 / 酒店 / 民宿
  - 预算 / 省钱 / 花费
- fallback cases:
  - semantic provider disabled
  - missing embeddings
  - timeout
- dynamic budget cases:
  - profile-heavy
  - slice-heavy
  - balanced
  - zero affinity fallback

### Metrics

- selected ids exact match for deterministic eval
- per-signal score snapshots
- false drop / false keep for hard filters
- P50/P95 reranker latency
- semantic fallback rate
- pairwise selected similarity

## Rollout

1. Phase A lands with no model dependency and behavior invariance.
2. Phase B lands with semantic disabled.
3. Enable semantic provider with weights still 0; collect telemetry.
4. Run reranker-only eval and selected live shadow traces.
5. If semantic signal improves synonym cases without regressions, raise weights in small increments.
6. Phase C dynamic budget enabled behind config flag.
7. Phase D decisions require eval evidence and separate design.

## Open implementation notes

- Do not use Python or shell ad-hoc writes for code edits; use normal project edit flow.
- Keep `per_item_reason` backward compatible.
- Keep `RecallCandidate` public fields unchanged.
- Do not commit this spec unless also updating `PROJECT_OVERVIEW.md` as required by repo policy.
