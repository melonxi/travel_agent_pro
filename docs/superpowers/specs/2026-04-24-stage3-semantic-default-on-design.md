# Stage 3 语义召回默认启用 + Evidence 权重激活（第一期）

## 背景

当前记忆召回管线 Stage 3 已经具备 symbolic / lexical / semantic 三路 lane，Stage 4 reranker 也已经接线 evidence 信号 (`lane_fused` / `lexical_score` / `semantic_score` 等)，但生产默认配置下：

- `Stage3SemanticConfig.enabled = False`、`lexical.enabled = False`，只有 symbolic lane 参与召回
- `RerankerEvidenceConfig` 所有权重均为 `0.0`，reranker 退化为纯 rule-based

导致 `recall_stage3_*` 与 reranker evidence 子系统在默认路径下"形同不存在"。在当前旅行 agent 的个性化使用场景里，用户的风格/推荐类问题（"按我偏好安排"、"住宿怎么安排比较好"、"带老人出行要舒服点"）与 profile/slice 里的内容字面重合度低，symbolic-only 默认无法稳定召回相关历史。

本期目标：**在不改动任何召回/排序算法代码的前提下，flip 默认值把 semantic lane 与 evidence 通道打开，让 Stage 3/4 的既有能力进入默认生产路径**，为第二期的 intent→source 映射与 golden eval 扩展建立 baseline。

## 目标与非目标

### 目标

1. 默认启用 Stage 3 semantic lane（`BAAI/bge-small-zh-v1.5` + FastEmbed + ONNX CPU + 本地 cache）
2. 把 evidence 权重从全 0 改为保守但可观测的正值，让 semantic / lexical / fused 信号真正参与 Stage 4 排序
3. 保证本地测试 suite 与生产回滚路径稳定：任何人可以通过 config.yaml 单步回退到第零期行为

### 非目标

- ❌ 不修改 Stage 2 retrieval plan 生成逻辑
- ❌ 不修改 Layer 1/2/3 gate 决策
- ❌ 不修改 reranker 评分算法或 intent profile 权重
- ❌ 不新增 SSE/trace 字段（现有 `reranker_per_item_scores` 已足够观测）
- ❌ 不做 intent→source 映射与新 golden eval 样本（第二期）
- ❌ 不启用 lexical lane 默认、不启用 source_widening、不启用 dynamic budget

## 变更清单

### 1. `backend/config.py` dataclass 默认值

```python
@dataclass(frozen=True)
class Stage3SemanticConfig(Stage3LaneConfig):
    enabled: bool = True              # was False
    provider: str = "fastembed"
    model_name: str = "BAAI/bge-small-zh-v1.5"
    cache_dir: str = "backend/data/embedding_cache"
    local_files_only: bool = True     # was False
    min_score: float = 0.58
    cache_max_items: int = 10000
    cache_max_mb: int = 64


@dataclass(frozen=True)
class RerankerEvidenceConfig:
    symbolic_hit_weight: float         = 0.0
    lexical_hit_weight: float          = 0.0
    semantic_hit_weight: float         = 0.0
    lane_fused_weight: float           = 0.25   # was 0.0  ← 主力
    lexical_score_weight: float        = 0.08   # was 0.0
    semantic_score_weight: float       = 0.15   # was 0.0
    destination_match_type_weight: float = 0.0
```

不改动其他 dataclass、`_build_*` loader、reranker / lane 运行逻辑。`local_files_only` 翻转为 True 的依据：embedding cache (`backend/data/embedding_cache/models--Qdrant--bge-small-zh-v1.5`) 已预置，生产默认离线更安全；需要首次下载的开发者可在本地 config.yaml 覆盖为 `false`。

### 2. 权重取值依据

Rule score 最大量级 ≈ 1.52（`0.62 + 0.34 + 0.24 + 0.18 + 0.08 + 0.06`），候选间典型差分 0.3–0.6。
在 Stage 4 `source_score = rule_score + evidence_score` 后会做 min-max 归一化再加 source_prior，因此 **evidence 的绝对量级不影响排名，只有相对差分参与排序**。目标：让 evidence 差分占候选间 rule 差分的 25–40%。

- `lane_fused_weight = 0.25`：RRF 融合三路 lane 的归一化主信号（0–1），差分 0.5 × 0.25 ≈ 0.125
- `semantic_score_weight = 0.15`：单路余弦原始分旁路（命中候选区间 0.5–0.8），差分 0.2 × 0.15 ≈ 0.03
- `lexical_score_weight = 0.08`：字面覆盖旁路（差分更集中）
- `symbolic_hit_weight` 保持 0：symbolic 信号已通过 bucket/domain/keyword 隐含进 rule_score，避免双计
- `*_hit_weight` 保持 0：已有 `*_score_weight` 更细腻地消费连续分

在这组权重下 evidence 典型贡献 0.05–0.3，显著参与归一化 spread 但不独占排名。

### 3. 测试策略

默认值翻转会影响大量测试的隐式前提。采取 **"保守兼容 + 显式正向"** 双轨：

#### 3.1 新增 autouse fixture `_stage3_defaults_compat`（`backend/tests/conftest.py`）

自动把 `MemoryRetrievalConfig()` 默认值在测试进程内回滚为第零期行为：
- `stage3.semantic.enabled = False`
- `reranker.evidence.lane_fused_weight = 0.0`
- `reranker.evidence.semantic_score_weight = 0.0`
- `reranker.evidence.lexical_score_weight = 0.0`

