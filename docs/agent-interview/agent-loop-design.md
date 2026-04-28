# Agent Loop 设计面试问答

> 范围：本文件聚焦 Travel Agent Pro 的 Agent Loop、工具协议、阶段推进、恢复机制、SSE、trace/eval，以及与 OpenAI Responses API / Agents SDK 趋势的关系。它不是背诵稿，而是面试时可组合使用的技术表达素材。

## 0. 速记主线

### Q：如果用一句话解释 Travel Agent Pro 的 Agent Loop，你会怎么说？

推荐回答：

Travel Agent Pro 的 Agent Loop 是一个自研的、状态机约束下的 think-act-observe runtime：LLM 负责开放推理和选择工具，Python 控制面负责工具协议、状态写入、阶段 gate、上下文重建、取消/继续、SSE 事件、trace 和 eval。它不是把用户输入转发给模型，而是把模型的每一步行动放进可观察、可回滚、可评估的工程边界里。

代码主线：

- `backend/agent/loop.py::AgentLoop.run`
- `backend/agent/execution/llm_turn.py::run_llm_turn`
- `backend/agent/execution/tool_batches.py::execute_tool_batch`
- `backend/agent/execution/phase_transition.py::detect_phase_transition`
- `backend/api/orchestration/chat/stream.py::run_agent_stream`

### Q：Agent Loop 的 mental model 是什么？

推荐回答：

我按四层讲：

1. **Think**：`before_llm_call` hook 先 flush pending system notes、做 compaction、注入 reflection，然后 LLM 以 streaming 方式输出文本和 tool calls。
2. **Act**：如果有 tool calls，loop 先把 assistant tool_calls message 写入 runtime messages，再执行工具；读工具可并行，写工具顺序执行。
3. **Observe**：工具结果以 `Role.TOOL` 追加回消息历史，触发 validator、soft judge、state update、trace/stats，再把 tool result 通过 SSE 发给前端。
4. **Transition**：写状态工具成功后统一检测 phase / Phase 3 step 是否变化；变化时先 flush 历史，再重建短 runtime context，避免旧阶段上下文污染新阶段。

一句话总结：LLM 做“下一步该干什么”，loop 做“这一步是否合法、是否持久化、是否推进状态、是否还能恢复”。

## 1. Think-Act-Observe 主循环

### Q：`AgentLoop.run()` 的核心循环是什么？

推荐回答：

它不是简单的 `while True` 调模型，而是每个 iteration 固定走一条协议化路径：

1. 根据 `plan.phase` 和 `phase3_step` 获取当前可用工具。
2. 检查是否应直接进入 Phase 5 并行 Orchestrator。
3. 检查 cancel。
4. 调 `run_llm_turn()`，流式收集 `text_delta`、`provider_state_delta`、`tool_call_start` 和 usage。
5. 如果没有 tool call，判断是否需要 state repair hint；不需要则 `DONE`。
6. 如果有 tool call，追加 assistant tool_calls message。
7. 调 `execute_tool_batch()` 执行工具并追加 tool result。
8. 调 `detect_phase_transition()`，只在完整工具批次之后检测一次阶段变化。
9. 如果 phase 或 Phase 3 step 改变，触发 context rebuild 并继续下一轮。
10. 超过 `max_iterations` 后返回“达到最大循环次数”的文本提示并结束。

这个循环的核心价值是把模型的一次“想法”拆成可审计的 trajectory：模型输出了什么、调用了哪个工具、工具返回什么、状态是否变化、为什么推进或没推进。

### Q：为什么说它是 Agent，而不是 ChatGPT wrapper？

推荐回答：

ChatGPT wrapper 的核心是“用户输入 -> 模型输出”。这里的核心是“用户输入 -> 运行时决策 -> 工具执行 -> 状态变更 -> 再决策”。LLM 不是只生成文本，而是在 `ToolEngine` 暴露的工具空间里选择下一步动作；所有写入必须落到 `TravelPlanState`；阶段推进由 `PhaseRouter` 和质量门控决定；前端展示的是文本、工具卡、内部任务、状态更新和 trace，而不是一段最终答案。

所以它更接近 code-first agent runtime：模型负责不确定推理，代码负责不可违反的状态、协议和可观测性。

### Q：`run_llm_turn()` 做了哪些事情？

推荐回答：

`run_llm_turn()` 是单轮 LLM 调用的封装。它先触发 `before_llm_call` hook：flush pending notes、压缩上下文、更新 system/runtime messages。然后它发出 `agent_status`，必要时发出 `context_compaction` internal task；再注入 reflection；最后调用 provider 的 `chat(..., stream=True)`。

在 streaming 中，它会：

- 收集文本增量到 `text_chunks`；
- 记录 provider-specific state，比如 DeepSeek reasoning content；
- 收集 tool call start，补上工具 human label；
- 维护 `IterationProgress`，供中断后的 `continue` 判断使用；
- 每个 chunk 前检查 cancel，避免用户点停止后继续消耗 token。

它的返回值 `LlmTurnOutcome` 是 loop 后续决策的事实来源：文本、tool calls、provider state、progress、下一轮 iteration index、Phase 3 step 标记。

### Q：为什么 phase transition 要在工具批次之后统一检测，而不是每个工具后立刻检测？

