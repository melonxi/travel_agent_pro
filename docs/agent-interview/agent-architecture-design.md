# Travel Agent Pro Agent 架构设计面试问答

> 分类：Agent 架构设计  
> 目标：用中文回答架构、状态机、编排、工具、hook、生产化和迁移边界问题。  
> 注意：这里是面试组织素材，不是背诵稿。回答时优先讲清“为什么这样设计”和“代码里真实做到什么”，不要把规划项说成已落地能力。

---

## 0. 外部趋势定位

### Q：你如何把 Travel Agent Pro 和 2026 年 Agent 平台趋势对齐？

推荐回答：

我不会泛泛说“Agent 很火”，而是把项目和当前平台原语对齐。OpenAI 的 Agents SDK / AgentKit / Responses API / Evals / MCP connectors 强调的是 code-first agent runtime、tools、handoff、guardrails、trace 和 eval；Anthropic 的 “Building Effective Agents” 强调先用简单 workflow 和充分评估，只有复杂度真的需要时才引入更 agentic 或 multi-agent 结构；Google 的 A2A / agent protocol 方向强调跨 agent、跨工具、跨 UI 的标准化互操作。

Travel Agent Pro 当前是自研 loop，不是直接接 Agents SDK。但它的核心抽象是同一类问题：`run`、`tool_call`、`state`、`handoff`、`guardrail`、`trace`、`eval`、`memory`、`human-visible progress`。区别是这个项目选择把业务状态和写入边界留在服务端强管控，而不是交给 hosted workflow。

参考资料：

