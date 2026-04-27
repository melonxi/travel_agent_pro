# Travel Agent Pro Ownership Audit（面试版）

## 审计依据

本次审计基于当前仓库的真实代码、配置、README、`PROJECT_OVERVIEW.md`、测试、eval、脚本和部分历史文档。审计目标是帮助项目 owner 在技术面试中解释架构、边界、设计取舍和风险点。

验证命令：

```bash
cd backend && pytest --collect-only -q
```

当前可收集 `1638 tests`。本次只做 collect，没有运行全量测试，因此不能声称全部测试通过。

关键入口文档与配置：

- `PROJECT_OVERVIEW.md`
- `README.md`
- `backend/pyproject.toml`
- `config.yaml`

## 一句话项目定位

Travel Agent Pro 是一个手写 Agent Loop 的全栈旅行规划 Agent。React 前端通过 SSE 与 FastAPI 后端交互，后端用 Phase 1/3/5/7 状态机、工具系统、记忆系统、质量门控、Trace 和 eval harness，把模糊旅行需求逐步推进到可交付的 `travel_plan.md` / `checklist.md`。

面试表达重点：这不是 LangChain 调包项目，核心价值在于你实现并能解释 Agent 系统里的状态机、工具调用协议、上下文装配、并行 Worker、记忆召回、质量控制和可观测性。

## 核心运行链路

1. 前端 `frontend/src/components/ChatPanel.tsx::ChatPanel` 调用 `frontend/src/hooks/useSSE.ts::sendMessage`，向 `POST /api/chat/{session_id}` 发送消息。
2. 后端 `backend/api/routes/chat_routes.py::register_chat_routes` 内的 `chat()` 恢复 session、追加 user message、触发同步 memory recall 和异步 memory extraction snapshot。
3. `backend/api/orchestration/chat/stream.py::run_agent_stream()` 把 AgentLoop chunk 转成 SSE：`text_delta`、`tool_call`、`tool_result`、`state_update`、`internal_task`、`memory_recall`、`error`、`done`。
4. `backend/agent/loop.py::AgentLoop.run()` 执行 think-act-observe：LLM 输出工具调用，工具执行，结果回灌，再判断阶段转换。
5. `backend/tools/engine.py::ToolEngine.execute_batch()` 按读写拆分：读工具可并行，写状态工具顺序执行。
6. `backend/phase/router.py::PhaseRouter.infer_phase()` 根据 `TravelPlanState` 完整度推进 Phase。
7. 成功后 `backend/api/orchestration/chat/finalization.py::finalize_agent_run()` 保存 plan、messages、session run 状态，Phase 7 时归档。

## 你必须掌控的模块地图

| 模块 | 面试要点 | 代码证据 |
|---|---|---|
| 应用装配 | `create_app()` 创建 config、state、memory、routes、lifespan | `backend/main.py::create_app` |
| 状态模型 | `TravelPlanState` 是当前旅行唯一权威事实源 | `backend/state/models.py::TravelPlanState` |
| 状态持久化 | JSON plan/snapshot/deliverables，session id 正则校验 | `backend/state/manager.py::StateManager` |
| Agent Loop | 自研循环、修复 hint、取消、Phase 5 分流 | `backend/agent/loop.py::AgentLoop` |
| LLM 抽象 | OpenAI/Anthropic provider，错误归一化 | `backend/llm/factory.py::create_llm_provider` |
| 上下文 | `soul.md`、时间、phase prompt、runtime state、memory | `backend/context/manager.py::ContextManager` |
| 工具系统 | `@tool` + schema + phase gate + read/write side effect | `backend/tools/base.py`, `backend/tools/engine.py` |
| 写状态工具 | 17 个 plan writer，经 `PLAN_WRITER_TOOL_NAMES` 识别 | `backend/tools/plan_tools/__init__.py` |
| 记忆系统 | v3 profile / working memory / episodes / slices | `backend/memory/manager.py::MemoryManager` |
| Phase 5 并行 | Python Orchestrator + Day Worker + artifact handoff | `backend/agent/phase5/orchestrator.py::Phase5Orchestrator` |
| Harness | guardrail、validator、judge、feasibility gate | `backend/harness/guardrail.py`, `backend/harness/validator.py`, `backend/harness/judge.py` |
| Trace/Stats | in-memory `SessionStats` 转 Trace 视图 | `backend/api/trace.py::build_trace` |
| 前端交互 | SSE 消费、工具卡、记忆卡、并行进度、继续/重试 | `frontend/src/components/ChatPanel.tsx` |