可以被单个测试通过 `pytest.mark.stage3_defaults_on` 禁用，或显式构造 `MemoryRetrievalConfig(...)` 覆盖。

这样：
- 现有 180+ 测试保持断言与快照稳定，不会因为权重/lane flip 批量更新
- 在文档中明确 dataclass 默认**已变**，测试代码通过 fixture 显式 opt-out

#### 3.2 新增定向测试验证默认值与行为

新建 `backend/tests/test_stage3_semantic_defaults.py`：

- 直接实例化 `Stage3SemanticConfig()` / `RerankerEvidenceConfig()`，断言新默认值
- 在一个带 `pytest.mark.stage3_defaults_on` 的测试里，用 fake/null embedding provider 构造端到端 Stage 3 → Stage 4 路径，断言：
  - `Stage3LaneResult` 包含 semantic lane 结果
  - `RecallRerankResult.per_item_scores` 中 `evidence_score > 0` 的候选数 > 0（只要 fake provider 返回非零 cosine）
  - fallback 行为：当 embedding provider 抛错时 reranker 依然返回结果（evidence_score=0 退化为纯 rule）

#### 3.3 更新 `backend/tests/test_stage3_config.py`

- `test_memory_retrieval_config_stage3_defaults` 调整 `semantic.enabled` 与 `local_files_only` 的默认断言
- `test_memory_retrieval_config_reranker_defaults_include_evidence_blocks` 调整 evidence 权重默认断言
- 新增一个测试验证 `local_files_only=False` 的 config.yaml 覆盖路径仍可用（开发者逃生门）

#### 3.4 新增定向测试验证生产回滚 YAML 能禁用

在 `backend/tests/test_stage3_config.py` 加一个 case：加载一份显式关闭 semantic + 归零 evidence 的 config.yaml，断言 `cfg.memory.retrieval.stage3.semantic.enabled is False` 且所有 evidence 权重为 0。

### 4. Baseline & 验证

在 dataclass 变更前，先跑一次：

```bash
cd backend && OTEL_SDK_DISABLED=false python -m pytest tests/ -q
```

记录：（a）全量通过；（b）任何被 autouse fixture 遮蔽的隐式默认依赖。

变更后：

1. 跑同样命令，断言全绿
2. 跑 `pytest -q backend/tests/test_stage3_semantic_defaults.py` 验证新行为
3. 跑 `pytest -q backend/tests/test_recall_stage3_semantic.py backend/tests/test_recall_stage3_fusion.py backend/tests/test_recall_reranker.py` 三份定向 suite
4. （可选）跑 `backend/evals/reranker.py` 的 deterministic reranker-only eval，记录 selected_ids 前后 diff，存入 `docs/superpowers/specs/2026-04-24-stage3-semantic-default-on-design.md` 附录或 plan 关联产物

### 5. 回滚路径

生产端在任何时候写入如下 `config.yaml` 即可回到第零期：

```yaml
memory:
  retrieval:
    stage3:
      semantic:
        enabled: false
    reranker:
      evidence:
        lane_fused_weight: 0.0
        semantic_score_weight: 0.0
        lexical_score_weight: 0.0
```

因为 `_build_stage3_semantic_config` 与 `_build_*` loader 完全靠 key 覆盖，已有逻辑无需改动。

## 风险评估

| 风险 | 严重度 | 缓解 |
|---|---|---|
| 首次加载 embedding 模型耗时（已 cache 但冷启动） | 中 | cache 已预置；`local_files_only=True` 确保不会悄悄走网；FastEmbed ONNX CPU 首次 ~50–80ms |
| Stage 3 semantic lane 抛错拖垮整个召回 | 中 | 现有 lane 级 timeout 与 try/except 已覆盖；新增 fallback 测试确认 evidence_score=0 退化路径 |
| 测试批量失败（autouse fixture 覆盖不到的隐式依赖） | 高 | 先跑 baseline，fixture 失败面按测试分类补兜底或显式更新断言 |
| reranker 排名出现意料外的 regression | 中 | `backend/evals/reranker.py` deterministic eval + SSE `reranker_per_item_scores` 可快速诊断 |
| config.yaml 已在生产环境显式写了 `local_files_only: false` | 低 | 本期不动 config.yaml；dataclass 默认只影响未显式声明的字段 |

## 开放问题

- 是否需要把"evidence 总贡献占比"在 `memory_recall` 事件里新增一个聚合字段方便观测？—— 当前设计选择**不加**，依赖现有的 `reranker_per_item_scores` 明细；若第二期发现必要再加。
- FastEmbed 在高并发下的 GIL/线程表现是否需要 lock？—— `CachedEmbeddingProvider` 已有 OrderedDict 顺序保序，当前单进程 uvicorn worker 下足够；未来多 worker 再评估。

## 验收标准

1. `cd backend && python -m pytest tests/ -q` 全绿
2. `Stage3SemanticConfig().enabled is True` 且 `RerankerEvidenceConfig().lane_fused_weight == 0.25`
3. 在一个关闭 autouse fixture 的定向测试里，Stage 3 semantic lane 参与召回，且 Stage 4 `per_item_scores` 出现 `evidence_score > 0` 的候选
4. `config.yaml` 写入回滚片段后，端到端行为恢复第零期
5. `PROJECT_OVERVIEW.md` 的 Memory System / Stage 3 / Reranker 段落已同步更新：默认行为为 semantic lane 开启、evidence 通道激活、`local_files_only` 默认 True、首批权重数值
