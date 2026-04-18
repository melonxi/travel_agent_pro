# Phase 5 清晰计划工具重设计

日期：2026-04-18
关联指南：`docs/agent-tool-design-guide.md`

---

## 1. 背景

Phase 5 的实际任务是把已选骨架展开成覆盖全部出行日期的可执行 `daily_plans`。Prompt 中的工作流是：

1. expand：把骨架主题映射到具体日期
2. assemble：补齐 POI 和单日路线顺序
3. validate：验证移动成本、开放性、天气和真实体验
4. commit：把逐日行程写入状态

当前三个核心工具存在 ACI 层面的不清晰：

| 当前工具 | 问题 |
|---|---|
| `assemble_day_plan` | 名字像写计划，实际只做 POI 排序；还被标记为 `side_effect="write"`，会误导模型以为已经写入状态。 |
| `append_day_plan` | 只能追加新 day，不能修正已有 day；当用户要求修改某天时，模型只能转向全量替换。 |
| `replace_daily_plans` | 同时承担“全量重排”和“局部修正”两类职责，调用成本高且容易误删已有天数。 |

本设计以 Phase 5 的实际任务为锚点，按 `agent-tool-design-guide.md` 的原则重新设计工具名、职责、schema、返回值和错误反馈。

---

## 2. 目标

1. 让模型只读工具定义也能判断该调用哪个工具。
2. 把“路线辅助”和“状态写入”分开，避免 helper 工具伪装成 writer。
3. 增加单日修正能力，降低 `replace_daily_plans` 的误用概率。
4. 保留写入后冲突检测闭环，让模型能根据 `has_severe_conflicts` 自修复。
5. 保持 Phase 5 暴露工具数不增加到危险区间，继续控制在 10-20 个以内。

非目标：

1. 不重构 Phase 3 工具。
2. 不引入新的外部路线规划服务。
3. 不改变 `DayPlan` / `Activity` 的核心数据模型。
4. 不解决全量 baseline 中与 Phase 5 无关的既有失败。

---

## 3. 选择的方案

采用“清晰三工具版”：

| 新工具 | 替代/吸收 | 职责 |
|---|---|---|
| `optimize_day_route` | 替代 `assemble_day_plan` | 只做单日路线排序和移动估算，不写 `plan`。 |
| `save_day_plan` | 替代 `append_day_plan`，吸收单日修改能力 | 写入一天行程；通过 `mode` 明确创建还是替换已有 day。 |
| `replace_all_day_plans` | 替代 `replace_daily_plans` | 只用于完整覆盖全部逐日行程。 |

### 3.1 为什么不选最小改名

只把 `assemble_day_plan` 改成 `optimize_day_route` 能解决“helper 伪装 writer”的问题，但不能解决“修改某一天必须全量替换”的问题。Phase 5 的高频场景包括用户临时调整某天、时间冲突修某天、天气风险替换某天活动；缺少单日替换工具会继续逼模型使用大锤。

### 3.2 为什么不拆成 create/update 两个单日工具

`create_day_plan` + `update_day_plan` 的边界很清楚，但会把 Phase 5 写工具从 2 个增加到 3 个，再加上全量工具就是 4 个。`save_day_plan(mode=...)` 用 enum 把非法意图显式化，既保留清晰边界，也减少工具选择负担。

---

## 4. 工具契约

### 4.1 `optimize_day_route`

用途：对单日候选 POI 做路线排序和粗略移动成本估算。它是路线辅助工具，不写入 `daily_plans`。

何时使用：

- 一天内有 2 个及以上 POI，需要按地理临近度排序。
- 骨架只给出区域/主题，需要先把候选活动排成合理顺序。
- 写入前想快速估算当天是否过密。

何时不用：

- 已经有明确顺序且只需要保存状态时，直接用 `save_day_plan`。
- 需要验证两点间真实交通路线时，用 `calculate_route`。
- 需要查询 POI 坐标或价格时，用 `get_poi_info`。

入参：

