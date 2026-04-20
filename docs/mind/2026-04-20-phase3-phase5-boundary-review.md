# Phase 3 / Phase 5 边界复盘与演进方向评审

> 记录时间：2026-04-20
> 背景：围绕 Travel Agent Pro 当前 Phase 3 骨架规划与 Phase 5 并行日程编排的职责边界、失败模式与后续演进路径，对本轮讨论进行完整归档，供后续设计评审使用。

---

## 1. 评审问题

当前项目在 Phase 5 并行模式下，经常出现两类问题：

1. **重复日程**：不同天重复使用同一个强锚点或高度相似的 POI 组合
2. **忽略交通时间**：单天内部活动衔接不合理，或首尾日与大交通衔接不足

而这些问题通常不是在并行 Worker 初次生成时被阻止，而是在后续回到串行流程、走质检或局部修改时才暴露。

本轮讨论的核心，不是“模型为什么偶尔犯错”，而是要明确：

- 这些问题究竟是 Phase 3 提示词不足，还是 Phase 5 并行架构设计不足？
- 如果目标是“Phase 3 把骨架先分清，Phase 5 只做按天展开”，那整个系统边界应该如何重构？
- 除了强化 Phase 3 外，是否可以通过提升 Orchestrator 的职责来补足当前并行链路？

---

## 2. 当前系统问题的根因链

### 2.1 不是单点缺陷，而是四层叠加

本轮分析得出的结论是：重复日程和交通时间问题，并不是由单一提示词缺陷造成，而是四层问题叠加：

1. **Phase 3 骨架粒度偏粗**
2. **Phase 5 并行 Worker prompt 比串行 Phase 5 prompt 更弱**
3. **Orchestrator 的全局校验实现弱于设计稿承诺**
4. **并行写入绕过了串行工具链的冲突反馈闭环**

也就是说，当前并行模式不是“生成结果差一点”，而是“结构性约束不够 + 写前守门不严 + 写后闭环缺失”。

### 2.2 Phase 3 当前更像“骨架建议”，不是“日任务合同”

当前 Phase 3 skeleton prompt 已经强调：

- 基于攻略做经验采集
- 按区域连续性分组
- 识别硬约束
- 给出 2-3 套差异方案

但它的最小结构化产物仍然偏弱，主要是：

- 每天主区域
- 每天主题
- 核心活动
- 疲劳等级
- 预算等级
- tradeoffs

这套结构适合人类比较方案，但不够支撑 Phase 5 并行 Worker 进行“无跨天冲突展开”。

更具体地说，当前骨架没有显式表达：

- 哪些 POI 已被某天独占
- 哪些 POI 明确属于其他天，当前天禁止再碰
- 某天允许多远的移动半径
- 某天是否允许跨区
- 某天失败时有哪些本地 fallback

结果是：Phase 5 Worker 虽然拿到了“今天是浅草/上野、传统文化”，但它并不知道“浅草寺已经被 Day 2 锁定，Day 3 不能再用”。

### 2.3 Phase 5 并行 Worker 只看单天切片，没有跨天排他信息

当前并行 Worker 的输入有两个特点：

1. 共享 prefix 注入旅行上下文、偏好、约束、住宿、预算等
2. 每个 Worker 的 day suffix 只看到自己这一天的 `area/theme/core_activities`

这会导致一个结构性后果：

- Day Worker 在本质上是“单天独立求解器”
- 它并不知道其他天已经占用了哪些强锚点
- 也不知道哪些区域或 POI 被别天优先覆盖

因此只要两天的主题相近、区域相邻、或者骨架过于粗粒度，不同 Worker 很自然会各自独立选中同一个最显眼的地标。

这不是模型“偷懒”，而是上下文设计没有给足“避免重复”所需的信息。

### 2.4 并行版 prompt 失去了串行版最关键的“写入-校验-修复”闭环

串行 Phase 5 prompt 明确要求：

- 每完成一天就 `save_day_plan`
- 如果工具返回 `conflicts / has_severe_conflicts`，必须立刻修
- 必要时只修单天，不轻易全局替换

但并行 Worker prompt 只要求：

- 基于骨架展开
- 用工具补齐信息
- 用 `calculate_route` 验证关键移动
- 最后输出 DayPlan JSON

这个 prompt 缺了最关键的一层：**写入后的反馈闭环**。

于是并行 Worker 的目标退化成：

> 生成一个“看起来合理”的单天 JSON

而不是：

> 生成一个写入后能够通过冲突检测并且必要时继续修复的单天 DayPlan

### 2.5 Orchestrator 当前实现的全局校验范围过窄

设计文档里对 Orchestrator 的预期，包括：

- POI 去重
- 预算校验
- 首日衔接
- 尾日衔接
- 时间冲突
- 发现问题后定向修复或重跑

