# Memory Recall Deep Dive

## Stage 0：规则短路

三层召回门控中的前两层是确定性规则：

1. `recall_signals.py` 抽取 history / style / recommend / fact_scope / fact_field / ack_sys 等信号。
2. `recall_gate.py::apply_recall_short_circuit` 按 P1-P6 输出三值决策：force recall、skip recall、undecided。

常见规则：

- profile signal -> force recall。
- 当前行程纯事实问题 -> skip recall。
- ACK -> skip。
- recommend / ambiguous -> undecided。

## Stage 1：LLM Recall Gate

- 只判断 `latest_user_message` 是否语义上需要召回。
- `previous_user_messages` 只用于省略、指代、承接消歧。
- 不再接收或构建 `memory_summary`，避免库存信号污染判断。
- `mixed_or_ambiguous` 采用保守召回策略。

## Stage 2：Retrieval Plan

- source-aware query contract。
- `profile` / `hybrid_history` source 必填 `buckets`。
- `episode_slice` source 不暴露 `buckets`，使用 `domains`、`destination`、`keywords`、`top_k`。
- `plan_facts` 只用于抽取检索参数，不重新判断是否 recall。
- query timeout / error 会回退到 stage0-aware heuristic retrieval plan。

## Stage 3：Candidate Generation

- 返回 `RecallCandidate[]` 与 `evidence_by_id` sidecar。
- 默认生产行为启用 symbolic + semantic lane。
- semantic runtime：FastEmbed + `BAAI/bge-small-zh-v1.5` + ONNX Runtime CPU + 本地 cache。
- lexical lane 仍在 feature flag 后。
- 单元测试应通过 fake/null provider 避免模型下载。

## Stage 4：Reranker

- 在规则主干上叠加 evidence 权重。
- 默认 evidence 权重：
  - `lane_fused_weight=0.25`
  - `semantic_score_weight=0.15`
  - `lexical_score_weight=0.08`
- 最终 scoring：

```text
source_score = rule_score + evidence_score
final_score = source_normalized_score + source prior
```

- 空结果、小候选集、正常 source-aware 路径都要带 `selection_metrics` placeholder。
- 生产可在 `config.yaml` 把三个 evidence 权重写回 0 并关闭 semantic lane，回到 rule-only 行为。

## 提取和归档

- 每轮 chat 追加 user message 后，后台排队 memory extraction gate/job。
- route-aware gate 只按需触发 `extract_profile_memory` 与 `extract_working_memory`。
- Phase 4 完成后归档 `ArchivedTripEpisode`，并派生 `itinerary_pattern`、`stay_choice`、`transport_choice`、`budget_signal`、`rejected_option`、`pitfall` 等 slice taxonomy。

## 关键 telemetry

- `reranker_selected_ids`
- `reranker_per_item_scores`
- `reranker_intent_label`
- `reranker_selection_metrics`
- `recall_skip_source`
- `query_plan_fallback`
- `recall_attempted_but_zero_hit`

## 关键代码

- `backend/memory/recall_signals.py`
- `backend/memory/recall_gate.py`
- `backend/memory/symbolic_recall.py`
- `backend/memory/recall_stage3*`
- `backend/api/orchestration/memory/turn.py`
- `backend/api/orchestration/memory/recall_planning.py`
- `backend/api/orchestration/memory/extraction.py`
