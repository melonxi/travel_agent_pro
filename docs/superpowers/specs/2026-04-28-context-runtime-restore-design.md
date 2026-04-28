# 上下文运行时恢复设计（二期）

## 背景

一期解决“历史不丢”：phase rebuild 前将旧 runtime messages append 到 SQLite history，并用独立 `history_seq` 保证历史顺序。

二期解决另一个问题：恢复 session 时不能把完整 history 直接作为下一轮 LLM prompt。完整 history 包含旧阶段流水账、system message、handoff note、工具结果和可能重复的 user anchor；如果全部喂回模型，会破坏当前 phase handoff 的强隔离设计。

本期目标是明确恢复时的双视图：

- `history_view`：完整历史，只供系统内部读取和未来诊断。
- `runtime_view`：恢复后交给 AgentLoop 的短工作集，行为上保持 phase 隔离。

## 目标

1. `restore_session()` 显式加载完整 `history_view`，但不直接交给 AgentLoop。
2. 新增 runtime view builder，使用完整依赖重建当前阶段可继续的短上下文。
3. 恢复后下一轮 LLM 不看到旧阶段全量流水账。
4. backtrack 后恢复不 replay 目标阶段旧 segment。
5. 保持一期 append-only history 不变。

## 非目标

- 不实现 phase segment/debug API。
- 不实现前端 phase timeline。
- 不承诺逐 token 级别恢复到中断前现场。
- 不把旧阶段摘要重新加入 prompt。
- 不改变 phase transition/backtrack 的业务规则。

## 关键修正

二期明确：runtime view 不能由 `history_view + plan` 纯函数完成。

当前在线 runtime view 依赖：

- `phase_router.get_prompt_for_plan(plan)`
- `context_manager.build_system_message(...)`
- `tool_engine.get_tools_for_phase(plan.phase, plan)`
- `memory_mgr.generate_context(user_id, plan)` 或 memory disabled fallback
- 当前 `plan.phase` / `plan.phase3_step`
- 最新可用用户锚点
- 最近一次 phase handoff/backtrack 语义

因此 runtime view builder 必须接收这些依赖，不能藏在 `MessageStore` 或纯 storage 层。

## 模块设计

### 新增 `backend/api/orchestration/session/runtime_view.py`

职责：

- 输入完整 history、当前 plan 和 prompt 构建依赖。
- 输出恢复后 AgentLoop 使用的 `runtime_view`。
- 不写数据库。
- 不改变 `message_rebuild.py` 的在线 rebuild 行为。

推荐接口：

```python
async def build_runtime_view_for_restore(
    *,
    history_view: list[Message],
    plan: TravelPlanState,
    user_id: str,
    phase_router: Any,
    context_manager: Any,
    memory_mgr: Any,
    memory_enabled: bool,
    tool_engine: Any,
) -> list[Message]:
    ...
```

### `SessionPersistence.restore_session()`

恢复流程：

1. 加载 session meta。
2. 加载 plan。
3. 加载完整 history rows 并反序列化为 `history_view`。
4. 初始化 `next_history_seq = max(history_seq) + 1`。
5. 调用 `build_runtime_view_for_restore(...)` 得到 `runtime_view`。
6. 返回 session dict：

```python
{
    "plan": plan,
    "messages": runtime_view,
    "history_messages": history_view,
    "next_history_seq": next_history_seq,
    ...
}
```

`history_messages` 本期只在后端内部存在，不暴露到现有 API。

### Agent 构建依赖

现有 `SessionPersistence` 只有 `build_agent`，但 runtime view builder 需要 `tool_engine`。二期采用低改动方案：先 build agent，再把 `agent.tool_engine` 传给 runtime view builder。

本期不新增 `tool_engine_factory` 或 `build_tool_engine` 依赖，避免扩大 `SessionPersistence` 构造参数和测试替身范围。

## Runtime View 规则

### 规则 1：永远重建当前 system message

恢复时不复用 history 中旧 system message。system prompt 依赖当前 plan、工具列表、memory context，必须重新生成。

### 规则 2：只保留当前可继续所需 anchor

