# 上下文历史分段与诊断设计（三期）

## 背景

一期保住完整 history，二期保证恢复时不把 history 污染 prompt。三期解决“历史可解释”的问题。

仅有 `phase` / `phase3_step` 不足以表达真实 segment。用户可能从 Phase 5 回退到 Phase 3，或者在同一 Phase 3 子阶段内多轮重建。按 `phase=3` 聚合会把不同访问轮次混在一起，无法可靠诊断“第一次 Phase 3”和“回退后的 Phase 3”分别发生了什么。

三期目标是在不新增 `phase_segments` 表的前提下，让每条 history message 带有足够边界信息，从 `messages` 表派生出稳定的 segment。

## 目标

1. 引入上下文边界标识，区分多次进入同一 phase/substep。
2. 支持从 `messages` 表派生 phase/substep segment。
3. 提供后端内部 history/debug 查询能力。
4. 为 trace、eval、postmortem 提供可消费的历史结构。
5. 继续禁止把历史 segment replay 到 LLM prompt。

## 非目标

- 不做前端 phase timeline UI。
- 不新增 `phase_segments` 表。
- 不实现完整对话重放执行器。
- 不把历史 segment 用作 prompt recall 来源。

## 核心决策

### 决策 1：使用 `context_epoch` 作为分段边界

三期新增 `context_epoch INTEGER` 到 `messages` 表。

语义：

- 同一 session 内从 0 开始递增。
- 每次 runtime context 被 rebuild 时，进入新的 epoch。
- 同一 epoch 内的消息属于同一段运行时工作上下文。
- Phase 前进、Phase 3 子阶段切换、backtrack 都会开启新 epoch。

`context_epoch` 比单纯 `phase_visit_id` 更贴近当前系统：它不仅区分重复进入 Phase 3，也区分 Phase 3 内子阶段重建后的上下文边界。

### 决策 2：segment 是查询派生视图，不独立落表

三期仍不新增 `phase_segments` 表。Segment 通过以下字段派生：

- `session_id`
- `context_epoch`
- `phase`
- `phase3_step`
- `trip_id`
- `run_id`
- `history_seq`

派生规则：

- 相同 `context_epoch` 的连续消息组成一个 runtime context segment。
- segment 的 phase/substep 取该 epoch 内第一条带 phase 标签消息的值。
- segment 的开始/结束顺序由 `MIN(history_seq)` / `MAX(history_seq)` 决定。

### 决策 3：当前 epoch 是运行态和持久态共同字段

session dict 中新增：

- `current_context_epoch`

恢复时从数据库最大 `context_epoch` 初始化。发生 rebuild 前 flush 使用旧 epoch；rebuild 完成后递增 `current_context_epoch`，后续 runtime messages 使用新 epoch。

如果旧数据 `context_epoch IS NULL`，派生 segment 时把它们归为 legacy epoch，不参与精确分段断言。

## 数据模型

### `messages` 表新增字段

- `context_epoch INTEGER`
- `rebuild_reason TEXT`

`rebuild_reason` 只在表示边界的 synthetic/system/handoff 消息上有值，普通消息为空。取值限制为：

- `phase_forward`
- `phase3_step_change`
- `backtrack`
- `restore_fallback`

一期/二期已经存在的字段继续使用：

- `history_seq`
- `phase`
- `phase3_step`
- `run_id`
- `trip_id`

推荐索引：

- `CREATE INDEX idx_messages_epoch ON messages(session_id, context_epoch, history_seq)`
- `CREATE INDEX idx_messages_trip_epoch ON messages(session_id, trip_id, context_epoch)`

## Epoch 推进规则

### Phase 前进

1. 当前旧 epoch 内 runtime messages pre-rebuild flush。
2. `current_context_epoch += 1`。
3. rebuild 后短 runtime messages 属于新 epoch。
4. 新 epoch 的 handoff note 可带 `rebuild_reason="phase_forward"`。

### Phase 3 子阶段切换