推荐回答：

因为模型可能一次发出多个 tool calls，尤其是“补齐基础字段”或“候选池 + shortlist”这种组合动作。如果每个工具之后都立刻 transition，会让后续工具运行在已经改变的阶段语境下，甚至让同一组 assistant.tool_calls 被截断。

当前设计是：完整执行一组 tool calls，追加全部 tool result，然后 `detect_phase_transition()` 只做一次阶段判断。这样模型看到的是原子 observe 结果，phase gate 看到的是一批写入后的稳定状态。

## 2. Tool Calls 协议

### Q：项目内部的 tool call 数据结构是什么？

推荐回答：

内部协议在 `backend/agent/types.py`：

- `ToolCall`：`id`、`name`、`arguments`、`human_label`。
- `ToolResult`：`tool_call_id`、`status`、`data`、`metadata`、`error`、`error_code`、`suggestion`。
- `Message`：`role`、`content`、`tool_calls`、`tool_result`、`provider_state`、`incomplete` 等。

这个结构故意做成 provider-neutral。OpenAI Chat Completions 会转成 `assistant.tool_calls` 和 `role=tool`；Anthropic 会转成 `tool_use` / `tool_result` content block；将来迁移 Responses API 时，也可以映射到 `function_call` / `function_call_output` item。

### Q：你如何保证 `assistant.tool_calls -> tool result` 协议不被破坏？

推荐回答：

核心规则是：只要 assistant message 带了 tool_calls，后面必须紧跟对应数量的 tool result，中间不能插入 system、assistant 或 user message。否则 OpenAI/Anthropic 这类 provider 都可能报协议错误。

项目有三层保护：

1. `AgentLoop.run()` 在工具执行前先 append assistant tool_calls message。
2. `execute_tool_batch()` 对每个 tool call 追加 `Role.TOOL` result，即使 backtrack 后跳过后续工具，也会为剩余 tool_calls 生成 `skipped` result，保持组完整。
3. 工具执行期间产生的实时约束检查不会直接 append system message，而是进入 `_pending_system_notes`，下一轮 LLM 前再 flush。

这不是形式主义。`backend/tests/test_parallel_tool_call_sequence.py` 明确回归过一个真实问题：系统反馈如果插入在多个 tool responses 中间，会导致网关返回 400。

### Q：STAR：你如何修复 pending system notes 相关的 tool_calls 协议 bug？

推荐回答：

- **Situation**：一次会话中，LLM 在同一轮发出多个 plan-writing tool calls，其中一个工具触发实时约束检查。旧逻辑会把 `[实时约束检查]` system message 直接插到第一个和第二个 tool response 之间，破坏 provider 要求的 tool_calls 连续协议。
- **Task**：既要保留 validator 的系统反馈，让下一轮 LLM 能看到修正建议，又不能破坏 `assistant.tool_calls -> tool results` 的原子序列。
- **Action**：我引入 `push_pending_system_note()` / `flush_pending_system_notes()`：工具执行期间只把 system note 缓存在 session 的 `_pending_system_notes`，`before_llm_call` 时再 flush 到 messages。并加了 `test_parallel_tool_call_sequence.py`，断言每个 assistant.tool_calls 后面紧跟完整 tool messages。
- **Result**：工具协议和实时反馈解耦；并行或批量 tool_calls 不再被 system 注入打断，模型下一轮仍能观察到约束错误。

### Q：读工具和写工具在执行上有什么差别？

推荐回答：

项目用 `ToolDef.side_effect` 区分读写。`side_effect != "write"` 的连续读工具可以通过 `tool_engine.execute_batch()` 并行执行，并在结果 metadata 里写入 `parallel_group`；写工具属于 `PLAN_WRITER_TOOL_NAMES`，会顺序执行，避免多个状态 mutation 并发写 `TravelPlanState`。

要注意一个细节：模型可以在同一 assistant turn 里返回多个 tool_calls，这叫“多个 tool calls”；但不等于所有工具都并发执行。项目只并发 read-only 工具，write tools 即使来自同一个 tool_calls group，也按顺序执行，并保持 tool result 在消息历史中连续。

### Q：为什么工具错误要返回 `error_code` 和 `suggestion`？

推荐回答：

因为 Agent 的 observe 不只是“工具失败了”，还要告诉模型怎么修。`ToolResult` 里保留 `error_code` 和 `suggestion`，比如 `REDUNDANT_SEARCH` 会提示模型换查询方向或根据已有信息推进，`GUARDRAIL_REJECTED` 会说明输入被拒绝原因。这样错误结果既能进入下一轮 LLM prompt，也能进入前端工具卡和 TraceViewer。

这比抛异常更适合 agent loop：工具失败通常是可恢复的观察结果，只有 provider 错误、协议错误或系统异常才应该终止 run。

### Q：项目如何处理重复搜索？

推荐回答：

主 Agent Loop 里有 `SearchHistoryTracker(max_size=20)`。对 `web_search`、`xiaohongshu_search_notes`、`quick_travel_search` 等搜索工具，按 query/keyword 归一化计数；同一查询第 3 次出现时不再执行真实搜索，而是返回 `REDUNDANT_SEARCH` 的 skipped result。

