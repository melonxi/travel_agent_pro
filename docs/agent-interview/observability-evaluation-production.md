# 可观测性、评估、可靠性与生产化面试问答

> 范围：本文件聚焦 Travel Agent Pro 中容易被单独追问的“生产 Agent 工程能力”：TraceViewer / SessionStats / OpenTelemetry、agent evals、golden eval、reranker-only eval、trace grading、Quality Gate / Soft Judge / Validator、InternalTask UX、SSE retry / cancel / continue、成本延迟、安全隐私合规，以及当前实现短板的客观表达。
>
> 使用方式：不是背诵稿，而是面试中可拆开组合的技术表达素材。回答时优先结合代码路径和真实边界，不要把设计目标说成已落地能力。

## 0. 总览定位

### Q：如果用一句话概括这类能力在项目里的价值，你会怎么说？

推荐回答：

Travel Agent Pro 不只追求“模型最后答得像样”，而是把 Agent 的中间轨迹变成可观察、可评分、可恢复、可审计的工程对象。`SessionStats` 和 `TraceViewer` 让开发者能看到 LLM call、tool call、memory recall、state diff、validation error、judge score 和成本延迟；`evals` 让 prompt / tool / memory / reranker 改动有回归基线；`InternalTask` 和 SSE 让用户看到系统正在做的后台判断；guardrail、validator、quality gate 则把不确定的模型行为关在确定性边界内。

这也是 2025-2026 年 Agent 工程趋势的核心：面试和生产系统越来越关注 trajectory，而不是只看 final answer。OpenAI 的 agent eval / trace grading、Anthropic 的 multi-turn agent eval 和工具评估、Google A2A / MCP 这类互操作协议，本质都在要求 Agent 的工具调用、状态变化、handoff、guardrail 和成本延迟可度量。

### Q：为什么这一类能力值得单独成文档？

推荐回答：

因为它们不是普通“日志和测试”的附属品，而是 Agent 系统能否生产化的核心。传统 Web 服务的主要失败形态是 HTTP 500、数据库错误、接口超时；Agent 的失败更常见于中间轨迹：选错工具、重复搜索、工具参数错、记忆误召回、judge 误判、阶段提前推进、写状态后流中断、外部工具返回 prompt injection。单看最终回复很容易被自然语言掩盖。

所以这个项目把质量对象拆成四层：

- **状态质量**：Phase 是否正确推进，`TravelPlanState` 字段是否完整、一致。
- **轨迹质量**：工具是否必要，参数是否正确，是否重复搜索或越权调用。
- **运行质量**：token、成本、延迟、错误分类、continue / cancel 是否正确。
- **安全质量**：工具输入输出是否经过 guardrail，外部内容是否被当成不可信数据。

### Q：你会如何把项目和当前 Agent 平台趋势对齐？

推荐回答：

我会避免泛泛说“Agent 很火”，而是对齐到平台原语：

- OpenAI AgentKit / Agents SDK / Responses API 强调 tools、handoff、guardrails、stateful runs、streaming、trace 和 eval；Travel Agent Pro 虽然是自研 loop，但也有 `RunRecord`、`ToolCall`、`ToolResult`、`TravelPlanState`、Phase gate、TraceViewer 和 golden eval。
- OpenAI trace grading 明确把工具调用、推理步骤、workflow 行为作为可评分对象；项目当前还没接原生 trace grading，但 `build_trace()` 已经把 iteration、tool、state diff、memory recall、judge score 结构化出来，具备迁移基础。
- Anthropic “Building effective agents” 强调先用简单可组合 workflow 和充分 eval，复杂度必要时再拆 agent；项目的 Phase 5 Orchestrator-Workers 正是因为逐日规划天然并行，才引入 worker，而不是为了多 Agent 而多 Agent。
- MCP / connectors / A2A 代表工具和 Agent 互联趋势；项目当前的读写隔离、工具 guardrail、trace 审计和 human-in-the-loop 生产化计划，是接这些协议前必须保留的安全边界。

## 1. 可观测性与 Trace

### Q：项目中记录哪些 trace / stats？

推荐回答：

分两套链路：

第一套是面向外部 tracing backend 的 OpenTelemetry。`backend/telemetry/setup.py` 初始化 `TracerProvider`、OTLP gRPC exporter 和 FastAPI instrumentation；`ToolEngine.execute()` 创建 `tool.execute` span，并记录 `tool.input` / `tool.output` event；`OpenAIProvider` 和 `AnthropicProvider` 创建 `llm.chat` span，记录 `llm.request` / `llm.response` event；`PhaseRouter.check_and_apply_transition()` 记录 `phase.transition` span；`ContextManager.should_compress()` 记录 `context.should_compress` span；Phase 5 Orchestrator / Day Worker 也有各自 span。

第二套是面向产品调试 UI 的 `SessionStats`。它记录：

- LLM call：provider、model、input / output tokens、duration、phase、iteration。
- tool call：tool name、duration、status、error_code、phase、参数摘要、结果摘要、parallel_group、validation_errors、judge_scores、suggestion。
- memory hits：profile / working memory / episode slice 命中来源和 ids。
- recall telemetry：Stage 0 decision、LLM gate 结果、fallback、candidate_count、zero-hit、reranker selected ids、per-item scores、selection metrics。

代码：

- `backend/telemetry/stats.py::SessionStats`
- `backend/api/trace.py::build_trace`
- `frontend/src/components/TraceViewer.tsx`

### Q：`SessionStats` 记录什么？它和 OpenTelemetry 有什么区别？

推荐回答：

`SessionStats` 是业务调试模型，不是通用 tracing 标准。它按 session 聚合 LLM 和工具行为，目标是让前端 TraceViewer 和 eval runner 能直接回答：“这一轮用了多少 token、花了多少钱、哪个工具慢、哪个写工具改了状态、memory recall 为什么命中或跳过。”

OpenTelemetry 更适合跨服务调用链和生产 APM：HTTP root span、LLM span、tool span、phase transition span 可以进 Jaeger 或后续的 OTel backend。`SessionStats` 更贴近 Agent 语义：它知道 phase、iteration、state_changes、judge_scores、reranker_selected_ids，这些不是通用 HTTP tracing 自动知道的。

理想生产形态是两者结合：OTel 负责分布式链路、采样、告警；`SessionStats` / trace event 落库，负责 Agent 轨迹复盘、trace grading 和 eval 回放。

### Q：TraceViewer 如何组织 LLM call、tool call、memory hit？

推荐回答：

后端 `build_trace()` 以每个 LLM call 为一个 iteration，然后把这个 LLM call 到下一个 LLM call 之间发生的 tool calls、memory hit、recall telemetry 和 compression event 归到同一个 iteration。每个 iteration 会计算 significance：

- `high`：有 state_changes、validation_errors、judge_scores，或包含写工具。
- `medium`：有读工具。
- `low`：只有 memory recall 或 compression。
- `none`：纯 thinking，没有外部动作。

前端 `TraceViewer` 再按 phase 分组展示。顶部 SummaryBar 显示 tokens、cost、duration、LLM calls、tool calls；PhaseGroup 展示每个 phase 的汇总；低价值的纯 thinking steps 会折叠成 summary，真正有状态变化、工具调用、质量问题的 iteration 保持可展开。

这比“日志时间线”更适合 Agent，因为面试官可以直接看到一次规划为什么推进、哪里查了外部信息、哪个工具修改了状态、Soft Judge 给了什么分。

### Q：为什么传统 request-response 日志不够？

推荐回答：

Agent 的失败通常不发生在入口或出口，而发生在中间轨迹。比如最终回答看起来很完整，但中间可能：

