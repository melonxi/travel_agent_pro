# Phase 5 状态契约修复后复测残留问题复盘

- 事故日期：2026-04-17
- 复测对象：`docs/superpowers/plans/2026-04-17-phase5-state-contract-fix.md`
- 问题 session：`sess_633cc86ff6c9`
- 事故范围：`backend/tools/engine.py`、`backend/tools/plan_tools/daily_plans.py`、`backend/harness/validator.py`、`backend/main.py`、`backend/storage/session_store.py`
- 事故类型：修复后残留缺陷 + 工具参数错误仍可触发 + 校验结果未进入自修复闭环 + session meta 持久化滞后
- 事故等级：中
- 事故状态：已修复

---

## 1. 复测摘要

针对上一轮 `Phase 5 计划工具错误事故复盘` 中发现的状态契约问题，已按 `2026-04-17-phase5-state-contract-fix.md` 做过修复。随后使用真实前端重新跑 `sess_633cc86ff6c9`，从 Phase 3 lock 阶段确认交通住宿，再进入 Phase 5 生成逐日行程。

复测结果分为两部分：

1. 原主问题已明显改善：`Day 7` 不再因 `total_days == 6` 被拒绝，系统成功写入 Day 1-7。
2. 新的残留问题仍存在：`replace_daily_plans` 空参数调用仍发生一次；大量时间冲突 validator 错误仍持续累积；SQLite session meta 仍停留在 Phase 3。

这说明状态契约主修复有效，但 Phase 5 的工具错误诊断、行程质量闭环和持久化边界仍未完全收敛。

---

## 2. 复测环境

- 前端：`http://127.0.0.1:5173`
- 后端：`http://127.0.0.1:8000`
- 数据库：`backend/data/sessions.db`
- 计划文件：`backend/data/sessions/sess_633cc86ff6c9/plan.json`
- 浏览器自动化：Node.js + Playwright headless

截图和页面文本保存在：

- `screenshots/phase5-retest-before-send.png`
- `screenshots/phase5-retest-streaming.png`
- `screenshots/phase5-retest-after-run.png`
- `screenshots/phase5-retest-body.txt`

---

## 3. 复测路径

1. 启动开发服务：`bash ./scripts/dev.sh`。
2. 打开前端首页。
3. 进入 `九寨沟 + 成都 · 6天5晚` session。
4. 发送交通住宿确认：

```text
我选200元档春秋航空往返；成都文殊院选成都蓉城假日酒店，九寨沟沟口选九寨沟兰朵酒店，成都春熙路选成都雅熙酒店公寓。请锁定这些选择并进入日程详排。
```

5. 等待 SSE 流式执行，观察工具调用、前端状态和 trace。
6. 拉取 `/api/plan/sess_633cc86ff6c9`、`/api/sessions/sess_633cc86ff6c9/trace` 和 SQLite session meta。

---

## 4. 已修复确认

### 4.1 Day 7 超限错误消失

复测后 API 返回：

```json
{
  "phase": 5,
  "dates": {
    "start": "2026-05-24",
    "end": "2026-05-30"
  },
  "selected_skeleton_id": "plan_a",
  "selected_skeleton_days": 7,
  "daily_plans_count": 7,
  "daily_days": [1, 2, 3, 4, 5, 6, 7]
}
```

前端也显示：

```text
append_day_plan
追加一天行程
成功
旅行计划已更新
每日行程
7 天
```

这证明原来的核心错误：

```text
day 超出行程总天数 6: 7
```

已不再复现。`DateRange.total_days` / 骨架天数一致性这条主链路已修复。

### 4.2 plan 增量持久化改善

复测结束后，磁盘计划文件 `backend/data/sessions/sess_633cc86ff6c9/plan.json` 已包含 Phase 5 状态和 7 天 `daily_plans`：

```json
{
  "phase": 5,
  "daily_plans_count": 7,
  "daily_days": [1, 2, 3, 4, 5, 6, 7]
}
```

这比上一轮“API 已进入 Phase 5，但磁盘 plan 仍停在 Phase 3”的情况有改善。

---

## 5. 新发现问题一：replace_daily_plans 空参数仍发生

### 5.1 用户可见现象

前端在 Day 1-7 都写入成功后，又显示一次失败工具调用：

```text
replace_daily_plans
整体替换逐日行程
失败
make_replace_daily_plans_tool.<locals>.replace_daily_plans() missing 1 required positional argument: 'days'
请检查 replace_daily_plans 的参数是否完整且类型正确
```

相比修复前，错误提示已有改善：不再显示 `An unexpected error occurred`，而是提示检查参数完整性。

但问题仍然存在：模型仍能发起 `replace_daily_plans({})` 或空参数调用。

### 5.2 Trace 证据

`/api/sessions/sess_633cc86ff6c9/trace` 中 Phase 5 错误工具调用：