runtime view 至少包含：

- 当前 system message
- 最新用户锚点，或可继续的当前阶段尾部消息

如果 history 中能找到最近一次 rebuild 后的短工作集，可以复用其中的非 system anchor；否则退化为最新 user message。

### 规则 3：Phase 3 子阶段隔离

当前处于 Phase 3 时，runtime view 不混入更早 `phase3_step` 的原始消息。比如当前是 `skeleton`，不 replay `brief` 和 `candidate` 的工具结果。

### 规则 4：Backtrack 后不 replay 旧目标阶段

如果当前 plan 是回退后的 Phase 3，runtime view 不把上一次 Phase 3 的旧 segment 塞回 prompt。旧 segment 只属于 history。

### 规则 5：无法确定边界时保守降级

如果 history 中缺少新字段或无法确定 rebuild 边界，runtime view 使用：

- 新 system message
- 最新 user message

这比把全量 history 灌回 LLM 更安全。

## 与一期的关系

一期保留完整 history，但不承诺恢复等价。二期在此基础上收敛恢复行为。

二期不修改一期的 append-only 写入模型，只消费其产出的字段：

- `history_seq`
- `phase`
- `phase3_step`
- `run_id`
- `trip_id`

如果一期未实现 `run_id` 或 `trip_id`，二期仍可工作，但恢复边界判断会更弱。

## 数据流

### 服务启动后恢复 session

1. API route 调用 `restore_session(session_id)`。
2. Persistence 加载完整 history。
3. Persistence 构建 agent，拿到 tool engine。
4. Runtime view builder 生成新 system message。
5. Runtime view builder 选择最小 anchor。
6. Session dict 中：
   - `messages = runtime_view`
   - `history_messages = history_view`
7. 下一次 chat 只在 `runtime_view` 后 append 新 user message。

### 继续对话

恢复后继续对话沿用现有 chat route。新消息仍 append 到 runtime messages，并由一期的 append-only persistence 写入 history。

## 测试策略

### Runtime View Builder

- 当前 Phase 5 时，不包含 Phase 1/3 原始工具结果。
- 当前 Phase 3 `skeleton` 时，不包含 `brief` / `candidate` 原始工具结果。
- backtrack 后 Phase 3 恢复时，不 replay 上一次 Phase 3 历史。
- history 缺少 phase 标签时，退化为 `[system, latest_user]`。
- system message 每次恢复重新构建，而不是读取旧 system row。

### Session Restore

- `restore_session()` 返回 `history_messages` 和短 `messages`。
- `next_history_seq` 初始化为数据库最大值 + 1。
- 恢复后的 `messages` 不是完整 history。
- 恢复后再发一轮消息，history_seq 继续递增。

### 集成测试

- Phase 1 -> 3 -> 5 后模拟进程重启，恢复后下一轮 LLM messages 不含 Phase 1 工具结果。
- Phase 3 `brief -> candidate -> skeleton` 后模拟重启，恢复后不含更早子阶段工具结果。
- Phase 5 -> 3 backtrack 后模拟重启，恢复后不含 Phase 5 旧 segment，也不 replay 老 Phase 3 segment。

## 风险

### “严格等价恢复”过度承诺

当前系统的 memory context、可用工具、plan 状态可能随配置变化而变化。二期只承诺恢复后不污染 prompt，并尽量与在线短上下文语义一致，不承诺字节级相同。

### Anchor 选择错误

如果 builder 选错 anchor，LLM 可能缺少用户当前意图。测试必须覆盖“phase 切换刚发生后重启”的场景，因为这时当前阶段还没有自然用户消息。

### 与前端恢复混淆

`history_messages` 是后端内部字段，不应直接返回现有 `/api/messages`。前端接口的语义仍由一期保持。

## 验收标准

1. 恢复后 AgentLoop 使用短 runtime view，而不是完整 history。
2. system message 由当前 plan 和工具列表重新生成。
3. Phase 3 子阶段恢复不混入旧子阶段流水账。
4. backtrack 后恢复不 replay 旧 segment。
5. 恢复后继续写入 history_seq 不重复、不倒退。
