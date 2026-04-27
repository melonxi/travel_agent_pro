# Travel Agent Pro Agent 面试问题清单

> 用法：这份文档只放"面试官可能怎么问"。建议先用它自测，再对照 `docs/agent-interview-answer-key.md`。

## 外部面试趋势依据

当前 Agent 应用工程面试通常不只问 prompt，而会围绕生产 Agent 的完整生命周期追问：orchestration、tool use、memory、eval、trace、guardrails、成本延迟和失败恢复。

参考资料：

- OpenAI Agent Evals: https://platform.openai.com/docs/guides/agent-evals
- OpenAI Trace Grading: https://platform.openai.com/docs/guides/trace-grading
- OpenAI Agent Safety: https://platform.openai.com/docs/guides/agent-builder-safety
- OpenAI Practical Guide to Building Agents: https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/
- Anthropic Building Effective Agents: https://www.anthropic.com/engineering/building-effective-agents
- LangChain Agent Observability: https://www.langchain.com/articles/agent-observability
- AI Agents and Tool Use Interview Questions: https://www.datainterview.com/blog/ai-agents-interview-questions
- Survey on Evaluation of LLM-based Agents: https://arxiv.org/abs/2503.16416
- TraceSafe guardrails on tool-calling trajectories: https://arxiv.org/abs/2604.07223

## Round 0：开场项目介绍

1. 请你用 2 分钟介绍一下 Travel Agent Pro。
2. 这个项目为什么算 Agent 应用，而不只是一个 ChatGPT wrapper？
3. 你在这个项目里最核心的 ownership 是什么？
4. 简历上如果写"手写 Agent Loop"，你具体手写了哪些东西？
5. 你觉得这个项目最能体现 Agent 工程能力的点是什么？

追问：

- 如果面试官说"旅行规划不是普通表单工作流吗，为什么要 Agent？"你怎么回答？
- 如果面试官说"这看起来像 prompt + API glue"，你怎么证明它不是？
- 如果只能展示一个 trace 或代码文件，你展示哪个？

## Round 1：Agent Loop 与 Orchestration

1. `AgentLoop.run()` 的核心循环是什么？
2. 一轮用户消息从进入后端到返回前端，完整链路是什么？
3. 你如何处理 `assistant.tool_calls -> tool result` 的消息协议一致性？
4. 如果 LLM 不调用工具，只输出自然语言，你怎么避免状态没写入？
5. `max_iterations`、`max_retries`、`max_llm_errors` 分别是什么概念？
6. 这个项目里哪些地方是 deterministic workflow，哪些地方是 agentic decision？
7. 为什么不直接使用 LangChain / CrewAI / AutoGen？

追问：

- 如果 LLM 在 Phase 3 输出了骨架文本但没有调用 `set_skeleton_plans`，系统怎么处理？
- 如果工具执行完后阶段变了，系统如何重建上下文？
- 如果最后一轮刚好把 phase 推到 5，会不会错过 Phase 5 并行入口？
- Agent Loop 最近从单体文件拆成了 `agent/execution/` 和 `agent/phase5/` 两个子包，拆分的动机和边界是什么？

## Round 2：Phase 状态机与产品建模

1. 为什么项目采用 Phase 1/3/5/7，而不是 1/2/3/4/5/6/7？
2. `PhaseRouter.infer_phase()` 的判断依据是什么？
3. Phase 3 为什么拆成 `brief/candidate/skeleton/lock` 四个子步骤？
4. Phase 3 工具为什么要分子阶段开放？
5. Backtrack 怎么做？回退时哪些状态会清掉，哪些会保留？
6. 为什么 `TravelPlanState` 是当前旅行事实的权威来源？
7. Phase 3→5 的门控条件是什么？骨架天数校验的具体语义是什么？

追问：

- 用户中途改日期，怎么避免旧 `trip_brief.total_days` 污染后续规划？
- 为什么进入 Phase 5 前要校验 skeleton days 与 `dates.total_days`？
- 如果用户在 Phase 5 发现骨架不合理，如何回到 Phase 3？
- Phase 3 子步骤切换（brief→candidate→skeleton→lock）会触发什么系统行为？handoff note 在这里是否也适用？

## Round 3：Tool Calling 与工具系统