```json
{
  "pois": [
    {
      "name": "明治神宫",
      "lat": 35.6764,
      "lng": 139.6993,
      "duration_hours": 1.5
    }
  ],
  "start_location": {"name": "新宿酒店", "lat": 35.6938, "lng": 139.7034},
  "end_location": {"name": "新宿酒店", "lat": 35.6938, "lng": 139.7034},
  "day_start_time": "09:00",
  "day_end_time": "21:00",
  "transport_mode": "transit"
}
```

字段规则：

- `pois` 必填，至少 2 个元素才有排序意义；1 个元素可返回原样和提示。
- `lat/lng` 必须是数字。
- `duration_hours` 是小时数，缺省时按 1.0 小时估算。
- `transport_mode` enum：`walking`、`transit`、`driving`，默认 `transit`。
- `start_location` / `end_location` 可选；存在时纳入估算，但返回的 `ordered_pois` 只包含 POI。

出参：

```json
{
  "ordered_pois": [...],
  "estimated_total_distance_km": 8.4,
  "estimated_travel_minutes": 126,
  "estimated_activity_minutes": 270,
  "estimated_total_minutes": 396,
  "can_fit_in_day": true,
  "warnings": [],
  "next_action": "Use save_day_plan to persist the selected schedule. This tool did not write daily_plans."
}
```

错误和边界：

- 缺少坐标时返回 `INVALID_VALUE`，建议先用 `get_poi_info`。
- POI 少于 2 个时不报错，返回原顺序和 warning。
- 工具 `side_effect="read"`，允许与其他读工具并行。

实现：

- 从 `backend/tools/assemble_day_plan.py` 演进或新增 `backend/tools/optimize_day_route.py`。
- 保留当前 Haversine + nearest-neighbor 近似算法。
- `assemble_day_plan` 不再暴露给 Phase 5；如保留兼容 wrapper，`phases=[]` 或只供旧测试直接构造。

### 4.2 `save_day_plan`

用途：写入或替换单个 DayPlan，是 Phase 5 默认的状态写入工具。

何时使用：

- 生成新的一天行程：`mode="create"`。
- 用户要求修改已有某天：`mode="replace_existing"`。
- 写入工具返回严重冲突后，修复同一天：`mode="replace_existing"`。

何时不用：

- 需要一次性重排所有天，使用 `replace_all_day_plans`。
- 只想排序 POI，使用 `optimize_day_route`。

入参：

```json
{
  "mode": "create",
  "day": 1,
  "date": "2026-05-01",
  "notes": "上午原宿，下午涩谷，晚上回新宿",
  "activities": [
    {
      "name": "明治神宫",
      "location": {"name": "明治神宫", "lat": 35.6764, "lng": 139.6993},
      "start_time": "09:30",
      "end_time": "11:00",
      "category": "shrine",
      "cost": 0,
      "transport_from_prev": "从酒店乘地铁",
      "transport_duration_min": 25,
      "notes": ""
    }
  ]
}
```

字段规则：

- `mode` 必填 enum：`create`、`replace_existing`。
- `day` 必须是 `1..plan.dates.total_days`。
- `date` 必须是 `YYYY-MM-DD`。
- `activities` 必须是 list，每个 activity 至少包含 `name`、`location`、`start_time`、`end_time`、`category`、`cost`。
- `location` 必须包含 `name`、`lat`、`lng`。
- `start_time` / `end_time` 必须是 `HH:MM`。
- `cost` 必须是数字。

写入规则：

- `mode="create"` 且 day 已存在：返回 `DAY_ALREADY_EXISTS`，建议改用 `mode="replace_existing"`。
- `mode="replace_existing"` 且 day 不存在：返回 `DAY_NOT_FOUND`，建议改用 `mode="create"`。
- 成功后按 day 排序 `plan.daily_plans`，避免前端顺序被调用顺序污染。

出参：

```json
{
  "updated_field": "daily_plans",
  "action": "create",
  "day": 1,
  "date": "2026-05-01",
  "activity_count": 4,
  "covered_days": [1],
  "missing_days": [2, 3, 4, 5],
  "total_days": 1,
  "previous_days": 0,
  "conflicts": [],
  "has_severe_conflicts": false
}
```

错误和边界：

