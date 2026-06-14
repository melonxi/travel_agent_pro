# SSE Events Deep Dive

## Chat SSE 事件

| 事件 | 含义 |
|------|------|
| `text_delta` | 助手文本增量 |
| `tool_call` | 工具调用开始或更新 |
| `tool_result` | 工具调用结果 |
| `phase_transition` | 阶段或 Phase 2 子步骤切换 |
| `agent_status` | ThinkingBubble 状态 |
| `state_update` | 完整 TravelPlanState |
| `context_compression` | 上下文压缩通知 |
| `internal_task` | 内部任务生命周期 |
| `memory_recall` | 本轮结构化记忆召回诊断 |
| `error` | LLM 或运行时错误 |
| `keepalive` | 心跳 |
| `done` | 流结束 |

## Background Internal Task SSE

`GET /api/internal-tasks/{session_id}/stream` 承载与当前回答解耦的后台任务：

- `memory_extraction_gate`
- `memory_extraction`
- `profile_memory_extraction`
- `working_memory_extraction`

前端按 `task.id` 合并 chat SSE 与 background SSE。

## memory_recall payload 要点

payload 包含：

- `sources`
- `profile_ids`
- `working_memory_ids`
- `slice_ids`
- `matched_reasons`
- `stage0_decision`
- `stage0_reason`
- `stage0_matched_rule`
- `stage0_signals`
- `gate_needs_recall`
- `gate_intent_type`
- `gate_confidence`
- `final_recall_decision`
- `fallback_used`
- `recall_skip_source`
- `query_plan`
- `query_plan_source`
- `query_plan_fallback`
- `recall_attempted_but_zero_hit`
- `candidate_count`
- `reranker_selected_ids`
- `reranker_per_item_scores`
- `reranker_selection_metrics`

`sources` 当前只保留 `query_profile`、`working_memory`、`episode_slice`。

## 顺序注意

- `phase_transition` 可能早于 `state_update`。
- `tool_result` 结束真实工具卡后，才可能出现 soft judge / memory extraction 等内部任务。
- error 事件需要携带 retryable / can_continue，前端据此展示继续能力。

## 关键代码

- `backend/api/orchestration/chat/stream.py`
- `backend/api/orchestration/chat/events.py`
- `backend/api/routes/internal_task_routes.py`
- `frontend/src/hooks/useSSE.ts`
- `frontend/src/components/ChatPanel.tsx`