这个策略不是为了“禁止多查”，而是防止模型在没有新信息时陷入搜索死循环。它把“重复查同一句话”变成一个可观察的工具结果，让 LLM 换关键词、换工具或写入已有结论。

## 3. State Repair Hints

### Q：如果模型只在正文里说“已规划”，但没有调用写状态工具，怎么办？

推荐回答：

项目用 state repair hints 修复“文本像状态，但状态没写入”的问题。`AgentLoop` 在一轮 LLM 没有 tool call 时，会拿完整 assistant text 检查当前 phase 是否出现应写未写的信号；如果命中，就追加 system repair message，让模型下一轮必须调用正确写工具。

例子：

- Phase 3 brief：文本已给旅行画像，但 `trip_brief` 为空，提示调用 `set_trip_brief`，偏好/约束分别用 `add_preferences` / `add_constraints`。
- Phase 3 candidate：文本已有候选或 shortlist，但 `candidate_pool` / `shortlist` 缺失，提示调用 `set_candidate_pool` / `set_shortlist`。
- Phase 3 skeleton：文本有“轻松版/平衡版/方案 A/B/C”等骨架信号，但 `skeleton_plans` 为空，提示调用 `set_skeleton_plans` 和必要时 `select_skeleton`。
- Phase 5：文本已经给逐日行程，但 `daily_plans` 不完整，提示调用 `save_day_plan` 或 `replace_all_day_plans`。

代码：`backend/agent/execution/repair_hints.py`。

### Q：state repair hints 为什么不是最佳实践里的最终形态？你怎么客观解释？

推荐回答：

它是一个实用的补救层，不是理想的主路径。理想情况下，prompt、工具 schema 和 tool choice 应该让模型在需要写状态时稳定调用工具；repair hint 是为了处理现实中的模型漂移：模型会把结构化产物写进自然语言，尤其在长上下文和多工具场景下。

我会客观说：

- 好处：不需要重新跑整轮用户请求，能在同一个 loop 内自修复，用户无感。
- 风险：基于文本信号词的启发式可能误判或漏判。
- 项目里的控制：Phase 3 每个子阶段最多两次修复尝试，Phase 5 当前一次；`repair_key` 去重，避免 repair 本身变成死循环。
- 生产化方向：用更强 schema、强制工具、eval 覆盖“只说不写”case，并把 repair 命中率作为 prompt/tool contract 质量指标。

## 4. Phase Transition 与 Context Rebuild

### Q：阶段推进的真实触发路径是什么？

推荐回答：

写工具本身只改 `TravelPlanState`，阶段推进由 `detect_phase_transition()` 统一处理。它有三条路径：

1. **Backtrack rebuild**：如果工具结果是 `backtracked`，立即生成 `PhaseTransitionRequest(reason="backtrack")`。
2. **Direct phase mutation**：如果工具或外部逻辑已经让 `plan.phase` 变了，返回 `reason="plan_tool_direct"`。
3. **Router inference**：如果本批工具有成功的 state update，则调用 `phase_router.check_and_apply_transition()`；它会 infer 当前 state 是否满足下一阶段 gate，并触发 `before_phase_transition` quality gate。

这个设计把“工具执行”和“阶段判断”分开，避免每个 writer tool 自己偷偷推进 phase。

### Q：`PhaseRouter.infer_phase()` 的关键 gate 是什么？

推荐回答：

简化讲：

- 没有 `destination`，停在 Phase 1。
- 缺 `dates`、`selected_skeleton_id` 或 `accommodation`，停在 Phase 3。
- 已选 skeleton 的天数与 `plan.dates.total_days` 不一致，停在 Phase 3。
- `daily_plans` 数量小于 `total_days`，进入或停在 Phase 5。
- 日程完整后进入 Phase 7。

其中 Phase 3 -> 5 的骨架天数 gate 很关键。`DateRange.total_days` 是 inclusive 自然日语义，4 月 1 日到 4 月 3 日是 3 天。如果用户中途改日期，旧 skeleton 天数不匹配，就不能让 Phase 5 worker 按旧骨架继续展开。

### Q：Phase 变化后为什么要重建 messages？

推荐回答：

因为不同 phase 的系统目标、工具列表、状态快照和 prompt 纪律都不同。继续沿用旧 runtime messages，会让模型在新阶段看到旧阶段的工具暗示、旧候选和旧任务目标，容易重复搜索或越阶段写状态。

前进阶段时，`rebuild_messages_for_phase_change()` 会生成：

1. 新 system message：当前 phase prompt + plan state + memory context + available tools。
2. assistant handoff note：确定性说明上一阶段完成了什么、下一阶段目标是什么。
3. 原始 user message：保留本轮用户 anchor，避免新上下文变成 assistant-only。

回退时则插入 backtrack notice，而不是前进 handoff note。

### Q：Phase 3 子步骤变化和 phase 变化有什么不同？

推荐回答：

Phase 3 的 `brief -> candidate -> skeleton -> lock` 是同一大阶段内的渐进收敛。子步骤变化也要重建 system message 和工具列表，但不会注入 phase handoff note。重建后的 messages 只有新 system message 和原始 user message。

原因是子步骤切换更像“当前任务焦点变化”，不是完整工作流交接；过多 handoff note 会增加机器感和 token 噪声。

