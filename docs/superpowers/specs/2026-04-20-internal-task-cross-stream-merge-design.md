# Internal Task Cross-Stream Merge Design

## 1. 背景

当前聊天流中的内部任务卡片由前端 `ChatPanel` 在收到 `internal_task` SSE 事件后插入或更新。

现状只在单次 SSE 流内部成立：`ChatPanel` 通过当前流 handler 持有的 `internalTaskMessageIds` 映射，用 `task.id -> message.id` 做生命周期合并。同一条流里先收到 `pending`、后收到 `success` 时，卡片能正确从“进行中”变成“完成”。

问题出现在跨流场景：

- 第一条流插入 `pending` 卡片。
- 第二条流才收到同一个 `task.id` 的终态事件。
- 新流会重新创建 `internalTaskMessageIds`，无法命中旧卡片。
- 前端于是新增一张“完成”卡片，旧卡片永远停留在“进行中”。

这个问题当前最明显地出现在 `memory_recall`、`memory_extraction`、`quality_gate` 三类内部任务上。

## 2. 目标

- 修复 `internal_task` 卡片在跨流场景下的生命周期合并。
- 保证同一个 `task.id` 在聊天流中始终只对应一张卡片。
- 保持现有同流更新行为不变。
- 不修改后端 SSE 协议。
- 不扩大到真实工具卡片或页面刷新后的历史重建。

## 3. 非目标

- 不修改 `tool_call` / `tool_result` 的合并策略。
- 不引入新的后端任务聚合接口。
- 不处理“刷新页面后从历史消息重建内部任务生命周期”的一致性问题。
- 不改变内部任务卡片视觉样式和文案层级。

## 4. 根因

根因不是后端发送了错误的 `task.id`，而是前端把任务生命周期索引错误地绑定到了“单次流上下文”。

当前实现中：

- `startMessageStream()` 会创建新的 `EventHandlerState`。
- `continueGeneration()` 也会创建新的 `EventHandlerState`。
- `EventHandlerState.internalTaskMessageIds` 只存在于这次流中。

因此只要终态事件不是在同一次流里到达，前端就失去对旧卡片的定位能力。

## 5. 方案对比

### 方案 A：按当前消息列表回写旧卡片（推荐）

做法：

- 保留现有流内 `Map` 作为快速路径。
- 当 `Map` 未命中时，再在 `messages` 中查找相同 `internalTaskId` 的旧卡片。
- 找到后更新旧卡片，并把找到的 `message.id` 回填到当前流 `Map`。

优点：

- 改动最小。
- 直接命中跨流根因。
- 不增加新的全局状态源。
- 不需要后端改动。

缺点：

- 每次 fallback 时要扫描一次消息数组。
- 但聊天消息量有限，可接受。

### 方案 B：引入组件级全局索引

做法：

- 在 `ChatPanel` 顶层维护一份跨流存在的 `task.id -> message.id` 索引。

优点：

- 查询成本更低。

缺点：

- 增加第二套状态源。
- 需要处理索引与消息数组删除、重排、重置时的一致性。
- 对当前问题来说过度设计。

### 方案 C：后端只输出聚合后的最终任务态

优点：

- 前端更简单。

缺点：

- 会削弱进行中可见性。
- 破坏现有内部任务生命周期展示模型。
- 改动面过大。

本设计采用方案 A。

## 6. 前端设计

### 6.1 数据模型调整

`ChatMessage` 增加稳定字段：

- `internalTaskId?: string`

该字段只服务于前端消息定位，不改变展示组件 API 语义。

插入内部任务卡片时，除保留 `internalTask` 对象外，还写入 `internalTaskId = task.id`。

### 6.2 生命周期合并规则

前端收到 `internal_task` 事件时按以下顺序处理：

1. 优先从当前流的 `internalTaskMessageIds` 查找 `task.id`。
2. 如果命中，则更新对应消息。
3. 如果未命中，则扫描当前 `messages`，查找 `internalTaskId === task.id` 的旧卡片。
4. 如果找到旧卡片，则更新该卡片，并把它的 `message.id` 回填到当前流 `internalTaskMessageIds`。
5. 如果仍未找到，说明这是首次出现的任务，插入新卡片。

这样可以同时覆盖：

- 同一条流内的 pending -> success。
- 不同流之间的 pending -> success。
- 背景任务在下一条流开头补发终态的情况。

### 6.3 消息插入与排序

本次不改变现有插入策略：

- 新任务仍按现有规则插入到 assistant 消息前。
- 命中旧卡片时只更新内容，不改变原有位置。

这样可以保持用户看到的时序稳定，避免因为终态晚到而把老卡片重新移动到列表尾部。

### 6.4 性能与复杂度

扫描 `messages` 是一次线性查找，但聊天面板消息量通常较小，且 fallback 只发生在跨流场景，因此优先选择更少状态、更低实现复杂度的方案。

如果未来内部任务种类和消息量都显著增加，再考虑把这套 fallback 升级成组件级索引。

## 7. 测试设计

采用 TDD，先补一个前端行为测试，覆盖当前缺口。

需要新增的测试场景：

1. 第一条流发送某个 `internal_task.pending`。
2. 流结束或中断。
3. 第二条流发送同一个 `task.id` 的 `internal_task.success`。
4. 断言聊天区只存在一张该任务卡。
5. 断言这张卡的状态为成功，不再残留 pending 卡片。

测试应优先覆盖实际用户报告中的三类任务语义之一，例如：

- `记忆召回`
- `记忆提取`
- `阶段推进检查`

也可以使用通用 `internal_task` 事件模型，只要明确验证“跨流复用同一 task.id”即可。

现有同流生命周期测试继续保留，作为回归保护。

## 8. 影响文件

- `frontend/src/components/ChatPanel.tsx`
  - 调整 `ChatMessage` 结构。
  - 调整 `internal_task` 事件处理逻辑。
- `e2e-waiting-experience.spec.ts` 或对应前端交互测试文件
  - 新增跨流生命周期合并用例。

如果现有 E2E 更适合表达“停止/继续生成”或“下一轮流补发后台终态”，可以把用例放在更贴近该语义的现有 spec 中，但原则是不新增不必要的测试基础设施。

## 9. 风险与防护

风险：误把不同任务合并成一张卡片。

防护：

- 只按精确的 `task.id` 匹配，不按 `label` 或 `kind` 匹配。
- 命中旧卡片时要求该消息本身是内部任务消息。

风险：消息位置异常跳动。

防护：

- 命中旧卡片时只更新，不重新插入。

风险：后续流内更新性能退化。

防护：

- 保留原有 `internalTaskMessageIds` 快速路径。
- 只有 `Map` 未命中时才做消息数组 fallback 查找。

## 10. 验收标准

- 同一个 `internal_task.id` 在跨流场景下只显示一张卡片。
- 旧的 pending 卡片会被终态事件正确回写。
- 同流更新逻辑不回退。
- 不影响真实工具卡片行为。
- 不需要修改后端接口或 SSE 事件结构。
