# Travel Memory Stage 3 Hybrid Recall Upgrade v2

## 背景

当前记忆召回主链路是：

1. Stage 0/1 判断本轮是否需要召回历史记忆。
2. Stage 2 生成 `RecallRetrievalPlan`。
3. Stage 3 从 profile / episode slice 产生 `RecallCandidate`。
4. Stage 4 reranker 过滤、去重、排序、裁剪，决定最终注入 prompt 的记忆。

Stage 4 v2 已经把 reranker 设计成规则 + 语义信号 + telemetry 的混合 reranker。但 Stage 4 只能处理已经进入候选池的记忆。当前 Stage 3 仍以 `symbolic_recall.py` 的 exact domain / keyword / destination 匹配为主，容易出现 zero-hit 或候选池过窄。

本 spec 设计 Stage 3 的理想升级：把 Stage 3 从“单一路径 symbolic recall”升级为“多路候选生成 + 证据融合”的 Hybrid Candidate Generator。

## 对 v1 方案的评审结论

`2026-04-23-stage3-recall-upgrade-design.md` 的方向正确：它意识到 Stage 3 零命中是 Stage 4 的上游瓶颈，并提出目的地归一化、候选池扩大、semantic fallback。但它有几个落地风险：

1. **范围太窄**：v1 只改 `rank_profile_items()` / `rank_episode_slices()` 的评分和过滤逻辑。理想 Stage 3 不只是 ranker，而是候选生成系统，需要 query normalization、source widening、多路召回和候选融合。
2. **Phase A 行为等价表述不成立**：目的地归一化和候选池扩大都会改变候选集合，不能称为零行为风险。它们应该放在 feature flag 后并单独 eval。
3. **semantic fallback 触发太晚**：v1 只在 exact-match 全部失败时对 item 做 semantic fallback。这样无法改善 exact 命中但候选池质量差的情况，也不能让 semantic lane 与 symbolic lane 互相补充。
4. **目的地 sibling 匹配过宽**：把“大阪”和“京都”因为同属关西就视为匹配，容易误召。区域关系应产生弱证据，而不是等价匹配。
5. **候选池大小和 `query.top_k` 语义混杂**：Stage 2 的 `top_k` 是检索计划的一部分，Stage 3 的 pre-rerank pool limit 是工程预算，两者不应该混用。
6. **embedding 生命周期仍需边界**：v1 不持久化 embedding 是合理的早期选择，但要明确 runtime cache、lane timeout、缺失降级和后续持久化路径。

v2 采纳 v1 的核心意图，但重新划分职责：Stage 3 负责高召回候选生成，Stage 4 负责精排和注入裁剪。

## 设计原则

1. **Stage 3 职责**：扩大并改善候选池，不做最终注入决策。
2. **多路召回**：symbolic、expanded lexical、semantic、entity/destination、temporal lane 并行或按需执行。
3. **证据随候选同行**：Stage 3 输出的不只是 candidate，还要输出 retrieval evidence，供 Stage 4 和 eval 消费。
4. **精确优先、语义补漏**：exact match 是高置信证据；semantic/BM25/entity 是补充证据，不直接替代。
5. **可控扩宽**：source widening、destination region match、semantic lane 都有 caps、threshold 和 telemetry。
6. **默认可回滚**：每个新增 lane 和 widening 策略都由 config 开关控制。
7. **不污染 `RecallCandidate` 基础字段**：不在公共 dataclass 加 embedding vector；用 sidecar `RetrievalEvidence` 承载 Stage 3 信号。

## 非目标

| 不做 | 原因 |
| --- | --- |
| 改 Stage 0/1 recall gate | 召回触发与本 spec 分离。 |
| 改 Stage 2 query tool schema | Stage 3 可消费现有 `RecallRetrievalPlan`，未来再扩展。 |
| 直接替换 Stage 4 reranker | Stage 3 只生成候选，最终排序仍归 Stage 4。 |
| 默认启用 LLM agentic retrieval | 成本、延迟和稳定性不适合每轮主链路。 |
| 把 region sibling 当 exact destination match | 区域关系是弱证据，不能等价替代目的地。 |
| 一次性引入持久化 vector store | 先用 provider + runtime cache；持久化索引单独演进。 |

