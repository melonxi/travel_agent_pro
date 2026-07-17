# Memory Slice

## 什么时候读

当任务涉及长期画像、当前 session/trip 工作记忆、历史行程 episode、episode slices、召回、reranker、后台提取或 Memory UI 时读取。

## 最小事实

- v3 记忆只保留四类权威对象：
  - `profile.json`：长期用户画像。
  - `working_memory.json`：当前 session/trip 工作记忆。
  - `episodes.jsonl`：完成旅行归档。
  - `episode_slices.jsonl`：从 episode 派生的可召回切片。
- 当前旅行事实始终由 `TravelPlanState` 权威提供；working memory 不参与 historical recall。
- 长期 profile 不固定常驻 prompt，只有召回命中后才注入上下文。
- Phase 4 完成后归档 `ArchivedTripEpisode`，并派生 episode slices。
- 后台提取是 session 级 latest-wins coalescing queue，按 route-aware gate 决定是否跑 profile / working memory extractor。

## 同步召回链路

```text
Stage 0 rule short-circuit
  -> Stage 1 LLM recall gate
  -> Stage 2 source-aware retrieval plan
  -> Stage 3 candidate generation
  -> Stage 4 rule/evidence reranker
  -> turn_context memory injection + telemetry
```

## 召回来源

- `query_profile`
- `working_memory`
- `episode_slice`

不要重新引入旧的 legacy memory source。

## 相关代码

- `backend/memory/recall_signals.py`
- `backend/memory/recall_gate.py`
- `backend/api/orchestration/memory/turn.py`
- `backend/api/orchestration/memory/recall_planning.py`
- `backend/api/orchestration/memory/extraction.py`
- `backend/api/orchestration/memory/tasks.py`
- `backend/api/orchestration/memory/episodes.py`
- `backend/memory/symbolic_recall.py`
- `backend/memory/recall_stage3*`

## 深入阅读

- 召回/reranker 细节：`../deep/memory-recall.md`
- Stage 3 embedding 向量 sidecar：`../deep/memory-embedding-sidecar.md`
- 持久化位置：`persistence.md`
- Trace 可见性：`observability.md`、`../deep/trace-flight-recorder.md`
