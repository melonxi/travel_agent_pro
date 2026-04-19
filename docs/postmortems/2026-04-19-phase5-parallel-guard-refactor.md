# Phase 5 并行路由「热切换守卫」重构复盘

- 评审日期：2026-04-19
- 关联 commit：`80a7398 feat(phase5): re-enter parallel orchestrator right after phase3→5 transition`
- 涉及范围：`backend/agent/loop.py`（`run()` 入口 + 三处 phase-transition continue 点）、`backend/tests/test_agent_loop.py`
- 类型：已合并改动的结构性回顾 + 简化重构
- 等级：低（行为等价重构，无用户可见变化）
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

## 3. 等价的更优写法

三处 `continue` 的含义等于"回到 `for iteration in range(...)` 循环顶部"。所以**如果把守卫放在循环体第一行**，行为与"continue 前守卫再 return"完全等价，但只写一次。

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
| 行为 | phase 变化后立刻 return | `continue` → 下一轮顶部 return |
| 运行时开销 | 同 | 每迭代多 1 次 dict 读（可忽略） |

### 3.2 行为等价性证明

原写法：

1. write-tool 把 phase 改成 5
2. 守卫判定为真 → return

新写法：

1. write-tool 把 phase 改成 5
2. `continue`
3. 循环顶部守卫判定为真 → return

差别只在"return 发生的栈位置"——一个在 continue 前，一个在 continue 后。对外部可观察行为（yield 的 chunk 序列、最终状态）没有任何差别，因为 continue 到循环顶部之间没有任何代码。

---

## 4. 为什么不采用 PhaseExecutor 模式

评审中也考虑过更大的重构：把每个 phase 的执行策略抽成 `PhaseExecutor`，`run()` 只做分派（"看 plan.phase 选 executor 跑"）。

评估结论：**暂不做**。

- 目前只有 phase 5 有策略分叉（并行 vs 串行），其他 phase 都只走 serial 循环。只为一个分叉就引入抽象，违反三次重复再抽象的原则。
- 本次重构只需要挪 1 行、删 4 处，收益已经足够，不必过度设计。
- 如果未来 phase 7 或新 phase 也出现执行策略分叉，再升级到 Executor 模式也不迟——届时"每个循环顶部的路由查询"可以一步自然演化为"每个循环顶部的分派查询"。

---

## 5. 落地变更

### 5.1 `backend/agent/loop.py`

- 删除 `run()` 入口那次 `should_use_parallel_phase5` 判断（冷启动交给循环顶部）。
- 在 `for iteration in range(self.max_retries):` 首行加入统一守卫。
- 删除回溯 `continue`（`loop.py:469`）、plan 写工具 `continue`（`loop.py:508`）、`phase_router` `continue`（`loop.py:555`）前的三处重复守卫。
- `_run_parallel_phase5_orchestrator` 保留（循环顶部还是要调用它）。

### 5.2 `backend/tests/test_agent_loop.py`

原测试 `test_phase3_to_phase5_transition_rechecks_parallel_routing` 断言的是"phase 3→5 后 orchestrator 被触发一次且拿到正确输出"——这是**可观察行为契约**，新写法不改变契约，测试无须修改。

---

## 6. 回归风险

### 6.1 风险 1：循环最大轮次耗尽

`for iteration in range(self.max_retries)` 有上限。假设连续若干轮都因 write-tool 推进 phase 而 `continue`，理论上能耗尽迭代次数——但：

- 旧写法里命中守卫直接 `return`，**根本走不到下一轮**。
- 新写法里 `continue` 之后下一轮顶部立刻 `return`，**也只多消耗一次循环计数**。

且 phase 只会单调推进（3 → 5 → 7），不会反复触发 continue。风险可忽略。

### 6.2 风险 2：循环顶部守卫在非 phase 5 冷启动时多做一次 dict 读

`should_use_parallel_phase5` 先检查 `plan.phase != 5 → False`，是 O(1) 字段读。每轮多一次无影响。

### 6.3 风险 3：测试是否覆盖新的"热切换走循环顶部"路径

现有测试 mock 的 LLM 只会产生一次写工具调用（`promote_to_phase5`），从而触发一次 `continue` → 一次循环顶部守卫 → 一次 orchestrator 调用。这恰好覆盖新路径，断言"parallel_calls == 1"仍然成立。

---

## 7. 事后小结

本次重构是对 `80a7398` 的**结构性修正**，不是功能修复。原改动的问题不在"做错了"，而在"写法增加了未来的维护负担"：

- 决策点从 1 处膨胀为 4 处。
- 每多一条让 phase 升到 5 的路径，就要再加一份守卫。
- 串行主循环里夹着"跳车"逃生口，读代码时心智成本增加。

**一个简单的观察**：`continue` 总是把控制流送回循环顶部。如果"是否切换执行策略"只关心"plan 当前的 phase"这一事实，那么这个判断的最佳位置就是循环顶部，而不是每一处可能改动 phase 的写入点后面。

这条经验对未来类似场景（任何"plan 状态变化 → 要触发不同执行路径"的场合）都适用：**守卫在消费点做一次，而不是在每个生产点都做一次**。
