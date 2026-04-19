# Phase 5 并行路由「最后一轮热切换」边界兜底设计

日期：2026-04-19
关联 postmortem：`docs/postmortems/2026-04-19-phase5-parallel-guard-refactor.md`（第 7.1 节）
关联 commit：
- 前序结构性重构：`6d17027 refactor(phase5): hoist parallel-routing guard to loop top`
- 本次兜底落地：`21792af feat(phase5): route boundary phase→5 promotion to parallel orchestrator`
- 附带的 e2e baseline 修复：`0899ebb test(e2e): refresh golden-path expectations for current handoff + phase5 flow`

> 本文档为事后补档。实际实现先于 spec/plan 落地，出于流程闭环目的把需求、设计与验收条件显式化。

---

## 1. 背景

`6d17027` 将 Phase 5 并行路由的守卫从 4 处散落收敛为循环顶部 1 处，同时保持主路径的可观察行为等价。该重构的复盘（`2026-04-19-phase5-parallel-guard-refactor.md` 第 3.2 节）指出存在一处**非严格等价**的边界：

> 如果 phase 3→5 的热切换发生在最后一次 iteration，旧写法会在 `continue` 前立刻进入 orchestrator；循环顶部守卫写法会先退出 `for`，再落到 safety-limit 分支。这个边界不影响当前主路径测试，但需要在风险里明确记录。

具体失败链路：

1. 用户在某一轮对话触发 phase 3→5 的写入工具（如 `set_accommodation` + skeleton 已就绪）。
2. 工具在最后一次 `for iteration in range(self.max_retries)` 迭代里执行。
3. `phase_router.check_and_apply_transition` 将 `plan.phase` 改为 5。
4. `continue` 把控制流送回 `for` 顶部——但此时 `range` 已迭代完，循环正常退出。
5. 控制流落到 `yield LLMChunk(TEXT_DELTA, "[达到最大循环次数，请重新发送消息]")`，用户看到错误提示。
6. Phase 5 orchestrator 从未被触发，`daily_plans` 空空如也。

在默认 `max_retries=3` 下这种"刚好踩在最后一轮"概率较低，但不为零；随着 Phase 3 写工具数量增加、用户消息越来越"一口气把所有前置决策都给了"，踩中概率会升高。

postmortem 第 7.1 节已给出显式建议：

```python
if self.should_use_parallel_phase5(self.plan, self.phase5_parallel_config):
    async for chunk in self._run_parallel_phase5_orchestrator():
        yield chunk
    return
```

本次工作把这段建议落地。

---

## 2. 目标

1. 消除 `6d17027` 留下的"最后一轮热切换"边界差异，使循环顶部守卫写法与 `80a7398` 原散布守卫在**所有** `max_retries` 取值下保持可观察行为等价。
2. 守卫判断仍然只出现在 runner 边界（循环顶部 + 循环结束两处），不回退到"每个 phase-changing continue 前都加一遍"的老路。
3. 加一个显式覆盖该边界的单元用例，确保未来重构不会无声回归。

### 非目标

- 不改动 `should_use_parallel_phase5` 的判定条件。
- 不改动 Phase 5 orchestrator 的任何行为。
- 不扩大 `max_retries` 的默认值来"兜住"这个风险——那是补丁而不是修复。
- 不重写 `AgentLoop.run()` 的整体结构（例如提炼 `PhaseExecutor`），理由见 postmortem 第 5 节。

---

## 3. 设计

### 3.1 位置

把兜底守卫放在 `for iteration in range(self.max_retries)` **循环结束之后、safety-limit `yield` 之前**：

```python
for iteration in range(self.max_retries):
    # Loop-top guard (6d17027)
    if self.should_use_parallel_phase5(self.plan, self.phase5_parallel_config):
        async for chunk in self._run_parallel_phase5_orchestrator():
            yield chunk
        return
    # ... serial iteration body ...

# ↓↓ 本次新增：最后一轮热切换兜底 ↓↓
if self.should_use_parallel_phase5(self.plan, self.phase5_parallel_config):
    async for chunk in self._run_parallel_phase5_orchestrator():
        yield chunk
    return

# Safety limit reached
yield LLMChunk(type=ChunkType.TEXT_DELTA, content="[达到最大循环次数，请重新发送消息]")
yield LLMChunk(type=ChunkType.DONE)
```

### 3.2 为什么复用同一个谓词

`should_use_parallel_phase5` 是一个纯静态判定（`phase == 5` + `selected_skeleton_id` + `skeleton_plans` + `daily_plans` 为空 + config.enabled）。循环顶部与循环末尾用的是同一个状态问题，没有理由引入第二套判断逻辑。

保持两处调用点使用**同一个谓词**意味着：

- 路由条件未来演化（例如加入新的前置要求）只需要改一处。
- 兜底守卫和循环顶部守卫对行为的影响严格同步。

### 3.3 为什么不改成 `while` / 其他循环形式

- `while` 会让"上限"变得隐式。`for iteration in range(self.max_retries)` 明确了 safety limit，是个好约束。
- 循环末尾加兜底的写法成本 ~5 行，不破坏现有结构。

### 3.4 控制流正确性论证

设 `N = self.max_retries`。考虑三种情况：