- [OpenAI Agents SDK](https://developers.openai.com/api/docs/guides/agents)
- [OpenAI Agent Evals](https://developers.openai.com/api/docs/guides/agent-evals)
- [OpenAI MCP and Connectors](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
- [OpenAI AgentKit](https://openai.com/index/introducing-agentkit/)
- [Anthropic Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Google Developer's Guide to AI Agent Protocols](https://developers.googleblog.com/en/developers-guide-to-ai-agent-protocols/)

### Q：如果面试官问“为什么不用现成 Agent 平台”，怎么答？

推荐回答：

先承认现成平台的真实价值：Agents SDK / AgentKit / LangGraph / Mastra 在 tool registration、tracing、handoff、guardrails、eval pipeline 上已经把工程脏活做了不少，新项目从这些平台起步通常更快。所以这个回答不是“平台不好”，而是“这个项目当前阶段更适合自研 control plane，未来会按层迁移”。

具体两层原因。第一，项目最难的部分不是让模型多调用工具，而是让旅行规划变成可控状态机：当前行程事实必须以 `TravelPlanState` 为准，状态写入必须走 plan writer，Phase 推进必须走 gate，Phase 5 并行结果必须回到标准 `replace_all_day_plans` 写工具路径。这些是领域 invariant，不是 runtime plumbing。第二，业务状态、阶段边界、记忆策略、SSE 事件和前端工具卡耦合很强，过早把 loop 藏到框架里会牺牲调试边界——这是阶段问题，不是框架问题。

我对迁移路径有清晰判断：先迁 provider/transport（替成 Responses API + Agents SDK runner）、再迁 tracing/eval、再评估 handoff/guardrail 原语；`TravelPlanState` 权威、`plan_writers` mutation contract、Phase gate 和服务端持久化边界不应迁移掉。判断成功的标准不是“接了 SDK”，而是同一批 golden cases 下 trajectory 不倒退。

### Q：Context Engineering 作为独立工程学科，对这个项目意味着什么？

推荐回答：

Karpathy 把 prompt engineering 升级为 context engineering 的核心观点是：决定 LLM 行为的不是单条 prompt 文采，而是“塞进上下文窗口的所有信息”——system prompt、状态快照、记忆召回、工具结果、历史压缩、工具 schema、错误反馈——这些共同构成一次推理的输入空间。Anthropic 的 “Building Effective Agents” 也强调：先优化 context 和 evaluation，再考虑更复杂的 agentic pattern。

项目里几个具体落点对应这个学科：

- **权威分层**：当前事实只来自 `TravelPlanState`；记忆只是“候选偏好”；自然语言历史不能直接覆盖业务字段。这避免“stale context 当事实用”。
- **重建优于累积**：phase / Phase 3 step 切换时通过 `rebuild_messages_for_phase_change()` 重建 runtime view，而不是无限累积。append-only history 单独保留审计能力。
- **白名单序列化**：Phase 5 shared prefix 只白名单 `goal/pace/departure_city/style/must_do/avoid`，preferences 按 key 排序，把 per-day 差异放到 user message。这是 Manus context engineering 的项目化落地。
- **错误结构化**：`ToolResult.error_code/suggestion` 把工具失败变成可恢复的观察，而不是噪声。
- **客观短板**：`build_time_context()` 在 system prompt 里写到秒级（`backend/context/manager.py:124`），任何 prefix 缓存都会被破坏；token 估算用 `len(text) // 3`（`backend/agent/compaction.py:356`、`backend/llm/anthropic_provider.py:451`）粗糙到只能做趋势判断。这两个是 context engineering 维度的具体债务。

我把它视为长期方向：prompt 模板替换是单点优化；context engineering 是系统能力，决定 Agent 的稳定性和成本曲线。

---

## 1. 全栈架构

### Q：请用 2 分钟介绍 Travel Agent Pro 的整体架构。

推荐回答：

Travel Agent Pro 是一个全栈 AI 旅行规划 Agent。前端是 React + Vite，通过 SSE 展示模型文本、工具卡、internal task 卡、记忆召回、TraceViewer 和 Phase 5 并行进度；后端是 FastAPI + async Python，自研 Agent Loop，底层支持 OpenAI / Anthropic provider 切换、工具系统、状态机、记忆系统、质量门控、持久化和 OpenTelemetry。

生产主路径是 Phase 1/3/5/7：Phase 1 收敛目的地，Phase 3 做旅行画像、候选池、骨架方案和交通住宿锁定，Phase 5 把骨架展开为每天的行程，Phase 7 做出发前查漏并冻结 `travel_plan.md` / `checklist.md`。核心设计是：LLM 负责开放推理和工具选择，确定性代码负责状态权威、工具权限、阶段推进、错误恢复和可观测性。

代码入口可以这样讲：

- `backend/main.py::create_app` 是 FastAPI composition root。
- `backend/api/routes/chat_routes.py` 和 `backend/api/orchestration/chat/stream.py` 承载 chat SSE。
- `backend/agent/loop.py::AgentLoop` 承载 think-act-observe 主循环。
- `backend/phase/router.py::PhaseRouter` 根据状态推断 Phase。
- `backend/state/models.py::TravelPlanState` 是当前旅行事实权威。

### Q：它为什么不是 ChatGPT wrapper？

推荐回答：

ChatGPT wrapper 的核心是“把用户输入转发给模型，再展示回答”。Travel Agent Pro 的核心是“让模型在受控 runtime 里执行工作”：模型能选择工具、读取外部信息、调用写状态工具、观察工具结果、继续修复；代码则限制当前 Phase 可用工具、校验参数、执行读写隔离、推进状态机、记录 trace 和持久化。

更关键的是最终答案不是唯一质量对象。项目会检查中间 trajectory：是否选对工具、工具参数是否正确、状态是否真的写入、Phase 是否被 gate 允许推进、memory recall 是否误召回、Phase 5 workers 是否通过全局验证。这些都是 Agent 应用而不是聊天壳的特征。

### Q：一轮用户消息从前端到后端完整链路是什么？

推荐回答：

链路可以按 7 步讲：

1. 前端 `ChatPanel` 通过 fetch stream 发送 `POST /api/chat/{session_id}`。
2. chat route 恢复 session / plan，追加 user message，提交后台 memory extraction snapshot。
3. 同步执行 memory recall，生成本轮 memory context 和 `memory_recall` internal task。
4. `ContextManager.build_system_message()` 拼装 soul、时间、工具规则、phase prompt、当前状态和 memory context。
5. `AgentLoop.run()` 调 LLM，流式接收文本和 tool calls。
6. `ToolEngine` 执行工具，写工具成功后触发 hook、状态保存、PhaseRouter 推进和 SSE `state_update`。
7. `finalize_agent_run()` 保存 plan、append-only messages、session meta；Phase 7 时归档并派生 episode memory。

这条链路的重要点是：API 路由只负责 HTTP 边界，业务编排下沉到 `api/orchestration/*`，AgentLoop 只管运行时决策和状态推进，不直接承担所有 API 细节。

---

## 2. Deterministic Workflow vs Agentic Decision

### Q：项目里哪些部分是 deterministic workflow，哪些是 agentic decision？

推荐回答：

Deterministic workflow 是控制面：PhaseRouter、Phase 3 子步骤推断、工具白名单、写工具 schema、`TravelPlanState` mutation、backtrack 清理、context rebuild、append-only history、持久化、quality gate、trace/stats。这些不能交给模型自由发挥，因为它们决定系统一致性。

Agentic decision 是开放决策：用户模糊需求怎么解释，候选目的地怎么查，UGC 线索怎么筛，骨架方案怎么组织，一天内 POI 顺序怎么取舍，工具结果不足时怎么补查或保守落地。这些问题路径不固定，适合 LLM 做多步推理。

项目的架构价值就在边界：模型负责不确定推理，代码负责不可违反的状态和协议。

### Q：为什么旅行规划适合“状态机 + Agent”混合架构？

推荐回答：

如果旅行规划只是目的地、日期、预算三个字段，表单更好。但真实旅行规划存在模糊意图、UGC 信息、POI 可行性、路线顺路性、体力节奏、预算取舍和用户反悔。纯表单太僵，纯 Agent 又容易跑飞。

所以项目用 Phase 1/3/5/7 把大方向固定下来，用写工具把承诺状态结构化，用 LLM 在每个阶段内做开放探索。这样既保留 Agent 的灵活性，又避免“模型说已经确定了，但系统状态没有写入”的不一致。

### Q：这个项目最能体现 Agent 架构能力的地方是什么？

推荐回答：

我会展示 Phase 3 skeleton 到 Phase 5 parallel handoff 的一条 trace。它同时覆盖：

- Phase 3 写入骨架方案并锁定选择。
- PhaseRouter 校验目的地、日期、住宿、骨架天数后进入 Phase 5。
- AgentLoop 在循环顶部识别 Phase 5 并行入口。
- Python Orchestrator 拆 DayTask，Day Worker 并发生成候选。
- Worker 只写 candidate artifact，不直接写 `TravelPlanState`。
- Orchestrator 做全局校验，最终 handoff 给 AgentLoop。
- AgentLoop 构造内部 `replace_all_day_plans` tool call，复用标准写工具、hook、telemetry 和 Phase 5→7 transition。

这比单看最终旅行计划更能证明系统不是简单 prompt glue。

---

## 3. Phase 1/3/5/7 状态机

### Q：为什么是 Phase 1/3/5/7，而不是 1/2/3/4？

推荐回答：

这是历史演进后的生产主路径，奇数编号保留了早期阶段语义和文档兼容。当前真正重要的不是编号，而是每个 Phase 的职责和产物：

- Phase 1：目的地和基础信息收敛，核心 gate 是 `destination`。
- Phase 3：框架规划，包含 `brief/candidate/skeleton/lock` 四个子步骤。
- Phase 5：把已选骨架展开成每日 `daily_plans`。
- Phase 7：出发前检查和双 markdown 交付物冻结。

### Q：`PhaseRouter.infer_phase()` 的判断依据是什么？

推荐回答：

它只看 `TravelPlanState`，不看模型自述。规则大致是：

- 没有 `destination`，停在 Phase 1。
- 有目的地但缺 `dates`、`selected_skeleton_id` 或 `accommodation`，停在 Phase 3。
- 已选 skeleton 的天数和 `plan.dates.total_days` 不一致，仍停在 Phase 3。
- `daily_plans` 数量少于 `total_days`，进入或停在 Phase 5。
- 每日计划完整后进入 Phase 7。

这解决了 Agent 常见问题：模型可能说“我们进入下一步”，但系统必须以结构化状态和 gate 为准。

### Q：Phase 3 为什么拆成 `brief/candidate/skeleton/lock`？

推荐回答：

Phase 3 的本质是从“旅行画像”收敛到“可执行日任务合同”。四个子步骤对应不同决策粒度：

- `brief`：确认目标、节奏、约束、同行人和必须/避免事项。
- `candidate`：用搜索和 UGC 构建候选池，再筛 shortlist。
- `skeleton`：把候选项组织成 2-3 套日级骨架，明确区域、主题、locked/candidate POI。
- `lock`：锁交通和住宿，让 Phase 5 有真实时间、区域和预算边界。

拆子步骤的好处是工具可分阶段开放，prompt 目标更清晰，也能在 LLM 跳阶时通过“前瞻容错”保留写入能力。

### Q：Phase 3→5 的 gate 为什么要检查 skeleton 天数？

推荐回答：

`DateRange.total_days` 是 inclusive 自然日语义，例如 4 月 1 日到 4 月 3 日是 3 天。Phase 3→5 必须保证 selected skeleton 的 `days` 数量等于 `plan.dates.total_days`。否则用户中途改日期后，旧骨架可能仍是 5 天，新日期已经是 4 天，Phase 5 worker 会按错误骨架生成每日行程。

代码里 `_hydrate_phase3_brief()` 会用权威 `plan.dates` 强制覆盖 `trip_brief.dates/total_days`，避免 brief 里残留 stale 值。这个细节很重要：权威事实只能来自 `TravelPlanState`，不能来自历史自然语言或旧 brief。

### Q：Backtrack 是怎么设计的？

推荐回答：

Backtrack 是显式工具和 API 都支持的上游重决策机制。`request_backtrack(to_phase, reason)` 会委托 `BacktrackService`：

- 记录 `BacktrackEvent`。
- 清理目标阶段之后的下游产物。
- 保留 preferences / constraints 这类跨阶段仍有效的信息。
- 更新 `plan.phase`。
- 触发 runtime message rebuild，避免旧阶段 tool result 污染新阶段。

例如从 Phase 5 回 Phase 3，会清掉 `daily_plans` 和 deliverables，但不会删除用户明确表达的偏好约束。这样既能回滚方案，又不会把用户长期意图一起丢掉。

---

## 4. `TravelPlanState` 权威状态

### Q：为什么 `TravelPlanState` 是当前旅行事实的唯一权威？

推荐回答：

Agent 的对话历史、工具结果、memory 都可能包含候选信息或旧信息。比如用户改过日期，历史消息里仍然有旧日期；memory 里可能有“上次东京住新宿”的偏好，但这次已经锁定银座；工具结果里可能列出候选酒店，但不是最终选择。

所以当前事实必须统一落到 `TravelPlanState`：

- 当前目的地、日期、预算、住宿、骨架、每日计划，以 `TravelPlanState` 为准。
- memory 只提供偏好和历史经验候选，不直接覆盖当前事实。
- prompt 和 runtime context 从状态快照重建，避免旧自然语言污染。
- 所有状态变更必须走写工具和 `plan_writers.py`。

### Q：为什么把状态写入拆成工具层和 `plan_writers` mutation layer？

推荐回答：

工具层是给 LLM 用的接口，关注 schema、description、phase 门控、参数校验和错误反馈；`plan_writers.py` 是确定性 mutation layer，关注如何修改 `TravelPlanState`。

拆两层有三个价值：

- LLM 看到的是语义清楚的工具，例如 `set_skeleton_plans`、`select_skeleton`、`replace_all_day_plans`。
- API、backtrack、测试和未来非 LLM 入口可以复用相同 mutation 行为。
- `PLAN_WRITER_TOOL_NAMES` 可以统一驱动写后持久化、validator、phase transition 和 telemetry。

### Q：`set_skeleton_plans` 为什么要在写入边界校验 POI 唯一性？

推荐回答：

Phase 5 并行 worker 是单日求解器。如果同一个 skeleton 里多个天的 `locked_pois` / `candidate_pois` 已经重复，等到 Phase 5 再修就晚了：多个 worker 会各自认为自己使用该 POI 合理，Orchestrator 只能事后冲突处理。

所以项目把结构性冲突提前到 Phase 3 写入边界：同一套 skeleton 内 POI 在 `locked_pois` / `candidate_pois` 间必须全局唯一；`area_cluster`、`locked_pois`、`candidate_pois` 也有基本 schema 校验。这个设计把 Phase 3 从“方案建议”提升成“日任务合同”，让 Phase 5 只做事实落地。

---

## 5. API Orchestration 拆分

### Q：后端从 `main.py` 单体拆到 `api/` 包，动机是什么？

推荐回答：

早期 `main.py` 既负责 FastAPI 装配，又承担路由、SSE、memory recall/extraction、hook 构建、保底持久化和 session restore，职责过重。现在拆分为：

- `backend/main.py`：仍是 composition root，负责加载配置、创建 manager/store/provider、注册路由。
- `api/routes/`：按 HTTP 资源分组，只做参数解析和响应边界。
- `api/orchestration/agent/`：构建 `AgentLoop`、工具引擎和 hooks。
- `api/orchestration/chat/`：chat SSE、事件转换、run finalization。
- `api/orchestration/memory/`：memory recall、extraction、任务流和 episode 归档。
- `api/orchestration/session/`：restore、message persistence、runtime view、pending notes。
- `api/orchestration/common/`：跨编排复用的 telemetry helper 和 LLM error helper。

面试里要准确说：应用装配没有迁到 `api/app.py`，`main.py::create_app` 仍是入口；拆分的是路由和业务编排。

### Q：`api/orchestration/agent/builder.py` 和 `api/orchestration/chat/stream.py` 职责区别是什么？

推荐回答：

`builder.py` 负责构建可运行的 Agent：创建 LLM provider、ToolEngine、HookManager、ReflectionInjector、ToolChoiceDecider、ToolGuardrail，再把它们注入 `AgentLoop`。

`chat/stream.py` 负责把一个 Agent run 接到 SSE 协议上：消费 `AgentLoop.run()` 的 chunk，转成前端事件，记录 LLM/tool stats，写工具成功后增量保存 plan，处理 LLMError、cancel、continue 和 finally 保底持久化。

一句话：builder 管“运行时怎么装配”，stream 管“这次请求怎么跑完并发给前端”。

### Q：为什么把工具注册放在 `api/orchestration/agent/tools.py`，而不是 `backend/tools/` 自己注册？

推荐回答：

`backend/tools/` 只定义工具工厂和工具实现；实际注册需要 runtime 依赖，例如当前 `plan`、API keys、FlyAI client、config 开关。把注册放在 `api/orchestration/agent/tools.py` 可以让工具实现保持纯粹，也让 Agent 构建过程集中可测试。

这也符合依赖方向：领域工具不应该知道 FastAPI app 或 session；编排层负责把工具和运行时依赖组装起来。

---

## 6. Agent Loop 与消息协议

### Q：`AgentLoop.run()` 的核心循环是什么？

推荐回答：

核心是 think-act-observe：

1. 根据当前 `plan.phase` 和 `phase3_step` 获取可用工具。
2. 调 `run_llm_turn()`，流式收集文本和 tool calls。
3. 如果没有 tool calls，尝试 state repair hint；没有修复需求则结束。
4. 如果有 tool calls，先 append assistant tool_calls message。
5. 执行 `_execute_tool_batch()`，把 tool results append 回消息历史。
6. 执行 `detect_phase_transition()`，必要时重建 runtime messages 和工具列表。
7. 循环直到完成、取消或达到 max iteration。

Phase 5 并行入口在循环顶部和循环边界都有 guard，防止最后一轮刚好把 phase 推到 5 时漏掉并行 Orchestrator。

### Q：如何保证 `assistant.tool_calls -> tool result` 的协议顺序？

推荐回答：

模型 API 对工具消息顺序很敏感：assistant 一旦带 tool_calls，后面必须紧跟对应 tool result，中间不能插 SYSTEM 消息。项目做了两层保护：

- `AgentLoop` 先 append assistant tool_calls message，再 append 每个 `Role.TOOL` result。
- validator、soft judge 等工具执行期间产生的 SYSTEM 反馈先进入 `_pending_system_notes`，下一次 LLM 调用前由 `before_llm_call` hook flush。

这样既保留了约束反馈，又不破坏 provider 的工具协议。

### Q：如果 LLM 输出了自然语言方案但没调用写状态工具怎么办？

推荐回答：

项目有 state repair hints。比如 Phase 3 skeleton 子步骤，如果模型文本里出现“方案 A / 轻松版 / 高密度版”等骨架信号，但 `plan.skeleton_plans` 仍为空，`AgentLoop` 会追加 SYSTEM 修复提示，要求模型调用 `set_skeleton_plans` 和必要的选择工具。Phase 5 也有 daily plan repair hint。

这个机制承认 LLM 可能“说了但没写”，但不把自然语言当状态事实，而是强制回到写工具路径。

---

## 7. 工具系统与读写隔离

### Q：工具系统的抽象是什么？

推荐回答：

工具是带 `@tool` 元数据的 async function。`ToolDef` 包含 name、description、phases、parameters、side_effect、human_label。`ToolEngine` 注册所有工具，并按 phase / Phase 3 step 过滤暴露给 LLM；执行时做 required 参数预校验，捕获 `ToolError`，把 `error_code` 和 `suggestion` 返回给模型修复。

工具不是普通函数列表，而是 Agent 和外部世界之间的 interface contract。

### Q：read tool 和 write tool 为什么要区分？

推荐回答：

读工具没有状态副作用，例如搜索、POI 查询、路线计算，可以并行执行降低延迟；写工具会修改 `TravelPlanState`，必须顺序执行，否则会出现竞态和 phase 判断混乱。

`ToolEngine.execute_batch()` 和 AgentLoop 的 batch 执行都会利用 `side_effect` 做读写区分：连续 read calls 可以 gather，write calls 按顺序执行，并保持最终返回顺序。

### Q：Phase 3 工具为什么按子阶段过滤，同时又允许“前瞻容错”？

推荐回答：

严格过滤能降低工具选择混乱：brief 阶段主要写 `trip_brief` 和基础信息，candidate 阶段写候选池，skeleton 阶段写骨架，lock 阶段锁交通住宿。

但 LLM 可能在 brief 阶段提前完成候选池分析。如果此时没有 `set_candidate_pool`，它只能自然语言描述，状态丢失，然后系统还停在原阶段。前瞻容错就是向前开放下一阶段写工具：brief 可写 candidate，candidate 可写 skeleton。它不是鼓励跳阶，而是防止“已经推理出来但写不了”的死循环。

---

## 8. Hook 系统

### Q：项目里的 hook 系统解决什么问题？

推荐回答：

Hook 系统把质量控制、压缩、校验和内部任务从主 AgentLoop 解耦。主 loop 只保证运行时顺序，hook 承担横切关注点：

- `before_llm_call`：flush pending system notes，做 tool result compaction 和历史压缩。
- `after_tool_call`：写工具后做增量 validator、预算锁定检查、backtrack rebuild 标记。
- `after_tool_result`：对 `save_day_plan` / `replace_all_day_plans` / `generate_summary` 触发 soft judge。
- `before_phase_transition` gate：可行性检查、硬约束检查、LLM judge 质量门控。

这些任务通过 `InternalTask` 进入 SSE，用户能看到“记忆召回”“行程质量评审”“阶段推进检查”等系统工作，而不是只看到界面卡住。

### Q：Quality Gate 和 Soft Judge 有什么区别？

推荐回答：

Quality Gate 是阻断型或半阻断型，发生在 phase transition 前。它会先查硬约束，必要时用 LLM judge 评分；低于阈值时注入修改建议并阻止推进，超过重试上限才允许继续。

Soft Judge 是写入后的质量评审，发生在工具结果之后。它给 pace、geography、coherence、personalization 等维度打分，通常不阻断主流程，而是把建议附到 trace / stats / internal task。

两者的关系是：gate 控制“能不能进入下一阶段”，judge 帮助“理解当前产物质量如何”。

### Q：hook 里产生 SYSTEM 消息为什么不直接 append？

推荐回答：

因为工具协议要求 assistant tool_calls 后紧跟 tool results。hook 如果在工具执行中间 append SYSTEM，会破坏消息序列。项目用 pending system notes 缓冲，等下一次 `before_llm_call` 再 flush。这个设计牺牲了一点即时性，但保证 provider 协议正确，是多 tool call agent 必须处理的工程细节。

---

## 9. Phase 5 Orchestrator-Workers

### Q：Phase 5 为什么适合并行？

推荐回答：

Phase 5 的任务是把已选 skeleton 展开成每日行程。每天的 POI 查询、时间表、路线顺序相对独立，天然可拆成 day-level tasks；并行能降低用户等待时间。

但跨天仍有全局约束：POI 不能重复、总预算不能失控、首尾日要衔接大交通、节奏要一致。所以项目不是让多个 Agent 自由协作，而是用 Python Orchestrator 拆分、派发、收集、验证，LLM Day Worker 只负责单日落地。

### Q：Orchestrator 是 LLM Agent 吗？

推荐回答：

不是。Orchestrator 是纯 Python 调度器，负责确定性编排：

- 找到 selected skeleton。
- split 成 `DayTask`。
- 注入 `forbidden_pois`、`mobility_envelope`、`date_role`、`day_budget`、`arrival_time` / `departure_time`。
- 并行运行 Day Workers。
- 收集 candidate artifact。
- 做全局验证和最多一轮 re-dispatch。
- 暴露 `final_dayplans` 给 AgentLoop handoff。

真正调用 LLM 的是 Day Worker。这个边界体现了“multi-agent 克制原则”：只有天然可并行的单日生成拆成 worker，全局控制仍由确定性代码负责。

### Q：Worker 为什么不能直接写 `TravelPlanState`？

推荐回答：

因为多个 worker 并发写共享状态会引入竞态，也会绕过主 AgentLoop 的工具事件、hook、Phase transition、telemetry 和持久化。当前设计是：

- Worker 只调用只读工具和 worker-only `submit_day_plan_candidate`。
- candidate 写入 run-scoped artifact：`{artifact_root}/{session_id}/{run_id}/day_N_attempt_M.json`。
- Orchestrator 校验后把 final dayplans handoff 给 AgentLoop。
- AgentLoop 构造内部 `replace_all_day_plans` tool call，走标准 `_execute_tool_batch -> detect_phase_transition` 链路。

这保证并行模式和串行模式最终回到同一套状态写入路径。

### Q：Day Worker 和主 AgentLoop 有什么区别？

推荐回答：

两者都是 think-act-observe，但 Worker 更窄：

- Worker 没有用户交互、没有 continue/cancel、没有完整 SSE 文本体验。
- Worker 只有单日上下文：shared prefix + day suffix。
- Worker 只能使用只读工具和候选提交工具。
- Worker 有更激进的收敛保护：重复查询抑制、POI 补救链阈值、后半程强制收口、JSON 修复回合。
- Worker 的 `_WORKER_ROLE` 不加载 `soul.md`，因为 `soul.md` 中“向用户提问/给选项”对无用户通道的 worker 不适用。

### Q：Shared prefix / KV-Cache 策略怎么设计？

推荐回答：

目标是让多个 Day Worker 的 system message 尽量字节级一致，从而提高 provider 侧 prefix cache 命中概率。做法包括：

- `build_shared_prefix()` 只放全局上下文和 worker 角色。
- `trip_brief` 白名单只保留 `goal/pace/departure_city/style/must_do/avoid`，排除 `dates/total_days/budget_per_day` 等重复或不该膨胀的字段。
- preferences 按 key 排序，避免同一信息不同顺序破坏 cache。
- soft day-level constraints 放入 day suffix，不污染 shared prefix。
- day suffix 作为 user message 传入，不让每个 worker 的 system message 因“第 N 天”不同而变化。

需要客观说明：项目目前没有接入 provider 的 `cached_input_tokens` 统计，所以不要声称真实测得了某个命中率；只能说这是设计目标和后续可观测性方向。

### Q：当前 Phase 5 并行有哪些已知 gap？

推荐回答：

要主动承认三个真实 gap：

1. `fallback_to_serial` 当前在失败率超过 50% 时只是 return，上层会 warning，但没有同轮真正进入串行生成。
2. re-dispatch 后如果仍有 error，当前主要是 log unresolved，仍可能设置 `final_dayplans`，生产上应阻断或要求回退 Phase 3。
3. Worker timeout / generic exception 当前没有稳定设置 `TIMEOUT` / `LLM_ERROR` 结构化 `error_code`，诊断完整性需要补齐。

这些不是推翻架构，而是 hardening backlog。架构方向是对的：worker 不直接写状态，结果通过标准 write tool handoff；需要加强的是失败路径和阻断策略。

---

## 10. 多 Agent 克制原则

### Q：你如何解释“多 Agent 克制原则”？

推荐回答：

多 Agent 不应该是炫技。拆分的判断标准是：任务是否天然可并行、上下文是否可以隔离、合并是否可确定性验证、失败是否能降级。

Travel Agent Pro 只在 Phase 5 拆 Day Workers，因为每天行程相对独立，且 Orchestrator 可以做全局校验。Phase 1、Phase 3、Phase 7 没有拆多 Agent，因为这些阶段高度依赖连续对话、用户选择和全局取舍；强拆会增加成本、延迟和错误面。

这和 Anthropic 的建议一致：先用简单 workflow 和 eval，只有简单系统不足时再增加 agentic complexity。

### Q：如果未来要扩展更多 Agent，你会怎么做？

推荐回答：

我会先定义 ownership 和 handoff contract，而不是先起多个模型角色。例如可以考虑：

- Visa / policy verifier：只读官方来源，输出结构化风险，不写主状态。
- Booking executor：高风险写操作，必须 human approval。
- Budget auditor：只读 `TravelPlanState`，输出 budget issue。

所有 specialist 都必须满足：输入输出结构化、权限最小化、trace 可见、失败可降级、最终状态写入仍经过主 writer contract。

---

## 11. 生产平台化

### Q：如果要上线给真实用户，第一批改什么？

推荐回答：

我会按控制面优先：

1. Auth、tenant isolation、rate limit、session lock。
2. 外部工具 timeout、circuit breaker、fallback 和 source trust。
3. trace/stats 持久化，支持跨重启复盘。
4. eval 进入 CI，prompt/model/tool schema 变更必须跑 regression。
5. Phase 5 fallback 真正串行接管，unresolved error 阻断提交。
6. memory 可查看、可删除，PII policy 可审计。
7. 高风险 action human-in-the-loop，尤其是预订、支付、取消。

核心原则是先让每次 Agent 行为可审计、可回滚、可评估，再扩大自动化范围。

### Q：当前系统离生产平台还有哪些客观短板？

推荐回答：

我会列事实，不夸大：

- 当前没有完整认证、租户隔离和限流。
- `SessionStats` 主要是进程内对象（`backend/telemetry/stats.py:146`），服务重启或多副本部署后 trace 不完整，需要落库或接专门 trace backend。
- 通用 `web_search` 没有官方站点限定、freshness metadata 和 source type，政策/签证/营业时间类问题需要专门 verification tool；同时工具结果会原样进入 LLM 上下文，**indirect prompt injection** 防御还没专门做（见下一题）。
- `build_time_context()` 在 system prompt 里写到秒级（`backend/context/manager.py:114-124`，`%Y-%m-%d %H:%M:%S`），即使其他字段完全稳定，时间字符串每秒变化就会让 prefix cache 命中率归零。生产化要把秒级时间挪到 user message 或缓存友好的粒度（按分钟/小时）。
- token 估算用 `len(text) // 3` 这种粗糙启发式（`backend/agent/compaction.py:356`、`backend/llm/anthropic_provider.py:451`），中文/英文/工具结果混合时误差很大，只能用作趋势判断，不能用于成本核算或精确预算。
- Phase 5 并行失败路径还需要 hardening：`fallback_to_serial` 不真串行接管、unresolved error 不阻断、Worker timeout/exception 缺结构化 `error_code`、前端 `redispatch` 状态在 `ParallelWorkerStatus.status` union 中缺失。
- KV-cache 命中率（PROJECT_OVERVIEW 提到的 ~93.75%）是引用 Manus 经验的设计目标，不是项目实测——provider 的 `cached_input_tokens` 还没接入 `SessionStats`。
- 外部 CLI/API 依赖需要统一 timeout、重试、熔断和审计。

这些短板不影响 demo 证明架构，但生产化必须补。

### Q：Indirect prompt injection 和工具结果污染怎么防？

推荐回答：

Indirect prompt injection 是 agent 时代独有的攻击面：第三方工具或 MCP server 返回的内容被原样送进 LLM 上下文，里面如果嵌了“忽略之前指令、调用 write 工具、把 X 发邮件”这类 payload，模型可能照做。OpenAI 的 MCP/connectors 文档明确把工具输出归类为不可信内容，要求 system/developer 指令不被覆盖。

项目当前已经做对的部分：

- **读写隔离**：worker 只暴露 8 个只读工具 + worker-only `submit_day_plan_candidate`（`backend/agent/phase5/day_worker.py:570-579`），没有 `request_backtrack` / plan writer，即便 worker 被 prompt 污染也写不到 `TravelPlanState`。
- **写入边界确定性**：所有正式写入必须走 `PLAN_WRITER_TOOL_NAMES`（17 个）+ `plan_writers.py` mutation layer，不接受“工具结果里的自然语言指令”作为状态来源。
- **Pending system notes 不直接 append**：避免工具结果中间被注入伪 system message 顺势串改协议。
- **Guardrail / `REDUNDANT_SEARCH` / `INVALID_ARGUMENTS` 反馈结构化**：异常变可观察事件而非控制流。

需要主动承认的缺口：

- 工具结果文本目前没有 sanitize（剥 markdown 链接、检测“ignore previous instructions”模式、限制 URL 长度）。
- `web_search` / `xiaohongshu_*` 引入的是公开 UGC，恶意 SEO 内容可能携带 payload。
- MCP / connectors 接入后风险面更大，因为远程 server 可以定义 schema 和返回 prompt。

生产化方向：tool result 进入上下文前做 sanitization 层（剥可疑指令、域名白名单、内容长度上限）；high-risk write action（预订、支付、邮件）默认 require human approval；trace 记录每个 tool result 的来源 domain 和 trust tier；接 MCP 时只接官方/可信 server 并按 read/write/high-risk 分级。这些是“当前已有边界 + 还没补的层”，不是“正在修补的根本性漏洞”。

### Q：如何设计线上 eval 和 A/B prompt rollout？

推荐回答：

每个 run 要记录 prompt version、model/provider、phase、tool list、memory config、feature flags。线上按 session 或 user 分桶，指标包括：

- Phase completion rate。
- tool error rate / invalid argument rate。
- repeated search rate。
- memory false recall / false skip。
- judge score。
- latency / token cost。
- user correction / backtrack rate。

Eval 不只看最终回答，还要看 trajectory：工具是否必要、参数是否正确、guardrail 是否触发、handoff 是否合理、状态是否正确推进。OpenAI agent evals / trace grading 的方向也是 workflow-level 评估。

### Q：你怎么看 LLM-as-Judge 的偏差？

推荐回答：

LLM-as-Judge 在项目里已经有两个落点：`before_phase_transition` 的 quality gate 和写后的 soft judge。它能自动化“合理性、覆盖度、个性化”这类难以规则化的维度，但要承认它是有偏的判官，不是金标准。已知偏差包括：position bias（更偏向第一个候选）、verbosity bias（更长回答更高分）、self-preference bias（同 family 模型给自家输出更高分）、风格 vs 事实混淆、对小差异敏感度不足。

项目里的对冲做法和未来方向：

- judge 只做半阻断：低于阈值时注入修改建议，超过重试上限才允许推进，不让 judge 一票否决。
- judge 输出和阈值都纳入 trace，随 prompt 一起 version 化，可回归。
- 关键决策（POI 重复、时间冲突、交通衔接、预算）用确定性 `_global_validate()` 的 7 类 issue（`backend/agent/phase5/orchestrator.py:260-466`）做硬规则，不让 LLM 判官接管。
- 生产化要做的：rubric 拆细 + reference answer + pairwise（A/B）评分降低 verbosity bias；用人工标注子集校准 judge 模型，定期算 judge-vs-human agreement；不同 family 模型交叉评分，发现 self-preference。

我的判断是：LLM-as-Judge 是 trajectory eval 的高价值工具，但不能替代代码化的硬规则和人工标注；它适合做“快速回归 + 大面积扫描”，关键 gate 仍要双层（规则 + judge）。

---

## 12. 迁移与扩展边界

### Q：如果迁移到 OpenAI Agents SDK，哪些可以迁，哪些不能迁？

推荐回答：

可迁移：

- Provider 调用、streaming、tool schema 表达。
- tracing/evals 的部分基础设施。
- MCP hosted tools 或 connectors 的接入层。
- 某些 handoff / guardrail 原语。

不应迁移掉：

- `TravelPlanState` 作为当前事实权威。
- `plan_writers.py` mutation contract。
- PhaseRouter gate 和 backtrack 清理语义。
- API 层 append-only history / runtime view 分离。
- 高风险工具的服务端审批和审计。

也就是说，迁移应该替换 runtime plumbing，而不是把业务控制面交给 SDK。

### Q：如果接 MCP / connectors，安全边界怎么设计？

推荐回答：

MCP/connectors 的价值是减少自写工具适配，但风险是远程 server 可以定义工具 schema、读取上下文、返回 prompt injection，甚至执行敏感 action。

生产接入时我会做：

- 只接官方或可信 server。
- 工具按 read/write/high-risk 分级。
- 敏感写操作默认 require approval。
- 记录发给第三方的参数和返回摘要。
- 工具输出按不可信内容处理，不能覆盖 system/developer 指令。
- URL、文件、图片等外部引用做域名和内容校验。

这和项目当前读写隔离、ToolGuardrail、pending notes、trace 审计方向一致。

### Q：如果新增一个 LLM provider，需要改哪里？

推荐回答：

最小变更路径是：

- 新增 `backend/llm/<provider>_provider.py`，实现 `LLMProvider` Protocol。
- 在 `backend/llm/factory.py` 注册 provider。
- 在 `config.yaml` 或 `llm_overrides` 配置按阶段路由。
- 增加 provider 行为测试和错误分类测试。

不需要改 `AgentLoop`、`PhaseRouter`、`TravelPlanState` 和 plan writers。这说明 provider 被隔离在基础设施层，不污染业务控制面。

### Q：如果新增一个新领域，比如“留学规划 Agent”，哪些架构能复用？

推荐回答：

能复用的是 Agent control plane：

- Phase state machine 思路。
- `TravelPlanState` 等价的领域状态权威。
- read/write tool 隔离。
- writer mutation layer。
- hook / quality gate / soft judge。
- append-only history + runtime view 分离。
- trace/eval/internal task 可见性。

要重写的是领域模型、阶段产物、工具、prompt 和 eval case。也就是说护城河不是旅行 prompt，而是“把 LLM 不确定性放进可控工程边界”的系统方法。

---

## 13. STAR 答案

### Q：你在这个项目里最核心的 ownership 是什么？（STAR）

推荐回答：

- **Situation**：早期系统容易滑向聊天壳：LLM 能给旅行建议，但状态是否写入、阶段是否推进、工具是否选对、历史是否污染上下文都不稳定。
- **Task**：我的目标是把它做成可解释的 Agent control plane，有状态权威、工具协议、阶段 gate、可观测轨迹和回归评估。
- **Action**：我围绕 `AgentLoop`、`PhaseRouter`、`TravelPlanState`、plan writer 工具、hook、memory recall、Phase 5 Orchestrator-Workers、SSE internal task、append-only history 和 trace/eval 建立运行时边界。
- **Result**：系统不再只回答“推荐去哪”，而能解释每一步为什么发生：哪一轮调用了哪个工具、写了哪个状态字段、为什么 phase 推进或被阻断、并行结果如何通过标准写工具落回主状态机。

### Q：讲一个你主动修正的架构问题。（STAR）

推荐回答：

- **Situation**：Phase 5 并行最初的问题是 Orchestrator 容易变成“另一个写状态入口”。如果它直接写 `daily_plans`，就会绕过 AgentLoop 的工具事件、hook、phase transition 和 telemetry。
- **Task**：我要保留并行低延迟，同时保证并行和串行的状态写入路径等价。
- **Action**：我把 Day Worker 输出改成 candidate artifact，把 Orchestrator 改成只暴露 `final_dayplans`，再由 AgentLoop 构造内部 `replace_all_day_plans` 工具调用，走标准 `_execute_tool_batch -> detect_phase_transition`。
- **Result**：并行结果能复用现有 plan writer、validator、soft judge、Phase 5→7 gate、SSE tool result 和持久化逻辑，避免并行链路成为状态一致性的旁路。

### Q：这个项目最值得反思的一次设计是什么？（STAR）

推荐回答：

- **Situation**：早期 runtime messages 同时承担 LLM prompt 工作集和完整会话历史。Phase 切换、compaction、backtrack、restore 都会重建 runtime prompt，导致历史保全和上下文收缩纠缠。
- **Task**：我需要让 LLM 看到短而干净的当前上下文，同时保留完整审计历史和恢复能力。
- **Action**：我把消息体系拆成 append-only history 和可重建 runtime view。SQLite messages 增加 `history_seq`、`context_epoch`、`rebuild_reason`、`run_id`、`trip_id`；phase/step/backtrack 前先 flush 旧 runtime，再推进 context epoch。
- **Result**：runtime prompt 可以安全收缩，恢复 session 不需要 replay 旧阶段 tool results，backtrack 前后的同一 Phase 3 子步骤也能通过不同 epoch 做诊断。

### Q：如果面试官问“你的 Agent 工程第一性原理是什么”，怎么答？

推荐回答：

Agent 应用不是让模型“多想几步”，而是把不确定性关进可观察、可回滚、可评估的工程边界。LLM 适合开放推理、候选生成和工具选择；代码必须负责状态权威、工具权限、阶段边界、错误恢复、trace 和 eval。成熟 Agent 的质量不只看最终回答，还要看它走过的轨迹是否安全、必要、可解释、可复现。

