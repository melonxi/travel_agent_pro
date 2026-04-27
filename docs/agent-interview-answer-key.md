# Travel Agent Pro Agent 面试参考答案

> 用法：对照 `docs/agent-interview-question-bank.md` 使用。答案不是背诵稿，而是面试中可组织表达的技术要点。

## 0. 开场项目介绍

### Q：请你用 2 分钟介绍一下 Travel Agent Pro。

推荐回答：

Travel Agent Pro 是一个全栈 AI 旅行规划 Agent。前端用 React + Vite，通过 SSE 展示模型文本、工具调用、内部任务、记忆召回和并行规划进度；后端用 FastAPI，自研 Agent Loop，不依赖 LangChain。核心规划路径是 Phase 1/3/5/7：先把模糊意图收敛成目的地，再做旅行画像、候选池、骨架方案和交通住宿锁定，然后把骨架展开为每日行程，最后生成出发前 checklist 和 markdown 交付物。

我的重点不是做一个聊天壳，而是实现 Agent 应用里的关键工程问题：状态机驱动的阶段推进、工具调用协议、读写工具隔离、上下文压缩、v3 记忆系统、Phase 5 Orchestrator-Workers 并行生成、质量门控、OpenTelemetry trace 和 golden eval。

代码入口：

- `backend/main.py::create_app`
- `backend/agent/loop.py::AgentLoop`
- `backend/phase/router.py::PhaseRouter`
- `frontend/src/components/ChatPanel.tsx`

### Q：为什么它算 Agent，而不是 ChatGPT wrapper？

推荐回答：

它不只是把用户输入转发给模型。模型在一个受控循环里观察当前状态、选择工具、写入结构化旅行状态，再由 deterministic router 推进阶段。系统有工具权限边界、状态写入工具、backtrack、memory recall、quality gate、trace 和 eval。LLM 负责不确定的推理和工具选择，代码负责状态一致性、阶段边界和安全约束。

## 1. Agent Loop 与 Orchestration

### Q：`AgentLoop.run()` 的核心循环是什么？

推荐回答：

核心是 think-act-observe：

1. 构造当前 phase 可用工具。
2. `run_llm_turn()` 调 LLM，流式收集文本和 tool calls。
3. 如果没有 tool calls，检查是否需要 state repair hint；否则结束。
4. 如果有 tool calls，先记录 assistant tool_calls message。
5. `_execute_tool_batch()` 执行工具，把 tool result 追加回消息历史。
6. `detect_phase_transition()` 检查状态变化和 phase 推进。
7. 如果 phase 或 phase3 step 改变，重建 system message 和工具列表。
8. 循环直到完成、取消或达到 max iteration。

代码：`backend/agent/loop.py::AgentLoop.run()`。

### Q：如果 LLM 只输出文本，不调用写状态工具怎么办？

推荐回答：

项目有 state repair hint。比如 Phase 3 如果模型写了骨架方案的自然语言，但没有调用 `set_skeleton_plans`，`AgentLoop` 会通过 `build_phase3_state_repair_message()` 识别这种“文本像状态但未写入”的情况，追加 SYSTEM 修复提示，要求模型调用正确的写状态工具。Phase 5 也有类似的 daily plan repair。

代码：

- `backend/agent/execution/repair_hints.py`
- `backend/agent/loop.py::AgentLoop.run`

### Q：为什么不用 LangChain？

推荐回答：

这个项目的价值点在 Agent 生命周期控制，而不是快速搭 demo。我需要掌控：

- tool call message 协议；
- phase transition；
- write tool 后的状态一致性；
- SSE chunk 结构；
- memory recall 和 extraction 时机；
- quality gate 和 soft judge hooks；
- Phase 5 并行 worker handoff；
- trace/stats 数据结构。

这些和业务状态强耦合，用框架反而会隐藏关键控制点。选择自研 loop 是为了可解释、可测试、可调试。

### Q：Agent Loop 最近从单体拆成 `agent/execution/` + `agent/phase5/`，拆分动机和边界是什么？

推荐回答：

原始 `agent/loop.py` 承载了主循环 + LLM 调用 + 工具执行 + 阶段转换 + 状态修复 + Phase 5 并行分流等全部逻辑，文件膨胀且职责混杂。拆分动机：
- `agent/execution/`：把主循环中可独立测试的执行单元抽出来——`llm_turn.py`（单轮 LLM 调用）、`tool_invocation.py`（工具批量执行 + 搜索历史追踪）、`phase_transition.py`（阶段变化检测 + 质量门控）、`repair_hints.py`（状态修复提示）。
- `agent/phase5/`：Phase 5 并行模式足够复杂，独立成子包——`orchestrator.py`（调度器）、`day_worker.py`（Worker 引擎）、`worker_prompt.py`（prompt 模板）、`candidate_store.py`（artifact 存储）、`parallel.py`（入口守卫 + handoff）。
- `agent/loop.py` 缩身为纯骨架：think-act-observe 框架 + 取消检查点 + hook 编排。

边界是：loop 只管"什么时候调用什么"，execution 和 phase5 只管"具体怎么做"。

## 2. Phase 状态机

### Q：为什么是 Phase 1/3/5/7？

推荐回答：

这是项目演进后的生产主路径，不是数学编号。保留奇数 phase 是为了兼容历史阶段和文档语义。当前主路径是：

- Phase 1：灵感和目的地收敛。
- Phase 3：框架规划，拆成 `brief/candidate/skeleton/lock`。
- Phase 5：每日行程组装。
- Phase 7：出发前 checklist 和交付物冻结。

核心不是编号，而是每个阶段都有明确状态产物和完成 gate。

### Q：`PhaseRouter.infer_phase()` 怎么判断？

推荐回答：

它基于 `TravelPlanState` 完整度：

- 没有 `destination`，停在 Phase 1。
- 缺 dates、selected skeleton 或 accommodation，停在 Phase 3。
- skeleton 天数与日期总天数不匹配，停在 Phase 3。
- daily plans 数量不足，进入或停在 Phase 5。
- daily plans 完整后进入 Phase 7。

代码：`backend/phase/router.py::PhaseRouter.infer_phase()`。

### Q：Backtrack 怎么做？

推荐回答：

Backtrack 是显式工具和 API 都支持的上游重决策机制。`request_backtrack(to_phase, reason)` 会调用 plan writer 清理下游状态，并记录 `BacktrackEvent`。例如从 Phase 5 回 Phase 3 会清掉 daily plans 和交付物，但保留适合保留的偏好/约束。回退后 AgentLoop 会重建消息上下文，避免旧阶段信息污染新阶段。

代码：

- `backend/tools/plan_tools/backtrack.py`
- `backend/state/models.py::TravelPlanState.clear_downstream`
- `backend/phase/router.py::prepare_backtrack`

### Q：Phase 3 子步骤切换（brief→candidate→skeleton→lock）会触发什么系统行为？

推荐回答：

子步骤切换触发 system message 重建（通过 `ContextManager`），确保 runtime context 随子阶段即时刷新——例如从 brief 进入 candidate 后，system prompt 切换为 candidate 的角色和工具列表，runtime context 注入 trip_brief 当前值。但与 Phase 之间的 handoff 不同，子步骤切换**不注入 handoff note**——因为 Phase 3 四个子步骤是渐进收敛的连续过程，LLM 仍保留历史对话上下文即可衔接，不需要显式交接。

## 3. Tool Calling 与工具系统

### Q：工具系统的抽象是什么？

推荐回答：

工具是普通 async function，用 `@tool` 装饰器声明 name、description、phases、parameters、side_effect 和 human_label。`ToolEngine` 注册工具，根据当前 phase 和 Phase 3 子步骤过滤给 LLM；执行时先做 required 参数预校验，再捕获 `ToolError`，把 `error_code` 和 `suggestion` 反馈给 LLM。

代码：

- `backend/tools/base.py::tool`
- `backend/tools/base.py::ToolDef`
- `backend/tools/engine.py::ToolEngine`