1. 工具系统的抽象是什么？`@tool` 做了什么？
2. `ToolEngine` 如何按 phase 和 phase3 step 过滤工具？
3. read tool 和 write tool 为什么要区分？
4. 为什么写状态工具必须顺序执行？
5. 工具参数校验在哪里做？LLM 传空参数会怎样？
6. 工具错误如何反馈给 LLM 修复？
7. 你如何防止模型重复搜索或陷入工具循环？
8. FlyAI / Google Maps / Amadeus / Tavily / 小红书工具分别解决什么问题？
9. 17 个状态写工具和共享 `plan_writers` mutation layer 的关系是什么？为什么拆成两层？

追问：

- 为什么 `search_destinations` 当前 `phases=[]`，它是否真的会暴露给 LLM？
- `web_search` 的能力边界是什么？
- 你如何评估 tool selection 是否正确？
- 如果一个工具返回了错误但模型继续给用户编答案，怎么监控？
- Phase 3 每个工具的描述采用"四段式结构"（功能说明/触发条件/禁止行为/写入后效果），为什么这么设计？
- Phase 3 子阶段的"前瞻容错"是什么？brief 阶段为什么可以写 `set_candidate_pool`？

## Round 4：Context、Prompt 和 Token Budget

1. `ContextManager.build_system_message()` 由哪些层组成？
2. 为什么 memory context 要标注为"不可信系统指令"？
3. Runtime context 里注入哪些状态？
4. Phase 切换时为什么用 handoff note，而不是简单保留全部历史？
5. 项目里的 compaction 策略是什么？
6. token 估算现在准确吗？如果生产化要怎么改？
7. prompt 纪律和代码 guardrail 的边界是什么？

追问：

- 如果工具结果很长，比如小红书正文和评论，如何避免撑爆上下文？
- 为什么 Phase 7 要展开 daily plans，而 Phase 3 某些子阶段只显示摘要？
- 如果用户上一轮说的偏好和 memory 里的偏好冲突，优先级如何处理？
- `build_phase3_prompt(step)` 的拼装公式是什么？GLOBAL_RED_FLAGS 注入时机和内容？

## Round 5：Memory System

1. v3 memory 的层级是什么？
2. profile、working memory、episode、episode slice 分别是什么？
3. 当前旅行事实为什么不进入长期记忆？
4. 每轮对话什么时候 recall，什么时候 extraction？
5. Stage 0/1/2/3/4 recall pipeline 是什么？
6. recall gate 如何避免"当前事实问题"误触发历史记忆？
7. reranker 如何选出最终进入 prompt 的记忆？
8. MemoryPolicy 如何避免 PII 或低置信度信息污染长期画像？

追问：

- "我上次不喜欢住新宿，这次东京住宿怎么选？"这句话会怎么召回？
- "这次预算多少？"为什么不应走历史记忆？
- 后台 memory extraction 失败会不会阻塞当前回答？
- latest-wins memory job scheduler 解决什么问题？
- Stage 1 LLM gate 为什么只把 `latest_user_message` 作为主判断对象，`previous_user_messages` 仅用于承接消歧？
- Stage 2 retrieval plan 的 source-aware contract 是什么？为什么 profile source 必填 `buckets`，episode_slice 不暴露 `buckets`？

### 5.1 Stage 3 语义召回与 Hybrid Recall

1. Stage 3 的默认 embedding 模型和 runtime 是什么？
2. embedding cache 在哪里？为什么要 `local_files_only=True`？
3. symbolic lane、semantic lane、lexical lane 分别做什么？当前哪些 lane 默认启用？
4. `evidence_by_id` sidecar 是什么？它如何从 Stage 3 透传到 Stage 4？
5. source widening 配置当前是否启用？为什么？
6. 如何验证 Stage 3 embedding runtime 在生产环境可正常工作？

追问：

- 如果 semantic lane 出错或超时，Stage 3 会如何降级？
- lexical expansion 的 feature flag 当前状态是什么？

### 5.2 Stage 4 Reranker 深度

1. Stage 4 reranker 的核心公式是什么？（`source_score = rule_score + evidence_score`）
2. rule signals 包括哪些维度？（bucket prior、domain/keyword overlap、destination、recency decay、applicability、conflict penalty）
3. evidence lane 的三个 score 权重默认值是什么？哪个权重当前为 0？
4. source-aware normalization 怎么做？为什么 profile 和 slice 要分池归一化？
5. source prior 的作用是什么？
6. intent profile 解析如何支持局部覆盖和安全回退？
7. `per_item_scores` 里包含哪些字段？hard-filter reason 如何记录？
8. 如何把 reranker 退回到第零期 rule-only 行为？

