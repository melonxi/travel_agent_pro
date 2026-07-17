# API Slice

## 什么时候读

当任务涉及 FastAPI 路由、SSE、session、chat、memory、trace、deliverables 或 API 编排边界时读取。

## 主要端点

```text
GET    /health
POST   /api/sessions
GET    /api/sessions
DELETE /api/sessions/{session_id}
GET    /api/plan/{session_id}
POST   /api/chat/{session_id}
POST   /api/chat/{session_id}/cancel
POST   /api/chat/{session_id}/steer
POST   /api/chat/{session_id}/continue
GET    /api/internal-tasks/{session_id}
GET    /api/internal-tasks/{session_id}/stream
GET    /api/sessions/{session_id}/deliverables/{filename}
GET    /api/messages/{session_id}
POST   /api/backtrack/{session_id}
GET    /api/archives/{session_id}
GET    /api/sessions/{session_id}/trace
GET    /api/traces/{run_id}
POST   /api/traces/{run_id}/grade
GET    /api/sessions/{session_id}/stats
GET    /api/memory/{user_id}/profile
GET    /api/memory/{user_id}/episode-slices
GET    /api/memory/{user_id}/sessions/{session_id}/working-memory
POST   /api/memory/{user_id}/profile/{item_id}/confirm
POST   /api/memory/{user_id}/profile/{item_id}/reject
DELETE /api/memory/{user_id}/profile/{item_id}
GET    /api/memory/{user_id}/episodes
```

`/steer` 仅在 active run 存在时接收入队，成功返回 `status=queued`；队列满返回 429，
run 已关闭入队入口则返回 409。已排队但来不及在本轮应用的消息通过 chat SSE 发终结 ack。

## 编排边界

- `api/routes/` 只承载 HTTP 资源边界。
- `api/orchestration/chat/` 承载 chat stream、events、finalization、deliverables。
- `api/orchestration/agent/` 承载 AgentLoop 装配、工具注册、hooks。
- `api/orchestration/memory/` 承载召回、提取、后台任务、episode 归档。
- `api/orchestration/session/` 承载恢复、持久化、backtrack、deliverables。
- `api/orchestration/common/` 承载通用 telemetry / LLM error helper。

## 深入阅读

- Chat 数据流：`data-flow.md`
- SSE 协议：`../deep/sse-events.md`
- 持久化：`persistence.md`
