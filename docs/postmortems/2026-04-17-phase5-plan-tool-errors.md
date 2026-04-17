# Phase 5 计划工具错误事故复盘

- 事故日期：2026-04-17
- 事故范围：`backend/phase/prompts.py`、`backend/context/manager.py`、`backend/phase/router.py`、`backend/state/models.py`、`backend/tools/engine.py`、`backend/tools/plan_tools/daily_plans.py`
- 事故类型：阶段状态不一致 + 工具参数错误不可诊断 + 长流式运行未完成持久化
- 事故等级：中
- 事故状态：已修复（A/B/C 优先级项全部完成，分支 fix/phase5-state-contract）

---

## 1. 事故摘要

在一次九寨沟 + 成都行程规划中，用户确认交通和住宿后，系统从 Phase 3 进入 Phase 5（日程详排）。进入 Phase 5 后，模型开始调用计划写入工具生成 `daily_plans`，但真实前端流中出现大量计划工具错误：

1. `replace_daily_plans` 被空参数调用，缺少必填参数 `days`。
2. `append_day_plan` 多次尝试写入 `day=7`，但当前 `plan.dates.total_days` 为 6，工具拒绝写入。
3. 成功写入的 DayPlan 又触发大量时间冲突校验错误，主要表现为活动结束时间与下一段交通开始时间无缓冲。

本次事故不是单一工具实现错误，而是三类问题叠加：

1. 当前计划的日期总天数与已选骨架天数不一致。
2. Phase 5 prompt 和运行时上下文同时向模型暴露了互相冲突的天数信号。
3. 工具执行层把缺参 `TypeError` 包装成泛化 `INTERNAL_ERROR`，模型无法从错误中恢复。

---

## 2. 用户可见现象

问题 session：`sess_633cc86ff6c9`

用户在 Phase 3 lock 阶段已经选择：

```text
我选200元档春秋航空往返；成都文殊院选成都蓉城假日酒店，九寨沟沟口选九寨沟兰朵酒店，成都春熙路选成都雅熙酒店公寓。请锁定这些选择并进入日程详排。
```

前端随后显示阶段推进到“行程组装”，并出现以下工具事件：

```text
replace_daily_plans
整体替换逐日行程
失败
make_replace_daily_plans_tool.<locals>.replace_daily_plans() missing 1 required positional argument: 'days'
An unexpected error occurred
```

随后又出现：

```text
append_day_plan
追加一天行程
失败
day 超出行程总天数 6: 7
day 应介于 1 到 6 之间
```

部分 `append_day_plan` 调用成功后，前端继续显示行程逐日增加，但 trace 中出现大量 validator 错误，例如：

```text
Day 1: 上海飞往成都→前往酒店办理入住 时间冲突
上海飞往成都 14:30 结束，交通需 60min，但 前往酒店办理入住 14:30 开始，间隔仅 0min
```

---

## 3. 影响评估

### 3.1 用户影响

- 用户看到大量底层工具失败，感知为系统不稳定。
- Phase 5 虽然部分生成了行程，但用户无法判断哪些内容可信。
- “连接似乎不稳定”的提示与工具错误交织出现，会误导用户以为只是网络问题。

### 3.2 系统影响

- Phase 5 的核心产物 `daily_plans` 处于半成品状态，只生成到 Day 4。
- 内存状态与磁盘持久化状态可能不一致：API 中已进入 Phase 5，但 SQLite session meta 和磁盘快照仍可能停在 Phase 3。
- trace 中出现大量工具错误和 validator 错误，降低后续调试信噪比。

---

## 4. 复现过程

### 4.1 环境

- 前端：`http://127.0.0.1:5173`
- 后端：`http://127.0.0.1:8000`
- 数据库：`backend/data/sessions.db`
- 问题 session：`sess_633cc86ff6c9`

Chrome 的 Computer Use 权限被系统拒绝，因此本次使用本地 Playwright 脚本驱动前端。Playwright MCP 自身也因尝试创建 `/.playwright-mcp` 失败不可用，所以改为 Node.js 脚本调用项目依赖中的 Playwright。

### 4.2 操作路径

1. 打开前端首页。
2. 从 session 列表进入 `九寨沟 + 成都 · 6天5晚`。
3. 确认当前停在 Phase 3 lock 的交通住宿选择点。
4. 在输入框发送交通和住宿选择：