但当前实现只做了：

- 精确 POI 名称去重
- 预算
- 天数覆盖

这意味着：

- 时间冲突没有在 Orchestrator 层被硬拦住
- 首尾日和大交通的结构问题没有被校验
- 语义近似重复没有被识别
- 即使发现 issue，也只是写进 summary，而不是阻止写入或触发定向重试

### 2.6 并行写入绕过了串行工具链的强反馈

串行模式的一个重要质量来源，不仅是 prompt 本身，而是运行链路：

- 调 `save_day_plan` / `replace_all_day_plans`
- 立即返回 `conflicts` / `has_severe_conflicts`
- after_tool_call / after_tool_result hooks 触发增量校验
- LLM 基于冲突结果继续修

并行 Orchestrator 当前直接调用底层 writer：

- 直接 `replace_all_daily_plans`
- 没有工具层的 conflict 返回
- 没有 LLM 参与的写后修复

所以“并行阶段放过、串行阶段再发现”并不是偶然，而是因为两个执行模式的闭环等级不同。

---

## 3. 一个关键划分：结构性冲突 vs 事实性冲突

如果要重设 Phase 3 和 Phase 5 的边界，必须先接受一个更清晰的分类：

### 3.1 结构性冲突

这类问题应该尽量在 Phase 3 消灭：

- 同一个强锚点被分配到多天
- 同一天区域跨度过大
- 首尾日和大交通结构不匹配
- 每天体力负载分布失衡
- 必去项未被唯一归属
- 某天整体移动 envelope 明显超标

### 3.2 事实性冲突

这类问题即使骨架做得很好，也不可能全部在 Phase 3 解决：

- 实际营业时间不匹配
- 某段 transit 比预估长
- 某 POI 临时关闭 / 限流
- 天气使某个活动当天不适合
- 检索结果缺失导致需要换备选

因此，合理目标不是：

> Phase 3 解决所有问题，Phase 5 完全不再验证

而应该是：

> Phase 3 消灭结构性冲突；Phase 5 只处理单日本地事实落地；一旦发现需要跨天协调的问题，则回退 Phase 3。

---

## 4. 路线 A：把 Phase 3 升级成真正的“日任务编译阶段”

这是用户最希望实现的方向：**Phase 3 把骨架彻底分清，Phase 5 只负责按天展开。**

### 4.1 设计目标

把 Phase 5 从“全局规划器”降级成“单日执行器”。

也就是说，Phase 5 在接到任务时，不再需要做这些事：

- 重新决定强锚点归属
- 重新决定某区域到底该放哪一天
- 在跨天范围内借用 POI
- 遇到局部困难时改写整天主题

它只负责：

- 在既定日合同范围内补齐时间、顺序、餐饮、休息
- 做关键路段验证
- 生成 DayPlan

### 4.2 Phase 3 骨架应从“建议”升级为“合同”

建议新增的 day-level 结构不再只是：

- `area`
- `theme`
- `core_activities`

而应升级为更接近“日任务合同”的结构：

```json
{
  "day": 2,
  "date_role": "full_day",
  "area_cluster": ["浅草", "上野"],
  "theme": "传统文化",
  "day_goal": "寺庙 + 老城散步 + 一处公园",
  "locked_pois": ["浅草寺"],
  "candidate_pois": ["仲见世商店街", "上野公园", "阿美横丁"],
  "excluded_pois": ["明治神宫", "涩谷Sky"],
  "mobility_envelope": {
    "max_cross_area_hops": 1,
    "max_transit_leg_min": 35,
    "hotel_return_required": false
  },
  "time_budget": {
    "start_after": "09:00",
    "end_before": "20:00",
    "activity_count_target": [3, 4],
    "buffer_min_total": 90
  },
  "fatigue_level": "medium",
  "budget_level": "medium",
  "fallback_slots": [
    {
      "replace_if_unavailable": "浅草寺",
      "alternatives": ["今户神社", "下町风俗资料馆周边散步"]
    }
  ]
}
```

### 4.3 这类字段解决的不是“表达更丰富”，而是“可执行边界更清晰”

- `locked_pois`
  - 明确这一天独占的锚点
  - 其他天默认不能再用

- `candidate_pois`
  - 规定当日允许补齐的池子
  - 防止 Worker 乱加骨架外的远点

- `excluded_pois`
  - 明确把跨天排他约束落在结构里，而不是留给 prompt 暗示

- `mobility_envelope`
  - 把“不要太折腾”转成可校验的日移动边界

- `time_budget`
  - 把 pace 从抽象人格约束变成可执行预算

- `fallback_slots`
  - 让 Phase 5 本地修复有边界，不至于一失败就跨天重排

### 4.4 对 Phase 3 prompt 的要求