追问：

- 如果 evidence 三个 score 权重都写回 0，reranker 会退化为什么行为？
- 为什么 evidence 分数归一化时，单个非正值或全 0 已知分数保持为 0？
- `RecallRerankResult.selection_metrics` 为空候选集时填充什么？
- reranker-only eval 怎么设计？它为什么能排除 Stage 0/1/2 的抖动？

### 5.3 Memory Extraction 与 Episode 归档

1. profile extraction 为什么要在持久化前做 domain/key 规范化？
2. "stable preference 升级"是什么逻辑？
3. Phase 7 结束后 episode 归档和 slice 派生流程是什么？
4. slice taxonomy 包含哪些类别？（itinerary_pattern、stay_choice、transport_choice、budget_signal、rejected_option、pitfall）
5. route-aware gate 如何决定触发 `extract_profile_memory` 还是 `extract_working_memory`？
6. 任一路由提取失败时，另一路已成功写入的记忆会回滚吗？

## Round 6：Evaluation、Testing 与质量保障

1. 这个项目有哪些测试层？
2. 单元测试、golden eval、reranker-only eval、Playwright E2E 分别验证什么？
3. `backend/evals/runner.py` 支持哪些断言？
4. 如何评估一个 Agent 是否真的变好了？
5. 为什么 trace eval 比只看最终答案更适合 Agent？
6. Quality Gate 和 Soft Judge 怎么工作？
7. 你如何处理 LLM judge 不稳定或解析失败？

追问：

- 如果你改了 Phase 3 prompt，怎么防止 Phase 1 或 Phase 5 回归？
- 如果模型工具选择正确但最终答案不好，eval 怎么设计？
- 如果最终答案正确但中间调用了危险工具，eval 怎么发现？
- README 里的测试数量和 collect 数量不一致，你怎么解释？
- `memory_recall_field` 断言和 `memory_recall` 聚合指标（false skip / false recall / hit rate / zero-hit rate）分别验证什么？

## Round 7：Observability、Trace 与 Debugging

1. 项目中记录哪些 trace / stats？
2. `SessionStats` 记录什么？
3. TraceViewer 如何组织 LLM call、tool call、memory hit？
4. OpenTelemetry 在哪里接入？
5. 如果一次规划失败，你会从哪里开始 debug？
6. 为什么传统 request-response 日志不够？
7. 如何定位"模型选错工具"这类问题？

追问：

- 进程重启后旧 session 的 trace 还能看吗？
- 你会如何把当前 trace 系统升级成生产监控？
- 每个 tool call 应该带哪些字段才便于 debug？
- `memory_recall` SSE 事件携带了哪些遥测字段？为什么零命中也要暴露 gate/reranker 摘要？

## Round 8：Phase 5 并行 Orchestrator-Workers

1. Phase 5 为什么适合并行？
2. Orchestrator 是 LLM Agent 吗？它负责什么？
3. Day Worker 能调用哪些工具？
4. Worker 为什么不能直接写 `TravelPlanState`？
5. candidate artifact store 解决什么问题？
6. 全局校验检查哪些问题？
7. re-dispatch 怎么工作？
8. 如果大多数 worker 失败，fallback 怎么处理？

追问：

- 当前 fallback_to_serial 是否真的进入串行？
- 如果 re-dispatch 后仍有 error，系统是否会阻断提交？
- 前端如何展示并行进度？
- `redispatch` status 前端类型是否覆盖？

### 8.1 Day Worker 内部机制

1. Day Worker 的 mini agent loop 和主 AgentLoop 有什么区别？
2. Worker 的 `_WORKER_ROLE` 为什么不再从 `soul.md` 加载，而是内联常量？
3. Worker 的 system prompt 如何拼装？shared prefix + day suffix 的拆分逻辑是什么？
4. shared prefix 的 KV-Cache 命中率目标是多少？通过哪些手段保证缓存命中稳定性？
5. `build_shared_prefix` 对 trip_brief 做了白名单过滤，保留了哪些字段？为什么排除 `dates`/`total_days`？
6. preferences 在 shared prefix 里为什么要按 key 字典序排序？
7. soft 约束为什么不放在 shared prefix，而通过 `day_constraints` 路径注入 suffix？

