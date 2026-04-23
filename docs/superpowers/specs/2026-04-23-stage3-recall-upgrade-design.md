# Travel Memory Stage 3 Recall Upgrade

## 背景

当前旅行 Agent 的记忆召回链路为：

1. Stage 0/1 判断本轮是否需要历史记忆。
2. Stage 2 生成 `RecallRetrievalPlan`（LLM 工具调用或 heuristic fallback）。
3. Stage 3 用 symbolic recall 从 profile / episode slice 产生 `RecallCandidate`。
4. Stage 4 reranker 对候选做过滤、去重、排序、裁剪。

Stage 4 v2 设计（`2026-04-23-reranker-stage4-upgrade-v2-design.md`）在 reranker 层加入语义增强，但明确指出 Stage 3 "零命中"才是更根本的瓶颈——reranker 无法对未被召回的候选进行排序。本 spec 只升级 Stage 3 的候选评分与过滤层。

## 当前问题

```
backend/memory/symbolic_recall.py
  rank_profile_items()
  rank_episode_slices()
```

| 问题 | 代码位置 | 实际后果 |
| --- | --- | --- |
| 候选匹配是二值判断 | `_score_profile_item()`: domain 和 keyword 同时为空则直接丢弃 | 同义词失配：profile 写 "安静"，用户问 "清幽" → 零命中 |
| 目的地用 `==` 精确比较 | `_match_destination()` | "关西" 无法匹配 "京都/大阪/奈良" 的 slice |
| 候选池太小 | `top_k=5` 由 manager.py 外部切片 | Stage 4 reranker 只有 5 个候选可排 |
| profile 更重 | — | profile 的零命中对体验影响高于 slice |

## 设计原则

1. **Stage 3 边界**：只改 `rank_profile_items` 和 `rank_episode_slices` 的评分/过滤逻辑，以及新增 `Stage3Config` 参数传递。不改 `should_trigger_memory_recall()`、`heuristic_retrieval_plan_from_message()`、domain 词表或 Stage 2 逻辑。
2. **默认零行为风险**：Phase A 只改目的地归一化和池大小；Phase B semantic 默认 `enabled=false`，与 Phase A 行为完全一致。
3. **不污染公共 contract**：`RecallCandidate` 公共字段不变。`matched_reason` 追加文字标注语义来源。
4. **EmbeddingProvider 共用**：在 `memory/embedding_provider.py` 定义 Protocol，Stage 3 和 Stage 4 v2 共用同一套接口和 ONNX 实现，不重复定义。
5. **Stage 3 先落地，Stage 4 v2 跟上**：两者并行设计，Stage 3 不依赖 Stage 4 v2 的任何代码。

## 非目标

| 不做 | 原因 |
| --- | --- |
| 改 `should_trigger_memory_recall()` 触发逻辑 | 属于 Stage 0/1/2 范围 |
| 改 `heuristic_retrieval_plan_from_message()` domain 词表 | 同上 |
| profile / slice 持久化 embedding | 涉及 storage、backfill、模型版本生命周期，单独设计 |
| `RecallCandidate` 添加 `embedding_vector` | 会污染当前公共候选 contract |
| LLM query 扩展（方案 C） | 增加 Stage 2 延迟和 prompt 复杂度 |
| 全量语义替换（方案 B） | 丢失 bucket 优先级等结构信息，短文本 embed 不稳定 |

## 目标架构

```text
RecallRetrievalPlan
  ↓
  [Phase A] rank_profile_items_v2 / rank_episode_slices_v2
      目的地归一化：DESTINATION_ALIASES + DESTINATION_PARENTS
      exact-match 评分（逻辑不变，归一化后）
      pool 大小 = profile_top_n (10) / slice_top_n (8)
  ↓
  [Phase B, semantic enabled=true]
      对 exact-match 全部失败的 item / slice：
        cosine(query_embed, item_embed) > threshold → semantic pool
      merge(exact pool + semantic pool)
      max_semantic_per_source 上限
      去重（exact 优先）
  ↓
  Stage 4 reranker（候选池更大，质量更高）
```

## 数据结构

### Stage3Config

```python
@dataclass(frozen=True)
class Stage3SemanticConfig:
    enabled: bool = False
    provider: str = "onnx"
    model_name: str = "text2vec-base-chinese"
    model_path: str = "models/text2vec-base-chinese.onnx"
    semantic_fallback_threshold: float = 0.60
    max_semantic_per_source: int = 5
    item_text_template_version: str = "v1"

@dataclass(frozen=True)
class Stage3Config:
    profile_top_n: int = 10
    slice_top_n: int = 8
    semantic: Stage3SemanticConfig = field(default_factory=Stage3SemanticConfig)
```

### EmbeddingProvider（共享定义）

新建 `memory/embedding_provider.py`，Stage 3 和 Stage 4 v2 均从此处 import：

```python
class EmbeddingProvider(Protocol):
    def enabled(self) -> bool: ...
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

class NullEmbeddingProvider:
    def enabled(self) -> bool: return False
    def embed_texts(self, texts: list[str]) -> list[list[float]]: return []
```

