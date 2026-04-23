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
5. **分数尺度一致** — 所有信号归一化到 [0,1] 再加权求和，不混用 RRF 与加权和

## 架构总览

```
RecallCandidate 列表
        │
        ▼
┌─────────────────────┐
│  Hard Filter        │  conflict >= 0.95 → 丢弃（现有逻辑）
│  (门槛信号)          │  weak relevance → 丢弃（现有逻辑）
│                     │  Phase C 增加: destination hard mismatch → 强降权（feature flag）
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Prior Score        │  profile/slice_source_prior × IntentVector
│  (先验加权)          │  + bucket_score
│  分数已归一化到 [0,1] │  + recency_score
│                     │  + applicability_score
│                     │  + domain_exact_score
│                     │  + keyword_exact_score
│                     │  + destination_score
│                     │  - conflict_penalty (soft, < 0.95 部分)
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Semantic Score      │  content_semantic_score — min-max 归一化到 [0,1]
│  (语义信号)          │  domain_semantic_score — min-max 归一化到 [0,1]
│                     │  negative_semantic_score — 否定改写相似度
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Score Merge        │  final_score = weighted_sum(prior_scores)
│  (加权和融合)         │              + α × content_semantic_score
│                     │              + β × domain_semantic_score
│                     │              - γ × negative_semantic_score
│                     │  α, β 从 config 读取，Phase B 默认 0
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Source Normalization│  profile / slice 分池归一化（现有逻辑）
│  & Dynamic Budget    │  + IntentVector 驱动的动态配额
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Dedupe & Select    │  group dedupe（现有逻辑）
│                     │  + SelectionMetrics 观测点
│                     │    selected_pairwise_similarity_max/avg
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

融合采用**加权和**而非 RRF，原因：
- 我们的候选池很小（≤15），RRF 消除尺度污染的优势不明显
- 分池归一化已经解决了跨源尺度问题
- 加权和的 α/β 参数空间直观、可逐步上调，无需担心 RRF 的 k 常数与权重联合调参

## Phase A：Reranker 可观测性与配置化

**目标**：在不引入任何模型、不改变任何行为的前提下，加固规则骨架、暴露信号级遥测、为后续 Phase 打好接口基础。

### A1：已知链路修复

- 确认 `active_plan` (TravelPlanState) 正确传入 reranker
- 确认所有 RecallCandidate 字段在 symbolic_recall 构建时正确填充

### A2：意图权重配置化

将 `_intent_weights` 的 4 组硬编码魔数抽到 `MemoryRerankerConfig`：

```python
from types import MappingProxyType

@dataclass(frozen=True)
class IntentWeightProfile:
    source_prior: float          # profile/slice 共用，从 IntentVector 派生
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
    # 新增：意图权重配置化，用 MappingProxyType 保证不可变
    intent_weights: tuple[tuple[str, IntentWeightProfile], ...] = (
        ("profile", IntentWeightProfile(
            source_prior=1.0, bucket_weight=0.34, domain_weight=0.24,
            keyword_weight=0.18, destination_weight=0.08, recency_weight=0.06,
            applicability_weight=0.10, conflict_weight=1.4,
        )),
        ("slice", IntentWeightProfile(
            source_prior=1.0, bucket_weight=0.16, domain_weight=0.22,
            keyword_weight=0.18, destination_weight=0.24, recency_weight=0.14,
            applicability_weight=0.08, conflict_weight=1.0,
        )),
        ("recommend", IntentWeightProfile(
            source_prior=0.9, bucket_weight=0.22, domain_weight=0.22,
            keyword_weight=0.20, destination_weight=0.18, recency_weight=0.10,
            applicability_weight=0.14, conflict_weight=1.2,
        )),
        ("default", IntentWeightProfile(
            source_prior=0.84, bucket_weight=0.24, domain_weight=0.22,
            keyword_weight=0.18, destination_weight=0.14, recency_weight=0.08,
            applicability_weight=0.12, conflict_weight=1.2,
        )),
    )