追问：

- 如果 Worker 的 shared prefix 和主 Agent 的 system prompt 冲突了怎么办？
- Worker 上下文没有用户消息时，初始 user message 里注入什么？

### 8.2 Day Worker 四重收敛保障

1. 四重收敛保障分别是什么？
2. **重复查询抑制**：同 query 滑动窗口去重的阈值是多少？去重范围是 query 字符串还是归一化后的意图？
3. **补救链阈值**：`_MAX_POI_RECOVERY` 是多少？连续补救轮次超限后触发什么行为？
4. **后半程强制收口**：迭代过半后禁止什么操作？`_LATE_EMIT_PROMPT` 允许最多再调几个工具？
5. **JSON 修复回合**：Worker 输出 JSON 解析失败时，`_JSON_REPAIR_PROMPT` 引导模型做什么？修复失败后的保守落地策略是什么？

追问：

- 如果 Worker 在 JSON 修复回合中仍然输出 `0,0` 假坐标，`_FORCED_EMIT_PROMPT` 如何兜底？
- `_FORCED_EMIT_PROMPT` 和 `_LATE_EMIT_PROMPT` 分别在什么阈值触发？触发后 Worker 是否还能调工具？

### 8.3 Worker 约束注入与 DayTask

1. `DayTask` 包含哪些约束字段？每个字段的用途是什么？
2. locked_pois / candidate_pois / forbidden_pois 三级约束用什么图标区分？违反后果分别是什么？
3. `arrival_time` / `departure_time` 从哪里提取？`arrival_departure_day` 混合日的 prompt 文案怎么处理无时间时的兜底？
4. `day_budget` 怎么计算？（总预算/天数取整）
5. `day_constraints` 过滤规则是什么？（过滤 non-hard）
6. `repair_hints` 在 prompt 中如何渲染？为什么强调"本轮必须逐一解决"？
7. `area_cluster` / `mobility_envelope` / `date_role` 分别约束 Worker 的什么行为？

### 8.4 Worker 错误类别与诊断

1. Worker 失败时输出哪些结构化错误码？
2. `REPEATED_QUERY_LOOP` 和 `RECOVERY_CHAIN_EXHAUSTED` 的区别是什么？
3. `NEEDS_PHASE3_REPLAN` 表示什么？Orchestrator 收到这个错误码后应该做什么？
4. 当前源码中 JSON emit 失败、timeout、generic exception 是否都会稳定输出结构化 `error_code`？如果不会，生产化恢复策略应如何补齐？
5. Orchestrator 如何利用 Worker 错误码做降级决策？

### 8.5 Candidate Store 与 Handoff 机制

1. `Phase5CandidateStore` 的 artifact 存储路径格式是什么？（`phase5.parallel.artifact_root/{session_id}/{run_id}/day_N_attempt_M.json`）
2. 为什么不把 candidate DayPlan 直接写入 `TravelPlanState.daily_plans`？
3. AgentLoop 如何接收 Orchestrator 的 handoff？`Phase5ParallelHandoff` 包含什么？
4. AgentLoop 收到 handoff 后，如何构造 `replace_all_day_plans` 内部工具调用？
5. 为什么 handoff 要走标准写工具路径，而不是直接写 state manager？

追问：

- 如果 Orchestrator 完成后 AgentLoop 没有正确消费 handoff，会发生什么？
- `submit_day_plan_candidate` 和 `save_day_plan` 有什么区别？Worker 为什么只能用前者？

### 8.6 Shared Prefix 与 KV-Cache 策略

1. "Manus pattern"在 Phase 5 并行场景下指什么？
2. shared prefix 包含哪些内容？（角色、工具列表、通用规划知识、全局硬约束）
3. shared prefix 里 trip_brief 的白名单字段有哪些？为什么要做白名单而不是全量注入？
4. preferences 排序为什么影响 KV-Cache 命中率？
5. day_suffix 为什么从 system message 移到 user message？（Task 0 迁移的背景）

## Round 9：Reliability、错误恢复与用户体验

1. LLM error 怎么分类？
2. 流式输出中断后，为什么不能随便 retry？
3. `can_continue` 怎么判断？
4. cancel 和 continue 的区别是什么？
5. 保底持久化在哪里做？
6. SSE keepalive 和 internal task stream 分别解决什么？

追问：

