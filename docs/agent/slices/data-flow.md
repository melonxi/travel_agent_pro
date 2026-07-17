# Data Flow Slice

## 什么时候读

当任务涉及 chat 请求、AgentLoop、工具执行、SSE、状态更新、持久化或跨模块调用链时读取。

## 主链路

```text
POST /api/chat/{session_id}
  -> api.routes.chat_routes
  -> api.orchestration.* 加载 session + plan + persisted history
  -> memory.turn 同步召回，得到本轮 memory_context
  -> ContextManager 构造 static system + persisted_history + current_user + turn_context
  -> AgentLoop.run()
  -> LLMProvider.chat()
  -> ToolEngine / ToolGuardrail / plan writers
  -> phase_transition + PhaseRouter
  -> SSE events
  -> run finalization + message/plan/trace persistence
```

## AgentLoop 内部顺序

- 迭代开始前检查取消信号。
- `before_llm_call` hook flush pending runtime notices、注入 reflection、执行 prompt 压缩。
- `ToolChoiceDecider` 当前总返回 `"auto"`。
- LLM 流式输出 `text_delta` 和 tool calls。
- 工具按读写分类执行：读工具可并行，写工具顺序执行。
- 写工具成功后触发状态保存、阶段推断、validator、soft judge / quality gate。
- 结果经 chat SSE 发送到前端；run 结束时做保底持久化。

## 运行时任务

- chat SSE 内部任务：`memory_recall`、`soft_judge`、`quality_gate`、`context_compaction`、`reflection`、`phase3_orchestration`。
- 后台 internal-task SSE：`memory_extraction_gate`、`memory_extraction`、`profile_memory_extraction`、`working_memory_extraction`。
- 前端按 `task.id` 合并生命周期，避免同一后台任务重复生成卡片。

## 容易踩坑

- runtime notice 不能插到一组 assistant tool_calls 和 tool results 中间；必须下一轮 LLM 前 flush。
- soft judge / quality gate / hard constraint 反馈统一走 `push_pending_system_note`，在 `before_llm_call` 时 flush；禁止直接 `active_runtime_messages.append`。
- D4 运行中引导：`POST /api/chat/{session_id}/steer` 入队 `session["_steer_queue"]`；普通迭代在 LLM 前 drain 为 `runtime_notice`；Phase 3 在 worker 收集循环 drain，按「第 N 天」挂 `repair_hints`，等待中的 worker 首次带入，已开始/刚完成的 worker 触发 bounded redispatch。进入最终收口后无法应用的消息，以及 run 返回时仍残留的消息，必须发终结 `steering_ack`，不能在 finally 静默丢弃。
- Phase 3 并行入口会检查用户是否明确暂缓（如「先等等」）；骨架失配时 orchestrator 输出可对话提示，不抛硬异常。
- Phase 转换或 Phase 2 子步骤变化会 rebuild runtime input；旧消息要先按 `context_epoch` 落盘。
- Phase 3 并行结果不能由 Orchestrator 直接写状态，必须交回 AgentLoop 用 `replace_all_day_plans` 标准工具路径写入。

## 深入阅读

- 上下文 rebuild：`context-compression.md`
- 工具写状态：`tools.md`、`../deep/tool-state-writes.md`
- SSE 协议：`../deep/sse-events.md`
- SQLite 历史：`persistence.md`、`../deep/sqlite-schema.md`