```

`_intent_weights` 函数改为从 config 读取，保留关键词匹配逻辑但输出 config key + 对应 IntentWeightProfile。

注意 `IntentWeightProfile.source_prior` 是单个值，在意图分类为 profile 时表示 `profile_source_prior`（slice_source_prior 由 IntentVector 控制），在意图分类为 slice 时表示 `slice_source_prior`。Phase B 引入 IntentVector 后，source_prior 从 IntentVector 派生，此字段从 IntentWeightProfile 中移除（见 B3 迁移说明）。

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
    # Phase B 新增字段先占位，默认 0.0
    content_semantic_score: float = 0.0
    domain_semantic_score: float = 0.0
    negative_semantic_score: float = 0.0
    # 意图维度预留，Phase B 填充
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
def _compute_hard_filter(
    candidate: RecallCandidate, user_message: str, ...
) -> tuple[bool, str]:
    """返回 (是否通过, 过滤原因)。Phase A 重构现有 conflict/weak 逻辑。"""

def _compute_prior_score(candidate: RecallCandidate, ...) -> float:
    """返回先验加权分。Phase A 重构现有 _score_candidate 逻辑。"""

def _compute_semantic_score(
    candidate: RecallCandidate, query_embedding: list[float] | None, ...
) -> tuple[float, float, float]:
    """Phase A 返回 (0.0, 0.0, 0.0)，Phase B 接入 embedding。
    返回 (content_semantic_score, domain_semantic_score, negative_semantic_score)。"""
```

### A5：行为等价强测试

新增 `test_phase_a_invariance`：随机生成 100 个候选场景，断言默认 config 下新代码输出的 `selected_item_ids` 和 `per_item_scores` 的每个字段值必须 bit-exact 等于旧代码。不是只跑 18 个 golden case，而是确保数值完全一致。

### A 的验收标准

- 所有 11 个现有单元测试 + 18 个 reranker-only eval golden case 通过
- 100 场景 invariance test: 默认 config 输出 bit-exact 等价于旧代码
- per_item_scores 在 SSE trace 中可见
- intent 权重从 yaml config 可调
- 无模型依赖、无新包引入
- **不新增任何 hard filter 行为**（架构图中的 hard filter 只是重述现有逻辑）

---

## Phase B：Embedding 语义分数

**目标**：引入中文 embedding 模型，为已有候选打语义分，与规则分并行，权重从 0 开始上调。

### B1：模型选型与部署

| 选项 | 模型 | 参数量 | ONNX CPU 延迟 | 中文覆盖 |
|------|------|--------|---------------|---------|
| 推荐 | `shibing624/text2vec-base-chinese` | ~100M | ~5-10ms/query | 原生中文 |
| 备选 | `BAAI/bge-small-zh-v1.5` | ~33M | ~3-5ms/query | 中文优化 |
| 备选 | `BAAI/bge-m3` (ONNX) | ~110M | ~8-15ms/query | 中英日韩多语言 |

运行时部署：
- 模型打包进 Docker 镜像，不启动时下载
- 进程级 model loading（不是 session 级），服务启动时加载一次，常驻内存
- 内存预算：ONNX 模型 ~200MB + embedding cache ~50MB（1000 候选 × 768 dim × 4 bytes）
- 新增依赖：`onnxruntime`（~30MB）、模型 ONNX 文件（~100-200MB）

### B2：Embedding 存储与生命周期

这是 Phase B 最大的工程改动。embedding 不存在 RecallCandidate 公共 contract 中，由 reranker 内部的 `_EmbeddingLookup` 类负责加载。

