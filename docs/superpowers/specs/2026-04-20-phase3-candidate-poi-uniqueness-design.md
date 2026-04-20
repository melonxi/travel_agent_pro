# Phase 3 Candidate POI 全局唯一性设计

> 日期：2026-04-20
> 背景：Phase 5 并行 Day Worker 按天隔离执行，当前 Orchestrator 只对 `locked_pois` 做跨天排他，不会在 dispatch 前消解 `candidate_pois` 重叠，导致同一 POI 容易被多个 worker 同时采纳并生成跨天重复日程。

---

## 1. 问题陈述

当前 Phase 3/Phase 5 边界存在一个结构性漏洞：

1. `set_skeleton_plans` 只校验 `locked_pois` 跨天唯一，不校验 `candidate_pois`
2. `candidate_pois` 当前语义是“该天允许使用的候选池”，但并未明确为单天专属
3. Phase 5 `Phase5Orchestrator` 在 `_compile_day_tasks()` 中只会把其他天的 `locked_pois` 反推为 `forbidden_pois`
4. 各 Day Worker 之间上下文隔离，无法在生成阶段协商“这个候选 POI 更应该归哪一天”
5. 结果是：即使骨架阶段允许的只是“弱候选”，进入并行 Phase 5 后也会演化成真实跨天重复规划，最后只能依赖 `_global_validate()` 的事后去重和一次 redispatch 补救

这类问题本质上是前置边界定义不充分，而不是后置 Orchestrator 调度不够聪明。

---

## 2. 设计目标

本次设计只解决一个明确问题：在现有并行 Phase 5 框架不变的前提下，禁止同一套 skeleton 内同一个 POI 被多天重复归属。

目标：

- 让 `candidate_pois` 从“软提示候选池”收敛为“单天专属候选池”
- 在 Phase 3 写入阶段拦截跨天冲突，阻止脏 skeleton 进入 Phase 5
- 保持现有 Phase 5 Orchestrator / worker prompt /历史 skeleton 读取逻辑不变

非目标：

- 不重写 Phase 5 Orchestrator 的全局分配模型
- 不对历史 skeleton 做迁移或补写
- 不自动修复重复 POI
- 不引入 `candidate_pois` 的优先级、ownership score、共享池等新概念

---

## 3. 规则定义

### 3.1 作用范围

新规则仅对 `set_skeleton_plans` 的新写入生效。

这意味着：

- 新写入 skeleton 时，必须满足全局唯一性规则
- 已经存在的旧 skeleton 不做强制回收或 Phase 5 阻断
- Phase 5 读取旧 skeleton 时不因为该规则新增而自动 backtrack

### 3.2 全局唯一性规则

在同一套 skeleton 的 `days[*].locked_pois` 与 `days[*].candidate_pois` 的并集中，一个 POI 名称只能出现一次。

允许：

- `locked_pois` 为空列表
- `candidate_pois` 非空，但其中每个 POI 只归属一天

禁止：

- 同一个 POI 同时出现在不同天的 `candidate_pois`
- 同一个 POI 同时出现在某天的 `locked_pois` 和另一天的 `candidate_pois`
- 同一个 POI 同时出现在不同天的 `locked_pois`
- 同一个 POI 在同一天里同时出现在 `locked_pois` 和 `candidate_pois`
- 同一个 POI 在同一天同一字段中重复出现

### 3.3 字段语义更新

- `locked_pois`：该天已经钉住的强锚点，必须保住，且在整套 skeleton 内全局唯一
- `candidate_pois`：该天专属候选池，供该天的 Day Worker 优先选取，且在整套 skeleton 内全局唯一

`candidate_pois` 不再被视为“多个天都可共享的松散备选池”。

---

## 4. 推荐方案

采用 `Prompt + Tool Validation` 双层约束：

1. 在 Phase 3 skeleton prompt 中把 `candidate_pois` 明确描述为“单天专属候选池”
2. 在 `set_skeleton_plans` 的 `_validate_skeleton_days()` 中加入跨字段、跨天的 POI 全局唯一校验

不采用仅改 prompt 的方案，因为它不能阻止脏数据进入状态。

不采用 Orchestrator 自动修复方案，因为这会继续把前置边界问题转嫁给后置并行补救逻辑。

---

## 5. 模块设计

### 5.1 Prompt 层

修改 `backend/phase/prompts.py` 中 skeleton 子阶段描述：