## 目标架构

```text
RecallRetrievalPlan
  + user_message
  + plan facts
        │
        ▼
Stage3QueryNormalizer
  - normalize destination / aliases
  - expand domains and keywords
  - derive source widening policy
  - produce RecallQueryEnvelope
        │
        ▼
Retrieval Lanes
  ├─ SymbolicLane       exact domain / keyword / destination
  ├─ LexicalLane        expanded keyword / phrase / BM25-lite
  ├─ SemanticLane       query embedding vs candidate text embedding
  ├─ EntityLane         destination / traveler / transport / lodging entities
  └─ TemporalLane       recency and same-trip-style priors
        │
        ▼
Candidate Fusion
  - union by item_id
  - lane-level weighted RRF
  - source caps and lane caps
  - exact evidence wins ties
        │
        ▼
Candidate Enrichment
  - RecallCandidate[]
  - RetrievalEvidence sidecar
  - Stage 3 telemetry
        │
        ▼
Stage 4 Reranker
```

Stage 3 可以使用 RRF，因为它融合的是多个 retrieval lane 的排名；Stage 4 v2 不使用 RRF，因为 Stage 4 是最终精排，主融合仍采用 calibrated weighted sum。

## 数据结构

### RecallQueryEnvelope

Stage 3 内部 query 表示，保留原始 plan，同时记录规范化和扩展结果。

```python
@dataclass(frozen=True)
class RecallQueryEnvelope:
    plan: RecallRetrievalPlan
    user_message: str
    source_policy: SourcePolicy
    original_domains: tuple[str, ...]
    expanded_domains: tuple[str, ...]
    original_keywords: tuple[str, ...]
    expanded_keywords: tuple[str, ...]
    destination: str
    destination_aliases: tuple[str, ...]
    destination_children: tuple[str, ...]
    destination_region: str
```

### SourcePolicy

```python
@dataclass(frozen=True)
class SourcePolicy:
    requested_source: str
    search_profile: bool
    search_slices: bool
    widened: bool = False
    widening_reason: str = ""
```

Rules:

- `profile` source 默认只查 profile；若 enabled source widening 且 profile exact/lexical/semantic 候选不足，可补少量 slice。
- `episode_slice` source 默认只查 slice；若 enabled source widening 且 slice 候选不足，可补少量 profile。
- `hybrid_history` 两边都查。

### Stage3Candidate

内部候选对象。

```python
@dataclass
class Stage3Candidate:
    candidate: RecallCandidate
    evidence: RetrievalEvidence
```

### RetrievalEvidence

```python
@dataclass
class RetrievalEvidence:
    item_id: str
    source: str
    lanes: list[str]
    lane_scores: dict[str, float]
    lane_ranks: dict[str, int]
    fused_score: float
    matched_domains: list[str]
    matched_keywords: list[str]
    matched_entities: list[str]
    destination_match_type: str  # exact | alias | parent_child | region_weak | none
    semantic_score: float | None = None
    lexical_score: float | None = None
    temporal_score: float | None = None
    retrieval_reason: str = ""
```

`RecallCandidate` 仍用于兼容 Stage 4 现有入口；`RetrievalEvidence` 通过 sidecar map 进入 Stage 4 v2 / trace：

```python
dict[str, RetrievalEvidence]  # item_id -> evidence
```

### Stage3RecallResult

```python
@dataclass
class Stage3RecallResult:
    candidates: list[RecallCandidate]
    evidence_by_id: dict[str, RetrievalEvidence]
    telemetry: Stage3Telemetry
```

### Stage3Telemetry