### Q：Quality Gate 在 loop 中处于什么位置？

推荐回答：

Quality Gate 挂在 `before_phase_transition` hook 上，只在 `PhaseRouter.check_and_apply_transition()` 推断出 phase 要变化时触发。它先跑硬约束：可行性、日期、预算、时间冲突等；对于 3 -> 5、5 -> 7 还会调用 judge 给方案质量打分。低于阈值时，hook 返回 `GateResult(allowed=False)`，阶段不推进，并把反馈写入消息，让下一轮 LLM 修正。

这就是 evaluator-optimizer 模式在项目里的落点：不是让 LLM 自己说“我觉得可以进入下一阶段”，而是由确定性 router + judge gate 控制阶段边界。

## 5. Cancel、Continue 与韧性

### Q：cancel 是怎么实现的？

推荐回答：

`POST /api/chat/{session_id}/cancel` 会把 session 里的 `_cancel_event` set 掉。AgentLoop 在三个关键位置检查：

- iteration 开始前；
- streaming chunk 处理中；
- 工具执行前。

如果命中 cancel，会抛一个 `LLMError(failure_phase="cancelled")`。`run_agent_stream()` 捕获后把 run 标记为 `cancelled`，发出 `done` 事件，并在 finally 里保底持久化 plan 和 messages。

这比直接杀任务更稳，因为已完成的工具结果、用户消息和状态更新仍能落盘。

### Q：continue 和 retry 有什么区别？

推荐回答：

retry 是重新跑上一轮，风险是重复输出、重复调用工具、重复写状态。continue 是在可恢复中断后，基于当前 messages 和 `RunRecord.continuation_context` 继续生成。

项目当前只把两类 progress 判为可继续：

- `PARTIAL_TEXT`：模型已经输出部分文本，中断后追加 system note，要求从断点继续，不重复已说内容。
- `TOOLS_READ_ONLY`：工具已经执行且没有写状态，中断后要求基于已有工具结果继续总结。

如果 progress 是 `TOOLS_WITH_WRITES`，不自动 continue，因为写状态工具可能已经产生副作用，盲目续写容易重复提交或制造状态冲突。

### Q：为什么流式输出后不能随便重试 LLM 调用？

推荐回答：

因为一旦 provider 已经 yield 文本或 tool call，再 retry 可能产生重复文本、重复工具调用或重复写状态。项目 provider 层只在连接类、可重试错误且尚未 yield 任何数据时做自动重试；一旦有 partial output，就交给 API 层做 error event、`can_continue` 判断和用户可见的继续按钮。

这也是 `IterationProgress` 的价值：它记录中断发生在 no output、partial text、partial tool call、read-only tools、write tools 哪个阶段，恢复策略不同。

### Q：`max_iterations`、`max_retries`、`max_llm_errors` 分别是什么？

推荐回答：

`max_iterations` 是 AgentLoop 的真实循环上限，默认 3。每个 iteration 最多包含一次 LLM turn 和一批 tool execution。超过后返回“达到最大循环次数”的文本提示，防止无限 think-act-observe。

`max_retries` 现在是历史兼容别名：如果没有显式传 `max_iterations`，就用 `max_retries` 作为 iteration 上限。测试里也把它作为 compatibility alias 覆盖。

`max_llm_errors` 当前只在 `AgentLoopLimits` 中校验和保存，默认 1，但 `AgentLoop.run()` 内没有主动按它累计 LLM error 并熔断。真实 LLM 错误目前由 provider 分类后冒泡到 `run_agent_stream()`，API 层发 `error` SSE 并设置 run status。面试时不能把 `max_llm_errors` 说成已实现的错误计数器；更准确的说法是：配置模型已预留这个维度，但 runtime enforcement 还需要补。

### Q：当前 continue 机制有哪些需要 hardening 的地方？

推荐回答：

我会主动说两个边界：

- `can_continue` 的判断基于 `IterationProgress`，但 `continuation_context` 只有在 `accum_text.strip()` 存在时才写入。极端情况下，如果 read-only 工具完成后还没生成文本就中断，可能出现 `can_continue=True` 但 context 不完整，continue endpoint 会因为未知 context type 拒绝。这是一个应补测试和修复的边界。
- continue 目前是继续同一 runtime messages，而不是 Responses API 那种原生 `previous_response_id` 或 encrypted reasoning item，所以 reasoning state 的恢复能力有限。项目用 append-only messages 和 incomplete assistant message 做工程兜底，但不是 provider-native continuation。

## 6. SSE 事件与前端体验

### Q：Agent Loop 会向前端发哪些 SSE 事件？

推荐回答：

主聊天流 `POST /api/chat/{id}` 会发：

- `text_delta`：模型文本增量。
- `tool_call`：模型请求调用工具。
- `tool_result`：工具执行结果。
- `keepalive`：长工具执行期间防超时。
- `agent_status`：thinking、summarizing、compacting、planning、parallel_progress。
- `context_compression`：上下文压缩事件。
- `internal_task`：memory recall、soft judge、quality gate、reflection、context compaction、Phase 5 orchestration 等系统任务。
- `phase_transition`：phase 或 Phase 3 step 变化。
- `state_update`：完整 `TravelPlanState`。
- `memory_recall`：本轮结构化记忆召回结果。
- `error`：LLM 或系统错误，含 `retryable`、`can_continue`、provider、model、failure phase。
- `done`：run 结束状态。