```text
我选200元档春秋航空往返；成都文殊院选成都蓉城假日酒店，九寨沟沟口选九寨沟兰朵酒店，成都春熙路选成都雅熙酒店公寓。请锁定这些选择并进入日程详排。
```

5. 等待 SSE 流式运行，观察 Phase 5 工具调用和前端状态。

### 4.3 截图证据

截图均按项目规范保存在 `screenshots/`：

- `screenshots/phase5-debug-jiuzhaigou-selected.png`：选中九寨沟 session 后的初始状态。
- `screenshots/phase5-debug-before-lock-send.png`：发送交通住宿确认前。
- `screenshots/phase5-debug-lock-streaming.png`：锁定后流式执行中。
- `screenshots/phase5-debug-after-lock.png`：Phase 5 工具错误出现后的页面状态。

---

## 5. 关键证据

### 5.1 当前计划状态

从 `/api/plan/sess_633cc86ff6c9` 读取到：

```json
{
  "phase": 5,
  "phase3_step": "lock",
  "dates": {
    "start": "2026-05-24",
    "end": "2026-05-30"
  },
  "selected_skeleton_id": "plan_a",
  "daily_plans_count": 4,
  "daily_days": [1, 2, 3, 4]
}
```

已选骨架 `plan_a` 实际包含 7 天：

```json
{
  "selected_skeleton_id": "plan_a",
  "selected_skeleton_days": 7
}
```

### 5.2 dates 总天数计算

`backend/state/models.py` 中 `DateRange.total_days` 当前实现：

```python
@property
def total_days(self) -> int:
    from datetime import date as dt_date

    s = dt_date.fromisoformat(self.start)
    e = dt_date.fromisoformat(self.end)
    return (e - s).days
```

因此 `2026-05-24` 到 `2026-05-30` 被计算为 6 天。  
但用户和 UI 文案中“6天5晚”通常是包含出发日、不包含返程后的过夜数；同一数据又被骨架生成成 7 个 `day`，形成运行时冲突。

### 5.3 Day 超限由工具正确拒绝

`backend/tools/plan_tools/daily_plans.py` 中：

```python
def _validate_day_in_range(plan: TravelPlanState, day: int, field_name: str) -> None:
    if plan.dates is not None and day > plan.dates.total_days:
        raise ToolError(
            f"{field_name} 超出行程总天数 {plan.dates.total_days}: {day}",
            error_code="INVALID_VALUE",
            suggestion=f"day 应介于 1 到 {plan.dates.total_days} 之间",
        )
```

所以 `append_day_plan(day=7, ...)` 报错不是工具误判，而是计划状态里的权威天数为 6。

### 5.4 `replace_daily_plans` 缺参变成不可诊断错误

`replace_daily_plans` 的函数签名：

```python
async def replace_daily_plans(days: list) -> dict:
```

当模型空参数调用时，Python 在进入函数体前抛出 `TypeError`，没有机会走函数内的 `ToolError` 结构化校验。

`backend/tools/engine.py` 捕获通用异常后返回：

```python
return ToolResult(
    tool_call_id=call.id,
    status="error",
    error=str(error),
    error_code="INTERNAL_ERROR",
    suggestion="An unexpected error occurred",
)
```

前端最终看到：

```text
missing 1 required positional argument: 'days'
An unexpected error occurred
```

这类错误对模型不可操作，不能明确指导它补 `{"days": [...]}`。

### 5.5 Phase 5 上下文同时注入冲突信号

`backend/context/manager.py` 在 Phase 5+ 会注入：

- `plan.dates`：日期和 `plan.dates.total_days`
- `trip_brief`：历史画像字段
- 已选骨架完整内容

本次状态中存在：

```json
"dates": {
  "start": "2026-05-24",
  "end": "2026-05-30"
}
```

同时 `trip_brief` 中残留：

```json
"dates": {
  "start": "2026-05-10",
  "end": "2026-05-16"
},
"total_days": 6
```

而已选骨架 `plan_a` 又包含 Day 1 到 Day 7。

这意味着模型在 Phase 5 看到至少三组不完全一致的时序信号：

1. 当前 `plan.dates`：`2026-05-24` 到 `2026-05-30`。
2. `trip_brief.dates`：`2026-05-10` 到 `2026-05-16`。
3. 已选骨架：7 天。

---

## 6. 事故时间线