```python
@dataclass
class Stage3Telemetry:
    lanes_attempted: list[str]
    lanes_succeeded: list[str]
    source_policy: dict[str, Any]
    query_expansion: dict[str, list[str]]
    candidates_by_lane: dict[str, int]
    candidates_by_source: dict[str, int]
    total_candidates_before_fusion: int
    total_candidates_after_fusion: int
    zero_hit: bool
    fallback_used: str = "none"
    lane_errors: dict[str, str] = field(default_factory=dict)
```

## Configuration

```python
@dataclass(frozen=True)
class Stage3LaneConfig:
    enabled: bool = True
    top_k: int = 20
    timeout_ms: int = 25

@dataclass(frozen=True)
class Stage3SemanticConfig(Stage3LaneConfig):
    provider: str = "fastembed"
    model_name: str = "BAAI/bge-small-zh-v1.5"
    cache_dir: str = "backend/data/embedding_cache"
    local_files_only: bool = False
    min_score: float = 0.58
    cache_max_items: int = 10000
    cache_max_mb: int = 64

@dataclass(frozen=True)
class Stage3FusionConfig:
    rrf_k: int = 60
    lane_weights: tuple[tuple[str, float], ...] = (
        ("symbolic", 1.0),
        ("lexical", 0.7),
        ("semantic", 0.6),
        ("entity", 0.8),
        ("temporal", 0.3),
    )
    max_total_candidates: int = 24
    max_profile_candidates: int = 12
    max_slice_candidates: int = 12
    exact_candidate_floor: int = 3

@dataclass(frozen=True)
class Stage3SourceWideningConfig:
    enabled: bool = False
    min_candidates_for_requested_source: int = 2
    widening_top_k: int = 3

@dataclass(frozen=True)
class Stage3Config:
    enabled: bool = True
    query_normalization_enabled: bool = True
    destination_normalization_enabled: bool = False
    symbolic: Stage3LaneConfig = field(default_factory=Stage3LaneConfig)
    lexical: Stage3LaneConfig = field(default_factory=lambda: Stage3LaneConfig(enabled=False))
    semantic: Stage3SemanticConfig = field(default_factory=lambda: Stage3SemanticConfig(enabled=False))
    entity: Stage3LaneConfig = field(default_factory=lambda: Stage3LaneConfig(enabled=False))
    temporal: Stage3LaneConfig = field(default_factory=lambda: Stage3LaneConfig(enabled=False))
    fusion: Stage3FusionConfig = field(default_factory=Stage3FusionConfig)
    source_widening: Stage3SourceWideningConfig = field(default_factory=Stage3SourceWideningConfig)
```

Default behavior:

- Only `symbolic` lane is enabled by default.
- `lexical`, `semantic`, `entity`, `temporal`, `destination_normalization`, and `source_widening` default disabled.
- This preserves current symbolic recall behavior in compatibility mode.

## Stage3QueryNormalizer

### Domain and keyword expansion

Do not mutate `RecallRetrievalPlan`; create `RecallQueryEnvelope`.

```python
DOMAIN_SYNONYMS = {
    "hotel": ["住宿", "酒店", "住处", "民宿", "落脚点"],
    "accommodation": ["住宿", "酒店", "住处", "民宿", "落脚点"],
    "pace": ["节奏", "轻松", "少走路", "别太累", "慢一点"],
    "family": ["亲子", "带孩子", "儿童", "家庭出行"],
    "budget": ["预算", "花费", "省钱", "费用"],
    "flight": ["航班", "飞机", "机票", "红眼"],
    "train": ["火车", "高铁", "新干线", "JR"],
    "food": ["餐饮", "吃饭", "美食", "餐厅"],
}
```

Expansion rules:

- Keep original keywords as high-confidence exact terms.
- Add synonyms as lower-confidence lexical terms.
- Record expansions in telemetry.
- Do not let expanded terms alone create hard conflict/drop decisions.

### Destination normalization

Destination matching must distinguish strength:

```python
class DestinationMatchType:
    EXACT = "exact"
    ALIAS = "alias"
    PARENT_CHILD = "parent_child"
    REGION_WEAK = "region_weak"
    NONE = "none"
```

Rules:

- `京都` == `京都`: exact.
- `Kyoto` / `京都市` -> `京都`: alias.
- query `关西` can retrieve child `京都` / `大阪`: parent_child.
- query `大阪` and candidate `京都` are only region_weak, not exact.
- region_weak can add evidence but must not be the only reason to admit a candidate unless source widening is enabled and lane caps allow it.

Destination data lives in `memory/destination_normalization.py` and is shared by Stage 3 and future Stage 4 only as a utility, not as hard filter policy.

## Retrieval Lanes

### SymbolicLane

Current behavior extracted into a lane:

- profile: exact domain or exact keyword match.
- slice: destination/domain/keyword match.
- bucket ordering and timestamp ordering preserved.
- output lane rank and symbolic score.

Changes from current code:

- symbolic lane should return a pre-rerank pool up to `symbolic.top_k`, not `query.top_k`.
- `query.top_k` remains Stage 2 intent, but Stage 3 owns engineering pool size.
- manager no longer slices `rank_profile_items(... )[:active_plan.top_k]`; Stage 3 returns capped candidates.

### LexicalLane

BM25-lite / token overlap over expanded query terms.

Initial implementation can be in-memory over loaded profile and slice objects:

- tokenize candidate text from domain/key/value/applicability/recall_hints/source_refs for profile.
- tokenize slice_type/content/applicability/entities/keywords for slices.
- score exact query terms higher than expanded synonyms.
- phrase hits like `别太累` / `住处` / `红眼` get a phrase bonus.

This lane is disabled by default in Phase A and can be enabled before semantic lane because it has no model dependency.

### SemanticLane

Semantic lane runs query embedding vs candidate text embedding for profile and slice objects.

Initial implementation:

- Use shared `EmbeddingProvider` protocol with `NullEmbeddingProvider` fallback.
- Default implementation uses FastEmbed with `BAAI/bge-small-zh-v1.5`.
- FastEmbed runs quantized ONNX weights on ONNX Runtime CPU and avoids PyTorch / Transformers dependencies.
- `BAAI/bge-small-zh-v1.5` is selected because it is a Chinese retrieval embedding model with MIT license, 512-dimensional vectors, and a small FastEmbed package size profile.
- Runtime cache by `(model_name, text_template_version, text_hash)`.
- No persistent embedding store in first implementation.
- Batch embed candidate texts per source.
- Enforce `semantic.min_score` and `semantic.top_k`.
- Time out and degrade without failing recall.
- Production deployments should pre-warm `cache_dir` or set `local_files_only=true`; development can allow first-run download.

Future implementation:

- Persistent embedding index can replace runtime candidate embedding behind the same `SemanticLane` interface.
- That change needs a separate embedding lifecycle spec.

### EntityLane

Entity lane extracts structured evidence, not broad text semantic match.

Initial entities:

- destination and region
- traveler hints: children, parents, elders
- lodging type: hotel, hostel, ryokan, minsu / 民宿
- transport type: flight, train, red-eye, JR
- budget terms

This lane should usually enrich candidates already found by symbolic/lexical/semantic. It can admit candidates only when:

- entity match is exact/alias/parent_child, and
- candidate also has at least one domain or keyword clue, or
- source widening is enabled.

### TemporalLane

Temporal lane should not admit candidates on its own. It provides evidence:

- recency score
- same destination / same region recency
- same traveler composition if available
- same travel style if explicit in slice taxonomy

Stage 4 still decides whether recency should matter for final injection.

## Candidate Fusion

### Fusion algorithm

Use weighted RRF across retrieval lanes:

```python
fused_score[item_id] += lane_weight / (rrf_k + lane_rank)
```

Why RRF here:

- Each lane has different score scales.
- Stage 3 needs robust candidate union, not final semantic precision.
- RRF preserves high-rank candidates from any lane without requiring cross-lane calibration.

### Fusion rules