### Q：read tool 和 write tool 为什么要区分？

推荐回答：

读工具如搜索、POI、路线查询没有状态副作用，可以并行执行降低延迟。写工具会修改 `TravelPlanState`，必须顺序执行，否则多个写入可能产生竞态或阶段判断混乱。`ToolEngine.execute_batch()` 会把连续 read calls 并发 gather，write calls 顺序执行，并保持原始返回顺序。

### Q：如何防止重复搜索和工具循环？

推荐回答：

主 Agent Loop 里有 `SearchHistoryTracker`，对 `web_search`、小红书搜索、quick search 的同 query 做滑动窗口统计，同一归一化 query 超过阈值会跳过并返回 `REDUNDANT_SEARCH`。Phase 5 Day Worker 里还有基于工具参数 fingerprint 的 `_MAX_SAME_QUERY` 和基于 POI 补救链的 `_MAX_POI_RECOVERY`，超限后注入 forced emit prompt，要求基于已有信息保守提交。

代码：

- `backend/agent/execution/tool_invocation.py::SearchHistoryTracker`
- `backend/agent/phase5/day_worker.py::_MAX_SAME_QUERY`

## 4. Context、Prompt 和 Token Budget

### Q：system message 由哪些层组成？

推荐回答：

`ContextManager.build_system_message()` 组合：

1. `soul.md` 角色和全局行为约束。
2. 当前时间上下文。
3. 硬工具规则。
4. 当前 phase prompt。
5. runtime context，也就是当前 plan 状态、可用工具、阶段目标。
6. memory context。

memory context 明确标注为用户记忆数据，不是系统指令，避免把记忆或检索内容当成 prompt authority。

代码：`backend/context/manager.py::ContextManager.build_system_message()`。

### Q：compaction 怎么做？

推荐回答：

项目有两层压缩：

- before LLM call：按 context window 预算，优先压缩工具结果，尤其是 web search 和小红书这类长内容。
- phase transition：不依赖 LLM 总结历史，而用 deterministic handoff note 交接当前阶段已完成事项、下一阶段目标和禁止重复事项。

当前 token 估算比较粗糙，主要是 `len//3`；生产化应该接入 provider tokenizer、记录每次 prompt tokens，并建立 per-session budget。

代码：

- `backend/agent/compaction.py`
- `backend/context/manager.py::compress_for_transition`

## 5. Memory System

### Q：v3 memory 的层级是什么？

推荐回答：

有四类：

- profile：跨行程稳定偏好、约束、拒绝项。
- working memory：当前 session/trip 的短期提醒，不参与历史 recall。
- episodes：完整归档的历史旅行。
- episode slices：从历史旅行切出来的可召回片段。

当前旅行事实不从 memory 回填，而由 `TravelPlanState` 权威提供。这能避免“记忆里的旧事实”和当前状态冲突。

代码：

- `backend/memory/v3_models.py`
- `backend/memory/v3_store.py`
- `backend/memory/manager.py::MemoryManager.generate_context`

### Q：每轮什么时候 recall，什么时候 extraction？

推荐回答：

每轮用户消息进入后，先同步做 memory recall，把本轮需要的记忆注入 system prompt；同时提交一个后台 memory extraction snapshot，本轮回答不等它完成。extraction 通过 session 级 latest-wins scheduler 合并任务，避免用户连续发消息时后台任务堆积。

代码：

- recall: `backend/api/orchestration/memory/turn.py::build_memory_context_for_turn`
- extraction: `backend/api/orchestration/memory/tasks.py::create_memory_task_runtime`

### Q：如何避免当前事实问题误触发历史记忆？

推荐回答：

Stage 0 规则会识别 fact-scope 和 fact-field。如果用户问“这次预算多少”“当前住哪里”，这属于当前 plan fact，应 skip recall，直接从 `TravelPlanState` 回答。只有历史、偏好、推荐、风格类问题才进入 profile / episode slice recall。

代码：

- `backend/memory/recall_gate.py::apply_recall_short_circuit`
- `backend/api/orchestration/memory/orchestration.py::_decide_memory_recall`

### 5.1 Stage 3 语义召回与 Hybrid Recall

#### Q：Stage 3 的默认 embedding 模型和 runtime 是什么？

推荐回答：

默认使用 FastEmbed + `BAAI/bge-small-zh-v1.5`，走 ONNX Runtime CPU，`local_files_only=True`。embedding cache 位于 `backend/data/embedding_cache`，最大 10000 条/64MB。选择这个组合是避免生产环境运行时下载模型，减少首次请求延迟和网络依赖。

代码：`backend/config.py::Stage3SemanticConfig`。

#### Q：symbolic lane、semantic lane、lexical lane 分别做什么？

推荐回答：

- symbolic lane：基于关键词/domain/bucket 的结构化检索，确定性最高，是 baseline。
- semantic lane：用 embedding 向量相似度做语义召回，捕获同义词和隐含关联，默认启用。
- lexical lane：基于文本 token 重叠的词汇级检索，当前在 feature flag 后面（默认关闭）。

三者通过 RRF（Reciprocal Rank Fusion）融合，lane weights 为 symbolic=1.0、lexical=0.6、semantic=0.8。

代码：`backend/config.py::Stage3FusionConfig`。

#### Q：`evidence_by_id` sidecar 是什么？

推荐回答：

Stage 3 在产出 `RecallCandidate[]` 的同时，会产出一个 `evidence_by_id` 字典，key 是 candidate item_id，value 是 `RetrievalEvidence`（包含 fused_score、lexical_score、semantic_score）。这个 sidecar 从 Stage 3 透传到 Stage 4 reranker，让 reranker 在 rule 评分之外叠加 evidence 信号。

代码：`backend/memory/recall_stage3_models.py::RetrievalEvidence`。

### 5.2 Stage 4 Reranker 深度

#### Q：Stage 4 reranker 的核心公式是什么？

推荐回答：

```
source_score = rule_score + evidence_score
```

然后分 source（profile / slice）做 min-max 归一化，再加 source prior 得到 `final_score`，最后按 source budget 选择 top candidates。

代码：`backend/memory/recall_reranker.py::choose_reranker_path`。

#### Q：rule signals 包括哪些维度？

推荐回答：

bucket prior（constraints > rejections > stable_preferences > preference_hypotheses）、domain/keyword overlap（和 retrieval plan 中请求的 domain/keyword 做精确匹配）、destination 匹配类型（exact/alias/parent_child/region_weak/none 五级）、recency decay（基于 180 天半衰期）、applicability（通用声明降权）、conflict penalty（用户当前消息中的否定词与记忆内容冲突时扣分）。

#### Q：evidence lane 的三个 score 权重默认值是什么？

推荐回答：

`lane_fused_weight=0.25`、`semantic_score_weight=0.15`、`lexical_score_weight=0.08`。`symbolic_hit_weight`、`lexical_hit_weight`、`semantic_hit_weight`、`destination_match_type_weight` 当前保持为 0。生产端可以把三个 score 权重写回 0 回到第零期 rule-only 行为。

#### Q：source-aware normalization 怎么做？

推荐回答：

profile 和 slice 候选人分池计算 `source_score`，然后分别在各自池内做 min-max 归一化。这样 profile 不会因为 rule 信号天然强于 slice 而垄断所有名额。归一化后各自加 source prior（profile 默认 0.84，slice 默认 0.84）得到 `final_score`，最后走 `_select_candidates` 按预算选择。

#### Q：如何把 reranker 退回到 rule-only 行为？

推荐回答：

在 `config.yaml` 中把 `evidence` 的三个 score 权重（`lane_fused_weight`、`semantic_score_weight`、`lexical_score_weight`）都写回 0，reranker 就退化为纯 rule signal 的加权排序，不再叠加 Stage 3 的 evidence 分数。

#### Q：reranker-only eval 为什么能排除 Stage 0/1/2 抖动？

推荐回答：