- 重复搜索同一个 query 5 次；
- 把小红书评论里的恶意指令当成系统指令；
- 在 Phase 3 没有写 `set_skeleton_plans`，只在自然语言里说“方案已确定”；
- memory recall 误把上次旅行偏好注入本次事实问题；
- Phase 5 worker 生成了 0,0 假坐标或跨天重复 POI；
- 流式中断后 retry 造成重复写状态。

传统日志最多告诉你接口成功返回了；TraceViewer 和 stats 能告诉你模型走了什么路径、为什么这个路径有风险。

### Q：OpenTelemetry 在哪里接入？当前覆盖完整吗？

推荐回答：

接入点在 `backend/telemetry/setup.py::setup_telemetry()`，由 `main.py::create_app()` 调用。配置来自 `TelemetryConfig`，默认 endpoint 是 `http://localhost:4317`，本地可用 `docker-compose.observability.yml` 启动 Jaeger，端口 `4317` 收 OTLP gRPC，`16686` 看 UI。

当前覆盖了关键路径，但还不是完整生产 APM：

- 已有：FastAPI HTTP instrumentation、`agent_loop.run` / `agent_loop.iteration`、`llm.chat`、`tool.execute`、`phase.transition`、`context.should_compress`、Phase 5 orchestrator / day worker spans。
- 已有：span events 做截断保护，例如 tool 入参/出参、LLM 请求摘要、LLM 响应预览、phase snapshot。
- 缺口：没有指标体系、告警、采样策略、日志关联 trace_id、用户/tenant 维度的 dashboard；部分业务事件仍只在 `SessionStats` 内存对象里，没有落到 OTel events 或数据库。

面试时要说“这是开发调试级可观测性 + 生产化雏形”，不能说已经是完整生产监控平台。

### Q：`memory_recall` SSE 事件为什么零命中也要暴露 telemetry？

推荐回答：

因为 memory 失败不只有“命中错了”，还有“该召回却跳过”“召回了但候选为 0”“候选有了但 reranker 过滤掉”。零命中如果只显示“没有记忆”，开发者不知道问题在哪一层。

项目的 `memory_recall` 事件会带：

- Stage 0：`stage0_decision`、`stage0_matched_rule`、signals。
- Gate：`gate_needs_recall`、`gate_intent_type`、fallback。
- Query plan：`query_plan_source`、`query_plan_fallback`。
- Stage 3 / 4：`candidate_count`、`recall_attempted_but_zero_hit`、`reranker_selected_ids`、`reranker_final_reason`、`reranker_per_item_scores`、`reranker_selection_metrics`。

这样 false skip、false recall、zero-hit、reranker 误杀能分开定位，后续 eval 也可以用 `memory_recall_field` 断言精确保护。

### Q：如果一次规划失败，你会从哪里开始 debug？

推荐回答：

我会按“用户可见症状 -> 轨迹 -> 状态 -> 上下文”的顺序查：

1. 看 TraceViewer Summary：成本和耗时是否异常、是否有大量 LLM calls 或 tool calls。
2. 展开最后一个 high significance iteration：是否是写工具、validation error、judge warning 或 phase transition。
3. 看 tool call arguments / result preview：参数是否空、是否重复搜索、是否 error_code 可恢复。
4. 看 state_changes：写入的是不是预期字段，是否写了旧日期、旧 skeleton 或跨 phase 字段。
5. 看 memory_recall：是否误召回、zero-hit、reranker fallback。
6. 看 SSE / InternalTask：quality_gate 是阻断、跳过还是达到重试上限放行。
7. 如果是上下文污染，再查 append-only messages 的 `context_epoch` 和 `rebuild_reason`。

这个路径体现的是 Agent debugging 思维：先看 trajectory，再看 prompt。

### Q：进程重启后旧 session 的 trace 还能看吗？

推荐回答：

不能完整看。当前 plan 和 append-only messages 能从 JSON / SQLite 恢复，但 `SessionStats` 是进程内对象，`restore_session()` 会创建新的空 stats；`/api/sessions/{id}/trace` 也只从内存 `sessions` 查，进程重启后旧 trace 可能 404 或不完整。

这是一个必须主动承认的生产化短板。合理解释是：项目当前优先保证旅行状态和消息历史可恢复，trace 还处于开发调试层。生产化下一步应该把 trace events / stats 持久化到数据库或专门 trace backend，并把 run_id、history_seq、context_epoch 关联起来。

## 2. Evaluation 与 Trace Grading

### Q：项目有哪些测试和评估层？

推荐回答：

可以分五层讲：

- **单元测试**：验证 plan writer、PhaseRouter、ToolEngine、guardrail、validator、judge、memory reranker 等确定性逻辑。
- **后端集成测试**：验证 chat stream、tool call sequence、Phase 5 parallel handoff、context persistence、session restore 等跨模块行为。
- **golden eval**：`backend/evals/golden_cases/*.yaml`，用场景断言 phase、state field、tool called / not called、文本、预算和 memory recall telemetry。
- **pass@k stability eval**：`scripts/eval-stability.py` 多次运行同一 case，输出 pass rate、assertion consistency、tool overlap、cost / latency stats。
- **reranker-only eval**：`backend/evals/reranker_cases/*.yaml` 固定 Stage 0/1/2 输出和候选集，只测 Stage 4 reranker 的 selected ids、fallback、final reason、per-item reason。

前端还有 Playwright E2E，覆盖主流程、重试/等待体验等用户路径。

### Q：`backend/evals/runner.py` 支持哪些断言？

推荐回答：

当前断言类型在 `backend/evals/models.py::AssertionType`：

- `phase_reached`：最终 phase 是否达到预期。
- `state_field_set`：状态字段是否存在或等于预期值。
- `tool_called` / `tool_not_called`：工具轨迹是否符合预期。
- `contains_text` / `not_contains_text`：最终回复文本是否包含或避免某些内容。
- `budget_within`：总费用是否在预算 margin 内。
- `memory_recall_field`：检查 `stats.last_memory_recall` 中的嵌套字段，例如 `final_recall_decision`、`query_plan_source`、`recall_attempted_but_zero_hit`。

这个设计说明项目的 eval 不只看最终文本，而是把状态、工具和 memory telemetry 都作为质量对象。

### Q：golden eval 和普通 pytest 有什么区别？

推荐回答：

pytest 更适合验证确定性函数和协议不变量，例如 `_skeleton_days_match()`、`ToolGuardrail.validate_input()`、`parse_judge_response()`。golden eval 验证的是端到端 Agent 行为：给一组用户消息，执行 agent，再检查最终状态、工具轨迹、响应和 stats。

对 Agent 来说，很多回归不是函数 bug，而是 prompt / schema / tool description 改动导致模型行为漂移。golden eval 的价值是把“模型这次有没有走对路线”变成可重复检查的场景。

### Q：reranker-only eval 为什么重要？

推荐回答：

Memory recall 是多阶段 pipeline：Stage 0 规则、Stage 1 LLM gate、Stage 2 retrieval plan、Stage 3 candidate generation、Stage 4 reranker。如果直接跑 live eval，失败可能来自 gate 抖动、query 规划变化、embedding lane、候选集变化，难以归因。

reranker-only eval 固定前面阶段和候选集，只验证 Stage 4：

- selected item ids 是否符合预期；
- conflicting profile 是否被 drop；
- episode slice intent 是否不会污染 profile；
- recency half-life 是否优先近期；
- negated preference 是否过滤；
- weak candidates 是否全 drop；
- per-item reason 是否包含关键解释。

这体现了一个成熟 eval 思路：把不稳定链路拆开，先测确定性子系统，再测端到端。

### Q：pass@k stability eval 解决什么问题？

推荐回答：

单次通过不能说明 Agent 稳。LLM 具有采样、工具结果和外部服务波动，同一个 case 可能这次走对、下次走偏。`scripts/eval-stability.py` 用 k 次运行衡量：

