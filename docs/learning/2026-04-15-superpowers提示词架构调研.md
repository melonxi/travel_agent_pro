# Superpowers 提示词架构调研

> **目标**：梳理 `superpowers` skills 的提示词架构思想，并评估如何借鉴到当前旅行 Agent 的 `backend/phase/prompts.py`。
>
> **结论先行**：当前旅行 Agent 的阶段提示词已经是“阶段作业手册”形态，尤其 Phase 3/5 已经具备较强的流程约束。下一步优化重点不应是继续加长提示词，而是借鉴 `superpowers` 的结构化技能系统：把阶段提示词拆成更小的技能卡，增加硬法则、输入 Gate、完成 Gate、Red Flags 和压力场景，并让 Phase 3 子阶段按需动态注入。

---

## 1. 调研对象

本次调研主要参考两类材料。

第一类是 `superpowers` skills：

- `~/.codex/superpowers/skills/using-superpowers/SKILL.md`
- `~/.codex/superpowers/skills/brainstorming/SKILL.md`
- `~/.codex/superpowers/skills/writing-plans/SKILL.md`
- `~/.codex/superpowers/skills/test-driven-development/SKILL.md`
- `~/.codex/superpowers/skills/systematic-debugging/SKILL.md`
- `~/.codex/superpowers/skills/verification-before-completion/SKILL.md`
- `~/.codex/superpowers/skills/subagent-driven-development/SKILL.md`
- `~/.codex/superpowers/skills/dispatching-parallel-agents/SKILL.md`
- `~/.codex/superpowers/skills/requesting-code-review/SKILL.md`
- `~/.codex/superpowers/skills/receiving-code-review/SKILL.md`
- `~/.codex/superpowers/skills/finishing-a-development-branch/SKILL.md`
- `~/.codex/superpowers/skills/writing-skills/SKILL.md`

第二类是当前项目提示词和运行时拼装链路：

- `backend/phase/prompts.py`
- `backend/context/manager.py`
- `backend/phase/router.py`
- `backend/agent/reflection.py`
- `backend/agent/tool_choice.py`
- `backend/tools/engine.py`
- `PROJECT_OVERVIEW.md`

---

## 2. Superpowers Skills 的核心特点

### 2.1 不是知识库，而是作业规程

`superpowers` 不是针对某个语言或框架的知识库，而是一套约束 Agent 行为的工程流程系统。它覆盖的是开发生命周期：

```text
进入任务
  -> 判断适用 skill
  -> 需求澄清 / 设计
  -> 写计划
  -> 隔离工作区
  -> TDD 实现
  -> 系统化调试
  -> 子 Agent 分工
  -> Code Review
  -> 完成前验证
  -> 分支收尾
```

它的设计目标是让 Agent 不跳步、不猜测、不在没有证据时声称完成。

### 2.2 强门槛优先于温和建议

很多 skill 都有类似 “Iron Law” 或 “Hard Gate” 的结构。例如：

- 没有设计和用户批准，不开始实现。
- 没有失败测试，不写生产代码。
- 没有根因调查，不提出修复。
- 没有新鲜验证证据，不声称完成。

这些表达不是为了给人阅读舒服，而是为了压住 Agent 常见的投机行为。

### 2.3 每个 skill 都有清晰触发条件

每个 `SKILL.md` 的 frontmatter 都用 `description` 描述触发条件，而不是总结流程。这一点很关键：触发条件决定 Agent 是否加载 skill，流程细节留在正文中执行。

这给当前旅行 Agent 的启发是：阶段提示词也应该区分“什么时候启用”和“启用后怎么做”，避免把所有规则都塞进同一个长段落里。

### 2.4 流程、Red Flags、完成判断并重

`superpowers` 的强点不只是正向流程，还包括：

- Common Mistakes
- Red Flags
- When to Stop
- Gate Function
- Verification
- Examples / Real Examples

这些内容专门针对 Agent 容易犯的错。它不是只告诉 Agent 应该做什么，也告诉 Agent 哪些想法代表它正在走偏。

### 2.5 证据优先

`verification-before-completion` 的核心是 evidence before claims。它要求：

```text
识别能证明声明的命令
运行完整命令
阅读输出和退出码
确认输出支持声明
然后才能说完成
```

迁移到旅行 Agent 后，对应的是：

- 没有工具结果，不得声称事实已验证。
- 没有结构化写入，不得声称阶段完成。
- 没有覆盖全部天数，不得声称行程完整。
- 没有用户明确确认，不得写入确定性决策字段。

---

## 3. 当前 `backend/phase/prompts.py` 现状