reranker-only eval 用固定的 `RecallRetrievalPlan` 和固定的 `RecallCandidate` 集合作为输入，跳过 Stage 0 规则/Stage 1 LLM gate/Stage 2 retrieval plan/Stage 3 candidate generation 的全部不确定性，只验证 Stage 4 的排序逻辑是否正确。这样可以单独评估 reranker 的 bucket prior、destination matching、conflict penalty 和 evidence 叠加行为。

代码：`backend/evals/reranker.py`。

### 5.3 Memory Extraction 与 Episode 归档

#### Q：profile extraction 为什么要在持久化前做 domain/key 规范化？

推荐回答：

因为 LLM 产出的 domain/key 可能不一致（如 `accommodation` vs `住宿偏好`），规范化把高价值 domain/key 映射到系统枚举值，确保后续 recall 时能正确匹配。同时把 `applicability`、`recall_hints`、`source_refs` 一起写成 recall-ready metadata。

#### Q：Phase 7 结束后 episode 归档和 slice 派生流程是什么？

推荐回答：

Phase 7 完成后，系统调用 `append_archived_trip_episode_once` 把本次行程写入 `ArchivedTripEpisode`，然后同步派生 6 类 `EpisodeSlice`：`itinerary_pattern`（行程模式）、`stay_choice`（住宿选择）、`transport_choice`（交通选择）、`budget_signal`（预算信号）、`rejected_option`（被拒绝选项）、`pitfall`（踩坑记录）。slice 写入按 id 幂等，重复归档不会产生重复切片。

代码：`backend/api/orchestration/memory/episodes.py`。

#### Q：route-aware gate 如何决定触发哪个 extractor？

推荐回答：

memory extraction gate 先判断本轮是否需要提取。如果需要，根据对话内容路由：
- 有 profile-relevant 信号（偏好/约束/拒绝）→ 触发 `extract_profile_memory`
- 有 working-memory-relevant 信号（当前行程提醒）→ 触发 `extract_working_memory`
- 两者都有 → 两个路由都触发
- 都没有 → 跳过

任一路由失败，另一路已成功写入的不回滚；聚合态 `memory_extraction` internal task 的 `status` 使用 `warning`，并在结果/消息中表达部分失败原因，而不是把 `partial_failure` 当作 `InternalTask.status`。

## 6. Evaluation、Testing 与质量保障

### Q：项目有哪些测试层？

推荐回答：

有多层：

- 单元测试：AgentLoop、ToolEngine、PhaseRouter、memory、validator、provider 等。
- golden eval：`backend/evals/golden_cases/`，验证 phase、state、tool call、文本等断言。
- reranker-only eval：固定候选和检索计划，专门验证 memory reranker。
- Playwright E2E：验证前端交互、等待态、重试/继续体验。

本次 collect 到 `1638 tests`，但没有跑全量测试，所以面试时应说“当前可收集测试数量”，不要说“全部通过”。

### Q：如何评估 Agent 是否真的变好了？

推荐回答：

只看最终答案不够，要看 trajectory。Agent 可能最终答对，但中间调用了错误或危险工具；也可能工具选对但最终文案不好。所以我会结合：

- final answer quality；
- state 是否达到目标；
- tool selection accuracy；
- tool argument correctness；
- trace-level failure localization；
- cost/latency；
- memory recall hit/false recall/false skip。

这也和 OpenAI trace grading、agent evals 的方向一致：对 workflow-level 和 trace-level 行为做评估，而不是只看黑盒输出。

### Q：Quality Gate 和 Soft Judge 怎么工作？

推荐回答：

Hook 系统把质量逻辑从核心 loop 解耦。`after_tool_call` 做增量硬约束验证，比如预算、日期、时间冲突。`after_tool_result` 对 `save_day_plan`、`replace_all_day_plans`、`generate_summary` 等触发 soft judge，给 pace、geography、coherence、personalization 打分。`before_phase_transition` 做 quality gate，低于阈值时注入修改建议；judge 失败时不阻断主流程。

代码：`backend/api/orchestration/agent/hooks.py`。

## 7. Observability、Trace 与 Debugging

### Q：为什么传统日志不够？

推荐回答：

Agent 的失败通常不是 HTTP 500，而是轨迹错误：选错工具、重复搜索、工具参数错、检索到无关记忆、上下文太长、阶段提前推进。传统 request-response 日志只看到请求和最终回答，看不到中间决策。所以项目记录 LLM calls、tool calls、memory hits、state changes、judge scores、validation errors，并用 TraceViewer 按 iteration 和 significance 展示。

代码：

- `backend/telemetry/stats.py::SessionStats`
- `backend/api/trace.py::build_trace`
- `frontend/src/components/TraceViewer.tsx`

### Q：进程重启后旧 trace 还能看吗？

推荐回答：

计划和消息能从 SQLite/JSON 恢复，但当前 `SessionStats` 是进程内的，restore session 会创建新的空 stats；`/api/sessions/{id}/trace` 只查内存 sessions。因此重启后旧 trace 不完整或可能 404。这是生产化要补的点：把 stats/trace events 持久化到数据库或专门的 trace backend。

代码：

- `backend/api/orchestration/session/persistence.py::restore_session`
- `backend/api/routes/artifact_routes.py::get_session_trace`

## 8. Phase 5 并行 Orchestrator-Workers

### Q：Phase 5 为什么适合并行？

推荐回答：

把已选 skeleton 展开为每日行程时，每一天有相对独立的 POI 查询、路线安排和时间表生成，天然可拆成 day-level tasks。并行能降低端到端等待时间。但跨天仍有全局约束，比如 POI 重复、预算、交通衔接、节奏一致性，所以需要 Orchestrator 做全局验证。

代码：

- `backend/agent/phase5/parallel.py`
- `backend/agent/phase5/orchestrator.py`
- `backend/agent/phase5/day_worker.py`

### Q：Orchestrator 是 LLM Agent 吗？

推荐回答：

不是。Orchestrator 是纯 Python 调度器。它负责：

- 找到 selected skeleton；
- split 成 DayTask；
- 注入 forbidden_pois、mobility_envelope、date_role、day_budget 等约束；
- 并发运行 Day Workers；
- 收集 artifact candidates；
- 做全局验证；
- 对 error severity issue re-dispatch；
- 把 final dayplans handoff 给 AgentLoop。

真正调用 LLM 的是 Day Worker。

### Q：Worker 为什么不能直接写 `TravelPlanState`？

推荐回答：

因为多个 Worker 并发写共享状态会带来一致性问题，也会绕过主 AgentLoop 的工具事件、hook、phase transition 和 telemetry。当前设计是 Worker 只写 run-scoped candidate artifact，正式写入由 AgentLoop 构造内部 `replace_all_day_plans` 工具调用，走标准写工具路径。

### Q：当前 Phase 5 有哪些已知风险？

推荐回答：

有三个要主动说：

1. `fallback_to_serial` 当前高失败率时只是 return，让上层 warning，并没有同轮真正串行生成。
2. re-dispatch 后如果仍有 error，当前只是 log unresolved，仍可能设置 `final_dayplans`。
3. Worker timeout/generic exception 没有设置结构化 `error_code`，和文档里的 `TIMEOUT/LLM_ERROR` 不完全一致。

这些都属于下一阶段 hardening。

### 8.1 Day Worker 内部机制

#### Q：Day Worker 的 mini agent loop 和主 AgentLoop 有什么区别？

推荐回答：

核心循环相同（think-act-observe），但有关键差异：
- Worker 无用户交互——没有 SSE streaming、没有 cancel/continue、不处理用户消息。
- Worker 只暴露只读工具和 `submit_day_plan_candidate` 候选提交工具，不能直接写 `TravelPlanState`。
- Worker 有四重收敛保障（重复查询抑制、补救链阈值、后半程强制收口、JSON 修复回合），比主 AgentLoop 的收敛逻辑更激进。
- Worker 的系统 prompt 是 shared prefix + day suffix 的拼接，而非 ContextManager 完整装配。

代码：`backend/agent/phase5/day_worker.py::run_day_worker`。

