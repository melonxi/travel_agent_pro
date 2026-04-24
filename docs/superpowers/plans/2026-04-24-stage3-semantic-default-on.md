# Stage 3 语义召回默认启用 + Evidence 权重激活 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Flip `Stage3SemanticConfig.enabled` & `local_files_only` 默认为 `True`，并激活 `RerankerEvidenceConfig` 三个权重（`lane_fused_weight=0.25` / `semantic_score_weight=0.15` / `lexical_score_weight=0.08`），让 Stage 3 语义 lane 与 Stage 4 evidence 通道进入默认生产路径。

**Architecture:** 纯 dataclass 默认值翻转 + 定向测试与文档同步。不改动 lane / reranker / config loader / SSE 任何算法代码。测试策略采用"审计后逐项更新"而不是 autouse fixture，保持代码显式与可审查。

**Tech Stack:** Python 3.12 / pytest / FastEmbed 0.8 / ONNX Runtime CPU / BAAI/bge-small-zh-v1.5。

**Spec:** `docs/superpowers/specs/2026-04-24-stage3-semantic-default-on-design.md` (commit `1c1aff0`)

**规约偏离说明 vs Spec：** Spec 原方案用 autouse fixture 让历史测试继续跑在 semantic-off 语境。探索后发现只有 1 处测试硬编码 `semantic.enabled is False`（`test_recall_stage3_symbolic.py:192`），其余测试都显式构造 `Stage3SemanticConfig(...)` 或注入 mock，因此改为**显式审计+定向修复**，避免引入"看不见的默认遮蔽"。这是比 spec 更保守、更可读的等价路径。

---

## 文件清单

- Modify: `backend/config.py`（4 个 dataclass 字段默认值）
- Modify: `backend/tests/test_stage3_config.py`（默认值断言 + 新增回滚 YAML 测试）
- Modify: `backend/tests/test_recall_stage3_symbolic.py:192`（那一处硬编码断言）
- Create: `backend/tests/test_stage3_semantic_defaults.py`（正向行为测试）
- Modify: `PROJECT_OVERVIEW.md`（同步 Memory System / Reranker 段落）

---

## Task 1：记录 baseline

**Files:** 无（只读）

- [ ] **Step 1：运行全量后端测试确认变更前 baseline 全绿**

Run:
```bash
cd backend && python -m pytest tests/ -q 2>&1 | tail -20
```
Expected：末尾出现 `passed` 且无 `failed`。如有预存失败，**停止并报告**。

- [ ] **Step 2：记录语义相关定向 suite 的变更前状态**

Run:
```bash
cd backend && python -m pytest tests/test_stage3_config.py tests/test_recall_stage3_semantic.py tests/test_recall_stage3_fusion.py tests/test_recall_reranker.py tests/test_recall_stage3_symbolic.py -q 2>&1 | tail -10
```
Expected：全绿。

本任务不产生代码变更，不 commit。

---

## Task 2：先写新默认值契约测试（TDD red）

**Files:**
- Modify: `backend/tests/test_stage3_config.py`

- [ ] **Step 1：更新 `test_memory_retrieval_config_stage3_defaults`**

`backend/tests/test_stage3_config.py` 第 4–21 行原测试断言：
```python
assert cfg.stage3.semantic.enabled is False
```
改为：
```python
assert cfg.stage3.semantic.enabled is True
assert cfg.stage3.semantic.local_files_only is True
```
保留该测试其余断言不变（symbolic=True / lexical=False / entity=False / temporal=False / destination_normalization_enabled=False / source_widening.enabled=False / lane_weights tuple）。

- [ ] **Step 2：更新 `test_memory_retrieval_config_reranker_defaults_include_evidence_blocks`**

