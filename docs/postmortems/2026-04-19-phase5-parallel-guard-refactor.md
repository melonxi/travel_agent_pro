# Phase 5 并行路由「热切换守卫」重构复盘

- 评审日期：2026-04-19
- 关联 commit：`80a7398 feat(phase5): re-enter parallel orchestrator right after phase3→5 transition`
- 涉及范围：`backend/agent/loop.py`（`run()` 入口 + 三处 phase-transition continue 点）、`backend/tests/test_agent_loop.py`
- 类型：已合并改动的结构性回顾 + 简化重构 + 外部设计参照
- 等级：低（结构性重构；主路径行为等价，需注意 `max_retries` 边界）
- 状态：本次一并落地

---

## 1. 背景

Phase 5 支持两种执行路径：

- **并行编排**（`Phase5Orchestrator`）：共享 KV-cache prefix、并发 Day Worker、全局校验（POI 去重 / 预算 / 天数覆盖）。
- **串行 LLM 主循环**（`AgentLoop.run()` 主体）：逐天让 LLM 自己规划。

是否走并行由 `should_use_parallel_phase5(plan, config)` 判断，条件包括 `plan.phase == 5`、`selected_skeleton_id` 已锁定、`daily_plans` 为空等。

`80a7398` 之前，`run()` 只在入口判一次。问题：进入 `run()` 那一刻 `plan.phase == 3`，**本轮某个写入工具**（例如 `lock_skeleton` / `promote_to_phase5` / 或 `phase_router` 规则推进）把 phase 升到 5 后，原代码只 `continue`，下一次迭代就继续用 phase 5 的工具集让 LLM 串行生成每天行程——orchestrator 被完全绕过。

`80a7398` 的补丁是：在三处可能把 phase 改到 5 的 `continue` 点前，各加一段"如果升到了 5 就跳到 orchestrator 并 return"的判断，并抽取 `_run_parallel_phase5_orchestrator` 消除方法体重复。

---

## 2. 原改动的结构性问题

### 2.1 判断逻辑被分散到 4 个点

并行路由决策散落在：

| 位置 | 触发场景 |
|------|----------|
| `run()` 入口（`loop.py:136`） | 冷启动（用户请求进来时已是 phase 5） |
| 回溯 `continue` 前（`loop.py:469`） | backtrack 写工具把 phase 推进 |
| plan 写工具 `continue` 前（`loop.py:508`） | `promote_to_phase5` 等直接改 phase |
| `phase_router` `continue` 前（`loop.py:555`） | 规则自动推进 |

阅读者需要扫完整个 `run()` 才能回答"并行会在哪些时刻被触发"。

### 2.2 同样 5 行出现三次

抽出的 `_run_parallel_phase5_orchestrator` 只压掉了方法体重复，**调用点本身**仍然是三处一模一样的：

```python
if self.should_use_parallel_phase5(self.plan, self.phase5_parallel_config):
    async for chunk in self._run_parallel_phase5_orchestrator():
        yield chunk
    return
```

### 2.3 对新增 phase 切换路径不友好

未来如果再增加第 4 条让 phase 升到 5 的路径（例如新的 hook、新的写工具、从 phase 6/7 回跳），又得同步在那处加第 4 个守卫。漏一处 = 悄悄退化回串行，且没有任何信号。

### 2.4 概念耦合

串行主循环的三个 `continue` 点里散落着"跳车去并行"的逃生口，把两个本该正交的概念（"serial 循环推进"和"切换执行策略"）绑在一起。

---

## 3. 主路径等价的更优写法

三处 `continue` 的含义等于"回到 `for iteration in range(...)` 循环顶部"。所以**如果把守卫放在循环体第一行**，在非最后一次 iteration 的主路径上，行为与"continue 前守卫再 return"等价，但只写一次。

```python
for iteration in range(self.max_retries):
    # 冷启动 + 热切换统一入口
    if self.should_use_parallel_phase5(self.plan, self.phase5_parallel_config):
        async for chunk in self._run_parallel_phase5_orchestrator():
            yield chunk
        return
    # ... 原有 serial 迭代体 ...
```

同时 `run()` 入口那段初始的 `if should_use_parallel_phase5` 也可以删掉——第 0 次迭代进到循环顶部时守卫会接住冷启动。

### 3.1 收益对比

| 维度 | 原 `80a7398` 写法 | 循环顶部守卫 |
|------|-------------------|-------------|
| 路由判断散布点 | 4 处 | 1 处 |
| 调用代码行数 | 4 处重复 | 1 处 |
| 新增 phase 切换路径 | 需要同步改 loop | 自动接住 |
| 阅读者心智 | "serial 里有逃生口" | "每轮先问一次策略" |
| 行为 | phase 变化后立刻 return | `continue` → 下一轮顶部 return（非最后一轮） |
| 运行时开销 | 同 | 每迭代多 1 次 dict 读（可忽略） |