1. 用户先前在 Phase 3 生成并选择 `plan_a`。
2. `plan_a` 骨架包含 7 个 `days`。
3. 当前计划 dates 被更新为 `2026-05-24` 到 `2026-05-30`。
4. `DateRange.total_days` 按 `(end - start).days` 返回 6。
5. 用户确认交通和住宿，Phase 3 lock 条件满足。
6. 系统进入 Phase 5。
7. Phase 5 prompt 要求“严格基于 selected_skeleton_id 对应的骨架展开”。
8. 运行时上下文注入完整 7 天骨架。
9. 模型开始按骨架写 DayPlan。
10. Day 1-6 在部分历史尝试中可写入，Day 7 被工具拒绝。
11. 模型又尝试 `replace_daily_plans` 修复，但空参数调用导致 `INTERNAL_ERROR`。
12. 长时间流式运行后前端显示连接不稳定，当前内存状态与磁盘持久化状态出现差异。

---

## 7. 根因分析

### 7.1 直接根因

Phase 5 的权威天数来自 `plan.dates.total_days == 6`，而模型展开依据的已选骨架包含 7 天。模型按 7 天骨架生成 Day 7 时，被 `append_day_plan` 的天数校验拒绝。

### 7.2 上游诱因

Phase 3 没有在进入 Phase 5 前校验：

```text
selected_skeleton.days.length == plan.dates.total_days
```

因此“6 天状态 + 7 天骨架”的不一致状态可以合法进入 Phase 5。

### 7.3 放大因素一：日期语义不清

`DateRange.total_days` 当前使用差值天数，不包含结束日期。对旅行产品来说，用户表达的“5/24 到 5/30”通常会被自然理解为 7 个自然日，或至少需要明确“6晚7天 / 6天5晚”的语义。

当前系统没有在状态层区分：

- 行程自然日数量
- 住宿晚数
- 返程日是否计入 DayPlan

### 7.4 放大因素二：trip_brief 残留旧日期

`trip_brief` 中仍保留旧日期 `2026-05-10` 到 `2026-05-16` 和 `total_days: 6`。虽然权威状态是 `plan.dates`，但 Phase 5 上下文会完整注入 `trip_brief`，增加模型混淆概率。

### 7.5 放大因素三：缺参错误不可恢复

空参数调用 `replace_daily_plans` 时，工具函数还没进入自定义校验，直接抛 `TypeError`。`ToolEngine` 把它包装成泛化 `INTERNAL_ERROR`，模型无法知道应该如何修正参数。

### 7.6 放大因素四：validator 只提示，不阻断

DayPlan 写入后，实时 validator 能发现时间冲突，但当前表现为附加诊断，不会阻止错误质量的 `daily_plans` 写入。模型可以继续基于已有冲突计划往后生成，导致冲突累积。

---

## 8. 为什么这不是单纯的模型问题

模型确实产生了错误工具调用，但系统给它的输入本身存在冲突：

- Phase 5 prompt 要求严格基于已选骨架。
- 已选骨架有 Day 1-7。
- 工具层只允许 Day 1-6。
- `trip_brief` 又包含旧日期和旧总天数。

在这种上下文下，模型无论遵循“骨架”还是遵循“工具报错”，都可能与另一部分系统状态冲突。

所以本次应定性为状态契约问题，而不是单纯“模型不听话”。

---

## 9. 当前遗留状态

本次复现结束时，前端显示：

- Phase 已进入“行程组装”。
- `daily_plans` 已写入 Day 1-4。
- 仍显示连接不稳定提示。

API 返回的当前内存状态：

```json
{
  "phase": 5,
  "daily_plans_count": 4,
  "daily_days": [1, 2, 3, 4]
}
```

但 SQLite session meta 仍显示：

```text
phase = 3
last_run_status = completed
last_run_error = null
```

磁盘 `backend/data/sessions/sess_633cc86ff6c9/plan.json` 也仍停留在 Phase 3 快照。  
推测原因是本次 Playwright 脚本等待 180 秒后关闭页面，SSE 流未自然收尾，导致 `_run_agent_stream()` 末尾的持久化路径没有完整执行。

这会造成调试时“前端/API 看起来是 Phase 5，但数据库/文件看起来是 Phase 3”的错觉。

---

## 10. 矫正与预防动作

### 10.1 修复优先级 A：统一行程天数语义

需要明确系统内的权威概念：