这套事件设计让用户和开发者看到的不是“模型卡住了”，而是“正在查工具、整理上下文、做质量检查、并行规划第几天”。

### Q：SSE 事件顺序有什么关键约束？

推荐回答：

最重要的是工具事件和状态事件的顺序：

1. 先发 `tool_call`，让前端创建工具卡。
2. 工具完成后先发 `tool_result`，结束真实工具卡。
3. 如果工具是 plan writer 或 `generate_summary`，后端立即保存 plan、更新 session meta，再发 `state_update`。
4. soft judge、quality gate、memory extraction 这类系统任务用 `internal_task` 展示，不能混淆成真实工具仍在执行。
5. phase transition 可以来自 loop chunk，也可以在写状态后由 stream 层补发 pending step transition；前端要能处理它先于或后于 `state_update` 的情况。

面试时可以强调：SSE 不是 UI 细节，它反映了 loop 的状态机语义和用户可解释性。

## 7. Trace、Eval 对 Loop 的影响

### Q：trace/eval 为什么会影响 Agent Loop 设计，而不是事后加日志？

推荐回答：

Agent 的错误常常发生在中间轨迹，而不是最终文本。比如：

- 选了错误工具；
- tool arguments 错；
- 重复搜索；
- memory false recall；
- guardrail 拒绝后模型没修；
- phase gate 提前放行；
- worker handoff 绕过标准写工具。

如果 loop 不在运行时显式记录 model call、tool call、state change、validation error、judge score、memory recall 和 phase transition，后面就没法做 trace grading 或 eval。项目的设计是“先让 trajectory 可见，再让它可评分”。

代码：

- `backend/telemetry/stats.py::SessionStats`
- `backend/api/trace.py::build_trace`
- `backend/evals/runner.py`
- `backend/evals/stability.py`

### Q：OpenAI 最新 agent eval/trace grading 趋势和项目怎么对齐？

推荐回答：

OpenAI 官方文档已经把 agent workflow 的质量对象从 final answer 扩展到 trace：trace 捕获 model calls、tool calls、guardrails、handoffs，grader 可以用结构化标准评分。Travel Agent Pro 的 TraceViewer 和 eval pipeline 正好沿着这个方向：不仅看回答好不好，还看 phase 是否到达、状态字段是否完整、工具是否调用、参数是否正确、是否有重复搜索、memory recall 是否误召回。

我会把它总结为：Agent 评估要从“黑盒答案评估”升级为“轨迹评估 + 最终状态评估”。这个项目还没接 OpenAI 原生 trace grading，但内部 Stats/Trace/Eval 的数据边界已经接近这个模型。

参考：OpenAI agent evals 文档强调 trace grading 用于检查工具选择、handoff、安全策略和 workflow-level 行为。

### Q：如果一个 prompt 改动让最终答案更好，但 tool trajectory 更差，你怎么判断？

推荐回答：

我不会只看最终文案。旅行规划是工具型 Agent，轨迹错误可能被漂亮文案掩盖。如果 prompt 改动导致更多重复搜索、更多 `INVALID_ARGUMENTS`、更多 memory false recall、更多 phase gate retry，说明系统可靠性在下降。

我会把评估拆成：

- final-state：phase reached、`TravelPlanState` 字段完整性、deliverables 是否生成；
- trajectory：tool selection、tool arguments、guardrail、重复搜索、backtrack 是否合理；
- quality：soft judge、路线顺路性、预算、个性化；
- cost/latency：LLM iteration 数、tool calls 数、token usage；
- recovery：错误后是否能 cancel/continue，状态是否一致。

### Q：你怎么对待 LLM-as-Judge 在 trajectory eval 中的偏差？

推荐回答：

项目里 quality gate 和 soft judge 都用 LLM 给 trajectory / 最终产物打分。要客观说：LLM-as-Judge 是有偏的——已知 position bias（偏第一个候选）、verbosity bias（更长回答更高分）、self-preference（同 family 模型给自家输出更高分）、风格 vs 事实混淆。HELM、Chatbot Arena 等系统级研究都强调 judge 必须经校准。

项目里的对冲：

- judge 不一票否决：低于阈值时注入修改建议，达到重试上限才放行，避免 judge 抖动卡死流程。
- 关键 invariant（POI 重复、时间冲突、交通接驳、预算溢出）用确定性 `_global_validate()` 的 7 类 issue 做硬规则，不交给 judge 判断。
- judge 输出与 prompt 版本一起进 trace，便于回归。

生产化方向是经典的：pairwise（A/B）替代单点打分降低 verbosity bias；rubric + reference answer 提高一致性；用人工标注子集定期算 judge-vs-human 同意率，发现自我偏好就轮换 judge model。这样 LLM-as-Judge 仍是 trajectory eval 的主力扫描器，但不会成为唯一裁判。

## 8. 为什么不用 LangChain / 何时迁移 Agents SDK

### Q：为什么不用 LangChain？

推荐回答：