### 3.1 当前已经具备的优势

当前阶段提示词不是普通 role prompt，而是已经接近“阶段作业手册”。

Phase 1 的优势：

- 明确目标是目的地收敛，而不是泛泛旅行聊天。
- 限制候选数量为 2-3 个。
- 区分 `xiaohongshu_search`、`web_search`、`quick_travel_search` 的职责。
- 明确状态写入规则：只有用户明确表达的信息才能写入。
- 有按用户输入类型划分的 examples。

Phase 3 的优势：

- 已拆成 `brief`、`candidate`、`skeleton`、`lock` 四个子阶段。
- 明确结构化产物：`trip_brief`、`candidate_pool`、`shortlist`、`skeleton_plans`、`selected_skeleton_id`、`transport_options`、`accommodation_options`、`accommodation`。
- 提醒 `phase3_step` 由系统自动推导。
- 有明确工具边界，且 `ToolEngine` 也在运行时按子阶段裁剪工具。

Phase 5 的优势：

- 明确不重做 Phase 3 决策，只把已选骨架落成逐日行程。
- 明确 `daily_plans` 的 JSON 结构。
- 强调不能只在自然语言里输出行程，必须写入状态。
- 有 validate 动作，要求对开放、交通、天气、预算和节奏做必要验证。

系统层面已有配合机制：

- `ContextManager.build_system_message()` 注入全局工具硬规则、当前时间、当前规划状态和可用工具。
- `ReflectionInjector` 在 Phase 3 lock 和 Phase 5 complete 时注入自检。
- `ToolChoiceDecider` 在关键时机强制调用 `update_plan_state`。
- `ToolEngine` 对 Phase 3 子阶段做工具白名单过滤。

### 3.2 当前主要问题

第一，提示词长度和层级稀释注意力。

Phase 3 无论当前处于 `brief` 还是 `lock`，都会加载四个子阶段的完整规则。模型在 `brief` 阶段也会看到机酒锁定、骨架和动线工具规则，当前任务重点被稀释。

第二，很多规则是“建议式”，还不是“Gate 式”。

例如 Phase 5 已经说了“完成标志”，但还可以进一步变成更明确的 Gate：

```text
如果 daily_plans 未覆盖 total_days，不得声称行程完整。
如果没有工具结果，不得声称开放时间、天气、交通耗时已验证。
如果输出逐日安排但没有调用 update_plan_state，视为本阶段未完成。
```

第三，Red Flags 不够集中。

当前 prompt 有很多“不要”，但还没有像 `superpowers` 一样集中列出高频失败信号。集中 Red Flags 更利于模型在临界状态自检。

第四，Phase 7 明显弱于 Phase 3/5。

Phase 7 目前只有简短说明：生成清单、查天气、生成摘要、可搜服务。它缺少：

- 输入前置条件
- 工具边界
- 输出结构
- 完成 Gate
- 服务搜索的支付/下单边界
- 未验证事项处理方式

第五，examples 多是正常路径，压力场景不足。

Phase 1 的 examples 很有帮助，但多是理想路径。更需要补充的是 Agent 容易犯错的压力场景，例如“用户说五一但没确认具体日期”“用户要求直接给完整行程但当前仍在 Phase 3”“小红书返回价格但未经过确定性验证”等。

---

## 4. 可借鉴的提示词架构

### 4.1 从长提示词改成阶段技能卡

建议把每个 phase / subphase 组织为固定结构：

```text
# Phase N - 名称

## 角色
一句话定义当前身份。

## 目标
当前阶段只完成什么，不完成什么。

## 硬法则
违反后会造成状态错误或用户误导的规则。

## 输入 Gate
进入当前工作前必须满足的状态条件。

## 流程
按步骤说明当前阶段怎么推进。

## 状态写入契约
哪些字段可以写、何时写、用什么结构写。

## 工具契约
哪些工具是主工具，哪些只能验证，哪些不能在本阶段使用。

## 完成 Gate
满足哪些条件才算本阶段完成。

## Red Flags
哪些行为说明 Agent 正在走偏。

## 压力场景
少量高风险输入的正确处理方式。
```

这个结构对应 `superpowers` 的 `Overview / Iron Law / Process / Verification / Red Flags / Examples`。

### 4.2 Phase 3 动态子阶段注入

当前 `PHASE_PROMPTS[3]` 建议拆成：

```python
PHASE3_BASE_PROMPT = "..."

PHASE3_STEP_PROMPTS = {
    "brief": "...",
    "candidate": "...",
    "skeleton": "...",
    "lock": "...",
}
```