- Union candidates by `item_id`.
- Preserve all lane evidence in `RetrievalEvidence`.
- If the same item appears in multiple lanes, merge lane scores and reasons.
- Exact symbolic evidence wins tie-breaks.
- Apply source caps after fusion:
  - `max_profile_candidates`
  - `max_slice_candidates`
  - `max_total_candidates`
- If exact candidates exist, preserve up to `exact_candidate_floor` exact candidates even if semantic candidates score higher.

### Source widening

Source widening is disabled by default.

When enabled:

- If requested source has fewer than `min_candidates_for_requested_source`, allow the other source to contribute up to `widening_top_k`.
- Widening must be recorded in `Stage3Telemetry.source_policy`.
- Widening candidates should carry `retrieval_reason` including `source_widening`.

## API shape

Introduce a single Stage 3 entrypoint:

```python
def retrieve_recall_candidates(
    query: RecallRetrievalPlan,
    profile: UserMemoryProfile,
    slices: list[EpisodeSlice],
    *,
    user_message: str,
    plan: TravelPlanState,
    config: Stage3Config,
    embedding_provider: EmbeddingProvider | None = None,
) -> Stage3RecallResult:
    ...
```

Compatibility wrappers can remain:

```python
def rank_profile_items(query, profile) -> list[RecallCandidate]: ...
def rank_episode_slices(query, slices) -> list[RecallCandidate]: ...
```

During migration, wrappers call the new entrypoint with only the symbolic lane enabled, then filter by source. This avoids duplicating old and new logic.

## Phase Plan

### Phase A：extract symbolic lane and telemetry

Goals:

- Introduce `Stage3RecallResult`, `RetrievalEvidence`, `Stage3Telemetry`.
- Extract current symbolic logic into `SymbolicLane`.
- Add entrypoint `retrieve_recall_candidates()`.
- Keep lexical/semantic/source widening disabled.
- Keep selected candidate IDs identical to current behavior under compatibility mode.

Tests:

- Existing symbolic recall tests pass.
- New telemetry fields are populated.
- Compatibility wrappers return same candidate order as current implementation.

### Phase B：query normalization and destination evidence

Goals:

- Add domain/keyword expansion in `RecallQueryEnvelope`, but do not let expansions admit candidates unless `lexical.enabled=true`.
- Add destination normalization with match types.
- Replace exact `_match_destination()` internals with match type evaluation behind `destination_normalization_enabled`.
- Do not treat region sibling as exact.

Tests:

- exact/alias/parent_child/region_weak/none match types.
- region sibling does not admit a candidate by itself.
- query `关西` can retrieve `京都` slices when parent_child matching is enabled.

### Phase C：lexical lane

Goals:

- Add in-memory BM25-lite / token overlap lane.
- Use expanded keywords and domain synonyms.
- Keep lane disabled by default.
- Add lane-level telemetry and fusion.

Tests:

- `清幽` can retrieve `安静` only when lexical expansion covers it or synonym list says so.
- exact symbolic candidates remain preserved.
- lane timeout / error degrades to symbolic only.

### Phase D：semantic lane

Goals:

- Add shared `EmbeddingProvider`.
- Add `SemanticLane` with runtime cache and no persistent store.
- Use candidate text templates with versioned cache keys.
- Fuse semantic candidates with symbolic/lexical/entity evidence.

Tests:

- mock provider semantic recall for synonym cases.
- provider disabled / exception / timeout fallback.
- semantic candidates obey min score and caps.
- no public `RecallCandidate.embedding_vector`.

### Phase E：source widening and fusion tuning

Goals:

- Enable controlled source widening behind config.
- Tune lane weights and source caps with eval.
- Expose zero-hit and lane contribution metrics.

Tests:

- profile-requested query can add limited slices only when requested source underfills and widening enabled.
- widening disabled yields old source behavior.
- `max_total_candidates` and source caps hold.

### Phase F：persistent index future work

Only if runtime semantic lane is too slow or memory volume grows:

- persistent embedding store
- model versioning
- backfill job
- invalidation on model/template change
- optional SQLite FTS or vector index