- pass_rate：同一 case k 次有几次过。
- assertion_consistency：每个断言的通过率。
- tool_overlap_ratio：工具集合交并比，衡量工具选择是否稳定。
- cost_stats / duration_stats：成本延迟波动。

这比“我跑过一次成功”更接近生产质量。面试中可以说：对于 Agent，我不只关心平均效果，还关心方差和可复现性。

### Q：如何评估一个 Agent 是否真的变好了？

推荐回答：

不能只看一次 demo，也不能只看最终文字。我的标准是四类指标同时不回退：

- **最终状态**：Phase、`TravelPlanState`、deliverables 更完整。
- **轨迹**：更少重复搜索，更少错误工具，更少 repair hint，更少 backtrack 误触发。
- **可靠性**：pass@k 更高，unstable cases 更少，continue / cancel 后状态一致。
- **成本延迟**：token、wall-clock latency、tool latency、parallel worker 失败率不恶化。

如果某个 prompt 改动让最终回答更漂亮，但 tool calls 翻倍、memory false recall 上升、phase gate 经常靠重试放行，我不会认为它真的变好了。

### Q：为什么 trace eval 比只看最终答案更适合 Agent？

推荐回答：

Agent 的安全和质量风险经常藏在中间步骤。最终回答可以“包装”错误轨迹，例如先调用了危险工具、再用自然语言说得很稳。Trace eval 能检查：

- 是否选择了正确工具；
- tool arguments 是否符合 schema 和业务边界；
- guardrail 是否触发；
- handoff / backtrack 是否合理；
- memory recall 是否该召回、召回是否命中；
- 成本延迟是否在预算内。

OpenAI trace grading 的方向也是给 agent trace 打结构化标签或分数，用于发现 workflow-level 错误。Travel Agent Pro 当前还没接原生 trace grading，但 `SessionStats + build_trace` 已经具备内部 trace grading 所需的大部分数据边界。

### Q：你会如何把当前 eval 升级成 trace grading？

推荐回答：

我会分三步：

1. **落库 trace event**：把现在的 in-memory `SessionStats`、tool previews、state changes、internal tasks、memory telemetry 持久化，按 `run_id` 和 `context_epoch` 关联。
2. **定义 trace rubric**：例如“Phase 3 candidate 必须先搜索再 shortlist”“Phase 5 不允许跨天 POI 重复”“外部工具结果中的 prompt injection 不得导致写工具调用”“memory current-trip fact 必须 skip recall”。
3. **接 grader**：先用确定性 grader 覆盖工具名、参数、状态 diff；再用 LLM judge 对行程质量维度打分，但 LLM judge 只能辅助，不能替代硬约束。

这样 eval 才能从 final-state regression 升级到 workflow regression。

### Q：如果你改了 Phase 3 prompt，怎么防止 Phase 1 或 Phase 5 回归？

推荐回答：

我会按风险分层跑：

- 先跑 Phase 3 相关单元测试和 prompt regression cases，例如 candidate 必须搜索、handoff 自然开场。
- 再跑 golden cases 中 easy / medium / failure / memory_recall 的核心子集，确保工具调用和状态字段没有漂移。
- 如果改动影响 skeleton 或 Phase 5 handoff，再跑 Phase 5 parallel integration 和 reranker-only eval，防止候选池、骨架、记忆召回互相污染。
- 最后看 TraceViewer：状态是否通过 writer 写入，是否出现额外 repair hints、重复搜索或 quality gate 重试。

这体现的是 agent eval 的现实做法：按 blast radius 选择 eval，而不是每次只盯一个最终答案。

### Q：当前 eval 有哪些客观不足？

推荐回答：

有几类不足要主动说：

- golden eval 主要还是断言型，缺少统一 trace grading pipeline。
- 部分 live eval 依赖外部服务和模型，成本、速度、稳定性都受影响。
- LLM judge 没有人类标注校准集，不能把 judge score 当成绝对真理。
- 间接 prompt injection eval suite 还不完整。
- trace / stats 没落库，重启后无法做历史 trace 批量评分。
- pass@k 报告能指出方差，但还没有自动把失败 trace 聚类成根因。

合理表达是：现在已经从“无 eval”走到“可执行 golden + stability + reranker-only”，但离生产级 continuous evaluation 还有 trace persistence、grader calibration 和 CI gating 要补。

### Q：怎么把 Eval 接到 CI 形成 prompt / model regression gate？

推荐回答：

成熟的 Agent 团队会把 eval 当成一等 CI gate，不是每次手跑。我会按改动影响面分级触发：

| 改动类型 | 必跑 eval 子集 | gate 规则 |
| --- | --- | --- |
| prompt 改动 | 影响 phase 的 golden + reranker-only + stability k=5 子集 | pass_rate ≥ baseline；tool_overlap ≥ 0.85；budget_within 不退化 |
| tool schema / description 改动 | 全量 golden + tool trajectory 比对 | tool sequence diff 必须人工 review |
| LLM provider / model 升级 | 全量 golden + stability k=10 + cost / latency 对比 | cost 不能上升 >20% 且 latency P95 不能恶化 |
| memory pipeline 改动 | reranker-only + memory_recall golden | memory false recall 率不上升 |
| 安全相关改动 | indirect injection golden（待建） | 攻击成功率必须 = 0 |

落地要点：

1. **Baseline 锁版**：每次合并入 main 时把 `eval-stability` 的 pass rate / tool overlap / cost 写到 baseline 文件；PR diff 自动对比。
2. **Quarantine**：eval 偶发抖动应进 quarantine bucket 而不是直接 block PR；连续 3 次 quarantine 触发 owner 分配。
3. **Trace artifact 落 PR**：失败 case 的 trace JSON 作为 CI artifact 上传，reviewer 不用本地复跑。
4. **小模型抽样跑 + 大模型 nightly 全量**：成本控制和覆盖之间的平衡。

当前项目还没有这套 gating，是 demo 阶段合理的取舍；但要诚实说"还没接 CI"，不要假装已经做了。

## 3. Quality Gate、Soft Judge、Validator

### Q：Quality Gate、Soft Judge、Validator 分别是什么？

推荐回答：

三者分工不同：

- **Validator** 是确定性规则检查，发生在写工具之后或 phase transition 前。它检查预算、日期、时间冲突、天数超限、交通住宿锁定预算占比等。
- **Soft Judge** 是 LLM-as-Judge，发生在 `save_day_plan`、`replace_all_day_plans`、`generate_summary` 等工具结果之后，给 pace、geography、coherence、personalization 打分，并产生建议。它通常不阻断主流程。
- **Quality Gate** 是 phase transition 前的门控，属于 `before_phase_transition` hook。它先跑 feasibility / hard constraints；对 Phase 3→5、Phase 5→7 这类关键跳转，会用 judge 分数和阈值决定是否注入修正建议、阻断一次，或达到重试上限后放行。

代码：

- `backend/harness/validator.py`
- `backend/harness/judge.py`
- `backend/api/orchestration/agent/hooks.py`

### Q：Quality Gate 为什么放在 phase transition 前，而不是写工具里？

推荐回答：

写工具只负责 mutation，不能自己决定全局流程是否推进。比如 `replace_all_day_plans` 写入每日行程后，是否能进入 Phase 7 要看完整计划：天数、预算、时间冲突、行程质量。这属于状态机层的判断，不应散落在每个 writer 里。

项目的路径是：writer 修改 `TravelPlanState` -> hook 做增量检查 -> `detect_phase_transition()` 调 `PhaseRouter` -> `before_phase_transition` gate -> allowed 后才真正改 `plan.phase`。这样工具层、质量层、状态机层边界清楚。

### Q：Soft Judge 用 LLM 打分，怎么避免误导系统？

推荐回答：

LLM-as-Judge 是有偏的，项目没有把它当硬事实：