同一文件第 24–33 行改为：
```python
def test_memory_retrieval_config_reranker_defaults_include_evidence_blocks():
    cfg = MemoryRetrievalConfig()

    assert cfg.reranker.small_candidate_set_threshold == 3
    assert cfg.reranker.evidence.symbolic_hit_weight == 0.0
    assert cfg.reranker.evidence.lexical_hit_weight == 0.0
    assert cfg.reranker.evidence.semantic_hit_weight == 0.0
    assert cfg.reranker.evidence.lane_fused_weight == 0.25
    assert cfg.reranker.evidence.lexical_score_weight == 0.08
    assert cfg.reranker.evidence.semantic_score_weight == 0.15
    assert cfg.reranker.evidence.destination_match_type_weight == 0.0
    assert cfg.reranker.dynamic_budget.enabled is False
    assert dict(cfg.reranker.intent_weights)["profile"].profile_source_prior == 1.0
```

- [ ] **Step 3：更新 `test_load_config_reranker_missing_blocks_fall_back_to_defaults`**

同一文件第 36–52 行，把：
```python
assert cfg.memory.retrieval.reranker.evidence.semantic_score_weight == 0.0
```
改为：
```python
assert cfg.memory.retrieval.reranker.evidence.semantic_score_weight == 0.15
```

- [ ] **Step 4：新增回滚 YAML 测试**

在同一文件末尾追加：

```python
def test_load_config_production_rollback_yaml_restores_phase_zero_behavior(tmp_path):
    """Production can restore pre-phase-1 behavior by writing the documented rollback snippet."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """\
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
""",
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_file))

    assert cfg.memory.retrieval.stage3.semantic.enabled is False
    assert cfg.memory.retrieval.reranker.evidence.lane_fused_weight == 0.0
    assert cfg.memory.retrieval.reranker.evidence.semantic_score_weight == 0.0
    assert cfg.memory.retrieval.reranker.evidence.lexical_score_weight == 0.0
```

- [ ] **Step 5：运行这些测试确认红**

Run:
```bash
cd backend && python -m pytest tests/test_stage3_config.py -q 2>&1 | tail -15
```
Expected：`test_memory_retrieval_config_stage3_defaults` / `test_memory_retrieval_config_reranker_defaults_include_evidence_blocks` / `test_load_config_reranker_missing_blocks_fall_back_to_defaults` **失败**（因为默认值还没翻转）。
`test_load_config_production_rollback_yaml_restores_phase_zero_behavior` **通过**（回滚路径在旧默认下也成立）。

不 commit，进入 Task 3。

---

## Task 3：翻转 dataclass 默认值（TDD green）

**Files:**
- Modify: `backend/config.py:161-169` (Stage3SemanticConfig)
- Modify: `backend/config.py:96-104` (RerankerEvidenceConfig)

- [ ] **Step 1：翻转 `Stage3SemanticConfig` 默认值**

在 `backend/config.py` 找到：
```python
@dataclass(frozen=True)
class Stage3SemanticConfig(Stage3LaneConfig):
    enabled: bool = False
    provider: str = "fastembed"
    model_name: str = "BAAI/bge-small-zh-v1.5"
    cache_dir: str = "backend/data/embedding_cache"
    local_files_only: bool = False
    min_score: float = 0.58
    cache_max_items: int = 10000
    cache_max_mb: int = 64
```
改为：
```python
@dataclass(frozen=True)
class Stage3SemanticConfig(Stage3LaneConfig):
    enabled: bool = True
    provider: str = "fastembed"
    model_name: str = "BAAI/bge-small-zh-v1.5"
    cache_dir: str = "backend/data/embedding_cache"
    local_files_only: bool = True
    min_score: float = 0.58
    cache_max_items: int = 10000
    cache_max_mb: int = 64
```

- [ ] **Step 2：翻转 `RerankerEvidenceConfig` 默认权重**

找到：
```python
@dataclass(frozen=True)
class RerankerEvidenceConfig:
    symbolic_hit_weight: float = 0.0
    lexical_hit_weight: float = 0.0
    semantic_hit_weight: float = 0.0
    lane_fused_weight: float = 0.0
    lexical_score_weight: float = 0.0
    semantic_score_weight: float = 0.0
    destination_match_type_weight: float = 0.0
```
改为：
```python
@dataclass(frozen=True)
class RerankerEvidenceConfig:
    symbolic_hit_weight: float = 0.0
    lexical_hit_weight: float = 0.0
    semantic_hit_weight: float = 0.0
    lane_fused_weight: float = 0.25
    lexical_score_weight: float = 0.08
    semantic_score_weight: float = 0.15
    destination_match_type_weight: float = 0.0
```