- 重复创建、替换不存在 day 都是模型可修复错误，不应变成 `INTERNAL_ERROR`。
- 写入后继续调用 `validate_day_conflicts(plan, [day])`。
- 如果返回 `has_severe_conflicts=true`，prompt 要求下一步必须用 `save_day_plan(mode="replace_existing")` 修同一天。

实现：

- 在 `backend/tools/plan_tools/daily_plans.py` 中新增 `make_save_day_plan_tool`。
- 复用现有 `_validate_day`、`_validate_day_in_range`、`_validate_date_format`、`_validate_activities`。
- 在 `state.plan_writers` 新增或复用单日替换 helper。

### 4.3 `replace_all_day_plans`

用途：整体替换所有逐日行程。只用于完整重排、严重全局冲突修复，或用户明确要求一次性完整版。

何时使用：

- 用户要求“直接给完整版”。
- 已有多天安排都需要被同一套新逻辑重排。
- 需要修复跨多天的全局结构问题。

何时不用：

- 只新增一天，用 `save_day_plan(mode="create")`。
- 只修改一天，用 `save_day_plan(mode="replace_existing")`。

入参：

```json
{
  "days": [
    {
      "day": 1,
      "date": "2026-05-01",
      "notes": "可选说明",
      "activities": []
    }
  ]
}
```

字段规则：

- `days` 必须是完整逐日行程列表。
- 如果 `plan.dates.total_days` 存在，`days` 必须覆盖完整 `1..total_days`。
- 不允许重复 day、缺失 day、超出 day 范围。
- 每天的 activity schema 与 `save_day_plan` 相同。

出参：

```json
{
  "updated_field": "daily_plans",
  "action": "replace_all",
  "total_days": 5,
  "previous_days": 3,
  "covered_days": [1, 2, 3, 4, 5],
  "missing_days": [],
  "conflicts": [],
  "has_severe_conflicts": false
}
```

错误和边界：

- 如果 `days` 未覆盖完整行程，返回 `INCOMPLETE_DAILY_PLANS`，明确列出 `missing_days`。
- 如果有超限 day，返回 `INVALID_VALUE`，提示合法范围。
- 写入后对所有传入 day 调用 `validate_day_conflicts`。

实现：

- 在 `backend/tools/plan_tools/daily_plans.py` 中新增 `make_replace_all_day_plans_tool`。
- 可复用现有 `replace_all_daily_plans` writer。
- 原 `replace_daily_plans` 不再暴露给 Phase 5；如保留兼容 wrapper，`phases=[]` 或只供迁移测试使用。

---

## 5. Prompt 变更

`PHASE5_PROMPT` 的工具契约和状态写入契约改成：

1. `optimize_day_route`：单日路线排序辅助，不写状态。
2. `save_day_plan(mode="create", ...)`：默认每完成一天就保存。
3. `save_day_plan(mode="replace_existing", ...)`：修改已有某天或修复严重冲突。
4. `replace_all_day_plans(days=[...])`：只用于完整重排或用户明确要求一次性完整版。
5. 任一写入工具返回 `has_severe_conflicts=true` 时，必须先修复冲突天数，不能继续追加后续天数。

压力场景同步更新：

- 用户要求修改第 3 天时，正确动作改为 `save_day_plan(mode="replace_existing", day=3, ...)`。
- 部分天数已存在时，正确动作改为用 `save_day_plan(mode="create")` 只补缺失天。
- 全量重排才使用 `replace_all_day_plans`。

---

## 6. 兼容与迁移

清晰优先，因此 Phase 5 对模型只暴露新三工具：

- `optimize_day_route`
- `save_day_plan`
- `replace_all_day_plans`

旧工具处理：

| 旧工具 | 处理 |
|---|---|
| `assemble_day_plan` | 从 Phase 5 prompt 和 Phase 5 工具暴露中移除；保留旧工厂用于测试或过渡时，`side_effect` 改为 `read`，或新增 alias 时 `phases=[]`。 |
| `append_day_plan` | 不再注册为 Phase 5 plan writer；测试迁移到 `save_day_plan(mode="create")`。 |
| `replace_daily_plans` | 不再注册为 Phase 5 plan writer；测试迁移到 `replace_all_day_plans` 或 `save_day_plan(mode="replace_existing")`。 |