| 情况 | phase 何时变为 5 | 旧写法（80a7398 + 6d17027） | 本次兜底后 |
|------|-----------------|---------------------------|-----------|
| A | 第 k<N-1 轮的写入工具 | 第 k+1 轮顶部守卫命中 → orchestrator | 同 |
| B | 第 N-1 轮（最后一轮）的写入工具 | 循环自然退出 → safety-limit 错误 | **循环结束兜底命中 → orchestrator** |
| C | 永不变为 5 | safety-limit 或 serial 终结 | 同（兜底谓词返回 False，跳过） |

情况 B 就是本次修复的目标场景；情况 A、C 未被影响。

---

## 4. 验收条件

- `AgentLoop` 在 `max_retries=1`、单次 LLM 调用触发 phase 3→5 写入工具的场景下必须：
  - 调用 `_run_parallel_phase5_orchestrator`（可用 monkey-patch 观测）。
  - 不 yield 含 `"达到最大循环次数"` 的 `TEXT_DELTA` chunk。
- 全量 `backend/pytest` 不出现新的失败。
- 不新增 orchestrator 路径上的任何副作用（orchestrator 内部不知道自己是被循环顶部还是循环末尾调用的）。
- 新增测试与 `test_loop_phase5_routing.py` 已有静态谓词测试**拓扑对齐**（同一文件、同一命名前缀可读），但与静态测试互补：它测的是动态 loop 行为，不是谓词本身。

---

## 5. 测试设计

### 5.1 新增用例

`backend/tests/test_loop_phase5_routing.py`：`test_parallel_orchestrator_fires_after_final_iteration_phase_promotion`

关键结构：

- 构造 `TravelPlanState(phase=3)`，预先填好 dates / skeleton_plans / selected_skeleton_id / accommodation，使得一旦 `phase` 被改成 5，`should_use_parallel_phase5` 的其它条件全部满足。
- 使用 `_PromotingRouter` 测试替身：任意一次 `check_and_apply_transition` 都会把 phase 从 3 改到 5。
- 注册一个 `set_accommodation` 同名的空壳写入工具——loop 内部 `tc.name in PLAN_WRITER_TOOL_NAMES` 才会把 `saw_state_update` 置 True，进而触发 router。这是当前 loop 的真实契约，用真实名字比再扩大 `PLAN_WRITER_TOOL_NAMES` 更稳。
- `max_retries=1` 强制边界场景。
- 用 `agent._run_parallel_phase5_orchestrator = _fake_orchestrator` 做观测点。
- 断言 orchestrator fired + safety-limit text 未出现。

### 5.2 已有用例的覆盖关系

`test_loop_phase5_routing.py` 里原有 6 个静态谓词测试继续覆盖 `should_use_parallel_phase5` 的真值表。本次新增用例填补"谓词 × 循环边界"的动态行为空白。

### 5.3 不覆盖 / 故意留白

- 不测试"非最后一轮"路径——那是循环顶部守卫的责任，已被 `test_agent_loop.py::test_phase3_to_phase5_transition_rechecks_parallel_routing` 覆盖。
- 不测试"兜底守卫处 config 为 None 跳过"——已被 `test_should_not_use_parallel_when_config_is_none` 静态测试覆盖，再测一次是重复。

---

## 6. 回归风险

### 6.1 多计一次迭代

`6d17027` 的循环顶部写法 + 本次兜底意味着"最后一轮写 → 下一轮顶部 return"的路径会**多消耗一次循环计数**。在 `max_retries=1` 场景下，兜底必须在循环外命中（因为没有"下一轮顶部"可用），这正是本次设计覆盖的边界。

在 `max_retries≥2` 的主路径下，下一轮顶部依然会先命中循环顶部守卫，兜底守卫只在 `max_retries=1` 或"每一轮都连续推进 phase 直到最后一轮"的极端路径下触发。该开销可忽略（一次谓词 + O(1) dict 读）。

### 6.2 和 serial 终结的歧义

兜底守卫只在**循环没产生 final text**时触发（因为 serial 产出 final text 走的是循环内 `return`，根本到不了兜底）。因此不会把"成功的 serial 输出"误覆盖为 orchestrator 输出。

### 6.3 和 serial → safety-limit 的顺序

兜底守卫放在 safety-limit yield **前**。这意味着：

- 若 phase 已成功到 5 且 orchestrator 可用 → 走 orchestrator，不出错误文案。
- 若 phase 未到 5（或 config 禁用） → 谓词返回 False → 老样子走 safety-limit 文案。

两条分支互斥且完备，不会出现"既 yield orchestrator 又 yield safety-limit"。

---

## 7. 外部设计参照

引用 `docs/postmortems/2026-04-19-phase5-parallel-guard-refactor.md` 第 4 节：**OpenAI Agents SDK** 将 `max_turns` 视为一等控制条件；**Anthropic "Building Effective Agents"** 把 routing/orchestrator-workers 模式的停止条件放在 runner 边界。本次兜底遵循这一原则——守卫和停止条件作为一对设计，一起摆在 runner 边界上。

---

## 8. 流程说明

本次改动在实施顺序上未严格遵循 brainstorming → spec → plan → execute：

- Spec 在 postmortem 第 7.1 节已具备。
- Plan 未写明文档，直接基于 postmortem 的 code snippet 进入 worktree 落地。
- Brainstorming 被跳过。

本 spec + 同期 plan 文档为追溯补档，目的是让后续 reader 看到与代码等齐的设计依据，而不是只能去 git history 里挖 commit message。