- **fail-open**：soft judge 失败或低分不会直接回滚已写状态；失败会以 `InternalTask(status="error")` 展示，"不影响已保存的行程"。
- **维度拆分**：不用一个总分判断所有质量，而是 pace、geography、coherence、personalization 四维。
- **解析保护**：`parse_judge_tool_arguments()` 和 `parse_judge_response()` 会 clamp 1-5 分，解析失败返回默认 3 分并记录 warning。
- **硬约束优先**：预算、日期、时间冲突、骨架天数一致性这类问题由 deterministic validator / PhaseRouter 处理，不交给 LLM judge 自由裁决。

但要面试讲透 LLM-as-Judge **必须正面承认它的几类偏差**：

- **Self-preference bias**：用同一家 model 做 judge 会偏向同 family 输出（Panickssery et al. 2024 实证）。本项目当前 judge 走的就是主 LLM 的 provider，这是已知风险。
- **Leniency bias**：对结构正确但内容平庸的输出打分偏高，对探索性输出偏低。clamp 默认 3 分进一步放大 leniency。
- **Position / verbosity bias**：在 pairwise 比较场景里，靠前或更长的候选打分更高（MT-Bench、Arena 都已记录）。
- **Prompt sensitivity**：judge prompt 微调一行措辞，分布会整体漂移。

缓解方案要分层：

1. **Cross-family judging**：判 OpenAI 输出时用 Anthropic / Gemini 做 judge，反之亦然。
2. **抽样人工标注校准**：每周或每次 prompt 改动 sample 30-50 条对比 judge vs human，看 Cohen's κ；不达标就 freeze judge prompt。
3. **多 judge 投票 + 弃权**：低置信度直接 abstain 而不是塞默认 3 分。
4. **将 judge 分数和 retention / NPS 等行为指标做相关性分析**，不达 0.3 以上就承认这个 judge 不是真正的 quality proxy。

短期内项目没做到 1-4，只做到 fail-open + 维度拆分 + clamp。这是**工程开局合理的取舍**，但生产化必须补上。

### Q：Validator 当前覆盖哪些硬约束？

推荐回答：

`validate_hard_constraints()` 覆盖：

- 相邻活动时间冲突，考虑 `transport_duration_min`。
- 活动总费用超过预算。
- daily plans 天数超过 `DateRange.total_days`。

`validate_incremental()` 覆盖：

- 写预算时检查非正预算和已有 daily plans 是否超预算。
- 写日期时检查旅行天数至少 1 天，并调用 `check_feasibility()`。
- 写 daily plans 时检查时间冲突。

`validate_lock_budget()` 覆盖交通 + 住宿占预算比例，超过预算或超过 80% 时给出 warning。这里当前住宿晚数用 `total_days` 保守估算，不是最精确的酒店晚数模型；面试要主动说明。

### Q：Feasibility gate 做什么？

推荐回答：

`backend/harness/feasibility.py::check_feasibility()` 是规则型可行性预检，用目的地、预算、天数挡住明显不可能的计划。例如东京建议至少 3 天，日均预算严重不足会生成原因。它在 Phase 1→3 的 `before_phase_transition` 里触发，避免系统在明显不可行的输入上进入昂贵的规划阶段。

这不是完整旅行可行性模型，只是低成本“别浪费后续 LLM 和工具调用”的第一道闸。

### Q：ToolGuardrail 和 Validator 的区别是什么？

推荐回答：

ToolGuardrail 保护工具边界，Validator 保护计划状态。

ToolGuardrail 在工具执行前后看单个 tool call：

- 输入长度；
- 中英文 prompt injection pattern；
- 过去日期；
- 空 location；
- update_trip_basics 的负数或零预算；
- 搜索输出空结果、价格异常、缺关键字段。

Validator 看 `TravelPlanState` 的业务一致性：

- daily plans 是否冲突；
- 总费用是否超预算；
- 日期天数是否匹配；
- 交通住宿是否挤占预算。

一个是“这个工具调用能不能执行 / 结果是否可疑”，一个是“当前计划是否还能推进”。

### Q：STAR：你如何把质量检查从“用户事后发现”前移到 Agent 运行时？

推荐回答：

- **Situation**：早期旅行计划看起来能生成，但预算、时间冲突、骨架天数不匹配等问题可能到最终回答才暴露，用户已经等了一轮长规划。
- **Task**：我要把质量检查插到 Agent loop 的关键节点里，既不破坏工具协议，又能让模型下一轮看到可修正反馈。
- **Action**：我把 `validate_incremental` 放到 `after_tool_call`，把 soft judge 放到 `after_tool_result`，把 feasibility / hard constraints / judge threshold 放到 `before_phase_transition`；同时把工具执行中的 system feedback 放入 pending notes，下一轮 LLM 前 flush，避免破坏 `assistant.tool_calls -> tool result` 协议。
- **Result**：预算和时间冲突能在写入后马上进入 trace 和 internal task；Phase 1→3 可以挡住明显不可行计划；Phase 3→5 / 5→7 不再只是字段填满就推进，而是有质量检查和重试上限。

### Q：这套质量体系哪里还不符合最佳实践？

推荐回答：

主要短板有四个：

- Soft Judge 没有人类标注校准，不能证明分数和真实用户满意度强相关。
- Quality Gate 达到重试上限后会放行，这是 demo 友好但生产上需要更细策略，比如降级、请求用户确认或转人工。
- Guardrail 主要是规则和 pattern，不能覆盖所有 indirect prompt injection。
- Validator 的旅行知识仍偏粗，例如住宿晚数、跨城距离、票务开放时间、真实天气/营业状态还不是完整约束模型。

我会解释为：当前实现是“运行时质量骨架”，优先把检查点和可见性建起来；生产化再提高规则覆盖率和 judge 校准，而不是假装已经解决所有安全质量问题。

## 4. InternalTask 与 SSE 可靠性体验

### Q：InternalTask 解决什么问题？

推荐回答：

很多 Agent 工作不是用户显式调用的工具，但会消耗时间、影响上下文或决定流程，例如 memory recall、soft judge、quality gate、context compaction、reflection、Phase 5 orchestration、memory extraction。如果这些任务不可见，用户只会觉得“系统卡住了”；如果把它们伪装成真实工具，用户又会误解系统在调用外部服务。

`InternalTask` 给这些系统任务一个统一协议：

- `id`：用于前端合并生命周期更新。
- `kind`：任务类型。
- `label` / `message`：用户可见说明。
- `status`：pending / success / warning / error / skipped。
- `blocking` / `scope`：是否阻塞当前 turn，属于 turn / background / session。
- `related_tool_call_id`：关联真实工具卡。
- `result` / `error` / started_at / ended_at：用于 trace 和 UX。

代码：`backend/agent/internal_tasks.py`。

### Q：chat SSE 和 background internal-task SSE 怎么分工？

推荐回答：

有两条流：

- **chat SSE**：`/api/chat/{session_id}`，承载与当前回答强绑定的任务，例如 `memory_recall`、`soft_judge`、`quality_gate`、`context_compaction`、`reflection`、`phase5_orchestration`。
- **background internal-task SSE**：`/api/internal-tasks/{session_id}/stream`，承载回答结束后仍可能发生的后台任务，例如 `memory_extraction_gate`、聚合态 `memory_extraction`、`profile_memory_extraction`、`working_memory_extraction`。

前端 `ChatPanel` 通过 task id 合并生命周期更新，并在 stream 结束后补拉 `/api/internal-tasks/{session_id}`，防止后台任务在 chat done 后才完成时丢卡。

### Q：为什么真实工具的 `TOOL_RESULT` 要先于 Soft Judge 内部任务展示？

推荐回答：

用户看到工具卡结束，会认为真实外部动作或状态写入已经完成。如果 Soft Judge 卡插在 tool result 前面，用户会误以为 `save_day_plan` 或 `replace_all_day_plans` 还在执行。