1. 旧 step 的 runtime messages flush 到旧 epoch。
2. `current_context_epoch += 1`。
3. 新 step 的短 runtime messages 属于新 epoch。
4. 新 epoch 可带 `rebuild_reason="phase3_step_change"`。

### Backtrack

1. 回退前 runtime messages flush 到旧 epoch。
2. `current_context_epoch += 1`。
3. backtrack notice 和 user anchor 属于新 epoch。
4. 新 epoch 带 `rebuild_reason="backtrack"`。

### Restore

恢复不自动开启新 epoch。只有恢复时无法构造安全 runtime view，并退化为 `[system, latest_user]` 时，才允许在后续写入中标记 `restore_fallback`。

## Segment 查询能力

新增后端内部 helper：

```python
def derive_context_segments(rows: list[dict]) -> list[ContextSegment]:
    ...
```

`ContextSegment` 字段：

- `session_id`
- `context_epoch`
- `phase`
- `phase3_step`
- `trip_id`
- `run_ids`
- `start_history_seq`
- `end_history_seq`
- `message_count`
- `rebuild_reason`

该 helper 只做查询聚合，不写状态。

### 内部 Debug 查询

三期先新增 service/helper 级查询能力，不公开 HTTP route：

- `list_context_segments(session_id) -> list[ContextSegment]`
- `load_context_segment_messages(session_id, context_epoch) -> list[Message]`

约束：

- 默认仅后端调试使用，不接入前端主 UI。
- 返回 system/tool/internal runtime messages 时保留原始结构。
- 不替代 `/api/messages/{session_id}`。
- 后续若要公开 HTTP debug API，必须先补权限控制设计；不属于三期范围。

## 与恢复的关系

三期的 segment 能辅助二期 runtime view builder 找到最近 epoch，但不能改变 prompt 红线：

- 不能把旧 epoch 的完整消息 replay 给 LLM。
- 不能把目标 phase 的历史 segment 当作 backtrack 后上下文。
- runtime view builder 只能用 segment metadata 选择 anchor，不能注入 segment body。

## 测试策略

### Epoch 写入

- 新 session 默认 epoch 为 0。
- Phase 1 -> 3 后，Phase 1 消息在 epoch 0，handoff 后 runtime messages 在 epoch 1。
- Phase 3 `brief -> candidate -> skeleton` 每次子阶段切换递增 epoch。
- Phase 5 -> 3 backtrack 后新 Phase 3 epoch 不等于旧 Phase 3 epoch。

### Segment 派生

- `derive_context_segments()` 按 `context_epoch` 聚合。
- 同一 phase 多次访问会生成多个 segment。
- 同一 Phase 3 不同 substep 会生成不同 segment。
- legacy rows 缺少 `context_epoch` 时不会破坏新 rows 分段。

### Debug 查询

- segment list 返回 start/end history_seq。
- 单个 epoch messages 按 history_seq 排序。
- service/helper 查询不影响 `/api/messages/{session_id}`。

### Prompt 红线

- 恢复或 backtrack 后，runtime view 不包含旧 epoch body。
- 测试显式断言旧 epoch 中的工具结果文本不出现在下一轮 LLM messages。

## 风险

### Epoch 与 run_id 混淆

一次 run 内可能发生 phase rebuild，也可能跨多个 epoch。`run_id` 表示 SSE run，`context_epoch` 表示 runtime context 边界，两者不能互相替代。

### Epoch 推进遗漏

如果某个 rebuild 路径忘记递增 epoch，segment 会合并。三期必须把 epoch 推进集中在统一 callback 或 coordinator 中，避免散落在多个 route。

### Debug API 泄露内部 prompt

完整 history 可能包含 system prompt、工具参数、provider_state。HTTP debug API 必须明确为内部用途；如果没有权限控制，先不暴露 route。

## 验收标准

1. 同一 phase 多次进入时，能派生出不同 context segment。
2. Phase 3 子阶段切换后，brief/candidate/skeleton/lock 可按 epoch 区分。
3. Backtrack 后新旧目标 phase 不混段。
4. Debug/helper 能按 segment 查询完整消息。
5. Runtime prompt 仍不 replay 历史 segment。
