# Persistence Slice

## 什么时候读

当任务涉及 SQLite、session 恢复、messages、plan snapshots、trace、deliverables、memory 文件或数据迁移时读取。

## SQLite 事实

- `sessions`：session 元数据、phase、status、last run 状态。
- `messages`：append-only 消息历史事实源，含 `history_seq`、`phase`、`phase2_step`、`run_id`、`trip_id`、`context_epoch`、`rebuild_reason`。
- `plan_snapshots`：旅行方案快照。
- `archives`：完成会话归档。
- `trace_runs` / `trace_events` / `trace_artifacts` / `trace_grades`：run-scoped trace flight recorder。

## 文件系统事实

```text
backend/data/
  sessions.db
  sessions/sess_*/
    plan.json
    snapshots/
    tool_results/
    deliverables/
  users/{user_id}/memory/
    profile.json
    events.jsonl
    episodes.jsonl
    episode_slices.jsonl
    sessions/{session_id}/trips/{trip_id}/working_memory.json
```

## 持久化语义

- plan writer 成功后应立即保存 plan，并同步 session meta。
- chat/continue/cancel/finalization 都要做消息和 plan 的保底持久化。
- restore 不推进 `context_epoch`；只有下一次 runtime-context rebuild boundary 才推进。
- Phase 4 冻结的 `travel_plan.md` 和 `checklist.md` 写入 session deliverables 目录，并由 `TravelPlanState.deliverables` 指向。

## 关键代码

- `backend/storage/database.py`
- `backend/storage/session_store.py`
- `backend/storage/message_store.py`
- `backend/storage/archive_store.py`
- `backend/storage/trace_store.py`
- `backend/api/orchestration/session/persistence.py`
- `backend/api/orchestration/session/deliverables.py`
- `backend/api/orchestration/chat/finalization.py`

## 深入阅读

- SQLite schema 细节：`../deep/sqlite-schema.md`
- 上下文历史：`context-compression.md`
- Trace：`observability.md`