- 如果 `dates.start` 到 `dates.end` 表示覆盖自然日，应将 `DateRange.total_days` 改为 inclusive：

```python
return (e - s).days + 1
```

- 如果 `dates.end` 表示离开后的非行程日，则必须在 UI、prompt、工具 schema 中明确说明，并禁止骨架包含返程日。

对旅行规划系统，更推荐使用 inclusive 自然日语义，并额外建模住宿晚数。

### 10.2 修复优先级 A：Phase 3 -> Phase 5 gate 校验骨架天数

在进入 Phase 5 前增加硬校验：

```text
selected_skeleton.days.length == plan.dates.total_days
```

不一致时不要进入 Phase 5，应要求模型在 Phase 3 修正：

- 调整骨架天数；
- 或修正日期；
- 或明确向用户确认“到底是 6 天还是 7 天”。

### 10.3 修复优先级 B：清理 trip_brief 中的派生日期字段

`trip_brief.dates`、`trip_brief.total_days` 与 `plan.dates` 重复且容易 stale。建议：

- Phase 5 上下文中不要注入 `trip_brief.dates` / `trip_brief.total_days`。
- 或在 `PhaseRouter._hydrate_phase3_brief()` 中强制用权威 `plan.dates` 覆盖，而不是 `setdefault`。

当前 `setdefault` 会保留旧值，无法修正 stale brief。

### 10.4 修复优先级 B：工具缺参错误结构化

在 `ToolEngine.execute()` 调用工具前做 schema required 参数校验，或在捕获 `TypeError` 时转换为更具体的错误：

```text
error_code = "INVALID_ARGUMENTS"
suggestion = "replace_daily_plans 必须传入 {'days': [...]}，days 是完整逐日行程列表"
```

目标是让模型能从错误中恢复，而不是看到泛化的 `An unexpected error occurred`。

### 10.5 修复优先级 C：validator 错误进入自修复闭环

当前 validator 能发现时间冲突，但没有阻断或触发强制修复。建议：

- 对严重时间冲突设置阈值。
- 同一天冲突超过阈值时，不立即接受为“有效 daily_plan”。
- 或在下一轮 system note 中明确要求先修复冲突，再继续后续天数。

### 10.6 修复优先级 C：长流式运行的持久化保障

如果 SSE 连接中断或前端关闭，当前内存状态可能先变化但未落盘。建议：

- 每次写工具成功并产生 `state_update` 后立即保存 plan 快照。
- 或在 `finally` 中区分 client disconnect，确保已写入状态落盘。

---

## 11. 建议验证用例

### 11.1 单元测试：DateRange 语义

覆盖：

```text
start=2026-05-24, end=2026-05-30
```

明确断言期望总天数。修复前应先确定产品语义。

### 11.2 集成测试：骨架天数与日期不一致时不得进 Phase 5

构造：

- `plan.dates.total_days == 6`
- `selected_skeleton.days.length == 7`
- `selected_skeleton_id` 已设置
- `accommodation` 已设置

预期：

- `PhaseRouter.infer_phase(plan)` 不应返回 5；
- 或 gate 阻止 transition，并给出明确修复提示。

### 11.3 工具测试：缺参返回 INVALID_ARGUMENTS

构造空参数调用：

```python
ToolCall(name="replace_daily_plans", arguments={})
```

预期：

```json
{
  "status": "error",
  "error_code": "INVALID_ARGUMENTS",
  "suggestion": "replace_daily_plans 必须传入 days"
}
```

### 11.4 E2E 测试：Phase 5 不写超限 day

使用九寨沟 + 成都场景，从 Phase 3 lock 推进到 Phase 5，断言：

- 不出现 `append_day_plan(day=7)` 超限错误；
- `daily_plans` 覆盖权威总天数；
- 前端 Trace 中无 `INTERNAL_ERROR` 工具结果。

---

## 12. 事故定性

这是一次“状态契约不一致”事故。

Phase 5 的工具层是按权威状态执行的，模型也是按上下文里完整骨架执行的；真正的问题是系统允许这两个权威来源发生冲突，并且没有在阶段边界阻断。

后续修复重点不应只放在 prompt 上。Prompt 可以降低概率，但无法保证状态一致性。更可靠的修复应落在：

1. 状态模型的天数语义；
2. 阶段转换 gate；
3. 工具参数 schema 预校验；
4. 写入后持久化保障。