如果实现中必须短期保留旧工具名，旧工具描述必须明确“legacy，不给模型使用”，并且不得出现在 `ToolEngine.get_tools_for_phase(5)` 返回值中。

---

## 7. 验证计划

### 7.1 单元测试

新增或迁移 `backend/tests/test_plan_tools/test_daily_plans.py`：

- `test_save_day_plan_create_adds_day`
- `test_save_day_plan_create_rejects_existing_day`
- `test_save_day_plan_replace_existing_updates_only_that_day`
- `test_save_day_plan_replace_existing_rejects_missing_day`
- `test_save_day_plan_returns_missing_days`
- `test_save_day_plan_returns_conflicts`
- `test_replace_all_day_plans_requires_complete_coverage`
- `test_replace_all_day_plans_rejects_duplicate_days`
- `test_replace_all_day_plans_rejects_out_of_range_day`
- `test_replace_all_day_plans_returns_conflicts`

新增或迁移路线工具测试：

- `test_optimize_day_route_orders_pois`
- `test_optimize_day_route_is_read_side_effect`
- `test_optimize_day_route_warns_for_single_poi`
- `test_optimize_day_route_requires_coordinates`

### 7.2 工具暴露测试

更新 `backend/tests/test_phase_router.py` / `backend/tests/test_tool_engine.py` / `backend/tests/test_prompt_architecture.py`：

- Phase 5 tools 包含 `optimize_day_route`、`save_day_plan`、`replace_all_day_plans`。
- Phase 5 tools 不包含 `assemble_day_plan`、`append_day_plan`、`replace_daily_plans`。
- `PHASE5_PROMPT` 不再提旧工具名。
- prompt 明确 `optimize_day_route` 不写状态。

### 7.3 Agent 行为回归

更新 Phase 5 state repair 和 reflection 相关测试：

- 当模型输出逐日行程但未写状态时，repair hint 指向 `save_day_plan` / `replace_all_day_plans`。
- 当已有 Day 1-3 且还差 Day 4-5 时，模型应使用 `save_day_plan(mode="create")` 补缺失天。
- 当用户修改某天时，模型应使用 `save_day_plan(mode="replace_existing")`。

### 7.4 Baseline 说明

当前独立 worktree baseline：

- `frontend npm run build` 通过。
- `backend pytest` 为 `1125 passed, 6 failed`，失败集中在非 Phase 5 工具路径。

本实现完成后，至少运行：

- `cd backend && . .venv/bin/activate && pytest tests/test_plan_tools/test_daily_plans.py tests/test_assemble_day_plan.py tests/test_tool_engine.py tests/test_prompt_architecture.py tests/test_agent_loop.py`
- `cd backend && . .venv/bin/activate && pytest`
- `cd frontend && npm run build`

全量 pytest 的 6 个既有失败作为 baseline 风险单独报告，除非本次改动影响其数量或失败类型。

---

## 8. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 旧测试大量引用旧工具名 | 先在测试中建立新工具期望，再迁移实现；必要时保留 legacy 工厂但不暴露。 |
| `PLAN_WRITER_TOOL_NAMES` 变更遗漏导致写后验证不触发 | 新写工具必须加入 `PLAN_WRITER_TOOL_NAMES`，旧写工具移除或不再注册；用 hook/telemetry 测试覆盖。 |
| Prompt 仍提旧工具名导致模型误用 | `test_prompt_architecture` 加负向断言。 |
| 全量替换要求完整覆盖过严 | Phase 5 仍保留 `save_day_plan` 做增量生成；全量工具只在完整场景使用。 |
| `optimize_day_route` 估算过粗 | 返回字段命名用 `estimated_*`，prompt 要求关键跨区路线仍用 `calculate_route` 验证。 |

---

## 9. 成功标准

1. Phase 5 暴露的新三工具职责清晰，旧三工具不再暴露给模型。
2. 单日新增、单日替换、全量替换三个动作都有直接工具路径。
3. 路线优化工具不再被误判为状态写入工具。
4. 写入后时间冲突反馈保持可用。
5. 相关 Phase 5 工具、prompt、agent repair 测试通过。