- 如果工具已经写状态，但 SSE 断了，用户刷新后会看到什么？
- 如果用户点停止，后端如何尽量保存已发生的状态？
- 如果 Anthropic streaming tool-use 有 SDK 问题，项目怎么处理？
- `classify_opaque_api_error` 对未知异常的兜底分类是什么？

## Round 10：Guardrails、安全与隐私

1. 当前有哪些 guardrail？
2. prompt injection 是在哪里拦截的？
3. tool input guardrail 和 output guardrail 分别做什么？
4. 为什么 Agent guardrail 不能只看最终输出？
5. 记忆系统如何处理 PII？
6. 当前项目没有认证/限流，如何解释？
7. 如果接入真实预订/支付，你会加哪些 human-in-the-loop 机制？

追问：

- 工具结果里如果包含恶意提示词，当前系统如何防？
- MCP / 外部工具调用在生产里有什么安全风险？
- Guardrail 误杀正常输入怎么办？
- `invalid_budget` 规则如何统一处理 dict/string/number 三种格式的预算？对负数、零值和中文数字字符串的处理逻辑是什么？

## Round 11：Persistence、数据一致性与会话恢复

1. SQLite 存什么？JSON 文件存什么？
2. `StateManager` 和 `SessionStore` 的职责怎么分？
3. message persistence 为什么会 delete 后 append batch？
4. session restore 会恢复哪些东西？哪些不会恢复？
5. deliverables 为什么冻结？
6. Phase 7 归档如何触发 memory episode？

追问：

- 如果服务重启，计划能恢复吗？Trace 能恢复吗？
- `session_id` 为什么有正则约束？
- 如果并发两个请求写同一个 session，当前会怎样？生产要怎么加锁？
- `plan_writer` 增量持久化的 finally 保底逻辑是什么？如果 finally 也失败怎么办？

## Round 12：Frontend 与 SSE 产品体验

1. 前端如何消费 SSE？
2. 为什么手写 fetch stream，而不是全部用 EventSource？
3. 工具卡、internal task 卡、thinking bubble 的区别是什么？
4. 继续生成、重新发送、停止生成分别对应哪些后端能力？
5. MemoryCenter 和 MemoryTracePanel 分别展示什么？

追问：

- 如果 SSE 提前断开，前端怎么判断是 completed、failed 还是 unexpected end？
- 如何避免同一个 internal task 在 chat stream 和 background stream 里重复显示？
- 右侧 Trace 面板的数据从哪里来？

### 12.1 Internal Task 双流架构

1. chat SSE (`/api/chat/{id}`) 和 background internal-task SSE (`/api/internal-tasks/{id}/stream`) 分别承载哪些任务？
2. 为什么 memory extraction 走后台流，而 memory recall 走 chat 流？
3. 前端 `ChatPanel` 如何通过 `task.id` 合并跨流生命周期更新？
4. 如果后台任务在 chat `done` 之后才结束，前端如何确保卡片回写而不是重复渲染？
5. `internal_task` SSE payload 包含哪些字段？

追问：

- 哪些 internal task 进入 chat 流？哪些进入后台流？
- `memory_extraction_gate`、`memory_extraction`、`profile_memory_extraction`、`working_memory_extraction` 四个任务之间的生命周期关系是什么？

### 12.2 ParallelProgress 组件

1. `ParallelProgress` 组件如何消费 `parallel_progress` SSE 事件？
2. `ParallelWorkerStatus.status` 当前覆盖哪些状态？（running / done / failed / retrying）缺失什么？
3. `STATUS_ICON` 映射对 `redispatch` 状态会显示什么？
4. Worker 进度回调 `on_progress(day, kind, payload)` 的 `kind` 有哪些值？

## Round 13：成本、延迟与扩展性

1. 当前成本如何估算？
2. 哪些地方会造成 token 成本高？
3. Phase 5 并行是降低延迟还是增加成本？
4. 现在的 `max_retries: 160` 是否合理？
5. 如何做 model routing？
6. 如果用户量上来，最先遇到的瓶颈是什么？

追问：

- 你会如何建立 per-session / per-user budget？
- 哪些任务应该用小模型，哪些必须用强模型？
- 如何把 expensive eval 和线上请求解耦？
- Phase 5 并行模式下，N 个 Worker 各自运行 LLM loop，总 token 成本与串行相比是增加还是减少？什么场景下值得？