## 关键设计决策

### 1. 不用 LangChain，而是手写 Agent Loop

项目目标是掌控 Agent 工程底层：tool protocol、状态机、streaming、trace、guardrail、memory 和并行调度，而不是快速 demo。核心实现在 `backend/agent/loop.py::AgentLoop.run()`。

### 2. 确定性状态机，而不是让 LLM 自己宣布进度

`PhaseRouter.infer_phase()` 用 `destination/dates/skeleton/accommodation/daily_plans` 判断阶段，降低 LLM 自述进度的不确定性。旅行规划的阶段边界相对清晰，所以适合规则推进；开放生成和工具选择仍交给 LLM。

### 3. 写状态必须走工具，不让 LLM 直接改 plan

例如 `update_trip_basics`、`set_skeleton_plans`、`replace_all_day_plans` 都在工具层校验参数，再委托 writer mutation。写工具集中在 `backend/tools/plan_tools/`，共享 mutation layer 在 `backend/state/plan_writers.py`。

### 4. 记忆与当前状态分权

当前旅行事实来自 `TravelPlanState`；长期偏好来自 profile；当前 trip 短期提醒来自 working memory；历史经验来自 episode slices。召回入口是 `backend/api/orchestration/memory/turn.py::build_memory_context_for_turn()`。

### 5. Phase 5 用并行 Worker 加速日程生成

满足 phase=5、无 daily_plans、已选 skeleton 等条件后，走 `backend/agent/phase5/parallel.py::should_use_parallel_phase5()`。Orchestrator 不直接写状态，而是把 `final_dayplans` handoff 给 AgentLoop，再通过内部 `replace_all_day_plans` 标准工具提交。

## 测试与评估资产

- 后端测试文件：`145` 个 `test_*.py`，当前 collect 到 `1638 tests`。
- Eval 数据：`backend/evals/golden_cases/` 下约 34 个 golden cases，`backend/evals/reranker_cases/` 下 18 个 reranker-only cases。
- Eval runner 支持 `phase_reached`、`state_field_set`、`tool_called`、`memory_recall_field` 等断言：`backend/evals/runner.py::evaluate_assertion()`。
- Playwright E2E 位于根目录 `e2e-*.spec.ts`，覆盖 Phase 1、发送按钮、等待态、重试/继续体验。
- 注意：README 仍写 `590+ tests`，但当前 pytest collect 是 `1638`；这是文档陈旧点，不要在面试中照 README 数字背。

## 外部依赖与运行边界

- LLM：OpenAI + Anthropic，按 `config.yaml` 可阶段覆盖 provider/model。
- 搜索/旅行工具：Tavily、Google Maps、Amadeus、OpenWeather、FlyAI CLI、小红书 CLI。
- `backend/tools/web_search.py::make_web_search_tool()` 明确不支持域名白名单、官方站点限定、时间窗口过滤。
- `backend/tools/flyai_client.py::FlyAIClient` 是 CLI wrapper，CLI 不可用时返回空结果，部分 MCP 错误会抛 RuntimeError。
- 当前没有从代码确认真实部署环境、真实 API key、外部服务稳定性。

## 面试可主动承认的风险点

### 1. Phase 5 fallback 语义不完全闭环

`backend/agent/phase5/orchestrator.py::Phase5Orchestrator.run()` 在失败率大于 50% 且 `fallback_to_serial` 开启时直接 `return`。上层会发 warning，但当前代码没有在同一轮真正进入串行生成。