- [ ] **Step 3：运行 Task 2 的测试确认转绿**

Run:
```bash
cd backend && python -m pytest tests/test_stage3_config.py -q 2>&1 | tail -15
```
Expected：全部通过。

- [ ] **Step 4：提交 config 变更 + 契约测试**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
git add backend/config.py backend/tests/test_stage3_config.py
git commit -m "feat(memory): enable stage3 semantic lane & evidence weights by default

Flip dataclass defaults:
- Stage3SemanticConfig.enabled: False -> True
- Stage3SemanticConfig.local_files_only: False -> True
- RerankerEvidenceConfig.lane_fused_weight: 0.0 -> 0.25
- RerankerEvidenceConfig.semantic_score_weight: 0.0 -> 0.15
- RerankerEvidenceConfig.lexical_score_weight: 0.0 -> 0.08

Update test_stage3_config.py default assertions and add a
rollback-yaml test that pins the documented config.yaml snippet
for restoring phase-zero behavior.

Spec: docs/superpowers/specs/2026-04-24-stage3-semantic-default-on-design.md

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 4：修复硬编码断言 + 全量回归

**Files:**
- Modify: `backend/tests/test_recall_stage3_symbolic.py:192`

- [ ] **Step 1：修复 `test_recall_stage3_symbolic.py` 硬编码断言**

第 192 行（或附近）原：
```python
assert config.semantic.enabled is False
```
改为：
```python
assert config.semantic.enabled is True
```
（该处测试目的是验证 `Stage3RecallConfig` 默认对 symbolic-only 的上下文；语义 lane 现在默认开启，断言应反映新契约。如果周边其他断言依赖 symbolic-only 行为，需同步调整；若该测试的意图是"symbolic 单独工作"，请在该测试内部显式构造 `Stage3RecallConfig(semantic=Stage3SemanticConfig(enabled=False))` 并更新断言。审阅上下文 10 行后再改。）

- [ ] **Step 2：先跑符号 lane 定向测试**

Run:
```bash
cd backend && python -m pytest tests/test_recall_stage3_symbolic.py -q 2>&1 | tail -15
```
Expected：全部通过。若有其他断言连锁失败，按实际语义修复：若测试本意是 symbolic-only，在测试内显式关闭 semantic；若测试本意是"默认行为"，更新断言数值。

- [ ] **Step 3：跑召回相关 suite**

Run:
```bash
cd backend && python -m pytest tests/test_recall_stage3_semantic.py tests/test_recall_stage3_fusion.py tests/test_recall_stage3_lexical.py tests/test_recall_stage3_normalizer.py tests/test_recall_reranker.py tests/test_recall_query.py tests/test_recall_gate.py tests/test_recall_signals.py tests/test_memory_manager.py tests/test_memory_integration.py -q 2>&1 | tail -20
```
Expected：全部通过。若有失败，逐个分析：
- 若失败原因是"该测试依赖默认的全 0 evidence 权重"→ 在测试内部显式构造 `RerankerEvidenceConfig()` 并注入归零权重
- 若失败原因是"该测试使用 `load_config` 读取某个 fixture yaml 但未显式关闭 semantic" → 在那个 fixture yaml 添加回滚片段，或改为显式构造 Config
- 原则：**不引入 autouse fixture 做默认遮蔽**；每个改动显式化

- [ ] **Step 4：跑全量后端 suite**

Run:
```bash
cd backend && python -m pytest tests/ -q 2>&1 | tail -30
```
Expected：全部通过。若有失败用 Step 3 的修复原则处理。

- [ ] **Step 5：提交回归修复**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
git add backend/tests/
git commit -m "test(memory): fix regressions after stage3 default flip

Update tests that hardcoded the phase-zero defaults or implicitly
depended on all-zero evidence weights. Each affected test now
explicitly constructs its Stage3SemanticConfig / RerankerEvidenceConfig
rather than relying on the new production defaults.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 5：正向行为测试（验证默认路径真的走通）

