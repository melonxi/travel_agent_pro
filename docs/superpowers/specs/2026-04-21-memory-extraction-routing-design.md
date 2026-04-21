# Memory Extraction Routing Design

> **状态**：草案，待评审
> **日期**：2026-04-21
> **范围**：将当前 v3 记忆提取链路从“一个 gate + 一个 combined extractor”调整为“一个 routing gate + profile / working memory 两个专用 extractor”。

---

## 1. 背景

当前后台记忆写入链路已经拆成两步：

```text
memory_extraction_gate
  ↓
memory_extraction
```

其中 gate 判断本轮是否值得提取，extraction 在一次 LLM 调用里同时输出两类结果：

```text
profile_updates = 跨旅行长期用户画像
working_memory  = 当前 session/trip 的短期推理材料
```

这个设计已经能工作，但 combined extractor 让模型同时承担两套不同判断：

- 哪些内容能跨旅行长期复用
- 哪些内容只对当前会话/当前 trip 临时有用
- 哪些内容其实属于 `TravelPlanState`
- 哪些内容应该丢弃

长期 profile 与 working memory 的生命周期、风险等级、schema、召回方式都不同。把它们放在同一个 extraction prompt 中，会增加模型分类压力，也更容易把“这次场景化偏好”误写成长期画像。

---

## 2. 设计目标

1. 降低单次 extraction prompt 的认知负担。
2. 强化长期画像与短期工作记忆的职责边界。
3. 保留当前异步后台 memory job、coalescing queue、internal task 展示模型。
4. 让 gate 从布尔门控升级为轻量路由器，决定后续应执行哪些 extractor。
5. 提高可观测性：前端和 trace 能区分 profile extraction 与 working memory extraction。

非目标：

- 不改 memory recall 链路。
- 不引入 embedding、向量库或 RAG。
- 不改变 `TravelPlanState` 是当前旅行权威状态的原则。
- 不在本阶段重构 working memory 存储路径。
- 不新增多 worker 调度；仍沿用每个 session 一个 memory scheduler 的模型。

---

## 3. 推荐方案

推荐采用：

```text
routing gate
  ↓
route = none | profile | working_memory | both
  ↓
按 route 顺序执行 extractor
  ├─ profile_extraction
  └─ working_memory_extraction
```

### 3.1 Routing Gate

现有 gate 的 `should_extract: boolean` 升级为结构化路由结果：

```json
{
  "should_extract": true,
  "routes": {
    "profile": true,
    "working_memory": false
  },
  "reason": "explicit_long_term_constraint",
  "message": "检测到长期旅行约束"
}
```

判定规则：

- 纯当前 trip facts，例如目的地、日期、预算、人数、候选池、骨架、每日计划 → `profile=false, working_memory=false`
- 跨旅行硬约束 / 明确长期偏好 / 明确拒绝 → `profile=true`
- 当前会话临时信号，例如“这轮先别考虑迪士尼”“先保留 A/B 两个方案” → `working_memory=true`
- 同一句话同时包含长期偏好与临时信号 → 两者都为 `true`
- 不确定但更像临时规划语境，而不是长期画像 → 偏向 `working_memory=true`

最后一条是保守策略：working memory 生命周期短，误写成本低；profile 生命周期长，误写成本高。

### 3.2 Profile Extractor

新增专用工具：

```text
extract_profile_memory
```

只输出：

```text
profile_updates.constraints
profile_updates.rejections
profile_updates.stable_preferences
profile_updates.preference_hypotheses
```

该 extractor 不允许输出 working memory，也不关心 session 临时便签。

prompt 重点：

- 只提取跨旅行可复用信息
- 单次观察默认进入 `preference_hypotheses`
- 高风险领域继续走 pending
- 当前 trip facts 直接忽略
- PII 直接忽略

### 3.3 Working Memory Extractor

新增专用工具：

```text
extract_working_memory
```

只输出：

```text
working_memory[]
```

该 extractor 不允许输出长期画像。

prompt 重点：

- 只提取当前 session/trip 临时有用的信号
- 适合提取：临时否决、临时偏好、决策提示、open question、watchout、普通 note
- 不提取当前 trip 权威事实；这些事实仍应由状态写入工具进入 `TravelPlanState`
- 不把长期偏好降级写成 working memory
- 每条 item 必须带 `expires`

---

## 4. 数据流