项目的顺序是：真实 `TOOL_RESULT` 先结束工具卡，然后再显示 `soft_judge` internal task，语义是“行程已保存 -> 系统自动做质量评审”。这保持了用户心理模型，也让 trace 中真实工具和内部评审分开。

### Q：Agent Loop 会发哪些 SSE 事件？

推荐回答：

主要有：

- `text_delta`：模型文本。
- `tool_call` / `tool_result`：真实工具开始和结束。
- `internal_task`：记忆召回、质量评审、阶段推进检查等。
- `state_update`：写工具成功后返回新的 plan。
- `phase_transition`：阶段或 Phase 3 step 变化。
- `context_compression`：上下文压缩。
- `agent_status`：thinking、planning、parallel_progress 等状态。
- `memory_recall`：兼容性和可解释性的记忆遥测事件。
- `error`：错误码、retryable、can_continue、provider、model、failure_phase。
- `done`：run 完成或取消。
- keepalive ping：避免长任务期间连接看起来静默。

SSE 在这个项目里不是纯 UI 细节，而是 Agent runtime 的可解释性协议。

### Q：cancel 是怎么实现的？

推荐回答：

前端 `useSSE.cancel()` 先 abort 当前 fetch，再调用 `POST /api/chat/{session_id}/cancel`。后端把 session 里的 `_cancel_event` set 掉；AgentLoop 在 LLM 调用前、streaming chunk 处理时、工具执行前检查 `_check_cancelled()`。如果取消命中，会抛 `LLMError(failure_phase="cancelled")`，SSE 层把 run 标记为 `cancelled`，发 `done`，finally 调 `persist_run_safely()` 做保底持久化。

关键点是取消不是简单断开浏览器连接，而是尽量通知后端停止继续消耗 token 和工具调用。

### Q：continue 和 retry 有什么区别？

推荐回答：

retry 是重新发送上一条用户消息，风险是重复输出、重复工具调用、重复写状态。continue 是在可恢复中断后，基于当前 runtime messages 和 `RunRecord.continuation_context` 继续生成。

项目只在相对安全的进度允许 continue：

- `PARTIAL_TEXT`：已经输出部分文本，可以提示模型从断点继续，不重复已说内容。
- `TOOLS_READ_ONLY`：只读工具已经完成，但总结中断，可以基于已有工具结果继续。

不自动 continue：

- `PARTIAL_TOOL_CALL`：半截工具调用不安全。
- `TOOLS_WITH_WRITES`：可能已经有副作用，盲目续写可能重复提交或污染状态。

### Q：为什么流式输出中断后不能随便自动 retry？

推荐回答：

因为 provider 已经 yield 过部分文本或 tool call 后，retry 不是幂等的。模型可能再次输出同样文本、再次调用同一工具，甚至重复写状态。

所以 provider 层只在尚未 yield 任何数据的连接类错误上做自动 retry；一旦有 partial output，就交给 `run_agent_stream()` 发 `error` SSE，并根据 `IterationProgress` 判断 `can_continue`，让前端展示“继续生成”或“重新发送”。

这是生产 Agent 的基本原则：副作用发生后，恢复必须基于状态和轨迹，而不是简单重放请求。

### Q：当前 continue 机制有哪些 hardening 缺口？

推荐回答：

要主动承认：

- `can_continue` 目前基于 `IterationProgress`，但 `continuation_context` 只有在 `accum_text.strip()` 存在时才写入。极端情况下 read-only 工具完成后还没输出文本就中断，可能出现可继续但上下文不完整。
- continue 使用 system note 让模型续写，不是 Responses API 那种 provider-native `previous_response_id` / reasoning state continuation，恢复能力有限。
- 缺少 idempotency key / operation id，无法对写工具重复提交做系统级去重。
- 客户端断开、服务端 cancel、provider stream 断开还可以进一步细分 failure_phase。

合理表达是：当前机制已经比盲目 retry 安全，但还没有做到生产级 exactly-once side-effect recovery。

### Q：Phase 5 Day Worker 支持 cancel / continue 吗？

推荐回答：

不支持完整的用户级 cancel / continue。Day Worker 是内部并行子任务，没有用户交互、没有文本流式 UX，也不处理用户消息；它通过 timeout、max_iterations、重复查询抑制、forced emit、JSON 修复和 Orchestrator re-dispatch 控制收敛。

这不是主 AgentLoop 的同等级运行时。生产化如果要让用户取消 Phase 5 并行，应把 cancel token 传到 Orchestrator 和每个 Worker；如果要 continue worker，需要让 candidate artifact、worker messages、attempt id 和已完成工具结果可恢复。当前项目只做了 run-scoped artifact 存储，不是完整 worker resume。

## 5. 成本、延迟与扩展性

### Q：当前成本如何估算？

推荐回答：

`SessionStats` 记录 provider usage chunk 里的 input / output tokens，再用 `backend/telemetry/stats.py` 的本地 `_PRICING` 表按每百万 token 估算美元成本。`build_trace()` 还会按 model 给 `by_model.cost_usd`。

但这只是调试估算，不是账单系统：

- 价格表硬编码，可能落后 provider 实际价格。
- 没有记录 cached input tokens，不能算 prefix cache 折扣。
- 没有 per-user budget、配额、报警。
- 没有把成本和 tenant / run / feature flag 关联落库。

面试时可以说：现在成本已经可见，但生产化还需要接 provider 实际 usage 字段、价格版本和预算治理。

### Q：Phase 5 并行是降低成本还是降低延迟？

推荐回答：

主要降低 wall-clock latency，不一定降低 token 成本。每个 Day Worker 都有自己的 LLM loop，总 token 可能比串行更多；并行把 N 天的规划同时跑，用户等待时间下降，但总调用量可能上升。

它适合“天数多、用户等待成本高、每日任务相对独立”的场景。生产化应该按天数、预算、模型等级、provider health 动态决定是否并行，而不是一刀切。

### Q：Shared prefix / KV-cache 能省什么？

推荐回答：

Phase 5 Day Worker 的 shared prefix 设计目标是让所有 worker 的 system message 字节级一致，只把 day suffix 放到 user message，从而提高 provider 侧 prefix cache 命中。理论收益主要是减少 attention 重算延迟和 provider 计算成本；部分 provider 也会对 cached input tokens 给价格折扣。

项目做了几件事：

- `build_shared_prefix()` 白名单过滤 trip_brief，只保留全局硬约束。
- preferences 按 key 排序，保证字节稳定。
- soft day constraints 放 day suffix，不污染 shared prefix。
- “第 N 天”任务信息放 user message，避免 system message 每个 worker 不同。

但要诚实说：项目当前还没把 provider `cached_input_tokens` 接进 `SessionStats`，所以不能声称实际命中率，只能说这是 cache-aware prompt 结构设计。

### Q：如何定位成本异常？

推荐回答：

先看 TraceViewer Summary：

- `by_model`：哪个模型调用次数和 token 高。
- phase grouping：是 Phase 3 搜索膨胀，还是 Phase 5 并行 workers 膨胀。
- iteration timeline：是否出现大量 pure thinking 或重复读工具。
- tool duration：是否某个外部工具慢。
- compression event：是否上下文过长导致频繁压缩。
- memory recall：是否候选过多或 reranker telemetry 异常。

然后按原因处理：重复搜索靠 SearchHistoryTracker 和 eval；prompt 膨胀靠 compaction 和更短 tool result；Phase 5 成本靠并行阈值和模型路由；memory 成本靠 gate 和 top_k。

### Q：如果要做生产级成本治理，你会怎么设计？

推荐回答：

我会做五件事：