运行时拼装：

```text
Phase 3 通用规则
  +
当前 phase3_step 对应的子阶段规则
```

这样可以让 `brief` 阶段只看到画像收束和状态写入，不提前加载 `lock` 的机酒搜索规则；`lock` 阶段则集中看到交通住宿、预算和可行性检查。

### 4.3 Completion Gate 作为阶段完成契约

每个阶段都应有明确完成 Gate。

Phase 1 Gate：

- 用户已明确确认目的地。
- 已调用 `update_plan_state(field="destination", value="...")`。
- 不把推荐候选误写成用户最终决定。

Phase 3 brief Gate：

- 用户明确字段已写入对应结构字段。
- `trip_brief` 至少包含当前可确认的 `goal`、`pace`、`departure_city`、`must_do`、`avoid`、`budget_note`。
- 没有为了缺少非关键字段而无限追问。

Phase 3 candidate Gate：

- `candidate_pool` 是 list。
- 每项尽量包含 `why`、`why_not`、`time_cost`、`area` 或 `theme`。
- `shortlist` 是 list，且由候选池筛选而来。
- 明确删掉了什么以及为什么。

Phase 3 skeleton Gate：

- `skeleton_plans` 是 list。
- 每套方案包含 `id`、`name`、`days`、`tradeoffs`。
- `id` 稳定唯一。
- 未经用户选择，不写 `selected_skeleton_id`。

Phase 3 lock Gate：

- `dates` 已确认。
- `selected_skeleton_id` 已确认。
- `accommodation` 已确认。
- 交通与住宿风险、预算压力或替代方案已写入结构化字段。

Phase 5 Gate：

- `daily_plans` 覆盖全部 `total_days`。
- 每天有主题，不是随意堆点。
- 关键活动具备时间、地点、费用、交通衔接。
- 高风险项已通过工具验证，或明确标注未验证风险。

Phase 7 Gate：

- 已生成出行摘要。
- 已基于 `daily_plans` 生成清单。
- 已用 `check_weather` 查询天气敏感信息。
- 服务推荐只提供链接和注意事项，不替用户支付或下单。
- 明确列出仍需用户自行确认的未验证事项。

### 4.4 Red Flags 集中化

建议每个阶段都加入一段 `Red Flags`。

通用 Red Flags：

- 用户只说“玩 5 天”，你写入了具体日期。
- 用户没有明确确认，你写入了确定性选择字段。
- 你在正文中给出候选池、骨架或逐日行程，但没有写入状态。
- 你凭记忆或常识声称营业时间、价格、签证、天气已验证。
- 小红书内容被当成官方事实。
- 当前可用工具列表没有某工具，你却承诺会调用它。
- 用户要求推翻前序决策，你没有使用 `backtrack`。

Phase 3 Red Flags：

- 还在 `brief` 就查航班、酒店或路线。
- 还没有 `selected_skeleton_id` 就锁住宿。
- 把 `phase3_step` 当作模型需要手动维护的字段。
- 把推荐理由写入 `preferences`。

Phase 5 Red Flags：

- 重新设计了与已选骨架不一致的路线。
- 只输出自然语言行程，没有写入 `daily_plans`。
- 计划没有覆盖全部天数却称为完整版。
- 活动时间无缓冲，或连续高强度安排违反用户节奏。

Phase 7 Red Flags：

- 没有天气工具结果就给具体天气穿衣建议。
- 把签证、保险、电话卡等服务推荐写成必须购买。
- 输出清单但没有覆盖已规划项目中的预约型、户外型或交通型事项。

### 4.5 压力场景替代普通示例

普通示例说明“理想情况怎么做”，压力场景说明“容易错时怎么做”。建议新增以下类型。

场景 A：相对日期但未确认具体日期

```text
用户：五一去东京，预算 2 万，两个人。

正确：
- 写入 destination、budget、travelers。
- 不写具体 dates。
- 询问是否按法定五一假期暂估，或请用户确认具体日期。

错误：
- 直接写入 2026-05-01 至 2026-05-05。
```

场景 B：当前仍在 Phase 3，但用户要求完整行程

```text
用户：你直接给我一版完整行程。

正确：
- 如果未锁住宿和骨架，先给可比较骨架方案。
- 告知逐日小时级安排会在 Phase 5 生成。
- 写入 skeleton_plans，而不是 daily_plans。

错误：
- 在 Phase 3 直接生成小时级 daily_plans。
```

场景 C：小红书返回价格或开放时间