```text
用户消息进入 chat
  ↓
提交 MemoryJobSnapshot
  ↓
memory_extraction_gate
  ↓
routes.profile / routes.working_memory
  ↓
构造增量 extraction window
  ↓
if routes.profile:
    profile_extraction
    policy.classify_v3_profile_item()
    v3_store.upsert_profile_item()
  ↓
if routes.working_memory:
    working_memory_extraction
    policy.sanitize_working_memory_item()
    v3_store.upsert_working_memory_item()
  ↓
发布 background InternalTask 结果
  ↓
按成功/跳过规则推进 last_consumed_user_count
```

默认执行顺序为 profile 后 working memory。两者没有写入依赖，顺序主要为了日志和任务展示稳定。

如果其中一个 extractor 失败：

- 已成功写入的另一类结果不回滚。
- job 总状态可以是 `warning`，结果中包含分项状态。
- `last_consumed_user_count` 只有在所有被路由的 extractor 都成功或跳过时推进。

---

## 5. Internal Task 展示

保留现有后台 internal task 流，但把 extraction 展示拆细：

```text
memory_extraction_gate
profile_memory_extraction
working_memory_extraction
```

每个分项 extraction task 的 `result` 都应包含分项计数；当 job 需要返回聚合结果时，统一使用以下字段：

```json
{
  "routes": {
    "profile": true,
    "working_memory": true
  },
  "saved_profile_count": 1,
  "saved_working_count": 2,
  "pending_profile_count": 1
}
```

前端不需要新增卡片类型，只复用现有 `InternalTask` 渲染。

---

## 6. 兼容策略

实施时保留旧函数名 `_extract_memory_candidates()`，但内部改成 route-aware orchestration：

```text
_extract_memory_candidates()
  ├─ _extract_profile_memory()
  └─ _extract_working_memory()
```

这样可以减少 `main.py` 外部调用面的变化。

工具 schema 的迁移方式：

- `decide_memory_extraction` 增加 `routes`
- 新增 `extract_profile_memory`
- 新增 `extract_working_memory`
- 旧 `extract_memory_candidates` 工具 schema 不再作为主路径暴露；旧 parser 只作为测试迁移期间的兼容辅助保留

---

## 7. 风险与应对

### Gate 漏判

风险：gate 将某轮误判为 `none`，导致两类 extractor 都不执行。

应对：

- gate prompt 明确“不确定但像临时规划语境时路由到 working_memory”
- 测试覆盖长期偏好、临时信号、两者混合、纯 trip facts

### 调用次数增加

风险：一轮可能从最多 2 次 LLM 调用变为最多 3 次：gate + profile + working。

应对：

- 只有 `both` 时才执行两个 extractor
- 纯 trip facts 仍由 gate 直接跳过
- profile / working prompt 更短，单次延迟和错误率应下降

### 结果消费语义复杂

风险：一个 extractor 成功、另一个失败时，`last_consumed_user_count` 推进规则不清。

应对：

- 分项失败时不推进 consumed count
- 已写入项不回滚
- 下一次 coalesced snapshot 可重新覆盖处理

### 重复提取

风险：分项失败导致下一轮重新处理同一窗口，已成功项可能重复写入。

应对：

- profile 继续使用稳定 id upsert
- working memory extractor prompt 输入已有 working memory，要求不重复
- 本设计不引入 working memory 内容规范化 id；重复控制依赖现有 working memory 输入上下文与 extractor prompt 约束

---

## 8. 测试策略

后端单元测试：

- gate 解析 `routes.profile / routes.working_memory`
- profile extractor 只接受并解析 `profile_updates`
- working extractor 只接受并解析 `working_memory`
- 纯 trip facts 不触发任何 extractor
- 长期偏好只触发 profile
- 临时否决只触发 working memory
- 混合输入触发 both
- profile 成功、working 失败时，返回 warning 且不推进 consumed count

集成测试：

- chat 主流程仍不等待后台 extraction 完成
- background task stream 能看到 gate 与分项 extraction
- 新写入的 profile 下一轮进入长期画像召回
- 新写入的 working memory 下一轮进入当前会话工作记忆召回

回归测试：

- 当前 trip facts 不会被写入 profile 或 working memory
- PII 仍被丢弃或脱敏
- pending profile items 仍通过现有确认入口处理

---

## 9. 成功标准

1. 记忆写入链路仍保持异步，不阻塞 chat 回复。
2. profile 与 working memory 的提取 prompt 分离。
3. gate 能稳定输出路由决策。
4. 测试能证明长期画像、短期 working memory、当前 trip state 三者不会互相污染。
5. `PROJECT_OVERVIEW.md` 同步反映新的记忆提取路由结构。