### 2. Phase 5 unresolved error 仍可能提交

re-dispatch 后如果仍有 error，代码只 log warning，仍设置 `final_dayplans`。相关逻辑在 `backend/agent/phase5/orchestrator.py` 的 re-dispatch 后 revalidate 分支。这是质量门控可进一步收紧的点。

### 3. Worker 错误码与文档不完全一致

`backend/agent/phase5/day_worker.py::run_day_worker()` 在 timeout 和 generic exception 返回 `DayWorkerResult` 时没有设置 `error_code`。`PROJECT_OVERVIEW.md` 里提到 `TIMEOUT/LLM_ERROR`，代码未完全落地。

### 4. 前端并行状态类型漏了 `redispatch`

后端会发 `status="redispatch"`，但 `frontend/src/types/plan.ts::ParallelWorkerStatus.status` 只有 `running/done/failed/retrying`，`frontend/src/components/ParallelProgress.tsx` 的图标表也未覆盖。

### 5. Trace/Stats 主要是进程内数据

session restore 会重建空 `SessionStats`：`backend/api/orchestration/session/persistence.py::restore_session()`。`/trace` 只查内存 sessions，进程重启后旧 session trace 可能 404：`backend/api/routes/artifact_routes.py::get_session_trace()`。

### 6. 预算锁定使用 total_days 作为住宿晚数

`backend/harness/validator.py::_trip_nights()` 返回 `plan.dates.total_days`。测试也按这个保守口径写在 `backend/tests/test_lock_budget_gate.py`。面试可解释为保守预算估计，但真实酒店晚数通常可优化为 `days - 1`。

## 无法从代码确认

- 当前生产环境是否部署、是否有认证、是否有多用户隔离。
- 真实 API key 是否配置、额度是否可用。
- 外部搜索、酒店、航班、小红书结果质量。
- README 中测试数量的历史通过率；本次只做 collect，没有跑全量测试。
- 文档里提到的 KV-cache 命中率、端到端性能收益，没有代码内可复验指标支撑。

## 面试高频问答准备

### 为什么不用 LangChain？

因为项目目标是掌控 Agent 工程底层：tool protocol、状态机、streaming、trace、guardrail、memory 和并行调度，而不是快速 demo。自研 Agent Loop 让状态推进、工具执行、错误恢复和可观测性都能按项目需求精确控制。

### Agent 跑飞怎么办？

已有 `max_iterations`、重复搜索抑制、state repair hint、guardrail、phase gate、quality gate、取消/继续机制。对应代码包括：

- `backend/agent/loop.py::AgentLoop.run()`
- `backend/agent/execution/tool_invocation.py::SearchHistoryTracker`
- `backend/api/orchestration/agent/hooks.py`

### 状态一致性怎么保证？

LLM 不能直接写状态，只能调用写工具；写工具校验 schema 和业务字段；成功写入后 `PhaseRouter` 再推断阶段。写状态工具集中在 `backend/tools/plan_tools/`，状态变更纯函数在 `backend/state/plan_writers.py`。

### 记忆系统怎么避免污染当前行程？

当前事实只读 `TravelPlanState`；profile 不常驻 prompt，只在 recall gate 命中时进入；working memory 仅当前 session/trip 有效；episode slices 只在历史经验相关问题中召回。

### Phase 5 并行为什么复杂？

单日规划天然可拆，但跨天有 POI 重复、预算、交通衔接、节奏冲突，所以 Orchestrator 负责拆分、约束注入、全局验证和标准工具提交。Worker 只负责单日，且只能使用只读工具和候选提交工具。

### 下一步 production hardening 会做什么？

优先补：

- 真正串行 fallback。
- Phase 5 unresolved error 阻断策略。
- 前端 `redispatch` status 类型修复。
- stats/trace 持久化。
- 外部工具 timeout/circuit breaker。
- auth/rate limit。
- 全量 eval CI。