#### Q：Worker 的 `_WORKER_ROLE` 为什么不再从 `soul.md` 加载？

推荐回答：

`soul.md` 包含"一次只问一个问题""提供 2-3 个选项让用户选择"等面向用户交互的行为指引，对 Worker 不适用——Worker 没有用户交互通道。改为 `_WORKER_ROLE` 模块常量内联 Worker 专属身份，包含并发语境、无用户交互声明、完成优于完美、优先级层次、交付唯一路径。

代码：`backend/agent/phase5/worker_prompt.py::_WORKER_ROLE`。

#### Q：shared prefix 的 KV-Cache 命中率目标是多少？通过哪些手段保证？

推荐回答：

目标命中率约 93.75%（Manus pattern 参考值）。保障手段：
1. `build_shared_prefix` 对 `trip_brief` 做白名单过滤（只保留 goal/pace/departure_city/style/must_do/avoid），排除 `dates`/`total_days`（这些每天相同但由 plan.dates 权威提供，不应在 prefix 中膨胀）。
2. preferences 按 key 字典序排序，保证多个 Worker 的 prefix 字节级一致。
3. soft 约束不放 shared prefix，走 `day_constraints` 路径注入 day suffix——因为 soft 约束每天可能不同。
4. day suffix（含"第 N 天"标识）从 system message 移到 user message，让所有 Worker 的 system message 完全一致。

代码：`backend/agent/phase5/worker_prompt.py::build_shared_prefix`。

### 8.2 Day Worker 四重收敛保障

#### Q：四重收敛保障分别是什么？

推荐回答：

1. **重复查询抑制**：按工具调用参数生成 fingerprint（例如 `web_search:{query}` / `get_poi_info:{q}`），`_MAX_SAME_QUERY=2` 表示同一 fingerprint 第 3 次出现时触发 forced emit，而不是在第 2 次直接跳过。
2. **补救链阈值**：连续补救轮次上限（`_MAX_POI_RECOVERY=3`），同一补救 key 第 4 次出现时强制保守落地。
3. **后半程强制收口**：迭代过半后 `_LATE_EMIT_PROMPT` 注入，允许最多再 2 个工具调用后必须提交。
4. **JSON 修复回合**：输出 JSON 解析失败时，`_JSON_REPAIR_PROMPT` 引导模型复用对话历史调用 `submit_day_plan_candidate`；修复失败后保守落地（返回当前已有结果，不无限重试）。

代码：`backend/agent/phase5/day_worker.py`。

#### Q：`_FORCED_EMIT_PROMPT` 和 `_LATE_EMIT_PROMPT` 分别在什么阈值触发？

推荐回答：

- `_FORCED_EMIT_PROMPT`：同一工具 fingerprint 超过 2 次或同一补救 key 超过 3 次时触发。代码层面会向对话注入强制收口 system prompt 并重新进入下一轮 LLM，而不是在执行器里硬性封禁所有工具调用；prompt 要求基于已有信息立即提交。兜底条款：禁止 `0,0` 假坐标、缺信息标注 notes、缺票价写 0。
- `_LATE_EMIT_PROMPT`：迭代超过 `max_iterations * 0.6` 时触发。允许最多再调 1-2 个工具补齐核心信息后提交，不强制禁止工具调用。

### 8.3 Worker 约束注入与 DayTask

#### Q：locked_pois / candidate_pois / forbidden_pois 三级约束用什么图标区分？

推荐回答：

- ⛔ `locked_pois`：硬约束，违反 = DayPlan 无效，Orchestrator 会拒绝。
- ✅ `candidate_pois`：推荐但非强制，Worker 可选择性纳入。
- 🚫 `forbidden_pois`：跨天重复 POI，由 Orchestrator 注入，违反 = 跨天 POI 重复触发重新分配。

代码中通过 `_build_constraint_block` 渲染为中文 prompt 硬约束块。

#### Q：`arrival_time` / `departure_time` 从哪里提取？

推荐回答：

从 `selected_transport` 中提取：`_extract_transport_time` 对 outbound 方向取最后一段的 `arrival_time`（到达目的地时间），对 return 方向取第一段的 `departure_time`（最早离开时间）。对单天旅行（`arrival_departure_day`），两个时间都注入同一天的 DayTask。无时间时有兜底缓冲文案。

代码：`backend/agent/phase5/orchestrator.py::_extract_transport_time`。

#### Q：`repair_hints` 在 prompt 中为什么强调"本轮必须逐一解决"？

推荐回答：

repair_hints 是上一轮全局验证失败后注入的修正指示（如"第 3 天和第 5 天重复了浅草寺，请从第 5 天移除"）。如果 Worker 忽略 repair_hints，re-dispatch 就会浪费。所以用加粗聚焦 + "本轮必须逐一解决"指令强调优先级，确保 Worker 优先处理 re-dispatch 修正需求。

### 8.4 Worker 错误类别与诊断

#### Q：Worker 失败时输出哪些结构化错误码？

推荐回答：

当前源码只有部分失败会带结构化 `error_code`：
- `REPEATED_QUERY_LOOP`：重复查询死循环被抑制
- `RECOVERY_CHAIN_EXHAUSTED`：补救链耗尽仍无法恢复
- `NEEDS_PHASE3_REPLAN`：locked_pois 全部不可行，需回退 Phase 3 重调骨架

需要注意：`TimeoutError` 和 generic exception 分支当前只写 `error` 文本，不设置 `TIMEOUT` / `LLM_ERROR` 这类结构化 `error_code`。`JSON_EMIT_FAILED` 在文档语义上可作为诊断类别，但当前 `run_day_worker` 的耗尽迭代路径并不会稳定产出这个 error_code。

代码：`backend/agent/phase5/day_worker.py`。

### 8.5 Candidate Store 与 Handoff 机制

#### Q：`Phase5CandidateStore` 的 artifact 存储路径格式是什么？

推荐回答：

`{phase5.parallel.artifact_root}/{session_id}/{run_id}/day_{N}_attempt_{M}.json`。run-scoped 意味着每次 run 有独立 artifact 空间，避免跨 run 污染。`attempt_{M}` 支持 re-dispatch 多轮尝试。

代码：`backend/agent/phase5/candidate_store.py`。

#### Q：AgentLoop 如何接收 Orchestrator 的 handoff？

推荐回答：

Orchestrator 完成后，通过 `on_handoff` 回调传入 `Phase5ParallelHandoff(dayplans, issues)`。AgentLoop 收到 handoff 后，构造一个内部 `replace_all_day_plans` 工具调用（不走 LLM 决策），走标准 `_execute_tool_batch → detect_phase_transition` 链路。这样并行结果和串行结果走完全相同的状态写入、hook、telemetry 和阶段推进路径。

代码：`backend/agent/phase5/parallel.py::Phase5ParallelHandoff`。

#### Q：为什么 handoff 要走标准写工具路径，而不是直接写 state manager？

推荐回答：

直接写 state manager 会绕过以下关键路径：
- `after_tool_call` hooks（validator、soft judge）
- `detect_phase_transition`（自动推进到 Phase 7）
- telemetry 记录（state_changes 追踪）
- plan writer 工具结果之后由 chat stream 统一做增量持久化（落盘 plan.json 并同步 session meta）

走标准 `replace_all_day_plans` 路径可以让并行模式复用所有这些已有的工程保障，保持串行和并行模式的路径等价性。

### 8.6 Shared Prefix 与 KV-Cache 策略

#### Q："Manus pattern"在 Phase 5 并行场景下指什么？

推荐回答：

Manus/Claude Code 在 fork 多个子 agent 时，所有子 agent 共享相同的 system prompt，只通过 user message 传递差异化任务。这样 LLM 提供商的 KV-Cache 可以复用 system prompt 的注意力计算结果，N 个 Worker 的 system prompt 仅计算一次。本项目中 shared prefix 就是这个思想的实现——所有 Day Worker 的 system message 完全一致，只有 user message（含 day suffix）不同。

---

## 9. Reliability、错误恢复与用户体验