```json
[
  {
    "name": "replace_daily_plans",
    "status": "error",
    "arguments_preview": "",
    "result_preview": "ERROR: make_replace_daily_plans_tool.<locals>.replace_daily_plans() missing 1 required positional argument: 'days'"
  }
]
```

这说明 `ToolEngine` 层的 TypeError 结构化修复至少部分生效，但 trace 的 `result_preview` 仍只暴露 Python 函数签名错误，无法直观看到 `INVALID_ARGUMENTS` 这类稳定错误码。

### 5.3 初步判断

当前修复把 TypeError 从 `INTERNAL_ERROR` 改成了更可诊断的 suggestion，但还没有从根上避免缺参调用。

可能缺口有三类：

1. **调用前 schema 校验不足**：工具执行前没有根据 `parameters.required` 拦截缺参，而是等 Python 函数调用抛 TypeError。
2. **工具函数签名不宽容**：`replace_daily_plans(days: list)` 没有默认值，空参数直接进入 Python TypeError，而不是工具自己的 `ToolError`。
3. **trace 可观测性不足**：trace preview 没展示 `error_code` / `suggestion`，调试时仍需要从前端文本或 ToolResult 原始对象推断。

### 5.4 建议修复

优先采用调用前 schema 校验：

```text
ToolEngine.execute()
  -> 读取 tool_def.parameters.required
  -> 对 call.arguments 做缺参检查
  -> 缺参直接返回 ToolResult(status="error", error_code="INVALID_ARGUMENTS")
```

同时增强 trace：

- `tool_calls[]` 中显式包含 `error_code`。
- `tool_calls[]` 中显式包含 `suggestion`。
- `arguments_preview` 为空时显示 `{}`，避免无法区分“空参数”和“未记录参数”。

---

## 6. 新发现问题二：validator 时间冲突大量累积但不阻断

### 6.1 用户可见现象

前端表面上显示 Day 1-7 均已生成，预算也正常累加。但 trace 中每次 `append_day_plan` 都带出大量时间冲突。

典型例子：

```text
Day 1: 上海飞抵成都→午餐：文殊院素斋/附近简餐 时间冲突
上海飞抵成都 14:30 结束，交通需 40min，但 午餐 14:30 开始，间隔仅 0min
```

Day 7 也出现负间隔：

```text
Day 7: 酒店出发前往熊猫基地→上午：熊猫基地看花花 时间冲突
酒店出发前往熊猫基地 08:30 结束，交通需 180min，但 熊猫基地看花花 08:00 开始，间隔仅 -30min
```

### 6.2 Trace 证据

Phase 5 的 7 次 `append_day_plan` 全部是 `success`，但均带有 `validation_errors`。这些错误随着天数增加持续累积：

- Day 1 写入后已有多个 0 分钟衔接冲突。
- Day 2 写入后继续叠加 Day 1 + Day 2 冲突。
- Day 7 写入后包含从 Day 1 到 Day 7 的全部冲突集合。

### 6.3 初步判断

当前 validator 是“诊断型”而不是“控制型”：

- 能识别冲突。
- 能把冲突附加到 trace / tool result。
- 但不会阻断低质量 DayPlan 写入。
- 也不会强制模型先修复已发现冲突再继续追加下一天。

这会导致 Phase 5 表面完成，实际产物质量不可用。

### 6.4 建议修复

需要把严重 validator 错误接入自修复闭环：

1. 设置严重度和阈值：
   - 单日出现负间隔；
   - 单日冲突数量超过阈值；
   - 跨城交通与活动无缓冲。
2. 对严重错误采取控制动作：
   - 阻止该 DayPlan 被视为完成；
   - 或写入后立即注入 system note，要求下一轮先 `replace_daily_plans` 修复已写天数；
   - 或限制继续追加新天数，直到冲突数下降。
3. 在 Phase 5 prompt 中明确：
   - validator 返回冲突后，必须先修复冲突；
   - 不允许继续生成后续天数并累积错误。

---

## 7. 新发现问题三：SQLite session meta 仍 stale

### 7.1 现象

复测结束后，API 和磁盘 plan 都显示 Phase 5 + 7 天行程已写入。

但 SQLite session meta 仍显示：

```text
session_id         title                   phase  status  updated_at                        last_run_status
-----------------  ----------------------  -----  ------  --------------------------------  ---------------
sess_633cc86ff6c9  九寨沟 + 成都 · 6天5晚  3      active  2026-04-17T07:27:13.197095+00:00  completed
```

### 7.2 初步判断

增量 plan 持久化已生效，但 session meta 的更新仍依赖 `_run_agent_stream()` 正常走到尾部。

本次 Playwright 等待到 420 秒后关闭页面，前端仍显示：

```text
连接似乎不稳定，正在等待模型继续响应。
如果长时间没有恢复，可先停止，再重新发送上一条消息。
```

因此 session meta 未刷新到 Phase 5。

### 7.3 影响