```text
正确：
- 可作为体验线索。
- 价格、营业时间、政策必须用 web_search 或官方类工具验证。

错误：
- 直接把 UGC 价格或营业时间当成确定事实。
```

场景 D：Phase 5 发现骨架不可执行

```text
正确：
- 说明不可执行原因。
- 必要时调用 update_plan_state(field="backtrack", value={"to_phase": 3, "reason": "..."})。

错误：
- 静默改成另一套路线。
```

---

## 5. 建议的落地方式

### 5.1 文件结构建议

低风险改法：

- 继续保留 `backend/phase/prompts.py`。
- 在文件内拆出多个常量。
- 不引入额外配置文件，避免运行时加载路径和测试复杂化。

建议结构：

```python
GLOBAL_PHASE_RED_FLAGS = "..."
PHASE1_PROMPT = "..."
PHASE3_BASE_PROMPT = "..."
PHASE3_STEP_PROMPTS = {
    "brief": "...",
    "candidate": "...",
    "skeleton": "...",
    "lock": "...",
}
PHASE5_PROMPT = "..."
PHASE7_PROMPT = "..."
```

### 5.2 路由拼装建议

`PhaseRouter.get_prompt()` 当前只接收 `phase`。如果要动态注入 Phase 3 子阶段，可以新增一个方法：

```python
def get_prompt_for_plan(self, plan: TravelPlanState) -> str:
    if plan.phase == 3:
        return build_phase3_prompt(plan.phase3_step)
    return PHASE_PROMPTS.get(plan.phase, PHASE_PROMPTS[1])
```

然后逐步替换调用点，或保持 `get_prompt()` 兼容旧测试。

### 5.3 与现有运行时机制的配合

不要只靠 prompt 做约束。应继续利用现有机制：

- `ToolEngine`：继续按 phase / subphase 裁剪工具。
- `ToolChoiceDecider`：继续强制关键状态写入。
- `ReflectionInjector`：把当前自检升级成更像 completion gate 的检查。
- `validator`：把 prompt 中的硬约束尽量沉淀为可运行校验。
- `evals/golden_cases`：把压力场景加入回归样例。

### 5.4 测试建议

字符串测试：

- Phase 3 `brief` prompt 不应包含 `search_flights`、`search_trains`、`search_accommodations`。
- Phase 3 `lock` prompt 必须包含交通住宿锁定 Gate。
- Phase 5 prompt 必须包含 `daily_plans` 覆盖全部天数的 Gate。
- Phase 7 prompt 必须包含 `check_weather`、`generate_summary`、不得代用户支付/下单。

行为测试：

- 用户给出相对日期时，不应写入具体 dates。
- Phase 3 输出骨架时必须写入 `skeleton_plans`。
- Phase 5 输出逐日行程时必须写入 `daily_plans`。
- Phase 7 必须调用天气和摘要工具。

Eval 压力样例：

- 五一但未确认具体日期。
- 用户要求直接出完整行程但当前仍在 Phase 3。
- 小红书给出价格/开放时间但未经过官方验证。
- Phase 5 发现已选骨架不可执行。

---

## 6. 优先级判断

P0：补强 Phase 7。

Phase 7 目前和其他阶段差距最大，应该优先补齐输入 Gate、工具契约、输出结构、完成 Gate 和 Red Flags。

P1：拆分 Phase 3 子阶段 prompt。

这是收益最高的结构性优化，能减少 token 干扰，让模型在当前子阶段更稳定。

P2：为 Phase 1/3/5 增加集中 Red Flags 和 Completion Gate。

这一步不会大改架构，但会增强提示词对高频失败模式的拦截能力。

P3：把压力场景加入 eval。

Prompt 改造后需要用 eval 证明稳定性，不应只依赖人工体验。

---

## 7. 总结

`superpowers` 的关键价值不是“提示词写得更长”，而是把 Agent 行为变成可触发、可执行、可验证、可复盘的流程系统。

当前旅行 Agent 已经具备这个方向的基础：阶段流转、状态模型、工具边界、反思注入、强制工具选择、质量门控和 eval 都已经存在。下一步应该把 `backend/phase/prompts.py` 从“长篇阶段说明”升级成“阶段技能系统”：

- 每个阶段有明确角色和目标。
- 每个阶段有硬法则。
- 每个阶段有输入 Gate 和完成 Gate。
- 每个阶段有 Red Flags。
- Phase 3 子阶段按需动态注入。
- Phase 7 补齐到和 Phase 3/5 同等严谨。
- 压力场景进入 eval，形成持续回归。

这会让提示词更短、更硬、更可测试，也更符合当前项目作为 Agent harness 的演进方向。