## Round 14：生产化与系统设计

1. 如果要把这个项目上线给真实用户，你第一批改什么？
2. 如何从单用户本地 demo 演进到多用户服务？
3. 如何设计权限、限流、审计和数据隔离？
4. 如何让 eval 进入 CI/CD？
5. 如何做 A/B prompt rollout？
6. 如何监控线上质量下降？

追问：

- 如果第三方工具不稳定，如何做 timeout、circuit breaker 和 fallback？
- 如果用户要求删除个人记忆，如何做 data deletion？
- 如何设计人工审核和高风险操作确认？

## Round 15：成熟面试官可能抓的具体代码风险

1. `fallback_to_serial` 当前实现和文档表述是否一致？
2. Phase 5 unresolved validation error 是否会阻断最终提交？
3. `DayWorkerResult` timeout 是否带结构化 `error_code`？
4. 前端 `ParallelWorkerStatus.status` 是否覆盖后端所有状态？
5. `SessionStats` 是否持久化？
6. `_trip_nights()` 用 `total_days` 是否符合真实住宿晚数？
7. `web_search` 不支持官方站点限定时，如何避免政策类幻觉？
8. `ToolGuardrail` 的 prompt injection 规则是否可能误杀？
9. `pytest --collect-only` 与 README 测试数量不一致说明什么？

## Round 16：Internal Task System 与异步任务编排

1. `InternalTask` 是什么？它和真实 tool call 有什么区别？
2. internal task 的生命周期状态有哪些？（pending / success / warning / error / skipped）
3. `ChunkType.INTERNAL_TASK` 在 SSE 事件流中如何与 `TOOL_RESULT`、`TEXT_DELTA` 互操作？
4. chat 流中的 internal task 和后台流中的 internal task，在 `MessageBubble` 渲染上如何区分？
5. 为什么真实工具的 `TOOL_RESULT` 要先于 soft_judge / memory task 到达前端？
6. `pending_system_notes` 缓冲区解决什么问题？为什么不直接 append 到消息历史？
7. 缓冲区在什么时机 flush？flush 后的消息顺序保证是什么？

追问：

- 如果 memory extraction 的两个路由（profile + working）一个成功一个失败，聚合态 internal task 的 status 是什么？
- 为什么 `last_consumed_user_count` 在提取失败时不前进？
- 同一个 internal task 的多个 chunk 如何在 `useSSE` 中做累计拼接和去重？

## Round 17：记忆召回管线深度（Stage 0-4 全链路）

1. 从用户消息进入到记忆注入 system prompt，全链路经过哪些阶段？
2. Stage 0（硬规则短路）的 6 类词表匹配是什么？P1-P6 优先级如何工作？
3. P1N（否定排除语境）为什么要把 profile signal 从 force_recall 降级为 undecided？
4. Stage 1 LLM gate 的 `intent_type` 有哪些枚举值？`mixed_or_ambiguous` 的保守召回策略是什么？
5. Stage 2 retrieval plan tool 的 `sources` schema 为什么区分 `profile`/`hybrid_history`/`episode_slice` 三类？
6. Stage 3 candidate generation 产出的 `RecallCandidate` 和 `evidence_by_id` sidecar 分别包含什么？
7. Stage 4 reranker 的 hard filter 会丢弃哪些候选？（冲突项、全弱相关候选）
8. recall gate 超时或异常时的 heuristic fallback 逻辑是什么？`recall_skip_source=gate_failure_no_heuristic` 什么情况下出现？

追问：

- Stage 1 gate 为什么不再接收 `memory_summary`？去掉它对召回准确率有什么影响？
- Stage 2 的 `memory_summary` 是谁构建的？在什么时候构建？
- long-term profile 为什么不再固定常驻注入 system prompt？
- Stage 3 超时或异常时，heuristic retrieval plan 如何根据 Stage 0 signals 生成？

## Round 18：Prompt 工程与上下文组装架构

