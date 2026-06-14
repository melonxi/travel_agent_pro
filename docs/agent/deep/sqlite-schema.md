# SQLite Schema Deep Dive

## 表

```sql
sessions
messages
plan_snapshots
archives
trace_runs
trace_events
trace_artifacts
trace_grades
```

## messages

`messages` 是 append-only 历史事实源，关键列包括：

- `id`
- `session_id`
- `role`
- `content`
- `tool_calls`
- `tool_call_id`
- `provider_state`
- `seq`
- `history_seq`
- `phase`
- `phase2_step`
- `run_id`
- `trip_id`
- `context_epoch`
- `rebuild_reason`

`(session_id, history_seq)` 保证单 session 历史顺序。旧数据允许新增列为空。

## context segment

- 不单独建表。
- `api.orchestration.session.context_segments` 按 `(session_id, context_epoch)` 从 messages rows 派生 segment。
- 重复进入同一 phase/step 时依靠不同 `context_epoch` 区分。

## restore 语义

- 恢复时加载完整 append-only history view。
- `session["messages"]` 恢复为最多 120 条非 system、非 transient 的前端/运行缓存视图。
- 恢复本身不推进 epoch。
- 下一次 runtime rebuild boundary 才推进 epoch 并写 `rebuild_reason`。

## Trace 表

- `trace_runs`：run 级元数据、模型/工具 schema 指纹、token/cost/duration 汇总。
- `trace_events`：run-scoped 事件流，含 phase/tool/llm/status/因果 metadata。
- `trace_artifacts`：大 prompt/tool/result/deliverable body 的脱敏 artifact 索引。
- `trace_grades`：deterministic trace grader 的 rubric 结果。

## 关键代码

- `backend/storage/database.py`
- `backend/storage/message_store.py`
- `backend/storage/session_store.py`
- `backend/storage/trace_store.py`
- `backend/api/orchestration/session/context_segments.py`