先承认 LangChain / LangGraph / LlamaIndex 在快速串链路、生态对接、教学样例方面的价值——很多项目从这里起步是合理选择。这里不用，是阶段判断而不是框架价值判断。

这个项目的难点不是“快速串几个工具”，而是掌控 Agent 生命周期。我要显式控制：

- tool_calls 消息协议；
- Phase 1/3/5/7 状态机；
- 17 个状态写工具的 writer contract；
- 写后 validator、quality gate、soft judge；
- pending system notes；
- append-only history 和 runtime view 分离；
- SSE 事件结构；
- Phase 5 Orchestrator-Workers handoff；
- trace/eval 数据结构。

把 loop 藏到框架里，会让协议顺序、状态推进和调试边界变得不透明，也让 “只说不写”、pending notes 顺序、redundant search、partial tool call recovery 这类边界 bug 更难定位。当业务 control plane 比 orchestration plumbing 更难时，自研 loop 的边际成本反而更低。

未来如果需要接入更多 specialist agent、跨进程 worker 或 MCP/connectors 生态，迁移到 Agents SDK 或 LangGraph 是合理路径——但要替换的是 runtime plumbing，不是把 `TravelPlanState` 权威和 writer contract 一起交出去。

### Q：既然 OpenAI Agents SDK 已经成熟，为什么还不迁移？

推荐回答：

OpenAI 官方对 Agents SDK 的定位很贴近这个项目：code-first agent app，应用自己拥有 orchestration、tool execution、approvals、state。它提供的 primitives 包括 running agents、orchestration/handoffs、guardrails、results/state、integrations/observability 和 eval workflows。

但迁移不能为了追新而牺牲项目已有的控制面。Travel Agent Pro 现在最有价值的资产是：

- `TravelPlanState` 作为当前旅行事实权威；
- 17 个 writer tools 和 `plan_writers` mutation layer；
- PhaseRouter / quality gate / backtrack；
- memory recall/extraction；
- append-only history + context epoch；
- SSE 产品事件；
- golden eval 和 trace schema。

所以我的策略不是大重写，而是分层迁移：先迁 provider/transport，再评估 SDK 的 tracing、tool schema、handoff 能否替代低层 runtime，业务状态和 writer contract 仍留在项目内。

### Q：什么时候适合迁移到 Responses API？

推荐回答：

如果要接 OpenAI 最新 reasoning models、built-in tools、remote MCP、provider-native statefulness、reasoning item 和更好的 cache 利用，Responses API 是更合理的新项目默认选择。官方也建议新项目优先 Responses；Chat Completions 仍支持，但 Responses 把 message/function_call/function_call_output 拆成更清晰的 item，并提供 agentic loop 能力。

对本项目，我会先做一个 `ResponsesProvider`，保持内部 `LLMProvider` Protocol 不变：

1. 把内部 `Message` 映射到 Responses input items。
2. 把 `ToolCall` 映射到 `function_call`，把 `ToolResult` 映射到 `function_call_output`。
3. 先关闭 provider-native statefulness，用项目自己的 append-only history 保持行为等价。
4. 等 trace/eval 对齐后，再考虑 `previous_response_id`、built-in web/file/MCP 等能力。

判断迁移成功的标准不是“用了新 API”，而是同一批 golden cases 下 phase、state、tool calls、deliverables 和 trace 可解释性不倒退。

### Q：什么时候适合迁移 Agents SDK？

推荐回答：

我会在三个条件满足后迁：

1. SDK 的 tracing 能覆盖当前内部 trace 的关键字段：model calls、tool calls、guardrails、handoffs、errors、latency/cost。
2. SDK 的 tool/handoff 模型能表达 Phase 5 Orchestrator-Workers，而不要求 Worker 直接写共享业务状态。
3. 迁移后仍能保留 `TravelPlanState`、writer tools、quality gate、memory policy、SSE payload 和 eval 断言。

适合迁的部分：

- provider streaming；
- tool schema registration；
- SDK tracing/export；
- handoff 或 specialist agent 的底层 runner；
- MCP/built-in tools 接入。

不应外包的部分：

- 当前旅行事实权威；
- plan writer mutation；
- phase gate/backtrack；
- PII/memory policy；
- 高风险工具审批；
- 项目自己的 eval contract。

## 9. Agent 最新发展与项目映射

### Q：2025-2026 年 Agent 发展对这个项目最重要的变化是什么？

推荐回答：

我会概括成四个变化，并映射到项目：

1. **API 原语更 agent-native**：Responses API 和 Agents SDK 把 tools、state、handoff、guardrail、trace/eval 变成平台原语。项目当前是自研 loop，但概念上已经有 run、tool、state、handoff、guardrail、trace、eval。
2. **评价对象转向 trajectory**：OpenAI trace grading 关注 tool selection、handoff、安全策略、prompt/routing 变化。项目的 TraceViewer 和 eval pipeline 正是在看 trajectory，而不是只看最终答案。
3. **工具互联标准化**：MCP/connectors 让工具接入更统一，但也带来第三方 server、敏感数据、prompt injection、write action 审批风险。项目已有 read/write 隔离和 guardrail，但外部工具 source trust 还要加强。
4. **多 Agent 更克制**：不是 agent 越多越好。项目只在 Phase 5 day-level 天然可并行处拆 Worker，Orchestrator 仍是 Python 控制器，最终结果回到标准 `replace_all_day_plans` 写工具。