1. `build_phase3_prompt(step)` 的三段拼装公式是什么？
2. `GLOBAL_RED_FLAGS` 包含哪些跨阶段通用禁令？在所有阶段 prompt 末尾注入的机制是什么？
3. Phase 3 每个子步骤的"⚠️ 输出协议"板块包含哪些要素？
4. "工具职责对照表"（10 行"你想做什么 → 应该调用 → 不要调用"映射）解决什么问题？
5. Phase 3 四个子阶段向前开放下一阶段写入工具的"前瞻容错"设计，为什么能防止状态丢失和死循环？
6. 四段式工具描述（功能说明/触发条件/禁止行为/写入后效果）的结构化模板如何引导 LLM 正确选择工具？
7. state repair hints 覆盖 Phase 3 全部 4 个子阶段，每个阶段的检测逻辑是什么？
8. repair hints 的"已消费 hint key"去重机制是什么？每个子阶段允许多少次修复尝试？

追问：

- Phase 5 的 skill-card 如何定位为"路径规划优化问题"？
- Phase 1 的"先查后说"回复纪律和"一次只给 2-3 个选项"约束，在 prompt 里如何强制？
- handoff note 的"开场白协议"要求下一次回复先用 1-2 句自然语言承上启下，为什么要禁止 `[Phase N 启动]` 式 machine checklist 开场？

## Round 19：Config 体系、Feature Flag 与可扩展性

1. `config.yaml` 的加载优先级是什么？（环境变量 > YAML > 代码默认值）
2. Phase 5 并行模式有哪些可配置参数？（`enabled` / `max_workers` / `worker_timeout_seconds` / `fallback_to_serial`）
3. LLM provider 如何按阶段覆写？什么场景下 Phase 1 用 Anthropic、Phase 5 用 OpenAI？
4. memory 系统有哪些 feature flag？（`stage3.semantic.enabled`、evidence 三个 score 权重、`dynamic_budget`、`lexical_expansion`、`source_widening`、`destination_normalization`）
5. 如何通过 config 把 reranker 退回到 rule-only 行为？
6. `${ENV_VAR}` 引用语法在 config.yaml 中如何使用？

追问：

- 如果新增一个 LLM provider（如 DeepSeek），需要改哪些文件？
- 如果新增一个 Phase 5 的 feature flag（如"跨天交通衔接严格模式"），配置层、代码层和测试层分别要做什么？

## Round 20：API 包重构与代码架构演进

1. 后端从 `main.py` 单体到 `api/` 结构化包的拆分动机是什么？
2. `api/orchestration/` 子包按什么维度组织？（agent / chat / memory / session / common）
3. `AgentLoop` 从单体文件拆成 `agent/execution/` + `agent/phase5/` 两个子包，边界是什么？
4. `agent/execution/` 下有哪些模块？（llm_turn / tool_invocation / phase_transition / repair_hints）
5. `api/orchestration/agent/builder.py` 和 `api/orchestration/chat/stream.py` 的职责分别是什么？
6. `api/orchestration/memory/orchestration.py` 的 `MemoryOrchestration` dataclass 为什么用 dataclass 而非 class？它组装了哪些能力？

追问：

- 为什么工具注册（`agent/tools.py`）和 API 编排（`api/orchestration/agent/`）分成两个位置？
- `api/orchestration/common/` 下的 `telemetry_helpers.py` 和 `llm_errors.py` 为什么是"common"而非分别归属 agent 和 chat？

## Round 21：Phase 3 骨架 POI 唯一性设计

1. `set_skeleton_plans` 在写入边界校验 POI 全局唯一性，为什么在 writer 层做而不在 Phase 5 Worker 层做事后去重？
2. 单个 skeleton 内 locked_pois 和 candidate_pois 之间的 POI 不可重复，跨 skeleton 是否也校验？
3. 如果 LLM 传入的 skeleton 数据中 `days` 为空数组或缺少 `area_cluster`/`locked_pois`/`candidate_pois` 三必填字段，writer 如何拒绝？
4. reader-side 过滤防御是什么？当 `skeleton_plans` 被外部数据污染时，`infer_phase3_step_from_state` 如何防御？

## 终面级综合题

1. 你会如何把 Travel Agent Pro 改造成一个可靠的生产 Agent 平台？
2. 如果面试官让你现场画架构图，你会画哪些模块和数据流？
3. 如果只给你两周，你会优先补 eval、guardrail、memory 还是 tool hardening？
4. 这个项目最失败的一次设计是什么？你如何发现和修正？
5. 你从这个项目里学到的 Agent 工程第一性原理是什么？
6. 如果面试官问"你这个项目的核心护城河是什么"，你怎么回答？
7. 假设你现在要招聘一个 Agent 工程师加入这个项目，你会出什么面试题？