ONNX 实现（`OnnxEmbeddingProvider`）放在 `memory/embedding_provider.py`，进程级加载，初始化失败自动降级到 `NullEmbeddingProvider`。

### RecallCandidate（不变）

不添加新字段。语义来源通过 `matched_reason` 追加标注：

```
"semantic_fallback: cosine=0.73 | budget 节省花费"
```

## Phase A：目的地归一化 + 候选池扩大

### A1：DestinationNormalizer

新建 `memory/destination_normalization.py`：

```python
DESTINATION_ALIASES: dict[str, list[str]] = {
    "关西":     ["大阪", "京都", "奈良"],
    "日本":     ["东京", "大阪", "京都", "奈良", "名古屋", "北海道", "冲绳", "福冈", "札幌"],
    "韩国":     ["首尔"],
    "台湾":     ["台北"],
    "法国":     ["巴黎"],
    "英国":     ["伦敦"],
    # 繁简/别称统一
    "東京":     ["东京"],
    "沖縄":     ["冲绳"],
    "福岡":     ["福冈"],
}

DESTINATION_PARENTS: dict[str, list[str]] = {
    "大阪": ["关西", "日本"],
    "京都": ["关西", "日本"],
    "奈良": ["关西", "日本"],
    "东京": ["日本"],
    "名古屋": ["日本"],
    "北海道": ["日本"],
    "冲绳": ["日本"],
    "福冈": ["日本"],
    "札幌": ["日本"],
    "首尔": ["韩国"],
    "台北": ["台湾"],
    "巴黎": ["法国"],
    "伦敦": ["英国"],
}

def expand_query_destination(destination: str) -> set[str]:
    """返回 query destination 的等价/子集目的地集合。"""
    result = {destination}
    result.update(DESTINATION_ALIASES.get(destination, []))
    return result

def destination_matches(query_destination: str, candidate_destination: str) -> bool:
    """三层匹配：精确相等 / 父→子 / 同区域。"""
    if not query_destination or not candidate_destination:
        return False
    if query_destination == candidate_destination:
        return True
    # 父→子（关西 → 大阪）
    if candidate_destination in expand_query_destination(query_destination):
        return True
    # 同区域兄弟（大阪 ↔ 京都，共同父节点 关西）
    query_parents = set(DESTINATION_PARENTS.get(query_destination, []))
    candidate_parents = set(DESTINATION_PARENTS.get(candidate_destination, []))
    return bool(query_parents & candidate_parents)
```

### A2：rank_profile_items 签名变更

```python
def rank_profile_items(
    query: RecallRetrievalPlan,
    profile: UserMemoryProfile,
    config: Stage3Config | None = None,
) -> list[RecallCandidate]: ...
```

函数内部按 `config.profile_top_n` 截断，不依赖 `query.top_k`。

manager.py 对应改动（微小）：

```python
# 移除外部切片：rank_profile_items(active_plan, profile)[:query_profile_limit]
recall_profile_items = rank_profile_items(active_plan, profile, stage3_config)
```

### A3：rank_episode_slices 签名变更

同上，内部按 `config.slice_top_n` 截断。`_match_destination()` 替换为 `destination_matches()`。

### A4：测试

- Phase A 不变性测试（`semantic.enabled=False`）：固定 fixture，`selected_item_ids` 与旧实现完全一致（不计目的地归一化修复的场景）。
- 目的地归一化单元测试：覆盖精确/父子/同区域/繁简四种匹配，以及空值/无关情况。
- 候选池大小：`profile_top_n=10` 时返回条数正确，不超限，不少于 exact-match 命中数。

验收命令：

```bash
pytest -q backend/tests/test_stage3_recall.py backend/tests/test_destination_normalization.py
```

Phase A 不新增依赖。

## Phase B：Semantic Fallback Pool

### B1：EmbeddingProvider 初始化

`memory/embedding_provider.py` 中提供 `build_embedding_provider(config: Stage3SemanticConfig) -> EmbeddingProvider`。失败降级到 `NullEmbeddingProvider`，不抛出异常。

### B2：item 文本模板（version="v1"，锁定）

```python
# profile item
def _profile_item_text_v1(item: MemoryProfileItem) -> str:
    parts = [item.domain, item.key, _stringify(item.value), item.applicability]
    return " ".join(part for part in parts if part)

# episode slice
def _slice_text_v1(slice_: EpisodeSlice) -> str:
    parts = [slice_.slice_type, slice_.content, slice_.applicability]
    return " ".join(part for part in parts if part)
```

模板版本字符串 `"v1"` 进入缓存 key。如果模板变更，必须同时更新版本号（否则会命中旧缓存）。

### B3：fallback 路径

精确定义"exact-match 全部失败"：`_score_profile_item()` 返回 `(None, "")` 时——即 `matched_domains == []` 且 `matched_keywords == []`。这是唯一触发语义打分的条件。

```python
for item in profile_items:
    exact_score, reason = _score_profile_item(query, bucket, item)
    if exact_score is not None:
        exact_pool.append(...)
    elif semantic_config.enabled and embedding_provider.enabled():
        # 批量 embed，cosine 计算
        semantic_score = cosine(query_embed, item_embed)
        if semantic_score > semantic_config.semantic_fallback_threshold:
            semantic_pool.append(... reason=f"semantic_fallback: cosine={semantic_score:.2f}")
```