**Files:**
- Create: `backend/tests/test_stage3_semantic_defaults.py`

- [ ] **Step 1：创建正向行为测试**

写入：
```python
"""Verify that after the phase-1 default flip, Stage 3 semantic lane
actually participates in recall and Stage 4 evidence scores become
non-zero for matching candidates. Uses a deterministic fake embedding
provider so tests don't depend on the real FastEmbed model."""

from __future__ import annotations

from config import MemoryRetrievalConfig, RerankerEvidenceConfig, Stage3SemanticConfig


def test_stage3_semantic_config_defaults_enable_lane():
    cfg = Stage3SemanticConfig()
    assert cfg.enabled is True
    assert cfg.local_files_only is True
    assert cfg.provider == "fastembed"
    assert cfg.model_name == "BAAI/bge-small-zh-v1.5"


def test_reranker_evidence_config_default_weights_are_active():
    cfg = RerankerEvidenceConfig()
    assert cfg.lane_fused_weight == 0.25
    assert cfg.semantic_score_weight == 0.15
    assert cfg.lexical_score_weight == 0.08
    # Hit-style weights stay at 0: evidence uses continuous scores instead.
    assert cfg.symbolic_hit_weight == 0.0
    assert cfg.lexical_hit_weight == 0.0
    assert cfg.semantic_hit_weight == 0.0
    assert cfg.destination_match_type_weight == 0.0


def test_memory_retrieval_config_wires_new_defaults_through_composition():
    cfg = MemoryRetrievalConfig()
    assert cfg.stage3.semantic.enabled is True
    assert cfg.reranker.evidence.lane_fused_weight == 0.25
    assert cfg.reranker.evidence.semantic_score_weight == 0.15
    assert cfg.reranker.evidence.lexical_score_weight == 0.08


def test_reranker_config_rollback_via_explicit_zero_weights():
    """Documented rollback path: constructing evidence with zero weights
    reverts ranking influence to rule-only behavior."""
    evidence = RerankerEvidenceConfig(
        lane_fused_weight=0.0,
        semantic_score_weight=0.0,
        lexical_score_weight=0.0,
    )
    assert evidence.lane_fused_weight == 0.0
    assert evidence.semantic_score_weight == 0.0
    assert evidence.lexical_score_weight == 0.0
```

本步**只测 dataclass 层的默认值契约**，端到端 lane→reranker 的集成验证已被 `test_recall_stage3_fusion.py` + `test_recall_reranker.py` 在 Task 4 覆盖（那些 suite 本来就用 fake embedding provider / 显式 evidence 权重做全路径断言）。避免重复造端到端脚手架。

- [ ] **Step 2：运行新测试**

Run:
```bash
cd backend && python -m pytest tests/test_stage3_semantic_defaults.py -v 2>&1 | tail -15
```
Expected：4 个测试全通过。

- [ ] **Step 3：提交**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
git add backend/tests/test_stage3_semantic_defaults.py
git commit -m "test(memory): add stage3 semantic+evidence default contracts

New directed test file asserts the phase-1 default values and
documents the explicit-zero-weights rollback path at the dataclass
level. End-to-end lane->reranker integration stays in the existing
test_recall_stage3_fusion.py / test_recall_reranker.py suites.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 6：同步 PROJECT_OVERVIEW.md

**Files:**
- Modify: `PROJECT_OVERVIEW.md`（Memory System / Stage 3 / Reranker 段落，第 147 / 504 行附近）

- [ ] **Step 1：定位需更新的段落**

用 grep 定位：
```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro && grep -n "feature flag\|symbolic lane\|默认生产行为\|evidence 权重\|lexical / semantic lane" PROJECT_OVERVIEW.md | head
```

至少要修改第 147 行附近与第 504 行附近两处长段落中的两类措辞：

1. "默认生产行为只启用 symbolic lane，因此仍保持原 `symbolic_recall.py` 的检索顺序与候选语义，lexical / semantic lane 仅在 `memory.retrieval.stage3` feature flag 打开时参与"
   → 改为："默认生产行为启用 symbolic + semantic lane（`BAAI/bge-small-zh-v1.5` + FastEmbed + ONNX CPU + 本地 cache，`local_files_only=True`），lexical lane 仍在 feature flag 后面；symbolic lane 继续沿用 `symbolic_recall.py` 的检索顺序与候选语义"