### Q：流式输出中断后为什么不能随便 retry？

推荐回答：

如果 LLM 已经 yield 了一部分文本或工具调用，自动 retry 可能造成重复输出或重复写状态。所以 provider 层只在还没 yield 数据时重试。出错后 `run_agent_stream()` 根据 `IterationProgress` 判断是否 `can_continue`，前端展示继续生成或重新发送。

代码：

- `backend/llm/openai_provider.py`
- `backend/llm/anthropic_provider.py`
- `backend/api/orchestration/chat/stream.py::run_agent_stream`
- `frontend/src/components/ChatPanel.tsx::createErrorFeedback`

### Q：cancel 和 continue 有什么区别？

推荐回答：

cancel 是用户主动停止当前 run，后端 cancel_event 会在 LLM 前、streaming 中、工具执行前检查，并尽量做保底持久化。continue 是在可继续的错误中断后，基于保存的 continuation context 继续生成，而不是从头重新发上一条消息。

代码：

- `backend/api/routes/chat_routes.py::continue_chat`
- `backend/api/orchestration/chat/finalization.py::persist_run_safely`

### Q：`classify_opaque_api_error` 对未知异常的兜底分类是什么？

推荐回答：

对裸 APIError（非 SDK 标准错误类），通过状态码和关键词做启发式分类。无法匹配任何已知模式时，当前源码保守归类为 `LLM_TRANSIENT_ERROR`，但 `retryable=False`。也就是说，它保留“可能是瞬态”的错误码语义，同时避免对未知 opaque error 自动重试造成重复成本或副作用。

代码：`backend/llm/errors.py::classify_opaque_api_error`，调用侧在 `backend/llm/openai_provider.py`、`backend/llm/anthropic_provider.py`。

## 10. Guardrails、安全与隐私

### Q：当前有哪些 guardrail？

推荐回答：

主要是工具级 guardrail：

- prompt injection pattern；
- 字符串长度；
- 过去日期；
- 空 location；
- 非法预算；
- 工具输出空结果、价格异常、缺关键字段 warning。

它不是全站安全体系，而是围绕工具调用输入/输出做确定性校验。代码在 `backend/harness/guardrail.py::ToolGuardrail`。

### Q：为什么 guardrail 不能只看最终输出？

推荐回答：

因为 Agent 的风险发生在轨迹中间。模型可能调用了错误工具、把恶意检索内容当系统指令、写了错误状态，最终再用自然语言包装得很合理。多步 tool-calling agent 的安全面在 intermediate trace，所以 guardrail、eval 和 trace 都要覆盖工具输入、工具输出和状态变化。

这与 TraceSafe 等研究方向一致：tool-calling 轨迹里的结构化数据能力和中间步骤安全同样关键。

### Q：记忆系统如何处理 PII？

推荐回答：

`MemoryPolicy` 会丢弃 payment、membership 等 denied domains，识别护照号、身份证、长数字、手机号/邮箱样式等 PII，并做 redaction 或 drop。低置信度长期偏好会进入 pending，而不是直接 active。

代码：`backend/memory/policy.py::MemoryPolicy`。

### Q：`invalid_budget` 规则如何统一处理 dict/string/number 三种格式？

推荐回答：

`_extract_numeric_budget` 方法：
- number：直接校验非正数/零值。
- string：先尝试解析为数值（`"10000"`→10000）；无法 `float()` 的正向字符串会返回 `None`，不会被 `invalid_budget` 直接拒绝。以 `"-"` 开头的不可解析字符串会被视为负数信号并拒绝。
- dict：递归处理 `total` 字段，支持 `{"total": "500"}` / `{"total": 10000}` 等变体。
这条 guardrail 的边界是“拒绝明确的负数或零值”，不是完整的预算解析器。例如 `"1万"` 在这里不会被拒绝；如果后续业务需要支持或拒绝中文金额，应放到 `update_trip_basics` 的参数解析/校验层处理。扩展到 dict 后覆盖了 `update_trip_basics(budget={"total": "-500"})` 等边缘情况。

代码：`backend/harness/guardrail.py::_extract_numeric_budget`。

## 11. Persistence、数据一致性与会话恢复

### Q：SQLite 存什么？JSON 文件存什么？

推荐回答：

SQLite 存 session metadata、messages、plan snapshots、archives。JSON 文件系统存当前 `plan.json`、snapshots、tool_results、deliverables，以及用户 memory 文件。这样本地开发简单，plan 快照和 deliverables 也方便人工查看。

代码：

- `backend/storage/database.py`
- `backend/state/manager.py`
- `backend/memory/v3_store.py`

### Q：`plan_writer` 增量持久化的 finally 保底逻辑是什么？

推荐回答：

plan writer 函数本身只负责修改内存中的 `TravelPlanState`。在 chat SSE 编排层，工具结果返回且属于 plan writer / summary 类工具后，会立即 `state_mgr.save(plan)` 并同步更新 session meta（phase/title），防止 SSE 中断导致已写状态丢失。同时，`run_agent_stream()` 的 finally 块会做保底持久化：保存 plan 和 messages，并把仍在 running 状态的 run 标记为 cancelled。这样可以确保即使用户刷新或连接断开，已写入的状态和消息不会丢失，三源（plan/messages/session meta）保持一致。

代码：`backend/api/orchestration/chat/finalization.py::persist_run_safely`。

### Q：服务重启后能恢复什么？

推荐回答：

能恢复 plan 和 messages，因为它们落在 JSON/SQLite。不能完整恢复进程内 stats、trace、pending system notes、reflection cache 等运行时对象。`restore_session()` 会重建 agent 和空 `SessionStats`。

## 12. Frontend 与 SSE 产品体验

### Q：为什么手写 fetch stream，而不是全部用 EventSource？

推荐回答：

聊天主请求是 `POST /api/chat/{session_id}`，需要传 JSON body，并且要支持 AbortController 取消，所以用 fetch stream 手动解析 `data: ` 行。后台 internal-task stream 是 GET 长连接，所以用 EventSource。

代码：`frontend/src/hooks/useSSE.ts`。

### Q：工具卡、internal task 卡、thinking bubble 的区别是什么？

推荐回答：

- 工具卡是真实 LLM tool call，例如搜索、写状态。
- internal task 是非用户工具但耗时或影响上下文的系统任务，例如 memory recall、soft judge、quality gate、context compaction、Phase 5 orchestration。
- ThinkingBubble 是 Agent 当前阶段状态提示，例如 thinking、summarizing、compacting、planning。

这能让用户知道系统不是卡住，而是在执行哪个环节。

### 12.1 Internal Task 双流架构

#### Q：chat SSE 和 background internal-task SSE 分别承载哪些任务？

推荐回答：

- **chat SSE** (`/api/chat/{id}`)：承载与当前回答强绑定的任务 — `memory_recall`、`soft_judge`、`quality_gate`、`context_compaction`、`reflection`、`phase5_orchestration`。
- **background SSE** (`/api/internal-tasks/{id}/stream`)：承载与回答解耦的后台任务 — `memory_extraction_gate`（轻量判断是否值得提取）、`memory_extraction`（聚合态提取结果）、以及按路由出现的 `profile_memory_extraction` / `working_memory_extraction`。

memory recall 必须在生成回答前完成（需要记忆注入 prompt），所以走同步 chat 流；memory extraction 不阻塞当前回答，所以走后台流。

#### Q：前端如何避免同一个 internal task 在双流中重复显示？

推荐回答：

`ChatPanel` 维护一个跨流共享的 `task.id -> message.id` 映射。同一个 `task.id` 的后续更新（如 status 从 pending 变为 success）会通过这个映射找到已存在的卡片并原地更新，而不是创建新卡片。即使后台任务在 chat `done` 之后才结束，也会回写到原卡片。`MessageBubble` 渲染时通过 `internal_task` chunk 的 `kind` 字段区分系统任务卡和真实工具卡的视觉样式。

代码：`frontend/src/components/ChatPanel.tsx`。