- **预算模型**：per-user / per-session / per-run budget，按模型和工具分桶。
- **模型路由**：小模型处理 memory extraction、recall gate、简单 judge；强模型处理复杂规划和冲突修复。
- **缓存观测**：记录 prompt_tokens、cached_tokens、completion_tokens、cache hit rate。
- **动态并行**：Phase 5 根据天数、预算、历史失败率、当前排队情况决定串行还是并行。
- **报警和降级**：成本或延迟超阈值时降级工具深度、减少候选数、请求用户确认继续。

这比单纯“换便宜模型”更稳，因为 Agent 成本来自多轮工具轨迹和上下文结构，不只来自模型单价。

## 6. 生产化优先级

### Q：如果要上线给 100 个内测用户，第一批改什么？

推荐回答：

我会优先补最容易把单次失败放大成事故的部分：

1. **多用户安全底座**：auth、tenant isolation、rate limit、session lock、audit log。
2. **持久化 trace / stats**：把 `SessionStats`、trace events、internal tasks 落库，支持跨重启复盘。
3. **写工具幂等**：operation id / idempotency key，避免 retry / continue / 网络恢复导致重复写状态。
4. **Phase 5 fallback hardening**：高失败率时真正串行接管；re-dispatch 后 error severity 未解决时不能静默提交。
5. **外部工具可靠性**：timeout、circuit breaker、provider health、quota 错误分类。
6. **成本治理**：per-user budget、模型路由、cache telemetry、报警。
7. **安全合规**：PII redaction、数据最小化、第三方工具审计、敏感操作审批。

### Q：STAR：如果只有两周做生产化 hardening，你会怎么排？

推荐回答：

- **Situation**：当前系统已经能跑完整旅行规划，但它仍是单进程 demo 形态：trace/stats 在内存，auth 缺失，Phase 5 fallback 不是完整串行接管，continue 没有写工具幂等。
- **Task**：两周内目标不是“做成完整平台”，而是防止内测用户遇到失败后无法恢复、无法复盘、无法控制成本。
- **Action**：第一周补持久化 trace/stats、session lock、write idempotency、Phase 5 fallback 阻断策略；第二周补基础 auth/rate limit、provider timeout/circuit breaker、成本 budget、核心 eval CI gate。
- **Result**：即使某次 planning 失败，也能知道是哪一轮、哪个工具、哪个 worker、哪个状态字段导致；用户不会因为刷新或重试重复写状态；团队能用 eval 和 trace 判断修复是否真的改善。

### Q：为什么不优先继续做 memory 或更多 Agent？

推荐回答：

因为生产化优先级要看风险，而不是看技术新鲜度。当前 v3 memory 已经有 Stage 0-4、policy、reranker-only eval，虽然还能优化，但它不是最容易导致线上事故的部分。更高风险的是：

- trace 不持久导致无法复盘；
- 写工具不幂等导致重复状态；
- 外部工具超时和 quota 导致长时间等待；
- 没有 auth / tenant isolation 导致数据边界不清；
- Phase 5 fallback 没真正接管导致用户拿不到可用计划。

这体现工程判断：先补 failure containment，再做能力扩展。

### Q：如何把当前 trace 系统升级成生产监控？

推荐回答：

路线是：

1. **事件落库**：LLM call、tool call、state change、validation error、judge score、memory recall、internal task 全部以 append-only trace_events 存储。
2. **关联键统一**：每个 event 带 session_id、run_id、trip_id、context_epoch、phase、iteration、tool_call_id。
3. **OTel + metrics**：把 latency、error rate、token、cost、tool failures、worker failures 变成 counters / histograms。
4. **Dashboard**：按 phase、model、tool、tenant 展示 p50/p95 latency、cost、failure rate、quality gate block rate。
5. **报警**：provider error spike、tool timeout spike、Phase 5 worker failure rate、memory false recall、cost budget breach。
6. **trace grading**：抽样或 CI 对 trace 运行 graders，发现工具越权、危险轨迹、重复搜索。

### Q：你会如何做线上 prompt / model A/B rollout？

推荐回答：

需要三层保护：

- **Feature flag**：prompt version、model route、tool description version 可按 session / tenant 灰度。
- **Eval gate**：上线前跑 golden + reranker-only + stability 子集；上线后抽样 trace grading。
- **指标回滚**：比较 pass rate、tool error rate、quality gate retry、cost、latency、user cancel rate、manual thumbs down。

核心原则：Agent 改动不能只看“回答更自然”，必须保证 phase、state、tool trajectory 和安全边界不倒退。

## 7. 安全、隐私与合规

### Q：当前有哪些 guardrail？

推荐回答：

当前主要是工具级 guardrail：

- 英文和中文 prompt injection pattern。
- 单字段输入长度上限。
- 过去日期。
- 空 location。
- `update_trip_basics` 的负数或零预算。
- 搜索工具输出空结果、异常高价、缺关键字段。
- 规则可通过 `guardrails.disabled_rules` 禁用。

还有状态级安全边界：

- Phase / step 过滤工具，模型不能随便看到全部写工具。
- read / write side effect 分离，写工具顺序执行。
- `TravelPlanState` 是当前事实权威，工具结果不能直接覆盖状态。
- MemoryPolicy 处理 PII 和低置信度长期偏好。

### Q：为什么 Agent guardrail 不能只看最终输出？

推荐回答：

因为风险发生在轨迹中间。模型可能先调用了不该调用的工具、传了敏感参数、把外部网页里的注入当成指令、写错了状态，最后再用自然语言包装成“已为你处理好”。如果 guardrail 只看最终回答，就已经晚了。

所以 Travel Agent Pro 的 guardrail / validator / trace 都覆盖工具输入、工具输出、状态变化和 phase transition，而不仅是最后一段 assistant text。

### Q：web_search / 小红书结果里有 indirect prompt injection 怎么防？

推荐回答：

当前有几层防线：

- 工具结果以 tool result 形式进入上下文，system prompt 明确工具返回是不可信内容。
- 写工具必须经过 phase gate 和 schema 校验，外部文本不能直接改 `TravelPlanState`。
- ToolGuardrail 会检测常见 prompt injection pattern。
- TraceViewer 会记录 tool arguments / result preview / state changes，便于复盘。

但我会主动承认：当前还没有完整 indirect injection golden suite，也没有对所有 tool output 做 quarantine / structured extraction。生产化要补：

- 搜索结果中嵌入恶意指令的 eval cases；
- tool result 进入 prompt 前做模式扫描和隔离；
- 对外部长文本只抽取结构化字段，不直接让其影响工具选择；
- 高风险写操作必须 human approval。

### Q：MCP / connectors 在生产里有什么安全风险？

推荐回答：

MCP 和 connectors 标准化了工具接入，但风险也集中：

- 第三方 MCP server 可能不可信。
- 工具 schema 可能要求过多敏感参数。
- tool output 可能包含 prompt injection。
- read/write action 可能触达真实外部系统。
- 数据一旦发给第三方 server，会受第三方数据保留和地域政策影响。

我会按四条线治理：

- 只接官方或可信 server，避免不明 aggregator。
- 工具做 read/write 风险分级，敏感 action require approval。
- 记录发给第三方工具的参数摘要和返回摘要，满足审计和数据最小化。
- tool output 永远按不可信内容处理，不能覆盖 system/developer 指令。

### Q：记忆系统如何处理 PII？

推荐回答：

`MemoryPolicy` 会丢弃 payment、membership 等 denied domains，识别护照号、身份证、长数字、手机号/邮箱等 PII，并 redaction 或 drop。低置信度长期偏好不会直接进入 active profile，而是 pending 或不保存。

另外，当前旅行事实不进入长期记忆。长期 memory 只保留稳定偏好、历史经验和 episode slices；当前日期、预算、目的地等权威事实来自 `TravelPlanState`，避免把一次性事实污染长期画像。

### Q：如果面试官问合规，你怎么答？