### Q：MCP 对 Travel Agent Pro 有什么意义？

推荐回答：

MCP 最适合标准化外部工具接入，比如 Google Maps、Amadeus、日历、邮件、企业知识库、签证核查服务。它能减少自研 tool wrapper 成本，也能让模型动态发现工具。

但我不会直接把所有 MCP server 暴露给模型。OpenAI 文档也强调 remote MCP 是第三方服务，可能访问、发送、接收数据并执行动作；敏感动作需要 approval，工具输出里的 URL 和隐藏指令也要谨慎。项目迁移 MCP 时应保持几条原则：

- 只接官方或可信 server；
- 用 `allowed_tools` 限制工具面；
- read/write/high-risk 分级；
- 敏感动作 require approval；
- 记录发给第三方的参数和返回摘要；
- 工具输出作为不可信内容进入上下文，不能覆盖 system/developer 指令。

### Q：项目里的 Phase 5 Orchestrator-Workers 和现代 multi-agent 思路有什么关系？

推荐回答：

它不是为了“多 Agent”而多 Agent，而是因为 Phase 5 的问题天然可拆：每天的 POI 查询和路线生成相对独立，可以并行降低 wall-clock latency；但跨天 POI 去重、预算、节奏和交通衔接必须全局验证。

所以 Orchestrator 是纯 Python，不是另一个 LLM agent。它负责任务拆分、约束注入、收集 artifact、全局验证和 re-dispatch；Day Worker 是轻量 LLM agent，只写 run-scoped candidate artifact。最终正式写入必须回到主 AgentLoop 的内部 `replace_all_day_plans` tool call。

这体现的原则是：多 Agent 只能拆“可隔离的不确定推理”，不能拆掉业务状态权威和全局一致性。

## 10. 已知短板与生产化表达

### Q：Agent Loop 当前有哪些你会主动承认的短板？

推荐回答：

我会主动讲这些，不包装成已完成能力：

- `max_llm_errors` 已在配置层存在（`backend/agent/execution/limits.py:9`，默认 1），但 `AgentLoop.run()` 内还没有按错误次数累计熔断；真实 LLM 错误目前由 provider 分类后冒泡到 `run_agent_stream()`，由 API 层处理。
- `continue` 对 `TOOLS_READ_ONLY` 的边界还需要 hardening：`continuation_context` 仅在 `accum_text.strip()` 存在时写入，read-only 工具完成后未生成文本就中断的边界情况会出现 `can_continue=True` 但 context 不完整。
- `SessionStats` 是进程内对象（`backend/telemetry/stats.py:146`），服务重启或多副本部署后 trace 不完整，且 `cached_input_tokens` 还没接入——KV-cache 命中率目前只能算设计目标，无法实测。
- `build_time_context()` 在 system prompt 里写到秒级（`backend/context/manager.py:124`），即使其他字段稳定，时间字符串每秒变化也会让 prefix cache 命中率归零。生产化要把秒级时间挪到 user message 或缓存友好的粒度（按分钟/小时）。
- token 估算用 `len(text) // 3`（`backend/agent/compaction.py:356`、`backend/llm/anthropic_provider.py:451`）只能做趋势判断，不能用于成本核算或精确预算阈值；中文/英文/工具结果混合时误差大。
- Phase 5 `fallback_to_serial` 文档目标是失败后降级串行，但当前高失败率路径只是发 warning + return，没有同轮真正串行接管；Worker `TimeoutError` / generic exception 分支也没填 `error_code`（`backend/agent/phase5/day_worker.py:548-565`）。
- 外部 `web_search` 工具对政策/签证/开放时间类事实缺少 domain allowlist 和 freshness/source trust metadata；工具结果文本目前没有针对 indirect prompt injection 的 sanitization 层。

这些短板不推翻架构，而是生产化路线：错误熔断、trace 持久化、继续语义、context engineering 维度的时间/token 精度、外部工具可信度、Phase 5 fallback 是下一批 hardening。

### Q：如果只给两周优化 Agent Loop，你会优先做什么？

推荐回答：

我会按用户可见风险排序：

1. 补 `max_llm_errors` enforcement 和 continuation context 边界测试，确保错误恢复语义真实可靠。
2. Phase 5 fallback 真正串行接管，并让 unresolved error 阻断 final dayplans 提交。
3. trace/stats 最小落库，让线上失败能跨进程复盘。
4. 给外部工具加 source trust / freshness / allowed domain，特别是 Phase 7 的签证、开放时间、预订核查。
5. 把“只说不写”“重复搜索”“错误工具参数”“phase gate 误放行”加入 golden eval 和 trace-level eval。

### Q：STAR：你如何把一个“能聊天”的系统变成 Agent control plane？

推荐回答：