- session 列表仍可能显示旧阶段。
- 恢复会话时，数据库 meta 与 plan 文件状态不一致。
- 调试时 `/api/plan`、磁盘 plan、SQLite sessions 三者给出不同答案。

### 7.4 建议修复

在每次 plan writer 成功并产生 `state_update` 后，同步更新 session meta 的关键字段：

- `phase`
- `title`
- `updated_at`
- 可选：`last_run_status = "running"` 或更明确的中间态

或者至少在 `finally` 里无论 SSE 是否被客户端关闭，都基于当前 plan 更新 session meta。

---

## 8. 额外观察：当前相关测试仍非全绿

本轮运行：

```bash
cd backend && source .venv/bin/activate && pytest tests/test_state_models.py tests/test_phase_router.py tests/test_tool_engine.py -q
```

结果：

```text
46 passed, 2 failed
```

失败用例：

- `tests/test_phase_router.py::test_phase1_prompt_encourages_reading_recommendation_posts_and_comments`
- `tests/test_phase_router.py::test_phase3_candidate_prompt_limits_search_and_forbids_search_narration`

这两个失败是 prompt 文案断言，与本次 Phase 5 状态契约复测不是同一问题。但它们意味着当前相关测试集还不是全绿，后续提交前需要处理或更新断言。

---

## 9. 当前事故定性

这次复测说明上一轮修复解决了“状态契约主链路”：

- 日期自然日语义已统一到 7 天；
- 骨架 7 天可以写入 Day 7；
- plan 文件已增量持久化到 Phase 5。

但仍残留三个独立问题：

1. 工具调用参数错误仍可触发，只是错误提示更可诊断。
2. validator 发现的问题没有进入自修复或阻断机制。
3. session meta 与 plan 状态仍可能在长流式中断时不一致。

因此本次不是“修复失败”，而是“主根因修复后暴露出下一层质量与可观测性问题”。

---

## 10. 建议下一步任务拆分

### Task A：工具参数 schema 预校验

- 在 `ToolEngine.execute()` 调用工具前根据 `tool_def.parameters.required` 校验参数。
- 缺参返回 `INVALID_ARGUMENTS`。
- 测试覆盖 `replace_daily_plans({})`。

### Task B：Trace 暴露完整错误结构

- 在 `/api/sessions/{id}/trace` 中增加：
  - `error_code`
  - `suggestion`
  - 原始 `arguments` 或更清晰的空参数标记

### Task C：validator 严重错误闭环

- 定义严重时间冲突阈值。
- 严重冲突时阻止继续追加，或强制下一轮先修复。
- 增加 Phase 5 集成测试：带 0 分钟衔接的 DayPlan 不应被视为完成。

### Task D：session meta 增量更新

- plan writer 成功后同步更新 `sessions.phase` 和 `updated_at`。
- 覆盖 SSE 中断场景：plan 文件和 SQLite meta 应保持一致。

---

## 11. 验收标准

后续修复完成后，使用同一复测路径应满足：

1. Trace 中无 `replace_daily_plans` 空参数错误。
2. Trace 中无 `INTERNAL_ERROR` 工具结果。
3. `append_day_plan` 能写入 Day 1-7。
4. 严重 validator 时间冲突不再累计到最终可见计划中。
5. `/api/plan`、`backend/data/sessions/<id>/plan.json`、`backend/data/sessions.db.sessions.phase` 三者一致。
6. 相关后端测试全绿。

---

## 12. 修复记录

**修复分支**：`fix/phase5-residual-issues`

| Task | 修复内容 | 提交 |
|------|---------|------|
| Task 1 | ToolEngine 增加 required 参数预校验，拦截空参数调用 | `ab9e9e2` |
| Task 2 | validator 新增 `validate_day_conflicts` 按天过滤时间冲突 | `c16883a` |
| Task 3 | daily_plans 工具写入后返回时间冲突检测结果（`conflicts` + `has_severe_conflicts`） | `2af229b` |
| Task 4 | Phase 5 prompt 增加时间冲突处理指令，引导模型修复冲突 | `5b9ded5` |
| Task 5 | trace 暴露 `error_code` 和 `suggestion` 字段 | `b31f94f` |
| Task 6 | plan writer 成功后同步更新 session meta + finally 保底保存增加日志 | `4591666` |
| Task 7 | 更新文档和 PROJECT_OVERVIEW | — |

**问题 A（空参数调用）**：Task 1 在 ToolEngine.execute() 中插入 required 预校验，缺参直接返回 INVALID_ARGUMENTS，不再走 Python TypeError 路径。

**问题 B（时间冲突累积不阻断）**：Task 2+3 让 daily_plans 工具写入后即时检测冲突并在返回 dict 中告知模型；Task 4 在 prompt 中增加 `has_severe_conflicts=true` 时的修复指令。

**问题 C（session meta stale）**：Task 6 在增量持久化路径同步更新 session meta；finally 块改为 logger.warning 以保留诊断信息。
