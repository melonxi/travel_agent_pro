# Trace Flight Recorder Deep Dive

## 目标

run-scoped trace 是可复现、可评分、可做失败归因的行为记录，不只服务前端展示。

## 数据表

- `trace_runs`
- `trace_events`
- `trace_artifacts`
- `trace_grades`

## 事件类型覆盖

当前 trace recorder 覆盖：

- `run_start` / `run_end`
- `llm_call` / `llm_output`
- `tool_call` / `tool_result`
- `state_snapshot` / `state_diff`
- `phase_gate` / `phase_transition`
- `quality_gate`
- `soft_judge`
- `validation`
- `memory_recall`
- `memory_hit`
- `context_build`
- `context_compression`
- `context_rebuild`
- Phase 3 orchestrator / worker
- Phase 4 draft / finalize
- `error`

## 事件 metadata

事件通常携带：

- phase / phase2_step / iteration
- tool_name
- LLM provider / model
- status
- duration_ms
- cost_usd
- `parent_event_id`
- `root_event_id`
- `correlation_id`

大 prompt、工具结果、交付物 body 不直接塞进事件 payload；进入 `trace_artifacts`，事件只保留 hash、preview、artifact id、redaction 状态。

## Trace grader

`backend/evals/trace_grader.py` 是确定性规则评分器，不依赖 LLM judge。

覆盖主题包括：

- Phase 2 先搜索后写候选。
- 骨架天数匹配。
- 状态写入只走 plan writer。
- Phase 3 POI 去重、日程覆盖、并行候选经 `replace_all_day_plans` 落盘。
- 工具错误率。
- 空工具结果不当证据。
- 相同失败参数不反复重试。
- 阶段转换有 gate 证据。
- 写工具有 state diff。
- 天气不确定性保留。
- 锁定需要用户授权。
- 当前行程事实问句跳过召回。
- Phase 4 交付物质量通过后冻结。

## 关键代码

- `backend/telemetry/trace_recorder.py`
- `backend/storage/trace_store.py`
- `backend/evals/trace_grader.py`
- `backend/evals/trace_models.py`
- `backend/evals/failure_report.py`
- `backend/api/routes/artifact_routes.py`
- `backend/api/orchestration/chat/stream_trace.py`
- `backend/api/orchestration/chat/trace_persistence.py`