#### Q：为什么真实工具的 TOOL_RESULT 要先于 soft_judge / memory task 到达前端？

推荐回答：

用户看到工具卡（如 `save_day_plan`）的 `TOOL_RESULT` 后，心理模型上认为该工具已完成。如果随后又出现 `soft_judge` 卡，用户可能误以为真实工具仍在执行或出错。所以 SSE 事件序列中，真实 `TOOL_RESULT` 先到达并关闭工具卡，系统内部任务（soft judge、memory extraction）的 internal task 事件在其后到达——这样用户看到的是"工具完成 → 系统自动做质量检查"的自然流程。

### 12.2 ParallelProgress 组件

#### Q：`ParallelProgress` 组件如何消费 `parallel_progress` SSE 事件？

推荐回答：

`parallel_progress` SSE 事件携带 `workers[]` 数组，每个 worker 包含 `day`、`theme`、`iteration`、`current_tool`、`activity_count`、`status`、`error` 字段。前端解析该事件后更新 `ChatPanel` 里的 `parallelProgress` 状态，`ParallelProgress` 组件渲染每个 Worker 的进度条（含状态图标、当前工具名、已收集活动数）。

代码：`frontend/src/components/ChatPanel.tsx`、`frontend/src/components/ParallelProgress.tsx`。

#### Q：`ParallelWorkerStatus.status` 当前覆盖哪些状态？缺失什么？

推荐回答：

当前 TypeScript 类型只覆盖 `running | done | failed | retrying`。后端 re-dispatch 时会设置 `status="redispatch"`，这个值不在类型中，运行时可能显示 undefined 图标或缺少状态文案。修复需要：1）类型 union 加 `"redispatch"`；2）`STATUS_ICON` 映射加 redispatch 图标；3）状态文案映射加对应文本。

代码：`frontend/src/types/plan.ts`。

---

## 13. 成本、延迟与扩展性

### Q：当前成本如何估算？

推荐回答：

`SessionStats` 记录 token usage，并通过本地模型价格表估算成本。这个成本适合 demo 和调试，不应视为实时准确账单。生产化需要接入 provider 实际 usage、价格版本、per-user budget 和报警。

代码：`backend/telemetry/stats.py::SessionStats`。

### Q：Phase 5 并行是降低延迟还是增加成本？

推荐回答：

主要降低 wall-clock latency，因为每天可以并行规划；但总 token 成本可能增加，因为多个 Worker 各自运行 LLM loop。它适合用户等待成本高、日程天数多的场景。生产化需要按天数、预算、模型等级动态决定是否并行。

### Q：Phase 5 并行模式下，N 个 Worker 总 token 成本与串行相比是增加还是减少？

推荐回答：

几乎必然增加。并行模式下 N 个 Worker 各自运行独立的 LLM loop，即使 shared prefix 的 KV-Cache 可以节省 prompt processing 成本（不重复计费），每个 Worker 的思考、工具调用、重试仍然独立计 token。串行模式只有一个 LLM 上下文，工具结果自然复用。并行模式的权衡是：用更多 token 换更低 wall-clock 延迟。所以项目需要按天数、用户等待容忍度动态决定是否并行，而不是一刀切。

## 14. 生产化与系统设计

### Q：上线给真实用户，第一批改什么？

推荐回答：

优先级：

1. Auth、rate limit、用户数据隔离。
2. 外部工具 timeout、circuit breaker、fallback。
3. trace/stats 持久化和线上 dashboard。
4. eval 进入 CI，prompt/model change 必跑 regression。
5. Phase 5 fallback 和 unresolved error 阻断。
6. memory deletion、PII 合规、用户确认机制。
7. 高风险操作 human-in-the-loop，尤其是预订、支付、取消。

### Q：如何做 A/B prompt rollout？

推荐回答：

给 prompt/version、model/provider、phase、tool list 都打版本标签。线上按 session 或 user 分桶，trace 中记录版本。对比指标包括 phase completion、tool error rate、retry rate、memory hit rate、judge score、cost、latency 和用户人工反馈。回滚条件必须提前定义。

## 15. 成熟面试官可能抓的具体风险

### Q：`fallback_to_serial` 当前实现和文档一致吗？

参考答案：

不完全一致。文档描述是降级串行，但代码在失败率大于 50% 时只是发 progress chunk 后 `return`，上层 `run_parallel_phase5_orchestrator()` 会产生 warning，`AgentLoop` 收到无 handoff 后 DONE。没有在同一轮自动进入串行 AgentLoop 生成。这是我会优先修的 production hardening 点。

### Q：前端 `redispatch` status 是否覆盖？

参考答案：

没有完全覆盖。后端 Orchestrator 在 re-dispatch 时会设置 `status="redispatch"`，但 `frontend/src/types/plan.ts::ParallelWorkerStatus.status` 只包含 `running | done | failed | retrying`，`ParallelProgress.tsx` 的 `STATUS_ICON` 也没有 redispatch。实际运行时可能显示 undefined 图标或缺少状态文本。修复方式是把 union 和渲染逻辑补齐。

### Q：`_trip_nights()` 用 total_days 合理吗？

参考答案：

这是保守预算估算，代码和测试都是按 total_days 计算住宿成本。但真实酒店晚数通常是 `total_days - 1`。面试里我会承认它是保守口径，不是业务上最精确的晚数模型。生产化可以按住宿 check-in/check-out 或 `max(total_days - 1, 0)` 修正，并更新测试。

## 终面级综合回答

### Q：你从这个项目里学到的 Agent 工程第一性原理是什么？

推荐回答：

Agent 应用不是”让模型多想几步”，而是把不确定性关进可观察、可回滚、可评估的工程边界里。LLM 适合做开放推理和工具选择；代码应该负责状态权威、工具权限、阶段边界、错误恢复、trace 和 eval。一个成熟 Agent 系统的质量不只看最终答案，还要看它走过的轨迹是否安全、必要、可解释、可复现。

### Q：如果面试官问”你这个项目的核心护城河是什么”，你怎么回答？

推荐回答：

不是某一个 prompt 或某一个工具，而是 **Agent 全生命周期控制**。从 Phase 状态机、工具权限模型、读写隔离、prompt 纪律与代码 guardrail 边界、context compaction、memory recall 5-stage pipeline 到 Phase 5 并行 Orchestrator-Workers、Quality Gate/Soft Judge hooks、OpenTelemetry trace、golden eval——每一层都不是”调 API”，而是手写了确定性工程约束来框住 LLM 的不确定性。这套体系迁移到其他 Agent 领域（客服调度、文档审查、代码 agent）时，Phase 状态机、工具系统、trace/eval 管线可以直接复用。

### Q：假设你现在要招聘一个 Agent 工程师加入这个项目，你会出什么面试题？

推荐回答：

我会给一段 trace 日志，要求候选人：
1. 指出模型在哪个 iteration 选错了工具；
2. 解释为什么 compaction 在这个位置触发是合理的/不合理的；
3. 设计一个 eval case 来回归这个问题。
然后让他现场写一个 `@tool` 函数，要求包含 error feedback、参数校验和 phase 门控。

---

## 16. Internal Task System 与异步任务编排

### Q：`InternalTask` 是什么？它和真实 tool call 有什么区别？

推荐回答：

`InternalTask` 是非用户工具但会消耗时间或影响上下文的系统运行时任务。和真实 tool call 的核心区别：
- 真实 tool call 是 LLM 决策调用的领域工具（搜索、写状态），会被渲染为用户可见的工具卡片。
- Internal task 是系统发起的后台操作（memory recall、soft judge、quality gate、context compaction），渲染为系统任务卡，视觉上与工具卡片区分。

代码：`backend/agent/internal_tasks.py::InternalTask`。

### Q：internal task 的生命周期状态有哪些？

推荐回答：

源码中的有效状态是 `pending`、`success`、`warning`、`error`、`skipped`。其中 `warning` 常用于“任务完成但有部分失败/降级”的场景；`partial_failure` 可以作为结果语义或消息描述出现，但不是 `InternalTask.status` 的合法枚举。