- **Situation**：早期系统能给旅行建议，但状态是否写入、阶段是否推进、工具是否选对、错误是否能恢复，都依赖模型自觉。
- **Task**：目标是把不确定的 LLM 推理纳入确定性控制面，让每一步都能被状态机、工具协议、trace 和 eval 解释。
- **Action**：我实现了 `AgentLoop`、`ToolEngine`、17 个 writer tools、PhaseRouter、pending system notes、state repair hints、quality gate/soft judge hooks、append-only history/runtime view 分离、cancel/continue、SSE 事件、TraceViewer 和 eval pipeline；Phase 5 还拆成 Orchestrator-Workers，但通过内部 `replace_all_day_plans` handoff 回到主 loop。
- **Result**：系统从“能回答旅行问题”变成“能解释每一步为什么发生”：失败能定位到 iteration、tool call、state field、memory candidate 或 phase gate；并行生成也不会绕过标准写状态路径。

## 11. 面试压迫题

### Q：如果面试官说“你这就是 while loop 调 LLM，有什么难的？”

推荐回答：

我会承认最表层确实是循环，但难点在循环边界：

- tool_calls 消息必须协议正确；
- 一批 tool calls 要原子 observe；
- 写工具要顺序执行并触发状态持久化；
- system feedback 不能插入 tool result 中间；
- phase gate 不能由模型自说自话；
- 上下文要能压缩和重建，但完整历史不能丢；
- 中断后要知道能不能继续；
- 前端要能看到工具、内部任务、状态和错误；
- trace/eval 要能复盘 trajectory。

所以这个 loop 的价值不是“循环”两个字，而是它把模型输出、工具副作用、业务状态和用户体验放在同一个可控运行时里。

### Q：如果模型一次返回文本和 tool calls，文本怎么处理？

推荐回答：

`AgentLoop` 会把文本片段和 tool calls 一起放进 assistant message：`content` 是已收集文本，`tool_calls` 是同一轮请求的工具。这样保留 provider 原始语义：模型可以先简短说明“我先查一下航班”，再调用工具。工具结果回来后，下一轮 LLM 能看到这段文本和 tool results。

如果没有 tool calls，文本会作为最终 assistant message；但在最终结束前会检查 state repair hint，避免模型只用文本描述结构化产物。

### Q：如果 tool call 部分输出后 LLM 流中断，能继续吗？

推荐回答：

当前不把 `PARTIAL_TOOL_CALL` 判为可继续。原因是工具调用的 JSON 参数可能不完整，贸然继续或重放会让工具协议和状态副作用不可控。provider 层会尽量只在还没 yield 时 retry；一旦进入 partial tool call，API 层更安全的处理是报错，让用户重新发送或系统提供明确恢复路径。

这也是 Responses API / Agents SDK 值得评估的地方：如果 provider-native item state 和 run state 能更好地表达 partial tool call，未来可以把这类恢复交给更底层的运行时。

### Q：为什么 pending system notes 不落盘？

推荐回答：

它是 runtime 协议缓冲，不是业务事实源。它存在的目的是保证 tool_calls group 原子性：工具执行期间产生的 system note 暂存，下一次 LLM 前 flush 到 messages；一旦 flush，消息会进入正常持久化链路。

不落盘的代价是：如果进程在 flush 前崩溃，尚未 flush 的实时约束提示可能丢失。但对应的状态、工具结果和验证错误可从 plan/stats/trace 重新推导一部分。生产化可以考虑把 pending note 作为 run event 持久化，但仍不能直接插到 tool_calls 中间。

### Q：为什么 Phase 5 并行结果不直接写 `TravelPlanState`？

推荐回答：

因为那会绕过主 loop 的关键保障：writer schema、validator、soft judge、phase transition、state_update、trace/stats 和增量持久化。当前设计让 Worker 只提交 candidate artifact，Orchestrator 验证后通过 `on_handoff` 交给 AgentLoop；AgentLoop 构造内部 `replace_all_day_plans` tool call，走标准 `_execute_tool_batch -> detect_phase_transition` 链路。

这保证串行模式和并行模式在最终写状态路径上等价。

### Q：如果要把 Travel Agent Pro 的 loop 抽象成通用 Agent 平台，你会保留哪些原语？

推荐回答：

我会保留这些平台原语：

- `RunRecord`：run id、status、error、can_continue、continuation context。
- `Message` / `ToolCall` / `ToolResult`：provider-neutral 轨迹协议。
- `ToolEngine`：工具注册、phase/permission 过滤、读写分类、错误反馈。
- `StateAuthority`：领域状态权威，不让自然语言历史直接改业务事实。
- `TransitionRouter`：状态推断和 gate。
- `HookManager`：validator、judge、memory、reflection、trace。
- `ContextBuilder`：system prompt、memory、compaction、runtime rebuild。
- `EventStream`：text/tool/state/internal/error/done 的产品事件。
- `EvalContract`：final state + trajectory + cost/latency + recovery。

这些原语迁到客服、文档审查、代码 agent 都成立；变化的是领域状态和工具集合。

## 12. 参考资料

- OpenAI Agents SDK overview: https://developers.openai.com/api/docs/guides/agents
- OpenAI Responses API migration: https://developers.openai.com/api/docs/guides/migrate-to-responses
- OpenAI Function calling: https://developers.openai.com/api/docs/guides/function-calling
- OpenAI Agent evals / trace grading: https://developers.openai.com/api/docs/guides/agent-evals
- OpenAI MCP and Connectors safety: https://developers.openai.com/api/docs/guides/tools-connectors-mcp