This is not part of initial Stage 3 upgrade.

## Telemetry contract

Add to memory recall telemetry / trace:

```json
{
  "stage3": {
    "lanes_attempted": ["symbolic", "lexical"],
    "lanes_succeeded": ["symbolic", "lexical"],
    "source_policy": {
      "requested_source": "hybrid_history",
      "search_profile": true,
      "search_slices": true,
      "widened": false,
      "widening_reason": ""
    },
    "query_expansion": {
      "domains": ["hotel", "accommodation"],
      "keywords": ["住宿", "住处", "酒店"]
    },
    "candidates_by_lane": {
      "symbolic": 3,
      "lexical": 4,
      "semantic": 0,
      "entity": 2,
      "temporal": 0
    },
    "candidates_by_source": {
      "profile": 4,
      "episode_slice": 3
    },
    "total_candidates_before_fusion": 9,
    "total_candidates_after_fusion": 7,
    "zero_hit": false,
    "fallback_used": "none",
    "lane_errors": {}
  }
}
```

Candidate-level evidence can be exposed in trace, not necessarily SSE UI:

```json
{
  "candidate_evidence": {
    "profile_123": {
      "lanes": ["symbolic", "lexical"],
      "lane_scores": {"symbolic": 1.0, "lexical": 0.73},
      "lane_ranks": {"symbolic": 1, "lexical": 2},
      "fused_score": 0.031,
      "matched_domains": ["hotel"],
      "matched_keywords": ["住宿", "住处"],
      "matched_entities": ["京都"],
      "destination_match_type": "alias",
      "retrieval_reason": "symbolic domain match; lexical synonym match"
    }
  }
}
```

## Evaluation

### Metrics

- Stage 3 zero-hit rate.
- recall@k against curated relevant memory IDs.
- lane contribution rate.
- semantic-only candidate acceptance rate.
- false recall candidate rate before Stage 4.
- downstream Stage 4 selected precision@k.
- P50/P95 Stage 3 latency.
- lane fallback/error rate.

### Golden case families

- Same-intent synonyms:
  - 安静 / 清幽 / 避世 / 不商业
  - 亲子 / 带孩子 / 儿童友好
  - 少走路 / 别太累 / 长辈轻松
  - 省钱 / 控预算 / 花费少
- Destination normalization:
  - Kyoto / 京都市 / 京都
  - 关西 -> 京都/大阪/奈良
  - 大阪 should not exactly match 京都 unless region weak is explicitly allowed
- Source widening:
  - profile underfills, slice has relevant evidence
  - slice underfills, profile has relevant constraints
  - widening disabled baseline
- Lane fallback:
  - lexical disabled
  - semantic disabled
  - embedding provider timeout
  - all lanes zero hit

### Invariance

Phase A compatibility mode must match current symbolic recall output:

- same candidate IDs
- same order
- same matched reasons, except additional telemetry fields

Later phases should not claim invariance because they intentionally broaden candidates.

## Rollout

1. Land Phase A with only symbolic lane enabled.
2. Add telemetry to observe current zero-hit and underfilled-source rates.
3. Enable destination normalization in shadow/eval first, then feature flag.
4. Enable lexical lane with conservative caps.
5. Enable semantic lane with provider disabled by default, then weights/caps in eval.
6. Enable source widening only after lane telemetry proves underfill is common.
7. Feed `RetrievalEvidence` into Stage 4 v2 once both contracts are stable.

## Open Implementation Notes

- Do not make Stage 3 depend on Stage 4 v2 implementation. Stage 3 can expose evidence before Stage 4 consumes it.
- Keep `RecallCandidate` public dataclass unchanged in the first implementation.
- Prefer new modules over growing `symbolic_recall.py` further:
  - `memory/recall_stage3.py`
  - `memory/retrieval_lanes.py`
  - `memory/recall_fusion.py`
  - `memory/destination_normalization.py`
  - `memory/embedding_provider.py`
- If committing this spec, update `PROJECT_OVERVIEW.md` in the same commit per repo policy.
