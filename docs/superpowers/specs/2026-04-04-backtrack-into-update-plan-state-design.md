# 将回退机制融入 update_plan_state tool

**日期**: 2026-04-04
**状态**: 已确认

> **Superseded（2026-04-15）**
> 本方案已被 `docs/superpowers/specs/2026-04-15-split-update-plan-state-design.md` 取代。
> 回退写入不再依赖单体状态写工具，当前实现统一使用 `request_backtrack`。

## 背景

当前 phase 回退由两层机制处理：

1. **隐式回退**（`main.py:_detect_backtrack()`）：关键词匹配用户消息，在 `agent.run()` 之前触发回退
2. **显式回退**（`POST /api/backtrack/{session_id}`）：前端主动调用

关键词匹配覆盖面窄、误判风险高。本设计将回退能力融入 `update_plan_state` tool，让 LLM 主动判断并触发回退，关键词检测降级为 fallback。

## 核心设计

### 1. `update_plan_state` 扩展

`_ALLOWED_FIELDS` 新增 `"backtrack"`。当 `field == "backtrack"` 时，`value` 结构为：

```python
{"to_phase": int, "reason": str}
```

tool 内部对 backtrack 走独立路径，调用 `BacktrackService.execute()` 完成回退事务，不经过普通字段更新逻辑。

返回值中包含引导指令：`{"backtracked": true, "next_action": "请向用户确认回退结果，不要继续调用其他工具"}`，确保 LLM 回退后只输出文字，agent loop 自然结束。

tool description 增加回退使用场景说明。

### 2. 新增 `BacktrackService`

新建 `backend/phase/backtrack.py`，统一执行回退事务：

```
BacktrackService.execute(plan, to_phase, reason, snapshot_path):
  1. 校验 to_phase < plan.phase（否则抛异常）
  2. plan.backtrack_history.append(BacktrackEvent(...))
  3. plan.clear_downstream(from_phase=to_phase)
  4. plan.phase = to_phase
```

`POST /api/backtrack` 和 `update_plan_state(field="backtrack")` 都调用同一个服务。

### 3. 修复 `_PHASE_DOWNSTREAM` 映射

当前映射：

```python
_PHASE_DOWNSTREAM = {
    3: ["accommodation", "daily_plans"],
    4: ["daily_plans"],
}
```

回退到 phase 2 时 `destination` 不被清除。补全为：

```python
_PHASE_DOWNSTREAM = {
    1: ["destination", "destination_candidates", "dates", "accommodation", "daily_plans"],
    2: ["destination", "dates", "accommodation", "daily_plans"],
    3: ["dates", "accommodation", "daily_plans"],
    4: ["accommodation", "daily_plans"],
    5: ["daily_plans"],
}
```

### 4. hook 修复：回退后跳过自动前进

`on_tool_call` hook 中，当 `update_plan_state` 返回 `backtracked=True` 时，跳过 `check_and_apply_transition()`：

```python
async def on_tool_call(**kwargs):
    if kwargs.get("tool_name") == "update_plan_state":
        result = kwargs.get("result")
        if result and result.data and result.data.get("backtracked"):
            return
        phase_router.check_and_apply_transition(plan)
```

### 5. main.py 变更

- `_detect_backtrack()` 和 `_BACKTRACK_PATTERNS` **保留**作为 fallback
- chat handler 中移除 agent.run() 之前的隐式回退检测，改为在 agent.run() 之后检查：如果本轮 agent 没有触发 backtrack tool，但关键词匹配命中，则执行回退
- session 增加 `needs_rebuild` 标记，下一轮 chat 时检查并重建 agent
- `POST /api/backtrack` 内部改调 `BacktrackService.execute()`

### 6. 不变的部分

- `PhaseRouter.infer_phase()` 和 `check_and_apply_transition()` 逻辑不变
- `BacktrackEvent` 模型不变
- `AgentLoop` 不改动
- `prepare_backtrack()` 方法保留，内部改调 `BacktrackService` 或直接复用其逻辑
- 前端交互不受影响

## 涉及文件

| 文件 | 变更类型 |
|---|---|
| `backend/phase/backtrack.py` | 新增 |
| `backend/tools/update_plan_state.py` | 修改 |
| `backend/state/models.py` | 修改 `_PHASE_DOWNSTREAM` |
| `backend/main.py` | 修改 hook、chat handler、backtrack endpoint |
| `backend/phase/router.py` | `prepare_backtrack` 改调 `BacktrackService` |
| `backend/tests/test_backtrack_service.py` | 新增 |
| `backend/tests/test_phase_router.py` | 更新 |
| `backend/tests/test_error_paths.py` | 更新 |

## 测试要求

1. `update_plan_state(field="backtrack")` 正常回退
2. 非法 `to_phase`（等于或大于当前 phase）被拒绝
3. 回退后 hook 不触发自动前进
4. API 回退和 tool 回退行为一致性
5. 回退后 `_PHASE_DOWNSTREAM` 正确清除目标字段
6. LLM 未触发 tool 时 fallback 关键词检测仍生效

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| LLM 不主动调用 backtrack tool | 保留关键词 fallback，埋点观察命中率 |
| 回退后被自动前进冲掉 | hook 中检测 `backtracked` 标记，跳过自动前进 |
| 回退后本轮继续用旧 tools | tool 返回值引导 LLM 只输出文字，循环自然结束 |
| `_PHASE_DOWNSTREAM` 扩展后误清数据 | 补充测试覆盖每个 phase 的清理边界 |