| 存储位置 | Profile embedding | Slice embedding | Domain embedding |
|---------|-------------------|-----------------|-----------------|
| 格式 | sidecar JSON 文件：与 profile 同目录，文件名 `{item_id}.emb.json`，内容为 `{"model_name": "text2vec-base-chinese", "vector": [0.1, ...]}` | SQLite 表 `episode_embeddings(slice_id TEXT PK, model_name TEXT, embedding_blob BLOB, created_at TEXT)` | 进程级 dict 缓存，启动时从代码内硬编码的中文同义词列表计算 |
| 写入时机 | Profile extraction 完成后异步写入（fire-and-forget，写失败不阻塞） | Episode archive 归档时同步写入 | 不持久化，启动时计算 |
| 失效策略 | `model_name` 字段标记版本；换模型时旧 sidecar 文件标记为 stale，按需重算 | 同上；换模型时 `DELETE FROM episode_embeddings WHERE model_name != ?` | 换模型时进程级缓存自动重建 |
| 读取时机 | Reranker 调用时按 item_id 查找；miss 则该候选 semantic_score=0.0 | 同左 | 每轮计算一次 domain → embedding 映射，session 内缓存 |

`_EmbeddingLookup` 类接口：

```python
class _EmbeddingLookup:
    def __init__(self, model_name: str, model_dir: str, domain_descriptions: dict[str, list[str]]):
        self._model = _load_onnx_model(model_dir)  # 进程级加载
        self._domain_cache: dict[str, list[float]] = {}  # domain → embedding

    def embed_query(self, text: str) -> list[float]:
        """每轮用户消息调用一次，session 内缓存"""

    def get_candidate_embedding(self, candidate: RecallCandidate) -> list[float] | None:
        """按 item_id 查找 embedding，miss 返回 None"""

    def get_domain_embedding(self, domain: str) -> list[float] | None:
        """从进程级缓存获取 domain embedding"""

    def invalidate_model(self, new_model_name: str) -> None:
        """换模型时清除所有缓存，标记旧 sidecar 为 stale"""
```

### B3：语义分数计算

新增三个信号，与规则分**并行**：

```python
def _content_semantic_score(
    query_embedding: list[float],
    candidate_embedding: list[float],
) -> float:
    """cosine similarity, max(0, sim)，归一化到 [0, 1]"""
    return max(0.0, cosine_similarity(query_embedding, candidate_embedding))

def _domain_semantic_score(
    query_embedding: list[float],
    candidate_domains: list[str],
    domain_embeddings: dict[str, list[float]],
) -> float:
    """query 与候选 domain 列表（中文同义词 mean pooling）的 max cosine"""
    if not candidate_domains:
        return 0.0
    scores = [
        cosine_similarity(query_embedding, domain_embeddings[d])
        for d in candidate_domains if d in domain_embeddings
    ]
    return max(scores) if scores else 0.0

def _negative_semantic_score(
    query_embedding: list[float],
    candidate: RecallCandidate,
    lookup: _EmbeddingLookup,
) -> float:
    """当候选 polarity 为 avoid/reject 时，将 content_summary 改写为否定形式，
    计算 query 与改写后文本的 embedding 相似度。如果相似度高，说明用户正在
    说支持这个否定偏好的话——这是冲突信号而非匹配信号。

    例如: candidate="喜欢红眼航班" polarity=avoid
    → 改写为 "不喜欢红眼航班"
    → 计算 cosine(query, embed("不喜欢红眼航班"))
    → 如果用户说"别太累"，改写文本与 query 语义接近，分数高
    """
    if (candidate.polarity or "").lower() not in ("avoid", "reject", "dislike"):
        return 0.0
    rewritten = _negation_rewrite(candidate.content_summary)
    rewritten_emb = lookup.embed_query(rewritten)
    return max(0.0, cosine_similarity(query_embedding, rewritten_emb))
```

Domain embedding 使用中文同义词列表解决英文枚举对中文模型无效的问题：