### 3.2 主路径等价性证明

原写法：

1. write-tool 把 phase 改成 5
2. 守卫判定为真 → return

新写法：

1. write-tool 把 phase 改成 5
2. `continue`
3. 循环顶部守卫判定为真 → return

在 `iteration < max_retries - 1` 的情况下，差别只在"return 发生的栈位置"——一个在 `continue` 前，一个在下一轮循环顶部。对外部可观察行为（yield 的 chunk 序列、最终状态）没有差别，因为 `continue` 到下一轮循环顶部之间没有任何业务代码。

但这不是无条件的严格等价：如果 phase 3→5 的热切换发生在最后一次 iteration，旧写法会在 `continue` 前立刻进入 orchestrator；循环顶部守卫写法会先退出 `for`，再落到 safety-limit 分支。这个边界不影响当前主路径测试，但需要在风险里明确记录。

---

## 4. 外部设计参照

本次判断与主流 agent runtime 的设计取向一致：**状态变化之后的执行策略选择，通常放在 agent loop / runner / orchestrator 的控制边界，而不是分散在每个可能改变状态的工具后面。**

### 4.1 OpenAI Agents SDK

OpenAI Agents SDK 的 Runner 文档描述了一个明确的 agent loop：调用当前 agent 的模型；如果是 final output 就结束；如果发生 handoff，就切换当前 agent 并重新进入循环；如果有 tool calls，就执行工具、追加结果并重新进入循环；超过 `max_turns` 则触发 `MaxTurnsExceeded`。[Running agents - OpenAI Agents SDK](https://openai.github.io/openai-agents-python/running_agents/)

这给本问题的启发是：

- tool 结果进入状态后，下一步执行者的选择发生在 runner loop 边界。
- handoff / tool result 都是"更新状态 → 回到 loop → 重新判定"。
- `max_turns` 是一等控制条件，因此本次循环顶部守卫也必须认真处理 `max_retries` 最后一轮边界。

### 4.2 Codex

Codex Subagents 文档说明，Codex 可以生成并行 subagent workflow，由 Codex 负责 spawn、路由后续指令、等待结果、关闭 agent threads，并在结果可用后合并响应；同时 Codex 只会在用户明确要求 subagents 或 parallel agent work 时创建 subagent。[Subagents - Codex](https://developers.openai.com/codex/subagents)

Codex 的 subagent concepts 还建议：主线程保留需求、决策和最终输出；并行 subagents 适合 exploration、tests、triage、summarization 等读多写少任务；并行写入要谨慎，因为会增加冲突和协调成本。[Subagents concepts - Codex](https://developers.openai.com/codex/concepts/subagents)

这与 Phase 5 当前结构一致：

- `AgentLoop` / `Phase5Orchestrator` 保留最终控制权。
- Day Worker 并行生成局部结果。
- 最终状态写入由 orchestrator 汇总后统一完成，避免多个 worker 并发写 `daily_plans`。

Codex customization 文档也把项目指导、skills、MCP、subagents 视为互补层，其中 subagents 用于委派专门任务，而不是把所有策略判断写进每个工具实现。[Customization - Codex](https://developers.openai.com/codex/concepts/customization)

### 4.3 Claude Code

Claude Code subagents 文档强调，每个 subagent 有独立 context window、专门 system prompt、工具权限和权限模式；Claude 会根据 subagent description 决定何时委派，subagent 完成后只返回摘要，以保护主上下文。[Create custom subagents - Claude Code Docs](https://code.claude.com/docs/en/sub-agents)

Claude Code hooks 文档把控制点定义为生命周期事件：session、turn、tool call、subagent、task 等边界。`PreToolUse` 可以在工具执行前 allow / deny / ask / defer；`Stop` / `SubagentStop` 可以阻止 agent 停止并要求继续。[Hooks reference - Claude Code Docs](https://code.claude.com/docs/en/hooks)

Claude Code agent teams 文档则把团队结构显式拆成 team lead、teammates、task list、mailbox；team lead 负责创建团队、spawn teammates、协调工作，task claiming 通过 file locking 防止竞争，并可用 hooks 在 teammate idle / task completed 等边界执行质量门禁。[Orchestrate teams of Claude Code sessions](https://code.claude.com/docs/en/agent-teams)

这些设计共同支持一个原则：策略切换、质量门禁和停止条件应放在稳定的生命周期边界，而不是散落在每个生产状态变化的局部工具后。

### 4.4 Anthropic agent pattern

Anthropic 的《Building effective agents》把常见 agentic patterns 拆成 routing、parallelization、orchestrator-workers、evaluator-optimizer 等。Routing 是把输入分类后送到专门后续任务；parallelization 是并行处理可拆分任务再聚合；orchestrator-workers 是中央协调者拆分任务、委派 worker、综合结果；agent loop 则依赖环境反馈多轮行动，并用最大迭代等停止条件保持控制。[Building effective agents - Anthropic](https://www.anthropic.com/engineering/building-effective-agents)

Phase 5 并行模式正是 routing + orchestrator-workers：

- `should_use_parallel_phase5` 是 routing predicate。
- `Phase5Orchestrator` 是中央调度器。
- Day Worker 是并行 workers。
- 全局验证和统一写入是 evaluator / guardrail / reducer。

因此，循环顶部守卫的设计方向与这些主流模式一致；需要补强的不是"每个生产点都加守卫"，而是把 runner 边界的停止条件处理完整。

---

## 5. 为什么不采用 PhaseExecutor 模式

评审中也考虑过更大的重构：把每个 phase 的执行策略抽成 `PhaseExecutor`，`run()` 只做分派（"看 plan.phase 选 executor 跑"）。

评估结论：**暂不做**。

- 目前只有 phase 5 有策略分叉（并行 vs 串行），其他 phase 都只走 serial 循环。只为一个分叉就引入抽象，违反三次重复再抽象的原则。
- 本次重构只需要挪 1 行、删 4 处，收益已经足够，不必过度设计。
- 如果未来 phase 7 或新 phase 也出现执行策略分叉，再升级到 Executor 模式也不迟——届时"每个循环顶部的路由查询"可以一步自然演化为"每个循环顶部的分派查询"。

---

## 6. 落地变更

### 6.1 `backend/agent/loop.py`

- 删除 `run()` 入口那次 `should_use_parallel_phase5` 判断（冷启动交给循环顶部）。
- 在 `for iteration in range(self.max_retries):` 首行加入统一守卫。
- 删除回溯 `continue`（`loop.py:469`）、plan 写工具 `continue`（`loop.py:508`）、`phase_router` `continue`（`loop.py:555`）前的三处重复守卫。
- `_run_parallel_phase5_orchestrator` 保留（循环顶部还是要调用它）。

### 6.2 `backend/tests/test_agent_loop.py`

原测试 `test_phase3_to_phase5_transition_rechecks_parallel_routing` 断言的是"phase 3→5 后 orchestrator 被触发一次且拿到正确输出"——这是**可观察行为契约**，新写法不改变契约，测试无须修改。

---

## 7. 回归风险

### 7.1 风险 1：循环最大轮次耗尽

`for iteration in range(self.max_retries)` 有上限。假设连续若干轮都因 write-tool 推进 phase 而 `continue`，或者 phase 3→5 的热切换恰好发生在最后一次 iteration，就可能耗尽迭代次数。

这里有一个真实边界差异：

- 旧写法里命中守卫直接 `return`，不会再消耗下一轮。
- 新写法里 `continue` 之后需要下一轮顶部才能 `return`，会多消耗一次循环计数。

当前测试和真实主路径通常只需要一次 phase 推进，默认 `max_retries=3` 下风险很低；但如果要做到严格等价，建议在 safety-limit 输出前再兜底检查一次：

```python
if self.should_use_parallel_phase5(self.plan, self.phase5_parallel_config):
    async for chunk in self._run_parallel_phase5_orchestrator():
        yield chunk
    return
```

这会把"最后一轮热切换"也收回 orchestrator，同时保持路由判断仍然集中在 runner 边界。

### 7.2 风险 2：循环顶部守卫在非 phase 5 冷启动时多做一次 dict 读

`should_use_parallel_phase5` 先检查 `plan.phase != 5 → False`，是 O(1) 字段读。每轮多一次无影响。

### 7.3 风险 3：测试是否覆盖新的"热切换走循环顶部"路径

现有测试 mock 的 LLM 只会产生一次写工具调用（`promote_to_phase5`），从而触发一次 `continue` → 一次循环顶部守卫 → 一次 orchestrator 调用。这恰好覆盖新路径，断言"parallel_calls == 1"仍然成立。

---

## 8. 事后小结

本次重构是对 `80a7398` 的**结构性修正**，不是功能修复。原改动的问题不在"做错了"，而在"写法增加了未来的维护负担"：

- 决策点从 1 处膨胀为 4 处。
- 每多一条让 phase 升到 5 的路径，就要再加一份守卫。
- 串行主循环里夹着"跳车"逃生口，读代码时心智成本增加。

**一个简单的观察**：`continue` 通常把控制流送回循环顶部。如果"是否切换执行策略"只关心"plan 当前的 phase"这一事实，那么这个判断的最佳位置就是 runner / loop 边界，而不是每一处可能改动 phase 的写入点后面。

这条经验对未来类似场景（任何"plan 状态变化 → 要触发不同执行路径"的场合）都适用：**守卫在消费点做一次，而不是在每个生产点都做一次**。同时，消费点守卫必须和停止条件一起设计，避免最后一轮热切换被 safety-limit 误吞。