- 保留现有 `candidate_pois` 非空要求
- 新增一条硬规则：同一套 skeleton 内，一个 POI 只能出现在一天的 `locked_pois` 或 `candidate_pois` 中一次
- 把 `candidate_pois` 定义为“单天专属候选池”
- 提供一个最小合法示例，直接展示某个 POI 不应跨天重复出现

Prompt 目标不是兜底，而是让模型在第一次生成骨架时就按正确心智模型组织 POI 归属。

### 5.2 Tool 校验层

修改 `backend/tools/plan_tools/phase3_tools.py` 的 `_validate_skeleton_days()`：

- 保留现有：
  - `days` 不能为空
  - `area_cluster` 必须是非空字符串列表
  - `locked_pois` 必须是字符串列表，可为空
  - `candidate_pois` 必须是非空字符串列表
- 新增：
  - 建立 POI 出现表，记录每个 POI 首次出现的位置
  - 扫描顺序按 `days` 顺序、每一天先 `locked_pois` 后 `candidate_pois`
  - 任意 POI 第二次出现即报错

建议记录的位置信息格式：

- `plans[{plan_idx}].days[{day_idx}].locked_pois`
- `plans[{plan_idx}].days[{day_idx}].candidate_pois`

### 5.3 错误行为

一旦发现重复：

- 直接抛 `ToolError`
- `error_code="INVALID_VALUE"`
- `set_skeleton_plans` 不写入任何状态

推荐错误文案风格：

```text
'上野公園' 已出现在 plans[0].days[1].candidate_pois，又出现在 plans[0].days[2].candidate_pois；
同一套 skeleton 内，POI 只能归属一天
```

推荐 suggestion：

```text
把 '上野公園' 只保留在最适合的一天；如果只是弱备选，不要在多天重复写入 candidate_pois
```

如果是 `locked_pois` 与 `candidate_pois` 冲突，也使用同一错误结构，只是位置不同。

---

## 6. 测试策略

测试集中在既有测试文件，避免无关扩散。

### 6.1 `backend/tests/test_plan_tools/test_skeleton_schema.py`

新增测试：

1. `candidate_pois` 跨天重复时报错
2. `locked_pois` 与另一天下 `candidate_pois` 冲突时报错
3. 同一天内同一 POI 同时出现在 `locked_pois` 和 `candidate_pois` 时报错
4. 同一天内 `candidate_pois` 自身重复时报错
5. 同一天内 `locked_pois` 自身重复时报错

保留现有合法样例，确保非冲突 skeleton 仍能写入。

### 6.2 `backend/tests/test_prompt_architecture.py`

新增或更新断言，确保 skeleton prompt 明确提到：

- `candidate_pois` 是单天专属候选池
- `locked_pois + candidate_pois` 在整套 skeleton 内全局唯一

---

## 7. 兼容性与风险

### 7.1 兼容性

该设计不会影响：

- 旧 skeleton 的读取与执行
- Phase 5 Orchestrator 的并行调度
- Day Worker 的 prompt 结构
- `fallback_slots` 语义

### 7.2 风险

引入更严格校验后，Phase 3 首次写入骨架时的 tool error 数量可能上升。

这是预期行为，不是回归。因为此前这些重复 POI 会被“放行到 Phase 5 再炸”，现在只是把错误前移到了正确边界。

### 7.3 风险缓解

通过 prompt 示例和更明确的错误文案，降低模型反复犯同类错误的概率。

---

## 8. 不做事项

本次设计明确不做以下事项：

- 不让 Orchestrator 在 dispatch 前自动去重 `candidate_pois`
- 不把重复 POI 自动改写到 `fallback_slots`
- 不增加“共享 candidate_pois”机制
- 不对旧 skeleton 增加运行时强制 backtrack
- 不改变 `locked_pois` / `candidate_pois` 的数据结构

---

## 9. 结论

在当前架构下，`candidate_pois` 如果允许跨天重叠，就会天然与并行 Phase 5 的 worker 隔离模型冲突。

最稳妥的工程选择不是增强 Orchestrator 的后置补救，而是在 Phase 3 的 `set_skeleton_plans` 写入边界直接强制：

- 同一套 skeleton 内，一个 POI 只能归属一天
- 这个归属既适用于 `locked_pois`，也适用于 `candidate_pois`

这样可以把重复 POI 从“Phase 5 经常发生的后验错误”转成“Phase 3 当场阻止的前置输入错误”，让并行 Orchestrator 在现有框架下承接到更干净的计划框架。