```python
DOMAIN_SEMANTIC_DESCRIPTIONS: dict[str, list[str]] = {
    "itinerary": ["行程", "路线", "游览计划", "景点安排"],
    "pace": ["节奏", "行程节奏", "轻松", "紧凑"],
    "food": ["餐饮", "吃饭", "美食", "饭店", "小吃"],
    "hotel": ["酒店", "住宿", "宾馆", "旅馆"],
    "accommodation": ["住宿", "住处", "房源"],
    "flight": ["机票", "航班", "飞机", "飞行"],
    "train": ["火车", "新干线", "高铁", "JR"],
    "budget": ["预算", "花费", "价格", "省钱", "费用"],
    "family": ["亲子", "带孩子", "儿童", "家庭出行", "小孩"],
    "accessibility": ["无障碍", "轮椅", "方便", "行动不便"],
    "planning_style": ["安排风格", "计划方式", "自由行", "跟团"],
    "documents": ["签证", "护照", "入境", "材料"],
    "general": ["一般", "综合", "其他"],
}

def _compute_domain_embedding(domain: str, lookup: _EmbeddingLookup) -> list[float]:
    """每个 domain 的 embedding = 其中文同义词 embeddings 的 mean pooling"""
    descriptions = DOMAIN_SEMANTIC_DESCRIPTIONS.get(domain, [domain])
    embs = [lookup.embed_query(desc) for desc in descriptions]
    return [sum(dim) / len(embs) for dim in zip(*embs)]
```

### B4：意图连续化（接口铺设）

引入 `IntentVector` 数据结构，但 Phase B 阶段仍然由规则生成 4 个固定档位：

```python
@dataclass(frozen=True)
class IntentVector:
    profile_affinity: float  # 0.0 ~ 1.0
    slice_affinity: float    # 0.0 ~ 1.0

def _compute_intent_vector(
    user_message: str,
    retrieval_plan: RecallRetrievalPlan | None,
) -> IntentVector:
    """Phase B: 规则生成，输出固定档位。
    后续 Phase 可以让 Stage 2 query tool 直接输出连续值。"""
    source = retrieval_plan.source if retrieval_plan else "hybrid_history"
    reason = (retrieval_plan.reason if retrieval_plan else "") or ""

    if source == "profile" or "profile_" in reason:
        return IntentVector(profile_affinity=1.0, slice_affinity=0.62)
    if source == "episode_slice" or "past_trip" in reason:
        return IntentVector(profile_affinity=0.62, slice_affinity=1.0)
    if any(w in user_message for w in ("推荐", "比较好", "适合我", "怎么安排")):
        return IntentVector(profile_affinity=0.9, slice_affinity=0.9)
    return IntentVector(profile_affinity=0.84, slice_affinity=0.84)
```

**迁移说明**：引入 IntentVector 后，`IntentWeightProfile` 中的 `source_prior` 字段变成死代码。Phase B 的同一 commit 中移除 `source_prior`，改为从 `IntentVector` 派生：

```python
# Phase A IntentWeightProfile 有 source_prior 字段
# Phase B 移除 source_prior，改为：
profile_source_prior = intent.profile_affinity
slice_source_prior = intent.slice_affinity
```

### B5：语义权重渐进上调

Phase B 初始部署时语义权重为 0（行为与 Phase A 完全一致）。通过 config 逐步上调：

```python
@dataclass(frozen=True)
class SemanticConfig:
    enabled: bool = False                             # Phase B 默认关闭
    model_name: str = "text2vec-base-chinese"          # 可切换
    content_semantic_weight: float = 0.0               # 从 0 开始，逐步上调到 0.15-0.25
    domain_semantic_weight: float = 0.0                 # 从 0 开始，逐步上调到 0.08-0.15
    negative_semantic_weight: float = 0.0               # 从 0 开始，逐步上调到 0.05-0.10
    cache_query_embedding: bool = True                  # session 内缓存
```

融合公式（加权和，所有语义分数 min-max 归一化到 [0,1] 后再加权）：

```
final_score = weighted_sum(prior_scores)
            + α × content_semantic_score
            + β × domain_semantic_score
            - γ × negative_semantic_score
```

### B6：否定改写函数