推荐回答：

我会说当前项目是本地 demo / 面试项目，还没有完整合规体系，不能假装已经满足企业合规。生产化至少要补：

- auth、tenant isolation、RBAC；
- 数据保留和删除策略；
- PII 分类、脱敏、最小化；
- 第三方工具数据出境 / 数据地域说明；
- audit log；
- secret management，不把 API key 放配置文件；
- 用户导出和删除个人记忆；
- 高风险外部操作 human approval。

这比泛泛说“我们会加安全”更可信，因为它明确区分了已实现 guardrail 和未实现的合规底座。

## 8. 已知短板与客观解释

### Q：当前可观测性最大的短板是什么？

推荐回答：

最大短板是 trace / stats 没持久化。`SessionStats` 在内存中，重启后不可恢复；`/api/sessions/{id}/trace` 不会 restore session，只查内存。它适合开发调试，不适合线上事故复盘。

客观解释是：项目先保证 plan 和 append-only messages 持久化，避免用户交付物和历史丢失；trace 持久化是下一阶段生产化任务。不能把“有 TraceViewer”说成“生产 trace 平台已完成”。

### Q：当前 eval 最大的短板是什么？

推荐回答：

eval 已经有 golden、stability、reranker-only，但还缺 trace grading 和人类校准。它能检查 phase、state、tool calls、memory fields，但对“这个行程是否真的高质量”仍依赖 Soft Judge，而 Soft Judge 没有人类标注 agreement。

生产化方向是：确定性 trace rubric + LLM judge + 人工抽样校准三者结合。

### Q：当前 Phase 5 并行 fallback 有什么 gap？

推荐回答：

要分四个具体的 gap 讲，全部带源码行号：

1. **`fallback_to_serial` 名实不符**：`backend/agent/phase5/orchestrator.py:673-683` 在并行失败率超过 50% 时只 emit warning + `return`，**没有真正用串行 AgentLoop 接管同一个 run**。配置项 `Phase5ParallelConfig.fallback_to_serial=True` 给的是"心理安全感"，运行时实际拿到的是部分结果 + 提示。
2. **Worker `TimeoutError` 和 generic `Exception` 不设 `error_code`**：`backend/agent/phase5/day_worker.py:548-565` 这两个分支只写 `error` 文本，下游观测拿不到结构化 code，无法在 dashboard 里把"timeout"和"模型 5xx"区分聚合。已有的结构化 code（`JSON_EMIT_FAILED`、`REPEATED_QUERY_LOOP`、`RECOVERY_CHAIN_EXHAUSTED`、`NEEDS_PHASE3_REPLAN`）只覆盖 4 类业务错误。
3. **Re-dispatch 后 unresolved error 仍可能继续**：再调度一次后如果还有 error severity issue，只是 log unresolved，dayplans 仍可能被暴露给 handoff。生产化应让 unresolved error 阻断 phase transition。
4. **前端类型契约 bug**：`backend/agent/phase5/orchestrator.py:808` 实际会把 worker status 设为 `"redispatch"`，但 `frontend/src/types/plan.ts:239` 的 `ParallelWorkerStatus.status` union 不包含这个值。前端 TypeScript 检查不出来，只是 runtime 会拿到一个不在枚举里的字符串，UI 展示分支可能落到默认 fallback。

面试时要说这是 hardening backlog，而不是把 fallback_to_serial 说成已完全实现。

### Q：前端 `ParallelWorkerStatus.status` 的类型契约 bug 是怎么发现的？

推荐回答：

读代码做对照时发现的，是个典型"后端 emit 一个值，前端类型 union 漏一个值" 的契约不一致：

- 后端 `backend/agent/phase5/orchestrator.py:808` 在 re-dispatch 时把 worker status 设为 `"redispatch"`。
- 前端 `frontend/src/types/plan.ts:239` 的 `ParallelWorkerStatus.status` union 包含 `"queued" | "running" | "completed" | "failed" | "submitted"` 等，但**漏了 `"redispatch"`**。

后果有三层：

1. TypeScript 在编译期不报错（因为后端 SSE 是 runtime 数据），前端 narrowing 落到默认分支，UI 可能不显示重试状态。
2. 任何基于 status 做的 telemetry / 埋点都会把 redispatch 误归类。
3. 这暴露的是更系统的问题：**后端枚举和前端 type union 没有 single source of truth**。生产化做法是从后端导出 schema（OpenAPI / TypeScript codegen / proto），前端不再手写 union。

短期补丁是把 union 加上 `"redispatch"`；长期要建 codegen 链路。这是面试讲"我读了代码"和"我懂前后端契约工程化"的一个很自然的案例。

### Q：当前 `max_llm_errors` 是否真的生效？

推荐回答：

不能说已经完整生效。`AgentLoopLimits` 里有 `max_llm_errors` 配置和校验，但主 loop 目前没有按它累计 LLM error 并熔断。真实 LLM 错误由 provider 分类后冒泡到 `run_agent_stream()`，API 层发 `error` SSE，设置 run status。这个字段更像预留维度，runtime enforcement 需要补。

这个回答能体现你读过代码，而不是只读配置。

### Q：当前 token / cost 统计有什么不准确？

推荐回答：

三类：

- token budget 估算仍有粗略路径，context engineering 中提过 `len // 3` 这类估算不等同 provider tokenizer。
- 成本估算用本地价格表，未必等于实时账单。
- cached input tokens 未接入，所以无法衡量 KV-cache 命中和折扣。

生产化要接 provider usage 原始字段、tokenizer、价格版本、cached token telemetry。

### Q：当前 trace 是否可能泄露敏感信息？

推荐回答：

有风险。现在 tool arguments / result preview 会截断，但并不是完整 PII scrub。OpenTelemetry events 和 TraceViewer preview 都可能包含用户目的地、日期、预算、偏好，甚至第三方工具返回的内容片段。

生产化要做：

- trace 写入前 PII redaction；
- 按字段白名单记录，而不是任意 `str(args)`；
- trace retention policy；
- tenant-based access control；
- 对第三方 URL token 做 scrub，failure-analysis 脚本里已经有类似敏感 query token redaction 的思路。

### Q：你如何回答“这些短板是不是说明项目不成熟”？

推荐回答：

我会承认它不是完整生产平台，但短板并不等于方向错。这个项目已经把 Agent 生产化的关键边界建出来了：状态权威、writer contract、Phase gate、可观测 trace、eval harness、InternalTask UX、SSE recovery、guardrail。短板主要集中在“规模化运行”：持久化 trace、权限、多租户、成本治理、完整安全套件、幂等恢复。

这说明项目从 demo 走向生产还有明确路线，而不是只有 prompt 和聊天 UI。

## 9. 高频压迫题

### Q：如果最终答案是对的，但中间调用了危险工具，算成功吗？

推荐回答：

不算。Agent 的质量对象包括 trajectory。危险工具调用说明系统权限、guardrail 或 prompt 边界失败，最终答案对只是侥幸。eval 应该有 `tool_not_called`、tool argument grader 和 trace grading 来捕捉这种情况。

### Q：如果模型工具选择正确，但最终答案不好，eval 怎么设计？

推荐回答：

拆开评估：tool selection / arguments 通过 trajectory eval；最终行程质量通过 final-state eval 和 judge / human rubric。这样能判断是工具轨迹没问题但 synthesis 差，还是工具结果本身不足。

### Q：如果 trace 很多，会不会增加系统复杂度？

推荐回答：

会，所以要分层。开发态可以记录详细 preview；生产态要采样、字段白名单、PII scrub、retention 和聚合指标。复杂度不是理由，Agent 没有 trace 就无法可靠 debug 和 eval。

### Q：InternalTask 会不会让用户觉得系统太复杂？

推荐回答：