如果走这条路线，Phase 3 skeleton prompt 必须强化成：

1. 每个强锚点必须唯一归属某一天
2. 每个 day 必须显式写明自己拥有哪些资源、禁用哪些资源
3. 每个 day 必须声明自己的移动和时间 envelope
4. 进入 Phase 5 前，骨架必须满足“可单日独立展开”条件

这时，Phase 3 的本质不再是“生成 2-3 套人类可比较方案”，而是：

> 把整个旅行的关键资源分配到各天，并输出一组可执行日合同

---

## 5. 路线 B：提升 Orchestrator，但不要把它做成第二个全局规划器

用户提出的另一条可能路线是：**不把所有责任都前移到 Phase 3，而是提升 Orchestrator 的任务。**

这条路线是可行的，但必须区分两种提升方向。

### 5.1 值得做的提升：增强型编排器

这是推荐方向。Orchestrator 可以增强为：

- Skeleton Compiler
- Constraint Injector
- Global Validator
- Targeted Re-dispatch Controller

它负责：

1. 从 Phase 3 skeleton 编译出更严格的 `DayTask`
2. 给每个 Worker 注入跨天排他约束
3. 写前做 deterministic 全局校验
4. 对问题天做定向重试

这种提升，本质上是：

> Orchestrator 帮 Phase 3 的骨架变成可执行合同

它不是在“重新规划旅行”，而是在“落地旅行合同”。

### 5.2 不推荐的提升：全局再规划器

不推荐让 Orchestrator 承担以下能力：

- 发现 Day 2 / Day 3 冲突后自行决定谁保留 POI
- 把某个锚点挪到别天
- 因交通问题改主区域或改主题
- 在 Worker 失败后重新做跨天资源分配

一旦 Orchestrator 进入这层，它就不再是编排器，而成了：

> Phase 5 的第二个全局规划器

这样会带来三个问题：

1. 稀释 Phase 3 职责，系统逐渐依赖“Phase 5 会兜底”
2. 并行路径重新引入强串行协调
3. 责任归因变模糊，不利于 debug 和评估

### 5.3 推荐的 Orchestrator 增强边界

推荐把 Orchestrator 提升到以下五项即止步：

1. **Skeleton Compiler**
   - 输入 Phase 3 skeleton
   - 输出严格 `DayTask[]`

2. **Global Ownership Map**
   - `poi_owner`
   - `area_owner`
   - `must_do_owner`

3. **Constraint Injection**
   - `locked_pois`
   - `allowed_pois`
   - `forbidden_pois`
   - `mobility_envelope`
   - `time_budget`
   - `fallbacks`

4. **Hard Validation Before Write**
   - 时间冲突
   - 重复
   - 首尾衔接
   - 超 envelope

5. **Targeted Re-dispatch**
   - 只对问题天重跑
   - 重跑时携带精确 diff 和修复约束

---

## 6. 推荐架构：Phase 3 强结构，Orchestrator 强执行，Worker 强约束

结合两条路线的优点，本轮讨论最终收敛到一个更稳的架构边界。

### 6.1 角色边界

#### Phase 3：全局资源分配器

负责：

- 把强锚点唯一归属到具体天
- 把主区域、主题、节奏、负载分配清楚
- 定义每一天的允许活动池和禁用活动池
- 定义首尾日结构
- 定义每天的 mobility / time envelope

不负责：

- 小时级排时
- 最终交通时长查证
- 当天餐饮和休息插入

#### Orchestrator：合同编译器 + 全局守门员

负责：

- 读取 skeleton
- 编译成 `DayTask`
- 注入排他和边界约束
- 收集 Worker 结果
- 做 deterministic 全局校验
- 对问题天做定向重试

不负责：

- 重新决定全局主题
- 改写天级资源分配
- 做跨天再规划

#### Day Worker：单日执行器

负责：

- 在 `locked_pois + candidate_pois + fallback_slots` 范围内组装 DayPlan
- 排时间、补餐饮、调顺序
- 验证关键路段
- 本地替换不可行点

不负责：

- 跨天借 POI
- 改主题
- 改主区域
- 因单天困难而重写全局结构

### 6.2 一句话总结

最理想的职责边界是：

> 把 Phase 3 从“骨架建议生成器”升级为“日任务资源分配器”；把 Phase 5 从“并行规划器”降级为“受限展开器”；把 Orchestrator 固定为“编译器+守门员”。

---

## 7. 推荐的 DayTask 语义

不建议 Phase 5 Worker 继续直接消费原始 `skeleton.days[i]`。  
更合理的是新增 `DayTask`，由 Orchestrator 或独立 compiler 从 skeleton 编译得到。

建议 `DayTask` 至少包含：