```python
_NEGATION_TEMPLATES: dict[str, str] = {
    "喜欢": "不喜欢",
    "偏好": "不偏好",
    "推荐": "不推荐",
    "想住": "不想住",
    "愿意": "不愿意",
    # 更多样板在实现时扩展
}

def _negation_rewrite(content_summary: str) -> str:
    """对已是 avoid/reject 极性的候选，改写为否定形式。
    例如 "喜欢红眼航班" → "不喜欢红眼航班"
    """
    for positive, negated in _NEGATION_TEMPLATES.items():
        if positive in content_summary:
            return content_summary.replace(positive, negated, 1)
    return f"不{content_summary}"
```

### B 的验收标准

- semantic_weight=0 时，100 场景 invariance test 输出与 Phase A bit-exact 一致
- semantic_weight > 0 时，中文同义词对测试（安静/清幽、亲子/带孩子）content_semantic_score > 0.7
- negative_semantic_score 在冲突场景（"别太累" vs avoid 偏好"轻松行程"）下显著高于非冲突场景
- 单次 rerank 延迟增量 < 20ms（CPU, 候选数 ≤ 15）
- embedding 计算失败时 graceful 退化为纯规则分（所有 semantic_score = 0.0）

---

## Phase C：混合融合与动态预算

**目标**：语义分数归一化融入加权和、动态来源预算、destination 别名、MMR 观测点。

### C1：加权和融合（取代原 RRF 方案）

所有信号统一归一化到 [0,1] 再加权求和，语义分数与规则分数在同一尺度上竞争：

```
prior_score = intent_weighted_sum(
    source_prior_normalized,  # 分池归一化后的 source_prior
    bucket_score,             # 已有
    domain_exact,             # Jaccard，已归一化
    keyword_exact,            # Jaccard，已归一化
    destination_score,        # 已归一化
    recency_score,            # 指数衰减，已归一化到 [0,1]
    applicability_score,      # 已有
    - conflict_penalty,       # soft part (< 0.95)
)

semantic_score = α × content_semantic  +  β × domain_semantic  -  γ × negative_semantic

final_score = prior_score + semantic_score
```

从 Phase B 到 Phase C 的权重迁移：Phase B 的 `content_semantic_weight: 0.15` 直接等价于 Phase C 的 `α = 0.15`，无需换算。

### C2：动态来源预算

根据 `IntentVector` 动态计算 profile/slice 配额：

```python
def _compute_source_budget(
    intent: IntentVector,
    config: MemoryRerankerConfig,
) -> SourceBudget:
    total = config.hybrid_top_n
    denominator = max(intent.profile_affinity + intent.slice_affinity, 1e-6)  # 除零防御
    profile_share = round(total * intent.profile_affinity / denominator)
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
    selected_pairwise_similarity_max: float | None = None  # 缺失时为 None
    selected_pairwise_similarity_avg: float | None = None   # 缺失时为 None
    # 供 eval 分析 top-N 是否语义高度重叠
```

计算时机：选出最终 top-N 后，两两计算 embedding cosine，取 max 和 avg。如果 semantic 未启用或部分候选缺 embedding，输出 None 而非 0.0，避免 telemetry 误读。

### C4：destination 别名/层级映射

增加目的地归一化层，独立 feature flag 控制：

```python
DESTINATION_ALIAS: dict[str, str] = {
    "京都": "京都", "Kyoto": "京都", "京都市": "京都",
    "东京": "东京", "Tokyo": "东京", "东京都": "东京",
    "大阪": "大阪", "Osaka": "大阪", "大阪市": "大阪",
    "奈良": "奈良", "Nara": "奈良",
    "千叶": "千叶", "Chiba": "千叶",
    "横滨": "横滨", "Yokohama": "横滨",
    "神户": "神户", "Kobe": "神户",
    # ...
}

DESTINATION_HIERARCHY: dict[str, list[str]] = {
    "关东": ["东京", "千叶", "�的�的�埼玉", "神奈川"],
    "关西": ["京都", "大阪", "奈良", "兵库"],
    # ...
}
```

