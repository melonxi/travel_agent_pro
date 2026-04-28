# Travel Agent Pro 编排面试问答集

> 范围：本文件聚焦 orchestration，尤其是 Phase 5 Orchestrator-Workers、DayTask、candidate artifact store、handoff、global validation / re-dispatch / fallback、worker mini loop、parallel progress SSE、shared prefix / Manus pattern，以及多 Agent 与 A2A 的工程边界。
>
> 回答口径：优先讲真实代码已经做到什么；对未完全符合最佳实践的点要客观承认，并解释它为什么在当前 demo / 单进程阶段还能自圆其说，以及生产化应如何补齐。

## 0. 速记主线

### Q：如果用一句话解释 Travel Agent Pro 的“编排”，你会怎么说？

推荐回答：

Travel Agent Pro 的编排层不是简单把请求转给 LLM，而是一个服务端控制面：它决定什么时候进入哪个 Phase，给模型暴露哪些工具，如何把模型输出落到 `TravelPlanState`，如何把并行 worker 的候选结果收拢到标准写工具路径，并通过 SSE / trace / internal task 把每一步暴露出来。LLM 负责不确定推理，Python 编排层负责状态一致性、工具权限、失败恢复和用户可见进度。

代码锚点：

- `backend/agent/loop.py::AgentLoop.run`
- `backend/agent/phase5/parallel.py`
- `backend/agent/phase5/orchestrator.py`
- `backend/agent/phase5/day_worker.py`
- `backend/api/orchestration/chat/stream.py`
- `frontend/src/components/ParallelProgress.tsx`

### Q：你为什么把 Phase 5 拿出来作为编排设计的重点？

推荐回答：

因为 Phase 5 同时具备“可并行”和“必须全局收敛”两个特征。把已选骨架展开成每日行程时，每一天的 POI 查询、路线顺序、时间安排可以独立规划；但跨天 POI 重复、预算、交通接驳、节奏一致性必须统一校验。这个形状很适合用 Orchestrator-Workers：worker 专注单日，Orchestrator 负责全局约束和最终提交路径。

它也最能证明项目不是 prompt glue：Day Worker 不能直接写最终状态，只能提交 candidate artifact；最终结果必须 handoff 回 `AgentLoop`，由内部 `replace_all_day_plans` 工具调用写入状态，再触发 Phase 5 -> 7 的标准推进链路。

### Q：这套编排和当前 Agent 平台趋势怎么对齐？

推荐回答：

我会从四个趋势讲：

1. **code-first agent runtime**：OpenAI Agents SDK 把 tools、handoff、guardrails、tracing、state 作为一等原语。Travel Agent Pro 虽然自研 loop，但抽象一致：run、tool call、handoff、state writer、trace、eval 都在服务端控制。
2. **trajectory 成为质量对象**：OpenAI trace grading / agent evals 强调评估端到端轨迹，而不是只看最终回答。项目里的工具卡、internal task、parallel progress 和 stats 都服务于这个方向。
3. **多 Agent 更克制**：Anthropic 的 “Building Effective Agents” 强调先用简单、可评估的 workflow，只有复杂度确实需要时才引入更复杂的 agentic pattern。Travel Agent Pro 只在 day-level 天然可分处拆 worker，没有把整个系统做成多 Agent 群聊。
4. **Context / KV-cache 工程化**：Manus 的 context engineering 经验强调稳定 prefix、append-only / deterministic serialization、把差异放到后段。Phase 5 shared prefix 把所有 worker 的 system message 做到一致，把 day suffix 放到 user message，就是这个思路的项目化落地。

参考资料：

