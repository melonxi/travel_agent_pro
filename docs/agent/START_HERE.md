# Start Here

这是 agent 默认先读的鸟瞰文档。目标是建立项目 mental model，不展开实现细节。

## 1. 项目一句话

Travel Agent Pro 是一个 LLM 旅行规划 Agent：通过多轮对话把用户的模糊旅行意图推进到可执行行程，最终在 Phase 4 冻结 `travel_plan.md` 和 `checklist.md` 两个交付物。

## 2. 顶层模型：两层循环

```text
Human-Agent Loop
  用户输入 / 停止 / 继续 / 回退 / 切 session
    -> API chat/session layer
    -> 构造一次 runtime input
    -> 调用内层 AgentLoop
    -> SSE 把文本、工具、状态、阶段、记忆、trace 流回前端
    -> 用户基于结果继续下一轮

Agent Loop
  runtime input 已构造后进入
    -> LLM turn
    -> tool calls
    -> tool execution
    -> TravelPlanState mutation
    -> validation / judge / phase transition
    -> yield chunks/events
    -> done 或进入下一 iteration
```

外层解释“用户为什么继续交互”。内层解释“系统每一轮如何推进状态”。

## 3. 外层 Human-Agent Loop

外层是产品交互循环，不是一个单独类，分散在前端、API、session、SSE 和持久化中。

- 前端持有 `sessionId`、`plan`、session list、右侧 Plan / Trace / Memory 面板。
- 用户动作包括发送消息、停止、继续生成、重发、切换 session、删除 session、查看 trace / memory。
- 后端 `/api/chat/{session_id}` 接收用户消息，加载 session 和 plan，做 memory recall，构造本轮 runtime input。
- SSE 把 `text_delta`、`tool_call`、`tool_result`、`state_update`、`phase_transition`、`internal_task`、`memory_recall`、`error`、`done` 流回前端。
- session、messages、plan snapshots、trace 和 deliverables 让多轮交互可恢复、可观察、可继续。

## 4. 内层 Agent Loop

内层是显式代码实体：`backend/agent/loop.py::AgentLoop.run()`。

一次 Agent Loop run 是 bounded iteration：

1. 检查是否进入 Phase 3 并行 orchestrator。
2. 运行 `run_llm_turn()`：before hook、压缩事件、reflection、LLM streaming、tool call 收集。
3. 如果没有 tool calls，输出最终文本并结束；必要时注入 repair runtime notice 后继续。
4. 如果有 tool calls，追加 assistant tool-call message。
5. 执行 tool batch：读工具可并行，写工具顺序执行，guardrail 和 hook 包围执行。
6. 写工具成功后通过 plan writer 修改 `TravelPlanState`。
7. 检测 phase transition 或 Phase 2 step change，必要时 rebuild runtime messages。
8. 继续下一 iteration，直到 done 或达到安全上限。

## 5. 两层循环共享的工作台

- 当前旅行事实：`TravelPlanState`。
- 当前业务进度：`phase` + `phase2_step`。
- 对话连续性：SQLite `messages` append-only history。
- 本轮 runtime input：临时构造的 system/history/user/turn_context，不等于完整历史。
- 记忆：profile、working memory、episodes、episode slices 只作为召回上下文，不替代当前旅行事实。
- 可观察性：`SessionStats` 和 run-scoped trace 记录工具、LLM、state diff、phase gate、memory、quality gate。

## 6. Phase 如何嵌入 Agent Loop

Phase 是旅行规划业务状态机，不是顶层系统结构。

- Phase 1：目的地和基础行程收敛。
- Phase 2：旅行画像、候选池、骨架、交通住宿锁定。
- Phase 3：逐日日程详排；可串行，也可进入并行 Orchestrator-Workers。
- Phase 4：出发前查漏，质量检查通过后冻结交付物。

Agent Loop 通过工具写入和 `PhaseRouter` 推进 phase；Human-Agent Loop 通过用户确认、修改、授权和回退影响 phase。

## 7. 关键不变量

- 写当前旅行状态必须走 plan writer，不要直接改 `TravelPlanState`。
- Phase 3 Day Worker 只提交候选，不直接写 `TravelPlanState.daily_plans`。
- 当前旅行事实以 `TravelPlanState` 为准，不从 memory 推断。
- runtime input 每轮临时构造；SQLite messages 是 append-only 历史事实源。
- runtime notice 不能插在 assistant tool_calls 和 tool results 中间。
- `PROJECT_OVERVIEW.md` 是全量参考，不是默认 agent 入口。

## 8. 下一步读什么

先读 `TASK_ROUTING.md`，再按任务读取少量 slice / deep。

- 改主循环或阶段行为：`slices/data-flow.md`、`slices/context-compression.md`、`slices/tools.md`。
- 改工具或状态写入：`slices/tools.md`、`deep/tool-state-writes.md`。
- 改记忆：`slices/memory.md`、`deep/memory-recall.md`。
- 改前端或 SSE：`slices/frontend.md`、`deep/sse-events.md`。
- 改持久化：`slices/persistence.md`、`deep/sqlite-schema.md`。
- 改 trace / eval：`slices/observability.md`、`slices/testing.md`、`deep/trace-flight-recorder.md`。

完整文档地图见 `INDEX.md`。
