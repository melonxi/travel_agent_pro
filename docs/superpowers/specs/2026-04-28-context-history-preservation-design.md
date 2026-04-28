# 上下文历史保全设计（一期）

## 背景

当前会话消息只有一条轨道：`session["messages"]` 既是在线 LLM prompt 的运行时工作集，也是 `backend/api/orchestration/session/persistence.py::persist_messages()` 覆盖写回 SQLite 的来源。

Phase 前进、Phase 3 子阶段切换、backtrack 后，`AgentLoop` 会通过 `backend/agent/execution/message_rebuild.py` 重建短上下文，并用 `messages[:] = rebuilt_messages` 替换原列表。这个行为对 prompt 隔离是正确的，但它发生在最终持久化之前，因此旧阶段的 assistant/tool/tool result 原始轨迹会在落库前被丢弃。

一期目标不是重做恢复语义，也不是做历史浏览 UI，而是先解决最大数据风险：**phase rebuild 不应擦掉已经发生过的真实消息轨迹**。

## 目标

1. `messages` 表从“当前 runtime 快照”转为 append-only history 事实源。
2. phase rebuild 前必须把即将被替换的 runtime messages 先 flush 到 history。
3. 历史写入使用独立、单调递增的 `history_seq`，不得依赖 runtime `len(messages)`。
4. 不改变当前 LLM 看到的 prompt 形态。
5. 不改变当前前端聊天恢复体验；`/api/messages/{session_id}` 仍返回前端可消费的消息视图，不直接暴露完整内部历史。

## 非目标

- 不实现恢复后 prompt 与未中断会话的严格等价。
- 不实现 phase segment 精确分组。
- 不新增 phase timeline/debug UI。
- 不把旧阶段历史重新注入 LLM prompt。
- 不改变 `PhaseRouter` 的阶段推断和业务语义。

## 设计原则

### 1. Runtime messages 和 History messages 分离

`session["messages"]` 继续是短小、可被 rebuild 替换的运行时工作集。SQLite `messages` 表变为 append-only history，不再被 `DELETE FROM messages WHERE session_id = ?` 覆盖清空。

两者不再通过“当前列表完整写回”同步，而是通过明确的 append 操作同步。

### 2. Rebuild 前 flush 是硬要求

所有会缩短或替换 `session["messages"]` 的路径，必须先触发 pre-rebuild flush：

- Phase 1 -> 3 / 3 -> 5 / 5 -> 7 前进切换
- Phase 3 `brief -> candidate -> skeleton -> lock` 子阶段切换
- 工具触发的 backtrack runtime rebuild

flush 使用切换前的 `phase` / `phase3_step` 给消息打标签。之后 rebuild 仍按现有逻辑返回短 runtime messages。

### 3. Durable cursor 独立于 runtime list

一期禁止使用 `persisted_message_count == len(runtime_messages)` 作为持久化边界。phase rebuild 后 runtime list 会缩短，这个游标会失效。

应使用 session 级 durable cursor：

- `history_seq`：每个 session 内单调递增，从 0 开始。
- `next_history_seq`：运行态保存在 `session` dict，恢复时从数据库 `MAX(history_seq) + 1` 初始化。
- 每次 append rows 时，按 `next_history_seq` 连续分配，并在写入成功后推进。

`seq` 可继续保留兼容现有排序，但新逻辑应以 `history_seq` 作为历史顺序权威字段。后续可逐步让 `seq` 退化为 legacy alias。

## 数据模型

### `messages` 表新增字段

一期新增：

- `phase INTEGER`
- `phase3_step TEXT`
- `history_seq INTEGER`
- `run_id TEXT`
- `trip_id TEXT`

字段语义：

- `phase`：消息生成或 flush 当下所属的 `plan.phase`
- `phase3_step`：Phase 3 时记录当前子阶段，其余阶段为空
- `history_seq`：session 内 append-only 顺序，唯一、单调递增
- `run_id`：本轮 `RunRecord.run_id`，用于把一次 SSE run 中的消息关联起来
- `trip_id`：写入当下的 `plan.trip_id`，用于后续区分 reset/backtrack 后的新旅行语境

推荐约束与索引：

- `UNIQUE(session_id, history_seq)`
- `CREATE INDEX idx_messages_history ON messages(session_id, history_seq)`
- `CREATE INDEX idx_messages_phase ON messages(session_id, phase, phase3_step, history_seq)`

旧库迁移只补列，不回填。旧历史已经被覆盖过，无法可靠恢复真实 phase 边界。

## 写入模型

### `MessageStore`

`MessageStore.append()` / `append_batch()` 支持写入新增字段。`load_all()` 默认按 `history_seq ASC, id ASC` 排序；旧数据 `history_seq IS NULL` 时回退到 `seq ASC, id ASC`。

### `SessionPersistence`

`persist_messages()` 改为 append coordinator，签名应表达 durable cursor，而不是 runtime count：

```python
async def persist_messages(
    self,
    session_id: str,
    messages: list[Message],
    *,
    phase: int,
    phase3_step: str | None,
    run_id: str | None,
    trip_id: str | None,
    next_history_seq: int,
) -> int:
    ...
```