`_destination_match` 从子串匹配升级为：先归一化别名，再检查层级包含。**只在 feature flag 启用时生效**，并有独立 eval 评估其对现有 golden case 的影响。

### C5：destination hard mismatch 强降权（新增行为，feature flag）

当候选的 destination 与当前旅行目的地既无归一化匹配、也无层级包含时，将候选的 `destination_score` 从当前的 0.0（相当于中性保持）改为 -0.3（强降权但不丢弃）。这是 Phase C 唯一的行为变更，由 feature flag 控制，不在此前 Phase 引入。

### C 的验收标准

- 与 Phase A/B eval 对比，整体准确率不降
- 动态预算在 profile/slice 混合意图下正确分配
- MMR 观测点在 telemetry 中可见（semantic 启用时为 float，未启用时为 None）
- destination 别名测试覆盖 5+ 个常见日文地名
- destination hard mismatch 强降权有独立 feature flag，默认关闭

---

## Phase D：证据驱动增强

**目标**：根据 Phase C 的 eval 数据和 MMR 观测点决定后续动作，不预设实现。

### 可能路径

| 观测结论 | 动作 |
|---------|------|
| top-N 语义重叠严重（pairwise_similarity_avg > 0.85） | 引入 MMR 排序 |
| uncertain 样本多（top-3 scores 差值 < 0.05） | 条件触发 cross-encoder 精排 |
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
| RRF 融合 | 尺度不匹配问题在小候选池上不值得，加权和更直观 |
| LLM reranker | 延迟和成本不可接受，排除 |
| RecallCandidate 公共 contract 加 embedding_vector | 用内部 `_EmbeddingLookup` 隔离，不污染公共接口 |
| Phase A 新增 hard filter 行为 | Phase A 只重构接口，不改行为；destination mismatch 强降权在 Phase C feature flag |
| conflict 检测完全依赖规则 | Phase B 加 negative_semantic_score 补充，不替换规则 |

## 三大痛点对应

| 痛点 | 解决 Phase | 方案 |
|------|-----------|------|
| ① 同义词泛化差 | Phase B | content_semantic_score + domain_semantic_score（中文 embedding） |
| ② 冲突检测靠固定词表 | Phase B | negative_semantic_score（否定改写 + embedding 相似度） |
| ③ 意图分类太粗糙 | Phase B 接口 + Phase C 收益 | IntentVector 2 维连续谱 + 动态来源预算 |

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
          source_prior: 1.0
          bucket_weight: 0.34
          # ... 完整 8 维（source_prior 在 Phase B 移除）
        slice: { ... }
        recommend: { ... }
        default: { ... }

# Phase B: 增加语义配置
      semantic:
        enabled: false
        model_name: "text2vec-base-chinese"
        content_semantic_weight: 0.0
        domain_semantic_weight: 0.0
        negative_semantic_weight: 0.0
        cache_query_embedding: true

# Phase C: 增加动态预算和 destination 别名
      dynamic_budget: true
      destination_alias_enabled: false  # feature flag
      destination_mismatch_penalty: 0.0  # Phase C 默认关闭，开启后 -0.3
```

## 延迟预算

| Phase | P50 延迟增量 | P95 延迟增量 |
|-------|-------------|-------------|
| A | 0ms | 0ms |
| B | ~10ms | ~20ms (embedding 计算) |
| C | ~2ms | ~5ms (动态预算 + destination 别名) |
| D (如触发 cross-encoder) | ~50ms | ~100ms |

## 依赖演进

| Phase | 新增依赖 | 体积 |
|-------|---------|------|
| A | 无 | 0 |
| B | onnxruntime, 中文 embedding 模型 ONNX 文件 | ~250MB |
| C | 无 | 0 |
| D | 可能增加小型 cross-encoder ONNX | ~400MB |