批量 embed：每轮 recall 先收集所有需要语义打分的 item，批量调用 `embed_texts()`，避免逐条调用。

### B4：merge & deduplicate

```python
merged = list(exact_pool)
seen_ids = {c.item_id for c in exact_pool}
semantic_pool_sorted = sorted(semantic_pool, key=lambda c: c.score, reverse=True)
semantic_added = 0
for candidate in semantic_pool_sorted:
    if candidate.item_id not in seen_ids and semantic_added < config.semantic.max_semantic_per_source:
        merged.append(candidate)
        seen_ids.add(candidate.item_id)
        semantic_added += 1
# 截断
return merged[:config.profile_top_n]
```

exact 优先：相同 item_id 下，exact 版本保留，semantic 版本丢弃。

### B5：降级路径

- `provider.enabled() == False`：跳过语义打分，行为等价 Phase A。
- embedding 调用超时或异常：`try/except`，记录 telemetry `stage3_semantic_fallback="embedding_error"`，继续使用 exact-only 结果。
- `embed_texts()` 返回空列表：视为 provider 不可用，降级。

### B6：配置放量顺序

1. `enabled=false`（默认）：Phase A 行为。
2. `enabled=true`，threshold=0.60，max_semantic_per_source=5：观测 telemetry，看 semantic 命中分布。
3. 根据 eval 结果调整 threshold，不在 spec 中预设最终值。

### B7：Telemetry

在现有 `MemoryRecallTelemetry` 增加：

```python
stage3_semantic_fallback: str = "disabled"  # "disabled" / "provider_unavailable" / "embedding_error" / "ok"
stage3_semantic_candidates_added: int = 0
stage3_total_candidates: int = 0
```

### B8：测试

- `semantic disabled` 时，Phase A 不变性测试继续通过。
- mock provider：同义词 case 语义命中（安静↔清幽，省钱↔节省↔预算）。
- `max_semantic_per_source` 上限生效：超过时只取 top N。
- provider exception 优雅降级，返回 exact-only 候选。
- 去重：exact 和 semantic 都命中同一 item_id，只保留 exact 版本，`matched_reason` 不含 `semantic_fallback`。

## 目的地归一化适用范围

目的地归一化主要作用于 **episode slice 匹配**（`_match_destination()`），因为 slice 有明确的 `entities.destination` 字段。Profile item 没有独立的 destination 字段，目的地信息只存在于 `content_summary` 或 `applicability` 文本中，由 Stage 4 reranker 的 `destination_score` 信号处理，不在 Stage 3 做归一化。

`destination_normalization.py` 的数据只在 Stage 3 内部消费，不对外暴露为公共 API。如果未来 Stage 4 v2 reranker 也需要目的地归一化，直接 import 同一模块。

目的地别名表由人工维护，不自动生成。新增城市/地区需要同时更新 `DESTINATION_ALIASES` 和 `DESTINATION_PARENTS` 并写测试。

## Evaluation

### Deterministic tests

- `backend/tests/test_stage3_recall.py`：Phase A/B 分层测试
- `backend/tests/test_destination_normalization.py`：归一化单元测试

### Golden cases

新增的场景：

- 同义词 profile recall（semantic enabled）：
  - 安静 / 清幽 / 避世 / 不商业
  - 节省 / 省钱 / 预算有限 / 花不了太多
  - 亲子 / 带孩子 / 儿童
- 目的地归一化：
  - 关西 ↔ 大阪/京都/奈良
  - 日本 ↔ 各城市
  - 繁简变体（東京 ↔ 东京）
- fallback 兜底：
  - semantic provider disabled
  - embedding 异常
  - 候选数为 0

### Metrics

- 零命中率（stage 3 返回空候选）
- semantic fallback 命中率
- profile_top_n 命中分布
- stage3_semantic_candidates_added 分布

## Rollout

1. Phase A 落地：目的地归一化 + pool 扩大，无模型依赖，行为等价（修复目的地 bug 外）。
2. Phase B 落地：`enabled=false`，只部署代码，不启用语义。
3. 启用 semantic provider，阈值保守（0.65）：收集 telemetry。
4. 根据 eval 结果放量阈值，Stage 4 v2 跟进利用扩大后的候选池。

## Open implementation notes

- `EmbeddingProvider` Protocol 定义在 `memory/embedding_provider.py`，Stage 4 v2 实现在同一文件，不重复定义。
- `Stage3Config` 定义在 `config.py`（与其他 Memory config 同级），不独立为新文件。
- `_profile_item_text_v1` / `_slice_text_v1` 为内部函数，测试时直接调用验证格式。
- 不使用 Python shell 脚本临时写文件；所有代码改动走正常 edit flow。
- 不在本 spec 落地时同时修改 Stage 4 v2 相关代码。
- 每次 commit 必须同步更新 `PROJECT_OVERVIEW.md`（repo policy）。