- [OpenAI Agents SDK](https://developers.openai.com/api/docs/guides/agents)
- [OpenAI Trace grading](https://developers.openai.com/api/docs/guides/trace-grading)
- [Anthropic Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Manus: Context Engineering for AI Agents](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- [Google Agent2Agent Protocol](https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/)
- [Linux Foundation A2A project](https://www.linuxfoundation.org/press/linux-foundation-launches-the-agent2agent-protocol-project-to-enable-secure-intelligent-communication-between-ai-agents)

## 1. Phase 5 Orchestrator-Workers

### Q：Phase 5 为什么适合 Orchestrator-Workers，而不是继续用一个主 Agent 串行生成？

推荐回答：

Phase 5 的输入已经不是开放探索，而是 Phase 3 锁定后的骨架方案。骨架里的每一天都有 area、theme、locked/candidate POI、mobility envelope 等局部约束，所以可以拆成多个 `DayTask` 并行生成，降低用户等待时间。

但它不能完全分散自治，因为跨天约束仍然存在：同一个 POI 不能重复出现在多天，首末日要和交通时间衔接，总预算不能明显失控，不同天的节奏不能互相打架。所以项目采用“worker 并行生成候选，Orchestrator 全局验证，AgentLoop 标准写入”的三段式，而不是让 worker 直接写共享状态。

### Q：项目里的 Orchestrator 是 LLM Agent 吗？和 Anthropic 文里的 orchestrator-workers 有什么区别？

推荐回答：

不是。项目里的 `Phase5Orchestrator` 是纯 Python 调度器，不调用 LLM 做任务拆分。真正调用 LLM 的是 `run_day_worker()`。

这和 Anthropic 文章里的经典 orchestrator-workers 有一个关键差异：Anthropic 描述的是中央 LLM 动态拆任务、分派 worker、综合结果；Travel Agent Pro 的 day-level subtasks 是从 Phase 3 skeleton 确定性拆出来的，所以没有必要让中央 LLM 再做拆分。这样更可控，也更便于测试。

我会把它称为“deterministic orchestrator + LLM workers”：拓扑上像 orchestrator-workers，但编排权在代码，不在中央 LLM。

### Q：`Phase5Orchestrator.run()` 的主流程是什么？

推荐回答：

主流程可以按 10 步讲：

1. 找到 `selected_skeleton`。
2. 用 `split_skeleton_to_day_tasks()` 拆成 `DayTask[]`。
3. `_compile_day_tasks()` 注入跨天约束、交通时间、预算和软约束。
4. 构建所有 worker 共享的 `shared_prefix`。
5. 创建 run-scoped `Phase5CandidateStore`。
6. 初始化 `worker_statuses`，通过 `parallel_progress` SSE 暴露每个 worker 状态。
7. 用 `asyncio.Semaphore(max_workers)` 并发运行 `run_day_worker()`。
8. 收集成功/失败结果；失败率超过一半时按当前实现返回 warning。
9. 对失败天重试一次；对全局 error severity issue 做最多一轮 re-dispatch。
10. 加载最新 artifact candidates，做全局验证，暴露 `final_dayplans` 给 handoff。

注意最后一步：Orchestrator 自己不写 `TravelPlanState.daily_plans`，测试里也明确断言 `plan.daily_plans == []`。它只把 `final_dayplans` 交给 AgentLoop。

### Q：Phase 5 并行入口由谁决定？

推荐回答：

入口在 `agent/phase5/parallel.py` 的 guard 函数：

- `plan` 和 `config` 必须存在；
- `config.enabled == true`；
- `plan.phase == 5`；
- `plan.daily_plans` 为空；
- `selected_skeleton_id` 和 `skeleton_plans` 都存在。

`AgentLoop.run()` 有两个检查点：

1. 循环顶部的 `should_enter_parallel_phase5_now()`，覆盖正常进入 Phase 5 和恢复后冷启动。
2. 最后一轮工具调用刚好把 phase 推到 5 时的 `should_enter_parallel_phase5_at_iteration_boundary()`，避免达到 `max_iterations` 后错过并行入口。

如果 `daily_plans` 已经存在，项目不会再自动并行，因为这通常表示用户在修改已有行程，串行主 Agent 更适合处理局部编辑和对话语境。

### Q：为什么 worker 不能直接写 `TravelPlanState`？

推荐回答：

这是并行编排里最重要的状态一致性边界。worker 如果直接写 `TravelPlanState.daily_plans`，会带来四个问题：

1. 多个 worker 并发写同一个共享状态，顺序和覆盖关系不稳定。
2. 单日结果还没经过跨天 POI、交通、预算和节奏校验。
3. 会绕过主 `AgentLoop` 的 tool result、hook、soft judge、telemetry 和 phase transition。
4. 前端看不到标准工具卡和状态写入事件，trace 也会断层。

所以当前设计是：worker 只写 run-scoped candidate artifact；正式写入由 AgentLoop 构造内部 `replace_all_day_plans` tool call，走标准 `_execute_tool_batch -> detect_phase_transition`。

## 2. DayTask 与约束注入

### Q：`DayTask` 是什么？它为什么是 Phase 5 并行的核心合同？

推荐回答：

`DayTask` 是 Orchestrator 派给单个 Day Worker 的任务合同。它把 Phase 3 skeleton 的日级结构转换成 worker 可执行的约束集合，包括：

- `day` / `date`：第几天和日期。
- `skeleton_slice`：原始日骨架片段。
- `pace`：节奏，影响活动数量范围。
- `locked_pois`：必须包含的硬 POI。
- `candidate_pois`：优先选择但非强制的候选 POI。
- `forbidden_pois`：其他天已锁定的 POI，本天禁止使用。
- `area_cluster`：当天活动区域边界。
- `mobility_envelope`：跨区次数和单段交通时长限制。
- `fallback_slots`：备选方案。
- `date_role`：`arrival_day` / `departure_day` / `arrival_departure_day` / `full_day`。
- `repair_hints`：re-dispatch 时注入的修复要求。
- `day_budget` / `day_constraints` / `arrival_time` / `departure_time`：预算、天级软约束和交通锚点。

这个合同把 worker 的自由度压到“在给定边界内规划单日”，而不是让它重新发明整条旅行路线。

### Q：`_compile_day_tasks()` 做了哪些增强？

推荐回答：

它把 skeleton 拆出的基础 task 增强成可执行 task：

1. 建立 locked POI 的全局 ownership map。
2. 给每一天生成 `forbidden_pois`，避免使用其他天已锁定 POI。
3. 如果 skeleton 没给 `mobility_envelope`，按 pace 注入默认移动限制。
4. 推断首日 / 末日 / 单日旅行的 `date_role`。
5. 如果有总预算和日期，按 `budget.total / total_days` 注入 `day_budget` 软提示。
6. 把非 hard 的全局约束复制到 `day_constraints`，hard constraint 留在 shared prefix。
7. 从 `selected_transport` 提取到达和离开时间，注入首日/末日/单日任务。

这里的设计重点是“全局约束下沉到单日上下文”。Worker 不需要知道其他天完整计划，但必须知道哪些 POI 不能碰、当天区域边界是什么、首末日有哪些时间缓冲。

### Q：locked / candidate / forbidden 三类 POI 怎么区分？

推荐回答：

它们在 `_build_constraint_block()` 里用不同层级渲染：

- `locked_pois`：必须包含，违反后 DayPlan 无效，Orchestrator 会要求重做。
- `candidate_pois`：候选池，优先选取；如果全不可行，才在同 area cluster 内补充。
- `forbidden_pois`：其他天已锁定，禁止使用；违反会触发跨天重复问题。

提示词里用 `⛔ / ✅ / 🚫` 区分，这是少数我认为合理使用视觉符号的地方，因为它直接帮助模型理解约束层级，不是装饰。

### Q：`day_constraints` 为什么只放非 hard 约束？

推荐回答：

hard constraints 是全局硬规则，放在 shared prefix 中，保证所有 worker 都看到同一份不可违反的规则；非 hard 约束更像天级软提示，比如“尽量轻松”“偏好民宿氛围”，可以放到 day suffix，让 worker 在具体当天规划时参考。

代码里是 `c.type != "hard"` 才进入 `day_constraints`。这个点面试时要讲清楚：不是“过滤 non-hard”，而是“过滤出 non-hard，排除 hard”。hard 的位置在 shared prefix。

### Q：到达日 / 离开日约束怎么处理？

推荐回答：

Orchestrator 从 `selected_transport` 提取时间：

- outbound 取最后一段 `arrival_time`，作为到达目的地时间；
- return 取第一段 `departure_time`，作为最早离开时间。

然后 `_build_constraint_block()` 会渲染：

- 到达日：首活动不得早于到达时间加 2 小时；没有具体时间时，至少留 2 小时接驳缓冲。
- 离开日：末活动不得晚于出发时间前 3 小时；没有具体时间时，至少留 3 小时前往交通枢纽。
- 单日往返：同时注入到达和离开缓冲，建议只安排轻松活动。

这说明 Phase 5 并行不是简单“每天列景点”，而是在做路径规划优化和交通风险控制。

## 3. Day Worker Mini Loop

### Q：Day Worker 的 mini agent loop 和主 `AgentLoop` 有什么区别？

推荐回答：

两者都是 think-act-observe，但职责和边界不同。

主 `AgentLoop` 面向用户会话：处理 SSE streaming、cancel/continue、Phase transition、memory、reflection、context rebuild、写状态工具和前端工具卡。

Day Worker 面向单日子任务：没有用户交互，没有 continue/cancel，没有完整 ContextManager，也不负责 phase transition。它只拿到一个固定 `shared_prefix` 和一个 day-specific user message，在限定工具集里补信息、排路线、提交 `submit_day_plan_candidate`。

Worker 更像一个隔离的 execution sandbox：自由度更小，收敛策略更激进，输出必须通过 candidate store 进入 Orchestrator。

### Q：Worker 能调用哪些工具？

推荐回答：

Worker 只暴露只读工具和 worker-only 提交工具：

- `get_poi_info`
- `optimize_day_route`
- `calculate_route`
- `check_weather`
- `web_search`
- `xiaohongshu_search_notes`
- `xiaohongshu_read_note`
- `xiaohongshu_get_comments`
- `submit_day_plan_candidate`（仅 worker 内部 schema，不是普通 plan writer）

它没有 `save_day_plan`、`replace_all_day_plans`、`request_backtrack` 这类会改变全局状态的工具。这个读写隔离是并行安全的关键。

### Q：Worker 为什么有 `_WORKER_ROLE`，而不是复用 `soul.md`？

推荐回答：

`soul.md` 是面向用户对话的全局人格，里面可能有“一次只问一个问题”“给用户 2-3 个选项”等交互规则。但 Worker 没有用户通道，也不能请求确认。如果复用 `soul.md`，模型可能在子任务里停下来问用户，导致并行任务无法收口。

所以项目把 Worker 身份内联为 `_WORKER_ROLE`：强调“无用户交互”“完成优于完美”“只负责指定一天”“通过 `submit_day_plan_candidate` 交付”。这是把主 Agent 和子 Agent 的行为契约分开，避免错误的上层人格污染执行 worker。

### Q：Worker 的 system / user message 是怎么拼装的？

推荐回答：

Worker message 固定两段：

1. system message = `shared_prefix`。它包含旅行上下文、全局硬约束、Worker 角色、DayPlan schema。所有 worker 完全一致。
2. user message = `day_suffix + iteration_note`。它包含“第 N 天”、日期、当天 skeleton slice、活动数量范围、DayTask 约束块和工具调用预算。

这个拆分是为了 cache-aware。只要所有 worker 的 system message 字节级一致，provider 就更有机会复用 prefix cache；每天不同的内容放在 user message，不破坏 shared prefix。

### Q：Worker 的四重收敛保障是什么？

推荐回答：

1. **重复查询抑制**：对 `web_search:{query}` 和 `get_poi_info:{query/name}` 做 fingerprint；同一 fingerprint 第 3 次出现时进入 forced emit。
2. **补救链阈值**：同一 POI / query 的恢复 key 超过 `_MAX_POI_RECOVERY=3` 后进入 forced emit。
3. **后半程强制收口**：`iteration + 1 >= max(3, int(max_iterations * 0.6))` 时注入 `_LATE_EMIT_PROMPT`，提醒最多再调 1-2 个工具后提交。
4. **JSON 修复回合**：如果最终文本没有工具提交也没有可解析 JSON，注入 `_JSON_REPAIR_PROMPT`，要求复用历史调用 `submit_day_plan_candidate`；修复失败返回 `JSON_EMIT_FAILED`。

它们解决的是无人值守 worker 的现实问题：没有用户在场纠偏，不能让模型无限“再查一下”。

### Q：`_FORCED_EMIT_PROMPT` 的设计重点是什么？

推荐回答：

它不是让模型瞎编，而是让模型保守落地：

- 停止继续工具调用；
- 只保留已拿到坐标的 POI；
- 缺开放时间写 notes；
- 缺票价 cost 写 0 并标注；
- 绝不填 `0,0` 假坐标。

这比“失败就报错”更适合旅行规划：如果 worker 已经拿到足够的区域、主题和核心 POI，提交一个保守 DayPlan 供 Orchestrator 校验，通常比卡死更好。但它不是最终最佳实践，生产化还应把 forced emit 命中率纳入评估，避免保守落地掩盖工具质量问题。

### Q：Worker 失败错误码当前完整吗？

推荐回答：

不完整，要客观承认。

当前已经能看到的结构化错误包括：

- `JSON_EMIT_FAILED`：修复回合后仍未提交合法 DayPlan。
- `REPEATED_QUERY_LOOP`：重复查询触发 forced emit 后仍未成功收口时可能作为失败原因。
- `RECOVERY_CHAIN_EXHAUSTED`：补救链耗尽后仍未成功收口时可能作为失败原因。
- `NEEDS_PHASE3_REPLAN`：Orchestrator 已预留处理路径，测试也覆盖了 worker 返回该错误码时输出回退提示。

缺口是：`TimeoutError` 和 generic exception 分支当前只填 `error` 文本，没有稳定填 `TIMEOUT` / `LLM_ERROR` 这类结构化 `error_code`。这会降低 Orchestrator 的自动诊断和降级能力，是 production hardening 要补的点。

## 4. Candidate Artifact Store

### Q：candidate artifact store 解决什么问题？

推荐回答：

它解决“并行 worker 产物不能直接写最终状态，但又必须可追踪、可重试、可复现”的问题。

Worker 调用 `submit_day_plan_candidate` 后，`Phase5CandidateStore` 把候选 DayPlan 写入 run-scoped JSON artifact：

`{artifact_root}/{session_id}/{run_id}/day_{N}_attempt_{M}.json`

默认 `artifact_root` 是 `./data/phase5_runs`。`run_id` 类似 `phase5_<uuid>`，`attempt` 用来区分初次生成、失败重试和 redispatch 修复。

### Q：为什么 artifact 要按 run 隔离？

推荐回答：

因为同一个 session 可能多次进入 Phase 5：用户改日期、backtrack、重新选择骨架，都会产生新的并行规划 run。如果 artifact 不按 run 隔离，旧 worker 的 dayplan 可能污染新 run。

`Phase5CandidateStore.run_dir(session_id, run_id)` 还校验 path segment 只能是安全的 `[A-Za-z0-9_-]+`，避免路径穿越。写文件时先写 `.tmp`，再 `os.replace()` 原子替换，避免半写入 artifact 被 Orchestrator 读取。

### Q：candidate store 的校验边界是什么？

推荐回答：

它做的是 staging 层校验，不是最终 schema 校验。当前 `_validate_dayplan()` 检查：

- dayplan 是 object；
- `day` 等于 expected day；
- `date` 是非空字符串；
- `activities` 是 list。

更细的字段校验，例如 activity 是否有 `start_time` / `end_time` / `category` / `cost`，最终会在 `replace_all_day_plans` 标准写工具路径里被捕获。这个分层是合理的：candidate store 只保证“这是不是本 worker 的候选产物”，最终状态写入仍由 writer contract 负责。

### Q：Orchestrator 收集结果时为什么优先读 artifact，而不是直接用 `DayWorkerResult.dayplan`？

推荐回答：

因为 `submit_day_plan_candidate` 是 Worker 的正式交付路径。`DayWorkerResult.dayplan` 只是运行内存里的返回值，可能为空，也可能与 artifact 不一致。Orchestrator 在最终排序和 redispatch 替换时会优先 `load_latest_candidates()`，按 day 取最高 attempt 的 artifact。

测试也覆盖了这个边界：redispatch 时 artifact 里的 repair result 优先于 memory result。这个设计让“提交动作”成为事实来源，而不是让 worker 函数返回值偷偷变成另一个事实来源。

## 5. Handoff 到 AgentLoop

### Q：Orchestrator 如何把结果 handoff 给 AgentLoop？

推荐回答：

`run_parallel_phase5_orchestrator()` 包装器在 Orchestrator 完成后读取 `orchestrator.final_dayplans` 和 `final_issues`。如果有 dayplans，就通过 `on_handoff` 回调传出 `Phase5ParallelHandoff(dayplans, issues)`。

`AgentLoop._run_parallel_phase5_orchestrator()` 捕获 handoff 后构造内部工具调用：

```python
ToolCall(
    id="internal_phase5_parallel_commit",
    name="replace_all_day_plans",
    arguments={"days": list(_handoff.dayplans)},
    human_label="写入并行逐日行程",
)
```

然后把它追加成一条 assistant tool_calls message，调用 `_execute_tool_batch()`，再走 `detect_phase_transition()`。

### Q：为什么 handoff 后要构造内部 tool call，而不是直接调用 state manager？

推荐回答：

因为直接写 state manager 会绕过既有 Agent runtime 的关键保障：

- tool result 事件；
- writer schema / validator；
- after_tool_call / soft judge hook；
- telemetry state_changes；
- chat stream 里的增量持久化；
- PhaseRouter 推进到 Phase 7；
- 前端工具卡和 `state_update`。

内部 tool call 的意义是保持串行和并行路径等价：不管 daily plans 是主 Agent 串行写的，还是 worker 并行生成的，最终都走 `replace_all_day_plans` 这条唯一正式写入路径。

### Q：handoff 失败时用户会看到什么？

推荐回答：

如果 Orchestrator 没有生成 handoff，AgentLoop 直接发 `DONE`，计划仍停在 Phase 5，`daily_plans` 为空。包装器会把 internal task 标成 warning，消息是“并行生成未完全成功，已降级或等待后续串行处理”。

如果有 handoff，但 `replace_all_day_plans` 写入失败，AgentLoop 会输出文本警告：“并行行程写入失败，当前行程尚未保存到规划状态”，并带上 writer 返回的错误和 suggestion。测试覆盖了缺字段导致写入失败时不发生 Phase transition。

这个设计至少不会假装已保存，但当前 UX 还不够好。更好的生产体验应该是真正触发串行 fallback 或让用户一键重试，而不是只停在 warning。

## 6. Global Validation、Re-dispatch 与 Fallback 风险

### Q：全局校验检查哪些问题？

推荐回答：

`_global_validate()` 当前检查七类问题：

- `poi_duplicate`：同名 POI 出现在多天，error。
- `budget_overrun`：总活动成本超过预算，warning。
- `coverage_gap`：缺少天数，warning。
- `time_conflict`：同一天活动时间和交通时长冲突，error。
- `semantic_duplicate`：不同名字但坐标很近且名称相似，疑似同一地点，error。
- `transport_connection`：首日活动距到达不足 2 小时，或末日距离开不足 3 小时，error。
- `pace_mismatch`：活动数量超过 pace 上限，warning。

error severity 会触发 re-dispatch；warning 当前只进入 final issues 和摘要提醒。

### Q：re-dispatch 怎么工作？

推荐回答：

全局验证发现 error severity issue 后，Orchestrator 取所有 `affected_days`，把 issue description 写进对应 `DayTask.repair_hints`，把 worker status 改成 `redispatch`，然后用同一个 `shared_prefix`、新的 day suffix 和 `attempt=3` 重新运行该天 worker。

Worker 的 day suffix 会渲染“修复要求（上一轮校验发现的问题，本轮必须逐一解决）”，强制它优先处理重复、时间冲突或交通接驳问题。成功后 Orchestrator 重新从 artifact store 读取最新 attempt，替换原 dayplan，再跑一次全局验证。

### Q：如果 re-dispatch 后仍有 error，会阻断提交吗？

推荐回答：

当前不会稳定阻断，这是必须主动暴露的短板。

代码在 re-dispatch 后会重新 `_global_validate(dayplans)`，把 unresolved error 写 log，但仍然设置 `self.final_dayplans = list(dayplans)` 和 `self.final_issues = list(issues)`。也就是说未解决 error 仍可能进入 handoff，后续是否被 writer 拦下取决于 writer schema，而不是全局验证。

这不是生产最佳实践。生产化应该把 unresolved error 变成明确终止条件：要么进入串行修复，要么请求 Phase 3 backtrack，要么把结果标为不可提交并要求用户确认。

### Q：`fallback_to_serial` 当前真的会同轮进入串行吗？

推荐回答：

不会，这是文档/配置语义和实现之间的差距。

`Phase5ParallelConfig.fallback_to_serial` 默认是 true；Orchestrator 如果发现失败 worker 数超过总数一半，会发一条 progress chunk：“并行模式失败率过高，切换到串行模式...”，然后 `return`。包装器发现没有 `final_dayplans`，发 warning internal task；AgentLoop 收到无 handoff 后 `DONE`。它没有在同一轮重新进入主 AgentLoop 串行生成。

当前实现可以解释为“降级信号已发出，但串行接管还没做”。在 demo 阶段它至少不会写坏状态；生产化必须补成真正 fallback：禁用本轮 parallel guard，恢复 Phase 5 串行工具列表，让主 Agent 继续生成或提示用户重试。

### Q：`NEEDS_PHASE3_REPLAN` 表示什么？当前怎么处理？

推荐回答：

它表示 Day Worker 判断当前 skeleton 的 locked POI 全部不可行，单日修补已经解决不了，需要回到 Phase 3 重调骨架。Orchestrator 当前会扫描 worker status 中的 `error_code == "NEEDS_PHASE3_REPLAN"`，输出一段文本提示“骨架分配失败，需要回退到 Phase 3 重新调整骨架方案”，并保持 `final_dayplans` 为空。

但它还不是完整的自动 backtrack：没有自动调用 `request_backtrack` 或 plan writer 清理下游。更严谨的生产版本应该把这个错误码接入标准 backtrack 工具路径，生成可审计的 `BacktrackEvent`，而不是只发文本。

### Q：你如何客观解释这些不完美实现？

推荐回答：

我会说它是“状态安全优先、体验接管不足”的中间版本。

已经做对的部分是：worker 不直接写最终状态；handoff 必须走标准 writer；写入失败不会假装成功；高失败率不会把残缺结果硬写进 `TravelPlanState`。

没做完的部分是：fallback 还停留在 warning，unresolved error 没有阻断，`NEEDS_PHASE3_REPLAN` 没有接标准 backtrack，timeout/generic exception 错误码不完整。这些不会破坏状态一致性，但会造成用户可见体验差，是生产化第一批要补的 runtime hardening。

## 7. Parallel Progress SSE 与前端体验

### Q：Phase 5 并行进度如何通过 SSE 到前端？

推荐回答：

链路是：

1. `Phase5Orchestrator._build_progress_chunk()` 产出 `LLMChunk(type=AGENT_STATUS)`，payload 里 `stage="parallel_progress"`，包含 `total_days`、`hint` 和 `workers[]`。
2. `backend/api/orchestration/chat/events.py::passthrough_chunk_event()` 把它转成 SSE JSON：`{"type": "agent_status", "stage": "parallel_progress", ...}`。
3. `ChatPanel` 收到 `agent_status` 后，如果 `stage === "parallel_progress"`，更新 `parallelProgress` 状态并清除 thinking bubble。
4. `ParallelProgress` 渲染每个 worker 的 day、theme、status、current_tool、iteration、activity_count、error。

这条链路让用户看到“第几天正在查什么 / 哪天已完成 / 哪天重试”，而不是面对一个长时间无响应的 loading。

### Q：`parallel_progress` worker status 包含哪些字段？

推荐回答：

后端 status 字段大致包含：

- `day`
- `status`
- `theme`
- `iteration`
- `max_iterations`
- `current_tool`
- `activity_count`
- `error`
- `error_code`

`on_progress(day, kind, payload)` 目前主要有两个 kind：`iter_start` 更新轮次，`tool_start` 更新当前工具 human label。worker callback 异常会被吞掉并记录 warning，不会杀死 worker。

### Q：前端 `redispatch` status 当前是否覆盖？

推荐回答：

没有完整覆盖，这是一个具体前端风险。

后端 re-dispatch 时会设置 `worker_statuses[idx]["status"] = "redispatch"`。但 `frontend/src/types/plan.ts::ParallelWorkerStatus.status` 只声明了 `running | done | failed | retrying`，`ParallelProgress.tsx::STATUS_ICON` 也没有 `redispatch` 映射。运行时可能出现 undefined 图标或状态文案为空。

修复很直接：

- TypeScript union 加 `"redispatch"`；
- `STATUS_ICON` 加对应图标或文本；
- `renderTail()` 加 `redispatch` 文案；
- CSS 加 `parallel-worker--redispatch`。

这个问题不影响后端编排正确性，但会影响用户对“系统正在修复问题”的理解。

### Q：为什么 Phase 5 编排也要走 internal task？

推荐回答：

因为 Phase 5 编排不是用户显式调用的工具，但它会阻塞本轮回答、消耗时间，并影响最终状态。项目用 `InternalTask(kind="phase5_orchestration")` 在 chat SSE 中展示 pending / success / warning / error 生命周期。

这和 memory recall、quality gate、soft judge 是同一类：不是模型工具调用，却是 Agent runtime 的重要工作。把它们显示出来，可以减少“系统卡住”的感觉，也让 trace 能解释时间花在哪里。

## 8. Shared Prefix、Manus Pattern 与 Context Engineering

### Q：“Manus pattern”在这个项目里具体指什么？

推荐回答：

这里指的是一种 cache-aware context pattern：多个子 Agent / Worker 尽量共享完全一致的 prompt prefix，把差异化任务放到后段，从而提高 provider 侧 prefix / KV cache 命中率。

Phase 5 的落地方式是：

- `build_shared_prefix(plan)` 生成所有 Day Worker 相同的 system message；
- `build_day_suffix(task)` 生成每天不同的 user message；
- “第 N 天”这类 per-day 信息不放 system message；
- preferences 和 trip_brief 字段排序稳定，避免同样语义被序列化成不同字节。

注意：项目当前没有真实统计 provider 的 cached input tokens，所以不能宣称“已测得 93% 命中率”。正确说法是：这是设计目标和上下文工程策略，后续要把 cached token usage 接入 `SessionStats` 才能量化。

### Q：`build_shared_prefix()` 为什么对白名单字段做过滤？

推荐回答：

它只保留 trip_brief 中对所有天都稳定且有规划价值的字段：`goal`、`pace`、`departure_city`、`style`、`must_do`、`avoid`。它排除 `dates`、`total_days`、`budget_per_day` 等字段，因为日期和天数已有 `plan.dates` 权威提供，预算也有 `plan.budget` 和 `day_budget` 路径，重复注入会膨胀 prefix，也可能制造冲突。

这个做法体现了 context engineering 的核心：不是把所有信息塞进 prompt，而是按权威来源和使用范围放到正确层级。

### Q：preferences 为什么按 key 排序？

推荐回答：

因为 prefix cache 通常依赖字节级前缀一致。Python list 的原始顺序可能来自用户输入、数据库加载或不同 mutation 路径；如果偏好顺序不稳定，同一组偏好会生成不同 system message，破坏缓存。

项目用 `sorted([f"{p.key}: {p.value}" for p in plan.preferences if p.key])`，让 shared prefix 更确定。这个细节不是为了“好看”，而是为了减少无意义的 prompt 变动。

### Q：soft 约束为什么不放 shared prefix？

推荐回答：

soft 约束可能每天解释不同，尤其是 re-dispatch 或首末日约束场景。放到 shared prefix 会让所有 worker 都受到同样软约束影响，也会让 prefix 膨胀。项目选择把 hard constraint 放在 prefix，把 day-level non-hard constraints 放在 suffix。

这也是多 worker context 的基本原则：全局不变、所有 worker 必须遵守的东西放 prefix；每天不同或修复相关的东西放 suffix。

### Q：KV-cache 是省成本还是省延迟？

推荐回答：

要讲清楚两层：

1. 技术上，prefix / KV cache 主要减少相同 prefix 的 prefill 计算，降低 TTFT 和推理侧计算成本。
2. API 账单上，是否省钱取决于 provider 的 cached input token 计费策略。部分 provider 对 cached input tokens 有折扣，但不等于这些 token 免费。

所以 Phase 5 并行的真实 tradeoff 是：用更多总 token 和多个 worker loop，换更低 wall-clock latency；shared prefix 只能降低重复 prefix 的计算和部分计费压力，不能把 N 个 worker 的成本变成 1 个 worker。

## 9. 何时多 Agent，何时不多 Agent

### Q：这个项目为什么没有把所有 Phase 都拆成多 Agent？

推荐回答：

因为多 Agent 不是越多越好。它会增加 token 成本、状态同步、错误恢复、观测复杂度和 prompt 管理成本。Phase 1 和 Phase 3 很多决策是连续收敛：用户偏好、候选池、骨架选择、交通住宿会互相影响，拆太多 worker 反而容易丢上下文。

Phase 5 适合拆，是因为前置骨架已经把全局方向锁住，每一天有清晰边界，且端到端等待时间明显受天数影响。也就是说，多 Agent 的前提不是“系统看起来更高级”，而是任务本身可分、子任务边界清楚、合并标准可写成代码。

### Q：你判断“该不该用多 Agent”的标准是什么？

推荐回答：

我会用五个问题判断：

1. 子任务是否能并行，且互相依赖少？
2. 每个子任务是否能用独立上下文完成？
3. 合并结果是否有明确校验标准？
4. worker 失败是否能局部重试，而不是重跑全局？
5. 并行带来的 latency 收益是否足以覆盖 token / trace / failure complexity 成本？

Phase 5 满足大部分条件：day-level 可拆、repair 可按天重派、全局验证可代码化。Phase 3 candidate/skeleton 则不完全满足，因为候选项之间的取舍需要共享上下文和用户对话反馈。

### Q：如果面试官说“你这个不是真多 Agent，只是并行函数调用”，怎么答？

推荐回答：

我会承认它不是开放式多 Agent 群聊，也不是中央 LLM 动态创建 agent。它是更克制的 sub-agent workflow：每个 Day Worker 都有独立 LLM conversation、独立工具循环、独立上下文和独立提交路径；Orchestrator 负责调度和合并。

“多 Agent”这个词不重要，重要的是它解决了什么工程问题：降低多天行程的 wall-clock latency，同时保持最终写状态的单一权威路径。为了面试表达准确，我会称它为 “parallel worker agents under deterministic orchestration”。

### Q：什么时候明确不应该多 Agent？

推荐回答：

几个场景不应该拆：

- 用户还在澄清需求，任务目标不稳定。
- 子任务之间强依赖，比如住宿区域会改变每日路线。
- 没有可靠的 merge / validation 标准，只能靠模型自己判断合不合并。
- 结果需要统一口吻或全局叙事，而拆分会造成风格不一致。
- 成本和延迟不是主要瓶颈。
- trace 和错误恢复还没准备好，多 worker 只会让故障定位变难。

Travel Agent Pro 的策略是：主 Agent 控制全局状态，只有 Phase 5 的 day-level 生成做并行 worker；最终仍回到标准 writer 和 PhaseRouter。

## 10. A2A 的实际意义

### Q：A2A 对 Travel Agent Pro 有什么实际意义？

推荐回答：

A2A 解决的是“不同平台、不同厂商、不同框架的 Agent 如何发现彼此、交换任务、协作执行”的互操作问题。Google 在 2025 年发布 A2A，Linux Foundation 后续接管项目治理，方向是 secure agent-to-agent communication 和 vendor-neutral interoperability。

对 Travel Agent Pro 来说，A2A 不是拿来替代 Day Worker 的。Day Worker 是同一进程内的内部 worker，有共享代码、共享状态模型和统一 trace。A2A 更适合未来跨系统集成，例如：

- 把签证核查委托给外部 visa specialist agent；
- 把航班改签委托给 airline / booking agent；
- 把企业差旅合规交给报销合规 agent；
- 把日历和邮件协调交给用户授权的 personal assistant agent。

### Q：为什么现在不急着引入 A2A？

推荐回答：

因为当前瓶颈不在跨 Agent 协议，而在本项目自己的 runtime hardening：

- Phase 5 fallback 还没真正串行接管；
- unresolved global validation error 还没阻断提交；
- Worker timeout / exception 错误码不完整；
- trace / stats 还不是完整生产持久化；
- 外部工具可信度和来源分级还需要加强。

在这些基础能力稳定前，引入 A2A 只会把失败传播到跨系统边界。正确顺序是：先把本地 Agent runtime、tool guardrail、trace、eval、approval 做稳，再把 A2A 作为跨系统委托层。

### Q：MCP 和 A2A 的边界怎么讲？

推荐回答：

MCP 更偏“工具和数据如何接入 Agent”：Google Maps、Amadeus、日历、邮件、企业知识库可以变成标准 MCP server。

A2A 更偏“Agent 和 Agent 如何协作”：Travel Agent Pro 把一个子任务交给外部 specialist agent，对方有自己的工具、策略、状态和权限边界。

所以 MCP 是 tool interoperability，A2A 是 agent interoperability。两者都重要，但都必须配套权限、审计、数据最小化和 human approval，尤其是涉及预订、支付、邮件发送这类 write action。

## 11. STAR 题：编排设计与风险修复

### Q：STAR：你如何设计 Phase 5 并行 Orchestrator-Workers？

推荐回答：

- **Situation**：原先 Phase 5 由主 Agent 串行生成每日行程。天数一多，等待时间变长；模型在一个长上下文里同时处理多天路线，也容易重复 POI 或忽略首末日交通约束。
- **Task**：目标是降低多日行程生成的 wall-clock latency，同时不能牺牲状态一致性和 Phase 5 -> 7 的标准推进链路。
- **Action**：我把 Phase 5 拆成 deterministic Orchestrator 和 Day Workers。Orchestrator 从 selected skeleton 编译 `DayTask`，注入 forbidden POI、mobility、budget、arrival/departure 等约束；Worker 只调用只读工具和 `submit_day_plan_candidate`；候选结果写 run-scoped artifact；Orchestrator 做全局验证和最多一轮 re-dispatch；最终 handoff 给 AgentLoop，通过内部 `replace_all_day_plans` 标准工具调用写状态。
- **Result**：并行模式可以逐日生成，同时 `TravelPlanState` 仍只有一个正式写入路径。测试覆盖了 happy path、candidate artifact 优先、handoff commit、写入失败不推进、boundary iteration 进入并行等关键路径。

### Q：STAR：你会如何补齐当前 Phase 5 fallback 风险？

推荐回答：

- **Situation**：`fallback_to_serial` 配置默认 true，但当前高失败率时 Orchestrator 只是 `return`，包装器发 warning，AgentLoop DONE；用户可能看到并行失败但没有自动得到串行结果。
- **Task**：把“降级信号”变成“真实串行接管”，并保证不会重复进入 parallel guard。
- **Action**：我会给本轮 run 增加一个 `phase5_parallel_attempted` 或 `force_serial_phase5` 标记。Orchestrator 高失败率时返回结构化 fallback outcome，而不是空 handoff；AgentLoop 收到后临时禁用 parallel guard，恢复 Phase 5 串行工具列表，让主 Agent 继续用 `save_day_plan` / `replace_all_day_plans` 生成，或明确提示用户重试。SSE 上把 internal task 标为 warning 后继续串行 thinking，而不是 DONE。
- **Result**：用户不再因为 worker 大面积失败看到空状态；trace 能区分 parallel failure、serial fallback start、serial commit；golden eval 可以覆盖“worker 失败超过 50% 仍生成 daily plans”的回归。

### Q：STAR：你如何处理 re-dispatch 后仍有全局 error 的问题？

推荐回答：

- **Situation**：当前 re-dispatch 后 unresolved error 只记录 warning，仍可能把 `final_dayplans` handoff 给 AgentLoop。这对 demo 是可观察的，但生产上可能把有时间冲突或跨天重复的行程提交给用户。
- **Task**：让全局验证成为真正的提交 gate，而不是摘要提醒。
- **Action**：我会把 `_global_validate()` 的结果分成 `blocking_errors` 和 `advisory_warnings`。第一次 error 触发 re-dispatch；第二次仍有 blocking error 时，不设置 `final_dayplans`，返回 structured failure：可修复的进入串行 fallback，不可修复或 `NEEDS_PHASE3_REPLAN` 进入标准 backtrack。前端展示“需要重新调整骨架/重新规划第 N 天”，而不是显示完成。
- **Result**：并行输出只有通过 global validation 才会进入 writer path；失败路径也能被 trace/eval 评分，避免“看起来完成但实际有硬错误”。

### Q：STAR：你怎么把 Worker `error_code` 这条 hardening 链路落地？

推荐回答：

- **Situation**：当前 Worker 已经能产出 `JSON_EMIT_FAILED` / `REPEATED_QUERY_LOOP` / `RECOVERY_CHAIN_EXHAUSTED` / `NEEDS_PHASE3_REPLAN` / `SUBMIT_UNAVAILABLE` / `INVALID_DAYPLAN` 等结构化错误码，但 `TimeoutError` 和 generic `Exception` 分支只填 `error` 文本（`backend/agent/phase5/day_worker.py:548-565`），Orchestrator 无法基于错误类型自动做差异化降级；同时前端 `ParallelWorkerStatus.status` union 也缺 `redispatch`（`frontend/src/types/plan.ts:239`），运行时可能出现图标缺失。
- **Task**：把 Worker 失败语义补齐成可被 Orchestrator 路由、被前端展示、被 trace/eval 断言的一等数据。
- **Action**：第一步在 Worker 兜底分支上补 `error_code="TIMEOUT"` 和 `error_code="LLM_ERROR"`（区分 provider error vs JSON parse vs 真正 unknown）；第二步在 Orchestrator 的 retry/redispatch 逻辑里按 `error_code` 路由——`TIMEOUT` 增加 `worker_timeout_seconds` 后单次重试，`LLM_ERROR` 直接重派，`NEEDS_PHASE3_REPLAN` 走 backtrack，`JSON_EMIT_FAILED` 不重试避免 token 浪费；第三步把 `redispatch` 加进 frontend `ParallelWorkerStatus.status` union 并补 `STATUS_ICON` / `renderTail` / CSS；第四步在 `SessionStats` 里按 `error_code` 计数，写进 trace；第五步给 golden eval 加“worker timeout / LLM error / redispatch UI”三个回归 case。
- **Result**：失败路径从“一段文本 + warning”变成可路由的结构化事件；Orchestrator 能在不同失败类型上做不同决策，前端能解释“系统正在修复第 3 天”，trace/eval 能跨 run 统计某类错误的发生率，作为 prompt/工具质量回归指标。这条链路本身不改架构，只是把已有的边界数据补完整。

## 12. 成本、延迟与生产化

### Q：Phase 5 并行是降低延迟还是增加成本？

推荐回答：

主要是降低 wall-clock latency，不是天然降低总成本。

并行时 N 个 Day Worker 各自运行 LLM loop、工具调用和修复轮次，总 token 往往比一个串行上下文更多。shared prefix / KV-cache 可以减少重复 prefix 的 prefill 成本和 TTFT，但不能消除每个 worker 的输出 token、工具结果 token 和独立决策 token。

所以生产化不应该一刀切并行。可以按天数、用户等待容忍度、模型价格、当前系统负载和预算动态决定：

- 1-2 天短行程：串行通常够了。
- 3-7 天标准行程：并行收益明显。
- 超长行程：需要分批并行和更严格的预算上限。

### Q：如果上线给真实用户，编排层第一批改什么？

推荐回答：

我会按用户可见风险排序：

1. Phase 5 fallback 真串行接管。
2. re-dispatch 后 unresolved error 阻断提交。
3. Worker timeout / generic exception 补结构化 `error_code`。
4. `NEEDS_PHASE3_REPLAN` 接标准 backtrack 工具路径。
5. `redispatch` 前端状态补齐。
6. cached input tokens / worker cost / latency 写入 `SessionStats`。
7. trace 持久化，保留每个 worker 的 run_id、attempt、artifact path、error_code 和 validation issue。
8. 按天数和预算做 parallel / serial 动态路由。

如果只能做一个，我会先修 fallback，因为它直接决定 Phase 5 主路径失败时用户是否能拿到结果。

### Q：如何为这套编排设计 eval？

推荐回答：

不能只评最终 markdown，要评 trajectory：

- 是否在 Phase 5 入口触发 parallel guard；
- 是否为每个 skeleton day 生成一个 `DayTask`；
- Worker 是否只使用 read tools 和 `submit_day_plan_candidate`；
- candidate artifact 是否按 run / day / attempt 写入；
- 跨天重复 POI 是否被 global validation 捕获；
- error severity 是否触发 re-dispatch；
- handoff 是否走 `replace_all_day_plans`；
- Phase 5 -> 7 是否通过标准 `PHASE_TRANSITION`；
- fallback case 是否没有写入残缺状态。

这类 eval 比只看“最终行程像不像”更接近生产 Agent 质量，因为真实风险经常发生在工具选择、状态写入和 handoff 中间。

## 13. 代码风险快问快答

### Q：当前 `fallback_to_serial` 和配置描述一致吗？

推荐回答：

不完全一致。配置和文档语义像是真串行降级，但代码只是高失败率时发 progress chunk 后返回；AgentLoop 没有同轮启动串行 Phase 5。这是 production hardening 点。

### Q：global validation unresolved error 会阻断最终提交吗？

推荐回答：

当前不会稳定阻断。re-dispatch 后仍有 error 时只是 log unresolved，并把 issues 带入 summary / handoff。生产化应把 error severity 变成 blocking gate。

### Q：Worker timeout 是否有结构化 error_code？

推荐回答：

当前没有。`TimeoutError` 分支返回 `error="Worker 超时 (...)"`，但没填 `error_code="TIMEOUT"`。generic exception 也类似。这会影响 Orchestrator 自动诊断。

### Q：candidate store 会不会写坏路径？

推荐回答：

它有 `_SAFE_SEGMENT_RE` 校验 `session_id`、`run_id`、`worker_id`，拒绝 `../evil`、带 slash、空字符串、点号等 unsafe segment。写入用 tmp file + `os.replace()`，基本能防半写入和路径穿越。

### Q：为什么 `replace_all_day_plans` 失败后不继续调用 LLM 修复？

推荐回答：

当前内部 commit 是 deterministic handoff，不走 LLM 决策；写入失败时 AgentLoop 输出明确警告并 DONE。这避免在状态不明时让模型继续编，但体验上不够自动。生产化可以把 writer error 转成 repair task，回到串行 Phase 5 或重派对应 day。

### Q：前端并行进度有哪些已知缺口？

推荐回答：

主要是 `redispatch` 状态缺类型和图标映射；另外 `ParallelProgress` 只展示当前工具和活动数，没有展示 `error_code`，诊断信息仍偏少。可以在失败状态下展示短错误码，帮助用户理解是超时、重复查询、还是需要回到 Phase 3。

### Q：MCP / A2A 引入后，indirect prompt injection 的防御边界在哪？

推荐回答：

Phase 5 worker 是远程工具 / MCP server 的天然受害者：worker 上下文里只剩 `shared_prefix` + `day_suffix` + tool results，没有用户在场纠偏，模型一旦把 “tool result 里的伪指令” 当真，会触发 forced emit 或反复重查。所以即便接 MCP / A2A，下面四条边界必须保持：

1. **Worker 工具集只读** —— `_WORKER_TOOL_NAMES` 显式白名单 8 个工具（`backend/agent/phase5/day_worker.py:570-579`），没有任何 plan writer 或 `request_backtrack`；即便 tool result 注入“调用 replace_all_day_plans”payload，worker 也根本没有这个工具可调，是结构性而非提示性的防御。
2. **正式写入只走 `replace_all_day_plans`** —— Orchestrator handoff 后由 AgentLoop 构造内部 tool call，writer schema 校验在确定性代码层；任何“tool result 里说已经决定了”都不能成为状态来源。
3. **Tool result sanitization 当前还没做** —— 这是要主动暴露的 gap：UGC 内容（`xiaohongshu_*`、`web_search`）以及未来 MCP server 返回原文进上下文，没有剥可疑指令、没有 domain 白名单、没有长度上限。生产化要在 `ToolEngine.execute_*` 出口做一层 sanitize，并在 trace 里记录每条 tool result 的来源域名和 trust tier。
4. **A2A 跨系统委托默认 require approval** —— 把签证核查、订机票、改邮箱这类 high-risk action 委托给外部 specialist 时，`approval_required` 必须默认 true；接收方返回的“已完成”不能直接落 `TravelPlanState`，仍要走 writer contract + audit log。

我把这套思路概括为：MCP 解决“工具如何接进来”，A2A 解决“agent 如何协作”，但两者都不解决“不可信内容如何不污染决策”——这层防御必须由 host agent 自己设计。项目目前靠读写隔离顶住了 Phase 5 worker 这个最暴露的面，sanitization 和 source trust 是下一步要补的层。

## 14. 面试收尾表达

### Q：如果面试官问“这套编排最大的价值是什么”，怎么收束？

推荐回答：

我会说最大价值不是“用了多 Agent”，而是把多 Agent 控制在可验证边界内：拆分是确定性的，worker 权限是只读的，候选产物是 run-scoped 的，全局校验是代码化的，最终写入回到标准 writer path。这样既利用并行降低等待时间，又不让多个模型实例直接争抢业务状态。

这也是我对 Agent 工程的核心理解：真正难的不是让模型多跑几步，而是让每一步都有权威来源、失败边界、观测证据和可回归测试。

### Q：如果被追问“哪里还不够生产级”，怎么答？

推荐回答：

我会主动说四点：

1. fallback 还不是同轮串行接管。
2. re-dispatch 后 unresolved error 还没阻断提交。
3. Worker timeout / generic exception 缺结构化错误码。
4. shared prefix cache 只是设计目标，还没有 cached token telemetry。

这些不是设计上无法解决，而是当前阶段还没 harden。项目已经把边界留出来了：`Phase5ParallelHandoff`、candidate artifact、worker status、global validation issue、internal task 和 standard writer path 都是补齐生产化的插入点。