返回新的 `next_history_seq`。调用方负责把返回值写回 `session["next_history_seq"]`。

一期允许 `persist_messages()` append 传入列表中的全部消息；是否去重由调用方通过明确 flush 时机控制。不要在 persistence 层用 runtime list index 推断“哪些已写”。

### Pre-Rebuild Flush Callback

`AgentLoop` 增加可选回调：

```python
on_before_message_rebuild(
    *,
    messages: list[Message],
    from_phase: int,
    from_phase3_step: str | None,
) -> Awaitable[None]
```

调用点：

- `_handle_phase_transition()` 调用 `_rebuild_messages_for_phase_change()` 前
- Phase 3 step change 调用 `_rebuild_messages_for_phase3_step_change()` 前

回调失败时不应阻断在线对话，但必须记录 warning，并在测试中覆盖“失败不影响 rebuild”。这是为了避免持久化短暂故障把用户主流程卡死；同时日志要足够明显，便于后续补偿。

## API 行为

### `/api/messages/{session_id}`

一期不把完整内部 history 直接返回前端。原因：

- 前端当前把该接口当聊天窗口恢复源。
- 完整 history 会包含 system、handoff、工具轨迹和可能重复的 runtime anchor。
- 直接切换会改变 UI 行为，扩大一期风险。

一期策略：

- 现有接口继续返回前端可消费的消息视图。
- 内部完整历史通过 `MessageStore.load_all()` 保留，但不新增公开 debug API。
- 三期再设计 history/debug API。

## 数据流

### 正常聊天

1. chat route 把用户消息 append 到 runtime `messages`。
2. AgentLoop 按现有流程运行。
3. 若未发生 rebuild，finalization append 本轮 runtime messages 到 history。
4. 若发生 rebuild，AgentLoop 先触发 pre-rebuild flush，保存切换前 runtime messages。
5. AgentLoop rebuild runtime messages，继续在线流程。
6. finalization 对 rebuild 后的 runtime messages 是否落 history，由调用方根据本轮是否已经 flush 决定；一期重点是不能漏掉 rebuild 前 tail。

### Phase Rebuild

1. 当前 runtime messages 包含旧阶段完整工具调用轨迹。
2. pre-rebuild flush 使用旧 `phase` / `phase3_step` / `run_id` / `trip_id` 写入 history。
3. AgentLoop 替换 runtime messages 为短工作集。
4. LLM 后续仍只看到短上下文。

### 服务重启

一期只要求恢复时能加载完整 history，并初始化 `next_history_seq = max(history_seq) + 1`。不承诺 runtime view 严格等价恢复；该能力归二期。

## 测试策略

### 数据库迁移

- 新库创建后 `messages` 含新增列。
- 旧库只有 `provider_state` 时，迁移补齐 `phase` / `phase3_step` / `history_seq` / `run_id` / `trip_id`。
- 旧数据新增列允许为空。

### MessageStore

- `append()` 正确写入 phase 标签和 `history_seq`。
- `append_batch()` 保持 `history_seq` 顺序。
- `load_all()` 对新数据按 `history_seq` 排序。

### SessionPersistence

- 不再执行整段 delete。
- 多次 append 后数据库保留全部历史。
- `next_history_seq` 单调推进。
- runtime list 缩短后继续 append 不丢消息、不复用 history_seq。

### AgentLoop

- phase 前进 rebuild 前触发 flush，flush 收到的是切换前完整 messages。
- Phase 3 step rebuild 前触发 flush。
- backtrack rebuild 前触发 flush。
- flush callback 抛错时 rebuild 仍继续，并记录 warning。

### API/集成

- Phase 1 -> 3 -> 5 后，数据库 history 中仍能查询到 Phase 1 的 assistant/tool/tool result。
- `/api/messages/{session_id}` 响应不因完整 history 落库而突然暴露 system/history 全量内部轨迹。

## 风险

### 重复写入

一期最容易出错的是同一批 runtime messages 被 pre-rebuild flush 和 finalization 重复 append。解决方式不是回到 runtime count cursor，而是让调用方显式记录本 run 是否已 flush，以及 flush 后 finalization 是否只写 rebuild 后新增部分。

### System message 膨胀

当前 runtime messages 中包含 system message。append-only 后 system message 会进入 history。这个行为一期可接受，因为目标是保全实际 runtime 轨迹；前端接口继续过滤 system，三期再定义 debug view 的展示规则。

### 写盘失败

pre-rebuild flush 失败会造成历史缺口。由于在线主流程优先，失败不阻断对话，但必须进入日志和后续观测。二期/三期可再设计补偿机制。

## 验收标准

1. Phase rebuild 后，旧阶段原始消息不再从 SQLite 消失。
2. `messages` 表同一 session 的 `history_seq` 单调递增且不重复。
3. Runtime prompt 形态与当前线上行为一致。
4. 前端聊天恢复体验不因完整 history 落库发生明显变化。
5. 测试覆盖 phase 前进、Phase 3 子阶段切换、backtrack 三类 rebuild flush。