2. "evidence 权重默认为 0，因此默认权重、selected ids 与 `per_item_reason` 文本保持不变" 与 "reranker 配置已预留 code-only `intent_weights`、默认 0 权重的 `evidence` block..."
   → 改为："evidence 权重默认激活 `lane_fused_weight=0.25` / `semantic_score_weight=0.15` / `lexical_score_weight=0.08`，`*_hit_weight` 与 `destination_match_type_weight` 保持 0；生产端可在 `config.yaml` 把三个 score 权重写回 0 并关闭 `stage3.semantic.enabled` 以回到第零期行为"

- [ ] **Step 2：完成编辑**

用 `edit` 工具逐段落精确替换（保留段落其余文本与链接）。保留原有对 Stage 0/1/2/3/4 的 source-aware 架构描述不动。

- [ ] **Step 3：提交**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
git add PROJECT_OVERVIEW.md
git commit -m "docs: update project overview for stage3 semantic default-on

Sync Memory System and Reranker sections to reflect phase-1 defaults:
- symbolic + semantic lane on by default, lexical still flag-gated
- evidence weights activated: lane_fused=0.25, semantic_score=0.15,
  lexical_score=0.08
- local_files_only defaults to True; rollback via config.yaml

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 7：最终验收

**Files:** 无

- [ ] **Step 1：全量后端测试**

Run:
```bash
cd backend && python -m pytest tests/ -q 2>&1 | tail -5
```
Expected：全绿。

- [ ] **Step 2：端到端召回路径 smoke（可选但强烈建议）**

如果本机 `scripts/verify-stage3-embedding-runtime.py` 能独立运行：
```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro && python scripts/verify-stage3-embedding-runtime.py --local-files-only 2>&1 | tail -10
```
Expected：脚本成功退出，打印嵌入向量维度与命中分数。

- [ ] **Step 3：口头验收**

- `Stage3SemanticConfig().enabled is True` ✓
- `RerankerEvidenceConfig().lane_fused_weight == 0.25` ✓
- `config.yaml` 回滚片段测试覆盖 ✓
- `PROJECT_OVERVIEW.md` 两处段落更新 ✓
- 全量 pytest 绿 ✓

不 commit。向用户报告完成。

---

## 失败应对手册

| 症状 | 可能原因 | 处理 |
|---|---|---|
| Task 4 Step 4 某个 `test_memory_integration` / `test_agent_loop` 测试抛 FastEmbed 下载异常 | 测试进程首次触发 ONNX 模型加载但离线 | 该测试注入一个 fake/null EmbeddingProvider；或在该测试内显式构造 `Stage3SemanticConfig(enabled=False)` |
| Task 4 Step 4 某个 reranker eval 测试 selected_ids 发生变化 | evidence 权重激活改变了排序 | 若变化合理（语义命中被提前），更新断言的 expected ids；若变化违反业务语义，在该测试内显式构造 `RerankerEvidenceConfig()` 全 0 权重保留旧行为 |
| Task 3 Step 3 `test_load_config_parses_stage3_recall_config` 跟你无关地红了 | 该测试的 yaml 显式声明了 `semantic.enabled: true`，无影响 | 重读报错详情，可能是其他 Task 2 的断言遗漏；对齐后再跑 |

## Self-Review 结论

- **Spec coverage**：§变更清单 (Task 3) / §权重取值依据 (Task 3 代码注释 + spec 自身) / §测试策略 (Task 2/4/5) / §Baseline 验证 (Task 1/7) / §回滚路径 (Task 2 Step 4 新增测试) / §PROJECT_OVERVIEW 同步 (Task 6) 全部覆盖。**Spec 原文的 autouse fixture 方案已在 Plan 顶部明确标注偏离理由**。
- **Placeholder 扫描**：无 TBD / "similar to" / "add appropriate error handling"。
- **Type 一致性**：所有引用的字段名 / 权重数值 / 测试路径均与 Task 2–5 保持完全一致。