```json
{
  "day": 3,
  "date": "2026-05-03",
  "theme": "传统文化",
  "area_cluster": ["浅草", "上野"],
  "locked_pois": ["浅草寺"],
  "allowed_pois": ["仲见世商店街", "上野公园", "阿美横丁"],
  "forbidden_pois": ["明治神宫", "涩谷Sky"],
  "mobility_envelope": {
    "max_leg_min": 35,
    "max_cross_area_hops": 1
  },
  "time_budget": {
    "start_after": "09:30",
    "end_before": "20:00",
    "buffer_min_total": 90,
    "activity_count_target": [3, 4]
  },
  "fallback_slots": [
    {
      "replace_if_unavailable": "浅草寺",
      "alternatives": ["今户神社", "向岛散步"]
    }
  ],
  "repair_hints": []
}
```

这个结构的作用不是让 Worker 更聪明，而是让它**更受限、更稳定、更可测**。

---

## 8. 失败处理原则

如果 Phase 5 要真正降级为“展开器”，失败处理也要随之改变。

### 8.1 本地修复

适用于：

- 单个 POI 闭馆
- 某个点检索缺失
- 某段交通略超预期

处理方式：

- 优先使用同 day 的 fallback
- 或从同 `area_cluster` 的允许池中替换

### 8.2 本地降级

适用于：

- 活动数量略多
- 总时长略超
- 某个非关键点需删除

处理方式：

- 删弱活动
- 增缓冲
- 不跨天借资源

### 8.3 结构失败

适用于：

- `locked_pois` 本身不可行
- fallback 也无法满足
- 无论如何都超出 mobility envelope
- 首尾日结构无法成立

处理方式：

- Worker 返回结构化失败，如 `NEEDS_PHASE3_REPLAN`
- Orchestrator 不自行跨天修，而是要求回退更上游

这个边界很关键。  
一旦需要跨天协调，问题就不再是“单日展开失败”，而是“骨架分配失败”。

---

## 9. 当前阶段最现实的演进顺序

本轮讨论中，还形成了一个比较明确的优先级判断。

### 9.1 P0：先修并行写入和全局校验闭环

即使长期要把 Phase 3 做强，短期也应该先止血。

优先事项：

- Orchestrator 不再直接绕过工具链写入
- 写前必须做时间冲突 / 首尾衔接 / 重复校验
- issue 不再只是 advisory，而要能阻止写入或触发问题天重跑

这是短期质量收益最高的改动。

### 9.2 P1：让 Worker 接到更强的约束上下文

即使 Phase 3 schema 尚未全面升级，也可以先通过 Orchestrator 编译和补充：

- 给 Worker 注入跨天排他信息
- 明确 allowed / forbidden / fallback
- 把“避免重复”从软提示变成显式约束

### 9.3 P2：升级 Phase 3 skeleton schema

中期再把 Phase 3 从“骨架建议”升级为“日任务合同源”。

这时：

- Phase 5 才能真正降为执行器
- Orchestrator 才能稳定退化为编译器+守门员

---

## 10. 本轮讨论的最终判断

### 10.1 关于“Phase 3 先分清，Phase 5 只展开”

这是一个合理且值得追求的目标，但前提是：

- 你必须接受更强的 skeleton schema
- 必须区分结构性冲突与事实性冲突
- Phase 5 不能完全不验证，只能不再承担跨天协调责任

准确表述应是：

> Phase 3 保证全局结构无冲突；Phase 5 只处理单日本地事实落地；一旦需要跨天协调，直接回退 Phase 3。

### 10.2 关于“提升 Orchestrator 是否可行”

可行，而且非常适合当前阶段作为主抓手。  
但推荐提升方向是：

> 增强 Orchestrator 的编译、约束注入、全局守门和定向重试能力

而不推荐让它成为：

> 第二个跨天全局规划器

否则会抵消“把 Phase 3 做强”的架构目标。

### 10.3 最终推荐的架构路线

本轮建议的长期路线是：

```text
Phase 3 = 全局资源分配器
Orchestrator = 合同编译器 + 全局守门员
Day Worker = 单日执行器
```

这是当前讨论里边界最清晰、最利于并行化、也最利于后续测试与调试的一条路径。

---

## 11. 后续评审建议

如果后续要进入正式设计或实施评审，建议按以下顺序展开：

1. 先确认是否接受“结构性冲突 / 事实性冲突”这套边界定义
2. 再确认长期路线是否采用“Phase 3 强结构 + Orchestrator 强执行 + Worker 受限展开”
3. 若接受，再进入下一层设计：
   - skeleton day schema
   - DayTask schema
   - compiler / validator 规则
   - Worker 失败码与回退语义
   - Orchestrator 写前校验与定向重试机制

如果这四层没有先谈清楚，后续无论是只改 prompt，还是只改 Orchestrator，都容易继续在职责边界上摇摆。