设计上 InternalTask 不展示内部实现细节，只展示用户能理解的系统任务，如“记忆召回”“行程质量评审”“阶段推进检查”。它解决的是长等待的信任问题，而不是把 debug log 倒给用户。开发者要看细节走 TraceViewer。

### Q：为什么不直接用 LangSmith / LangFuse？

推荐回答：

可以接，但不能替代项目内部 trace schema。Travel Agent Pro 的关键语义是 phase、state_changes、memory_recall、quality_gate、plan writer、context_epoch，这些必须由业务运行时先结构化出来。外部平台适合作为 backend 和 UI 增强，不应该决定状态权威和工具写入边界。

### Q：如果接 OpenAI Agents SDK 的 tracing，现有 TraceViewer 还需要吗？

推荐回答：

需要，除非 SDK trace 能完整表达业务字段。SDK tracing 能覆盖 model calls、tool calls、handoffs、guardrails；但 `TravelPlanState` diff、Phase 3 step、quality gate retry、memory reranker per-item scores、context_epoch 仍是业务语义。迁移策略应该是保留内部语义 trace，再映射到平台 trace，而不是丢掉业务可解释性。

### Q：为什么不是所有质量问题都阻断？

推荐回答：

因为阻断过多会把 Agent 变成无法完成任务的系统。硬约束必须阻断，例如时间冲突、天数不匹配、预算严重不可能；软质量建议应该 fail-open，例如地理顺路性稍差、个性化不足。项目用 Validator / Quality Gate / Soft Judge 分层，就是为了区分“不能提交”和“可以提交但建议改进”。

### Q：如果用户点停止后，已经写入的状态怎么办？

推荐回答：

写工具成功后 SSE 层会立即 `state_mgr.save(plan)` 并更新 session meta；finally 里还有 `persist_run_safely()` 做保底持久化。所以用户停止或连接断开后，已经成功写入的状态不应该丢。风险在于未完成的 run 和 trace stats 可能不完整，这也是为什么写工具幂等和 trace 落库是生产化优先级。

### Q：这个项目最能展示生产 Agent 理解的点是什么？

推荐回答：

不是某个 prompt，而是运行时边界：LLM 可以自由做开放推理，但所有副作用必须经过工具 schema、phase gate、writer contract、validator、trace 和持久化。模型每一步不是黑盒，而是可观察、可评分、可恢复的事件。这是 Agent 从 demo 走向生产的核心。

### Q：你怎么看 multi-agent 路线？为什么这个项目没走那条路？

推荐回答：

我的判断和 Anthropic 在 [Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents) 里讲的一致：**先尽最大可能用 augmented single LLM（LLM + tools + retrieval + memory），只有当任务确实需要并行专才协作时才上 multi-agent**。Multi-agent 不是越多越好，它带来的是：

- **协调成本**：消息总线、role contract、handoff 协议、冲突解决都要新建。
- **状态权威割裂**：多个 agent 都要写状态时，并发和一致性问题立刻冒出来。
- **Eval 难度爆炸**：每个 agent 单独 eval + 协作 trace eval + 端到端，维度乘积。
- **成本和延迟非线性增长**：N 个 agent 各自 LLM loop，token 成本至少 N 倍。

Travel Agent Pro 当前的取舍很自洽：

- **主流程是 augmented single LLM + phase gate**：phase 不是 agent 切换，而是同一个 AgentLoop 在不同阶段加载不同工具集和 prompt 段，状态权威始终在 `TravelPlanState`。
- **Phase 5 是受控的 worker 并行，不是 multi-agent**：每个 day worker 只负责一天的候选 plan，submit 后由主 loop 统一拼接。worker 之间不会互发消息、不会重新协商 role。这是"data parallel"而不是"agent collaboration"。
- **Memory pipeline 是 staged LLM call**，不是独立 agent。Stage 0/1/2/4 各自只有一个明确职责，调用方仍是主 loop。

什么时候才值得上 multi-agent？我的判断是三个条件同时成立：

1. 子任务**真正异质**（不同领域专才，不是同模板并行）。
2. 子任务**必须协商**而不是简单 fan-out / reduce。
3. 团队已经有成熟的 single-agent eval / trace / cost 基础设施。

不满足这三条强行上 multi-agent，就是工程负债。

## 10. STAR 案例

### Q：讲一个你在可观测性上做工程取舍的例子。（STAR）

推荐回答：

- **Situation**：早期 debug 只能看日志和最终回答，无法解释为什么 Phase 没推进、工具为什么重复、memory 为什么误召回。
- **Task**：我要让 Agent 的中间轨迹可见，且前端能按旅行规划语义理解，而不是只看底层 spans。
- **Action**：我引入 `SessionStats` 记录 LLM/tool/memory/recall telemetry，用 `build_trace()` 聚合成 iteration，并按 significance 分类；前端 `TraceViewer` 按 phase 分组，展示 tokens、cost、duration、tool side_effect、state diff、validation error、judge score。
- **Result**：debug 从“猜 prompt 哪儿错了”变成“看第几轮哪个工具改了哪个状态”；memory zero-hit、reranker fallback、quality gate warning 都能在 trace 里定位。

### Q：讲一个你在 eval 上做工程取舍的例子。（STAR）

推荐回答：

- **Situation**：Memory recall pipeline 增加 Stage 3 semantic lane 和 Stage 4 reranker 后，端到端 case 失败很难判断是 gate、query plan、candidate generation 还是 reranker 的问题。
- **Task**：我要让 reranker 的质量可以独立回归，避免每次都被上游 LLM 抖动干扰。
- **Action**：我做了 reranker-only eval：YAML 固定 user_message、plan、retrieval_plan、候选集和 config，只断言 selected_item_ids、fallback、final_reason、per-item reason。
- **Result**：Stage 4 的行为可以 deterministic 地回归；自然语言偏好变化、冲突 profile、recent preference、weak candidates 都有专项 case，失败能直接归因到 reranker。

### Q：讲一个你在可靠性上做工程取舍的例子。（STAR）

推荐回答：

- **Situation**：流式 LLM 调用可能在输出一半时断开。如果简单 retry，可能重复文本、重复工具调用，甚至重复写状态。
- **Task**：我要区分哪些中断可以继续，哪些必须让用户重新发送或人工处理。
- **Action**：我用 `IterationProgress` 标记 NO_OUTPUT、PARTIAL_TEXT、PARTIAL_TOOL_CALL、TOOLS_READ_ONLY、TOOLS_WITH_WRITES；SSE 层只对 PARTIAL_TEXT 和 TOOLS_READ_ONLY 设 `can_continue=true`，continue endpoint 不新增用户消息，只注入恢复提示。
- **Result**：用户看到明确的“继续生成”或“重新发送”；系统避免对写状态工具做不安全自动重放，同时 finally 仍做保底持久化。

## 11. 参考资料

- OpenAI Agent evals: https://platform.openai.com/docs/guides/agent-evals
- OpenAI Trace grading: https://platform.openai.com/docs/guides/trace-grading
- OpenAI Agents / AgentKit: https://platform.openai.com/docs/guides/agents
- OpenAI Agents SDK: https://platform.openai.com/docs/guides/agents-sdk/
- OpenAI Responses API: https://platform.openai.com/docs/api-reference/responses
- OpenAI MCP / connectors safety: https://platform.openai.com/docs/guides/tools-remote-mcp
- OpenAI Safety in building agents: https://platform.openai.com/docs/guides/agent-builder-safety
- Anthropic Building effective agents: https://www.anthropic.com/engineering/building-effective-agents
- Anthropic Demystifying evals for AI agents: https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- Anthropic Writing effective tools for agents: https://www.anthropic.com/engineering/writing-tools-for-agents
- Google Agent2Agent Protocol announcement: https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/
- OpenTelemetry Traces: https://opentelemetry.io/docs/concepts/signals/traces/