代码：`backend/agent/internal_tasks.py::VALID_INTERNAL_TASK_STATUSES`。

### Q：chat SSE 和 background internal-task SSE 分别承载哪些任务？

推荐回答：

- chat SSE (`/api/chat/{id}`)：承载与当前回答强绑定的任务 — `memory_recall`、`soft_judge`、`quality_gate`、`context_compaction`、`reflection`、`phase5_orchestration`。
- background SSE (`/api/internal-tasks/{id}/stream`)：承载与回答解耦的后台任务 — `memory_extraction_gate`、`memory_extraction`（聚合态）、以及按路由出现的 `profile_memory_extraction`、`working_memory_extraction`。

### Q：前端如何避免同一个 internal task 在双流中重复显示？

推荐回答：

`ChatPanel` 维护一个 `task.id -> message.id` 映射，跨 chat 流和 background 流共享。同一个 `task.id` 的更新会回写到已存在的卡片，而不是再创建一个新卡片。这样即使后台任务在 chat `done` 之后才结束，也会更新原卡片而非长出一张重复卡。

### Q：`pending_system_notes` 缓冲区解决什么问题？

推荐回答：

工具执行期间如果直接往消息历史 append SYSTEM 消息，可能会插入在 `assistant.tool_calls` 和对应的 `tool` 答复之间，破坏 OpenAI 消息协议（要求 tool_calls 后紧跟全部 tool 结果）。所以实时约束检查、validator 反馈等产生的 SYSTEM 消息先缓存到 session 级 `_pending_system_notes` 缓冲区，在下一轮 LLM 调用前统一 flush。

代码：`backend/api/orchestration/session/pending_notes.py`。

### Q：为什么真实工具的 TOOL_RESULT 要先于 soft_judge / memory task 到达前端？

推荐回答：

因为用户看到工具卡（如 `save_day_plan`）结束后，会认为那个工具已经完成。如果 `TOOL_RESULT` 之后又插入 `soft_judge` 卡，用户可能误以为真实工具还在执行。所以 SSE 事件顺序确保真实 `TOOL_RESULT` 先到达并结束工具卡，`soft_judge` 和 `memory_extraction` 内部任务卡在此之后到达。

---

## 17. 记忆召回管线深度（Stage 0-4 全链路）

### Q：从用户消息进入到记忆注入 system prompt，全链路经过哪些阶段？

推荐回答：

5 个阶段：

1. **Stage 0 硬规则短路**：6 类词表匹配（history/style/recommend/fact_scope/fact_field/ack_sys），按 P1-P6 优先级输出三值决策（force_recall/skip_recall/undecided）。
2. **Stage 1 LLM gate**：仅处理 undecided 样本，LLM 判断 `intent_type` 和 `needs_recall`。
3. **Stage 2 retrieval plan**：LLM 生成 source-aware 检索计划（查哪些 source、domain、keywords、destination）。
4. **Stage 3 candidate generation**：按检索计划执行 symbolic + semantic + lexical 多 lane 召回，产出 `RecallCandidate[]` + `evidence_by_id` sidecar。
5. **Stage 4 reranker**：rule-based weighted reranker 对候选打分、去重、排序、按 budget 选择，最终注入 system prompt。

代码：`backend/api/orchestration/memory/turn.py::build_memory_context_for_turn`。

### Q：Stage 0 的 P1-P6 优先级如何工作？

推荐回答：

- P1：profile signal（如”按我习惯”）→ force_recall
- P1N：P1 信号前有否定词（”不要按我上次的”）→ undecided，交给 LLM
- P2：recommend 信号（”怎么安排比较好”）→ undecided
- P3：纯事实问句（”这次预算多少”）→ skip_recall
- P4：仅 ACK（”好的””知道了”）→ skip
- P5：空消息 → undecided
- P6：兜底 → undecided

代码：`backend/memory/recall_gate.py::apply_recall_short_circuit`。

### Q：Stage 1 LLM gate 的 `intent_type` 有哪些枚举值？

推荐回答：

`current_trip_fact`、`profile_preference_recall`、`profile_constraint_recall`、`past_trip_experience_recall`、`mixed_or_ambiguous`、`no_recall_needed`。其中 `mixed_or_ambiguous` 采用保守召回——即使 LLM 返回 `needs_recall=false` 也归一化为 `needs_recall=true`，防止模糊样本漏召。

代码：`backend/memory/recall_gate.py::VALID_RECALL_INTENT_TYPES`。

### Q：Stage 2 retrieval plan 的 source-aware contract 是什么？

推荐回答：

- `profile` 和 `hybrid_history` source：必须填写 `buckets`（指定查 constraints/rejections/stable_preferences/preference_hypotheses 中的哪些）。
- `episode_slice` source：不暴露 `buckets`，使用 `domains`（系统枚举值）、`destination`、`keywords`。
- `plan_facts` 只用于提取目的地/预算/同行人等检索参数，不重新判断是否 recall。

### Q：Stage 1 gate 为什么不再接收 `memory_summary`？

推荐回答：

之前的实现会在 gate 调用前预构建一个 memory summary，但这个 summary 本身就包含了库存信号，会导致 gate LLM 倾向判断”需要召回”——形成自证循环。现在的设计是 gate 只看用户消息（`latest_user_message` + 兜底 `previous_user_messages` 消歧）和当前 plan facts，先判断是否需要召回；只有在 gate 放行后，Stage 2 才自行构建 `memory_summary` 来规划如何检索。

### Q：long-term profile 为什么不再固定常驻注入 system prompt？

推荐回答：

长期 profile 可能很大会撑爆上下文，也可能包含与当前行程不相关的条目。改为和 episode slice 一样，只在 query recall 命中后才以 candidate 进入上下文，做到”按需注入”。这既节省 token 又避免无关记忆干扰模型决策。

### Q：recall gate 超时或异常时的 heuristic fallback 逻辑是什么？

推荐回答：

gate 超时/异常时，先根据 Stage 0 signals 做启发式判定：如果命中历史/画像线索（P1 信号）则保守走 recall；否则以 `no_recall_applied` 跳过。如果判定需要 recall，Stage 2 同样跳过 LLM，用 `heuristic_retrieval_plan_from_message` 生成 stage0-aware retrieval plan。所有 fallback 路径都记录在 `recall_skip_source` 或 `query_plan_fallback` 遥测字段中。

---

## 18. Prompt 工程与上下文组装架构

### Q：`build_phase3_prompt(step)` 的三段拼装公式是什么？

推荐回答：

`PHASE3_BASE_PROMPT`（通用角色、工具约束、输出协议）+ `PHASE3_STEP_PROMPTS[step]`（子步骤特定角色、目标、流程）+ `GLOBAL_RED_FLAGS`（跨阶段通用禁令）。

代码：`backend/phase/prompts/`。

### Q：”工具职责对照表”解决什么问题？

推荐回答：

Phase 3 有 11 个工具，LLM 容易把”记录骨架选择”和”生成骨架方案”混用，或者用 `set_trip_brief` 记录候选池。工具职责对照表是一张 10 行的”你想做什么 → 应该调用 → 不要调用”映射，例如”想记录骨架选择 → 调用 `select_skeleton` → 不要调用 `set_shortlist`”，防止工具混用。

### Q：state repair hints 覆盖 Phase 3 全部 4 个子阶段，每个阶段的检测逻辑是什么？

推荐回答：

- **brief**：检测 `trip_brief` 为空但文本包含画像/偏好/约束关键词 → 提示用 `set_trip_brief` 写入画像核心字段。
- **candidate**：检测 `candidate_pool` 为空但文本含候选信号词 → 提示写 `set_candidate_pool`；`candidate_pool` 存在但 `shortlist` 缺失 → 提示写 `set_shortlist`。
- **skeleton**：检测骨架信号词（”轻松版””平衡版””高密度版””方案 A/B/C”）但 `skeleton_plans` 为空 → 提示写 `set_skeleton_plans` + `select_skeleton`。
- **lock**：按类别独立检测——交通未锁、住宿未锁、风险未记录、备选未记录。

每个子阶段允许两次修复尝试（首次 + retry），`repair_key` 去重。

代码：`backend/agent/execution/repair_hints.py`。

### Q：Phase 3 的”前瞻容错”设计为什么能防止状态丢失和死循环？

推荐回答：

如果 LLM 在 brief 阶段就跳到了 candidate 的结论（写出了候选池内容），但没有白名单放行 `set_candidate_pool` 工具，模型就只能用自然语言描述——状态丢失。前瞻容错让每个子阶段向前开放下一阶段的写入工具（brief 可写 `set_candidate_pool`/`set_shortlist`，candidate 可写 `set_skeleton_plans`/`select_skeleton`），LLM 跳阶时工具仍在可用列表中，不会因为工具不可用而陷入”想说但写不了→重复描述→永远卡在当前阶段”的死循环。

### Q：handoff note 的”开场白协议”为什么禁止 `[Phase N 启动]` 式开头？

推荐回答：

Agent 阶段推进后如果以 `[Phase 3 启动]`、`前置条件检查：✓` 这种机器感 checklist 开场，用户看到的是系统内部的阶段切换噪音而不是自然对话。开场白协议要求模型用 1-2 句自然语言承上启下（如”好的，我现在根据你选的大阪深度游骨架来安排每天的具体行程”），保持对话自然感的同时完成阶段交接。

---

## 19. Config 体系、Feature Flag 与可扩展性

### Q：`config.yaml` 的加载优先级是什么？

推荐回答：

环境变量 > YAML > 代码默认值。YAML 中通过 `${ENV_VAR}` 语法引用环境变量，例如 `google_maps: “${GOOGLE_MAPS_API_KEY}”`。`config.py` 在加载时会用 `os.path.expandvars` 解析这些引用，然后和代码中的 dataclass 默认值合并。

### Q：Phase 5 并行模式有哪些可配置参数？

推荐回答：

`phase5.parallel` 段包含：
- `enabled`: 是否启用并行模式（bool）
- `max_workers`: 最大并发 Worker 数（int，默认 5）
- `worker_timeout_seconds`: 单个 Worker 超时秒数（int，默认 1200）
- `fallback_to_serial`: 失败时是否降级串行（bool，默认 true）

代码：`backend/config.py::Phase5ParallelConfig`。

### Q：LLM provider 如何按阶段覆写？

推荐回答：

`config.yaml` 中 `llm_overrides` 段支持按阶段指定不同的 provider 和 model。例如当前配置里 `phase_1_2` 使用 Anthropic Claude Sonnet 4，`phase_5` 使用 OpenAI GPT-4o。配置加载在 `backend/config.py::load_config` 中解析成 `AppConfig.llm_overrides`；LLM 实例实际由 `backend/llm/factory.py::create_llm_provider` 根据传入的 `LLMConfig` 创建。需要注意：当前代码中没有 `LLMFactory` 类，阶段 override 的选择逻辑应结合调用侧传入哪份 `LLMConfig` 来看。

### Q：memory 系统有哪些 feature flag？如何通过 config 把 reranker 退回到 rule-only？

推荐回答：

- `stage3.semantic.enabled`: 语义 lane 开关（默认 true）
- `stage3.fusion.lane_weights`: 各 lane 的融合权重
- `reranker.evidence.lane_fused_weight/semantic_score_weight/lexical_score_weight`: 三个 evidence 权重
- `reranker.dynamic_budget.enabled`: 动态预算（默认 false）
- `reranker.evidence.*_hit_weight`: hit flag 权重（默认全 0）

退回 rule-only：把三个 evidence score 权重都写为 0，reranker 的 `evidence_score` 恒为 0，退化为纯 rule signal 排序。

### Q：如果新增一个 LLM provider（如 DeepSeek），需要改哪些文件？

推荐回答：

1. `backend/llm/deepseek_provider.py`：实现 `LLMProvider` Protocol。
2. `backend/llm/factory.py`：在 `create_llm_provider` 中注册 deepseek 分支。
3. `config.yaml`：`llm_overrides` 中可选按阶段指定 deepseek model。
4. 测试：`backend/tests/test_llm_deepseek.py` 覆盖 provider 行为和错误分类。

---

## 20. API 包重构与代码架构演进

### Q：后端从 `main.py` 单体到 `api/` 结构化包的拆分动机是什么？

推荐回答：

原始 `main.py` 承载了 FastAPI 路由注册、依赖装配、SSE 生成器、保底持久化、memory recall/extraction 编排等全部逻辑，文件膨胀到难以维护。拆分为 `api/` 包后：
- `api/routes/`：按 HTTP 资源分组路由（只在路由层做参数解析和响应序列化）。
- `api/orchestration/`：按业务领域分组编排逻辑（agent/chat/memory/session/common），路由不再直接写业务逻辑。
- 当前代码库里 FastAPI 应用入口和核心依赖装配仍在 `backend/main.py::create_app`；并不存在 `backend/api/app.py`。因此面试中应把这次重构描述为“路由与业务编排下沉到 `api/` 包，入口仍保留在 `main.py`”，而不是说应用装配已经完全迁移。

### Q：`AgentLoop` 从单体拆成 `agent/execution/` + `agent/phase5/` 的边界是什么？

推荐回答：

- `agent/loop.py`：保留 AgentLoop 主循环骨架（think-act-observe 框架、取消检查点、hook 触发）。
- `agent/execution/llm_turn.py`：单轮 LLM 调用与流式 chunk 解析。
- `agent/execution/tool_invocation.py`：工具批量执行与搜索历史追踪。
- `agent/execution/phase_transition.py`：统一阶段变化检测与转换前质量门控。
- `agent/execution/repair_hints.py`：跨阶段状态修复提示。
- `agent/phase5/`：Phase 5 并行模式独立子包（orchestrator、day_worker、worker_prompt、candidate_store、parallel）。

### Q：`MemoryOrchestration` dataclass 为什么用 dataclass 而非 class？

推荐回答：

`MemoryOrchestration` 本质上是一个能力聚合容器——把记忆召回、提取、任务调度、episode 归档等可注入函数组合在一起，没有自己的生命周期或状态变更。dataclass 比 class 更适合这种”结构化配置对象”的语义：字段列表即文档，`__init__` 自动生成，类型检查友好。

代码：`backend/api/orchestration/memory/orchestration.py::MemoryOrchestration`。

---

## 21. Phase 3 骨架 POI 唯一性设计

### Q：为什么在 writer 层校验 POI 唯一性，而不在 Phase 5 Worker 层事后去重？

推荐回答：

事后去重有两个问题：
1. Phase 5 并行 Worker 在独立上下文中运行，Worker A 不知道 Worker B 用了哪些 POI，发现重复时已经生成了完整 DayPlan，修复成本高。
2. 去重后的 POI 需要重新分配，可能破坏原有活动序列的时间、交通和节奏安排。

在 `set_skeleton_plans` 写入边界校验 POI 全局唯一性，把重复拦截在骨架生成阶段——源头治理比末端修补更高效。

### Q：单个 skeleton 内 locked_pois 和 candidate_pois 之间的 POI 不可重复，跨 skeleton 是否也校验？

推荐回答：

当前校验只作用于单个 skeleton 写入边界——即同一个 skeleton 内的 locked_pois 和 candidate_pois 不可有 POI 重复。跨 skeleton（方案 A vs 方案 B）之间允许 POI 重叠，因为它们是互斥的候选方案，用户只会选一个。

### Q：reader-side 过滤防御是什么？

推荐回答：

`infer_phase3_step_from_state` 在读取 `skeleton_plans` 时会对污染的 skeleton 做过滤——如果某个 skeleton 缺少 `id`/`name` 或 `days` 为空，会被 reader 端跳过不计算。这样即使数据被外部写入污染，infer 逻辑也不会因此崩溃或返回错误子步骤。

代码：`backend/state/models.py::infer_phase3_step_from_state`。
