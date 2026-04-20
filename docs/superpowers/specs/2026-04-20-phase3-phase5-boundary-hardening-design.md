# Phase 3/Phase 5 边界强化设计

> 日期：2026-04-20
> 背景：并行 Phase 5 质量保障链路断裂——缺少全局校验、跨天排他约束和骨架合同结构，导致重复日程和交通时间问题。

---

## 1. 问题陈述

并行 Phase 5 的串行-并行质量鸿沟由四层问题叠加造成：

1. **Orchestrator 校验弱**：`_global_validate` 只做 POI 精确去重/预算/天数覆盖，缺少时间冲突、首尾日衔接、语义去重
2. **Worker 无跨天信息**：每个 Worker 只看到自己的 skeleton slice，不知道其他天占用了哪些 POI
3. **Phase 3 骨架松散**：schema 仅要求 `id` + `name`，`days` 为任意对象数组，不表达 POI 归属和排他
4. **校验失败无闭环**：validation issue 只 log warning，不阻止写入也不触发重试

## 2. 设计方案：混合模式

采用 Phase 3 + Orchestrator 分层协作：

- **Phase 3** 负责人类决策类字段：`locked_pois`（排他锚点）、`area_cluster`（区域）、`candidate_pois`（候选池）
- **Orchestrator** 负责推导类字段：`forbidden_pois`（从全局归属表反推）、`mobility_envelope`（从区域和 pace 计算）
- **Worker** 在严格约束下执行单日展开

---

## 3. 模块 A：Orchestrator `_global_validate` 增强

### 3.1 新增校验项

在现有 3 项（POI 精确去重、预算、天数覆盖）基础上新增 4 项：

| # | 校验项 | 逻辑 | severity |
|---|--------|------|----------|
| 4 | 时间冲突 | 同天内 `prev.end_time + transport_duration_min > curr.start_time` | error |
| 5 | 首尾日大交通衔接 | Day 1 首活动 start ≥ 到达时间 + 120min buffer；末日末活动 end ≤ 出发时间 - 180min buffer | error |
| 6 | 语义近似 POI 去重 | 坐标距离 < 200m AND（名称子串包含 OR 编辑距离 ≤ 2） | error |
| 7 | 单天活动数 vs pace | relaxed ≤ 3, balanced 3-4, intensive ≤ 5；超出为 warning | warning |

原有 POI 精确去重（#1）severity 升级为 error。

### 3.2 GlobalValidationIssue 升级

```python
@dataclass
class GlobalValidationIssue:
    issue_type: str       # "poi_duplicate" | "budget_overrun" | "coverage_gap"
                          # | "time_conflict" | "transport_connection" | "semantic_duplicate" | "pace_mismatch"
    description: str
    affected_days: list[int] = field(default_factory=list)
    severity: str = "warning"  # 新增: "error" | "warning"
```

### 3.3 校验结果处理流程

```
_global_validate(dayplans) → list[GlobalValidationIssue]
    ↓
分离 errors / warnings
    ↓
errors 非空？
    ├─ 是 → 收集 affected_days（去重）→ 对每个问题天做 targeted re-dispatch（最多 1 次）
    │       re-dispatch 时将 issue.description 注入为 repair_hints
    │       ↓
    │       re-dispatch 后重新 validate
    │       ├─ 仍有 error → 接受当前结果，标记 unresolved_issues
    │       └─ 通过 → 正常写入
    └─ 否 → 正常写入，warnings 写入 summary
```

### 3.4 时间冲突校验实现

复用 `harness/validator.py` 中 `_validate_time_conflicts` 的核心逻辑，适配 dayplan dict 输入格式（而非 TravelPlanState）：

```python
def _validate_time_conflicts_from_dicts(dayplans: list[dict]) -> list[GlobalValidationIssue]:
    issues = []
    for dp in dayplans:
        day = dp.get("day", 0)
        activities = dp.get("activities", [])
        for i in range(len(activities) - 1):
            prev = activities[i]
            curr = activities[i + 1]
            prev_end = _time_to_minutes(prev.get("end_time", ""))
            curr_start = _time_to_minutes(curr.get("start_time", ""))
            travel = curr.get("transport_duration_min", 0) or 0
            if prev_end is not None and curr_start is not None:
                if prev_end + travel > curr_start:
                    issues.append(GlobalValidationIssue(
                        issue_type="time_conflict",
                        description=f"Day {day}: '{prev.get('name')}' 结束 {prev.get('end_time')} + 交通 {travel}min > '{curr.get('name')}' 开始 {curr.get('start_time')}",
                        affected_days=[day],
                        severity="error",
                    ))
    return issues
```

### 3.5 首尾日衔接校验实现

```python
def _validate_transport_connection(dayplans: list[dict], plan: TravelPlanState) -> list[GlobalValidationIssue]:
    issues = []
    transport = plan.selected_transport
    if not transport:
        return issues

    sorted_days = sorted(dayplans, key=lambda d: d.get("day", 0))
    if not sorted_days:
        return issues

    # 到达日：提取到达时间，首活动 start 须 >= arrival + 120min
    # selected_transport 是 dict，包含 outbound/return 段，各含 departure_time/arrival_time 字段
    # _extract_arrival_time 从 outbound 段提取 arrival_time 并转为分钟数
    arrival_time = _extract_arrival_time(transport)
    if arrival_time is not None and sorted_days:
        first_day = sorted_days[0]
        first_activities = first_day.get("activities", [])
        if first_activities:
            first_start = _time_to_minutes(first_activities[0].get("start_time", ""))
            if first_start is not None and first_start < arrival_time + 120:
                issues.append(GlobalValidationIssue(
                    issue_type="transport_connection",
                    description=f"Day 1 首活动开始时间过早，距到达不足 2 小时",
                    affected_days=[first_day.get("day", 1)],
                    severity="error",
                ))

    # 离开日：提取离开时间，末活动 end 须 <= departure - 180min
    departure_time = _extract_departure_time(transport)
    if departure_time is not None and sorted_days:
        last_day = sorted_days[-1]
        last_activities = last_day.get("activities", [])
        if last_activities:
            last_end = _time_to_minutes(last_activities[-1].get("end_time", ""))
            if last_end is not None and last_end > departure_time - 180:
                issues.append(GlobalValidationIssue(
                    issue_type="transport_connection",
                    description=f"末日末活动结束过晚，距离开不足 3 小时",
                    affected_days=[last_day.get("day", len(sorted_days))],
                    severity="error",
                ))

    return issues
```

### 3.6 语义近似 POI 去重实现

```python
def _validate_semantic_duplicates(dayplans: list[dict]) -> list[GlobalValidationIssue]:
    """坐标距离 < 200m + 名称模糊匹配 → 视为重复"""
    issues = []
    all_pois = []  # list of (day, name, lat, lng)
    for dp in dayplans:
        day = dp.get("day", 0)
        for act in dp.get("activities", []):
            loc = act.get("location", {})
            lat = loc.get("lat")
            lng = loc.get("lng")
            name = act.get("name", "")
            if name and lat is not None and lng is not None:
                all_pois.append((day, name, float(lat), float(lng)))

    seen_pairs: set[tuple[int, int]] = set()
    for i, (day_a, name_a, lat_a, lng_a) in enumerate(all_pois):
        for j, (day_b, name_b, lat_b, lng_b) in enumerate(all_pois):
            if i >= j or day_a == day_b:
                continue
            pair = (min(i, j), max(i, j))
            if pair in seen_pairs:
                continue
            dist = _haversine_meters(lat_a, lng_a, lat_b, lng_b)
            if dist < 200 and _names_similar(name_a, name_b):
                seen_pairs.add(pair)
                issues.append(GlobalValidationIssue(
                    issue_type="semantic_duplicate",
                    description=f"'{name_a}'(Day {day_a}) 与 '{name_b}'(Day {day_b}) 疑似同一地点（距离 {dist:.0f}m）",
                    affected_days=[day_b],  # 保留较早天的，问题天为较晚天
                    severity="error",
                ))
    return issues


def _names_similar(a: str, b: str) -> bool:
    """子串包含或编辑距离 ≤ 2"""
    a_lower, b_lower = a.lower().strip(), b.lower().strip()
    if a_lower in b_lower or b_lower in a_lower:
        return True
    return _levenshtein(a_lower, b_lower) <= 2


def _haversine_meters(lat1, lng1, lat2, lng2) -> float:
    """两点之间的距离（米）"""
    from math import radians, sin, cos, sqrt, atan2
    R = 6_371_000
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))
```

---

## 4. 模块 B：Orchestrator 编译层 + Worker 约束注入

### 4.1 DayTask 数据结构升级

```python
@dataclass
class DayTask:
    day: int
    date: str
    skeleton_slice: dict[str, Any]
    pace: str
    # ---- 新增字段 ----
    locked_pois: list[str] = field(default_factory=list)
    candidate_pois: list[str] = field(default_factory=list)
    forbidden_pois: list[str] = field(default_factory=list)
    area_cluster: list[str] = field(default_factory=list)
    mobility_envelope: dict[str, Any] = field(default_factory=dict)
    fallback_slots: list[dict] = field(default_factory=list)
    date_role: str = "full_day"     # "arrival_day" | "departure_day" | "full_day"
    repair_hints: list[str] = field(default_factory=list)
```

### 4.2 编译流程：`_compile_day_tasks`

新增方法，在 `_split_tasks` 之后调用：

```python
def _compile_day_tasks(self, tasks: list[DayTask]) -> list[DayTask]:
    """从骨架编译完整的 DayTask 约束"""

    # 1. 构建全局 POI 归属表
    poi_owner: dict[str, int] = {}  # poi_name → day (仅 locked)
    for t in tasks:
        for poi in t.locked_pois:
            if poi in poi_owner:
                logger.warning("POI '%s' locked by both Day %d and Day %d", poi, poi_owner[poi], t.day)
            poi_owner[poi] = t.day

    # 2. 为每天推导 forbidden_pois
    for t in tasks:
        t.forbidden_pois = [
            poi for poi, owner_day in poi_owner.items()
            if owner_day != t.day
        ]

    # 3. 推导 mobility_envelope（如果骨架未提供）
    pace_defaults = {
        "relaxed":   {"max_cross_area_hops": 1, "max_transit_leg_min": 30},
        "balanced":  {"max_cross_area_hops": 2, "max_transit_leg_min": 40},
        "intensive": {"max_cross_area_hops": 3, "max_transit_leg_min": 50},
    }
    for t in tasks:
        if not t.mobility_envelope:
            t.mobility_envelope = pace_defaults.get(t.pace, pace_defaults["balanced"])

    # 4. 推导 date_role
    if tasks:
        sorted_tasks = sorted(tasks, key=lambda x: x.day)
        sorted_tasks[0].date_role = "arrival_day"
        sorted_tasks[-1].date_role = "departure_day"
        # 如果只有 1 天，同时是 arrival + departure
        if len(sorted_tasks) == 1:
            sorted_tasks[0].date_role = "arrival_departure_day"

    return tasks
```

### 4.3 `build_day_suffix` 改造

在现有 day suffix 末尾追加约束块：

```python
def _build_constraint_block(task: DayTask) -> str:
    lines = ["\n## 硬约束（必须遵守）\n"]

    if task.locked_pois:
        lines.append(f"- **必须包含的活动**: {', '.join(task.locked_pois)}")

    if task.candidate_pois:
        lines.append(f"- **允许使用的候选池**: {', '.join(task.candidate_pois)}")
        lines.append("- 优先从候选池选取，如需额外补充须在同 area_cluster 内")

    if task.forbidden_pois:
        lines.append(f"- **禁止使用（已分配给其他天）**: {', '.join(task.forbidden_pois)}")

    if task.area_cluster:
        lines.append(f"- **当日区域**: {', '.join(task.area_cluster)}")

    env = task.mobility_envelope
    if env:
        max_hops = env.get("max_cross_area_hops", "不限")
        max_leg = env.get("max_transit_leg_min", "不限")
        lines.append(f"- **移动限制**: 最多跨 {max_hops} 个区域, 单段交通 ≤ {max_leg} 分钟")

    if task.date_role == "arrival_day":
        lines.append("- **到达日**: 注意大交通到达时间，首活动须留足接驳缓冲")
    elif task.date_role == "departure_day":
        lines.append("- **离开日**: 注意大交通离开时间，末活动须留足前往交通枢纽的时间")

    if task.fallback_slots:
        lines.append("\n### 备选方案")
        for slot in task.fallback_slots:
            target = slot.get("replace_if_unavailable", "?")
            alts = slot.get("alternatives", [])
            lines.append(f"- 如 {target} 不可行 → 替换为: {', '.join(alts)}")

    if task.repair_hints:
        lines.append("\n### ⚠️ 修复要求（上一轮校验发现的问题）")
        for hint in task.repair_hints:
            lines.append(f"- {hint}")

    return "\n".join(lines)
```

### 4.4 Worker Prompt 约束遵守指令

在 `build_day_suffix` 的 Worker 角色指令中增加：

```
## 约束遵守规则
1. `locked_pois` 中的 POI 必须出现在你的 DayPlan 中，除非工具查询确认不可行
2. `forbidden_pois` 中的 POI 绝对禁止使用，即使你认为很合适
3. 优先从 `candidate_pois` 中选取补充活动
4. 如果 `locked_pois` 全部不可行且 fallback 耗尽，返回 error_code "NEEDS_PHASE3_REPLAN"
5. 不得超出 mobility_envelope 限制
```

---

## 5. 模块 C：Phase 3 Skeleton Schema 升级

### 5.1 Schema 变更（`_SET_SKELETON_PLANS_PARAMS`）

`days` 数组 item 从 `{"type": "object"}` 升级为有结构的 schema：

```python
_SKELETON_DAY_SCHEMA = {
    "type": "object",
    "properties": {
        "area_cluster": {
            "type": "array",
            "items": {"type": "string"},
            "description": "当天主区域列表",
        },
        "theme": {
            "type": "string",
            "description": "当天主题",
        },
        "locked_pois": {
            "type": "array",
            "items": {"type": "string"},
            "description": "该天独占的强锚点（其他天禁止使用）",
        },
        "candidate_pois": {
            "type": "array",
            "items": {"type": "string"},
            "description": "该天允许使用的候选 POI 池",
        },
        "core_activities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "核心活动或体验",
        },
        "fatigue_level": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "预期疲劳等级",
        },
        "budget_level": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "预期预算等级",
        },
        # 可选字段（Phase 3 可提供，否则 Orchestrator 推导）
        "excluded_pois": {
            "type": "array",
            "items": {"type": "string"},
            "description": "该天显式排除的 POI",
        },
        "date_role": {
            "type": "string",
            "enum": ["arrival_day", "departure_day", "full_day"],
            "description": "该天在行程中的角色",
        },
        "mobility_envelope": {
            "type": "object",
            "properties": {
                "max_cross_area_hops": {"type": "integer"},
                "max_transit_leg_min": {"type": "integer"},
            },
            "description": "移动边界限制",
        },
        "fallback_slots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "replace_if_unavailable": {"type": "string"},
                    "alternatives": {"type": "array", "items": {"type": "string"}},
                },
            },
            "description": "备选替换方案",
        },
    },
    "required": ["area_cluster", "locked_pois", "candidate_pois"],
}
```

### 5.2 Schema 校验逻辑

在 `set_skeleton_plans` 工具函数中增加 days 校验：

```python
def _validate_skeleton_days(plans: list[dict]) -> None:
    """校验所有骨架方案的 days 结构"""
    for plan_idx, plan in enumerate(plans):
        days = plan.get("days", [])
        if not days:
            raise ToolError(f"plans[{plan_idx}].days 不能为空")

        all_locked: dict[str, tuple[int, int]] = {}  # poi → (plan_idx, day_idx)

        for day_idx, day in enumerate(days):
            prefix = f"plans[{plan_idx}].days[{day_idx}]"

            # area_cluster: 必填，非空 list[str]
            ac = day.get("area_cluster")
            if not ac or not isinstance(ac, list) or not all(isinstance(x, str) for x in ac):
                raise ToolError(f"{prefix}.area_cluster 必须是非空字符串列表")

            # locked_pois: 必填，list[str]（可以为空）
            lp = day.get("locked_pois")
            if lp is None or not isinstance(lp, list):
                raise ToolError(f"{prefix}.locked_pois 必须是字符串列表（可以为空列表）")

            # candidate_pois: 必填，非空 list[str]
            cp = day.get("candidate_pois")
            if not cp or not isinstance(cp, list):
                raise ToolError(f"{prefix}.candidate_pois 必须是非空字符串列表")

            # 跨天 locked_pois 排他校验
            for poi in lp:
                if poi in all_locked:
                    prev_plan, prev_day = all_locked[poi]
                    raise ToolError(
                        f"'{poi}' 同时被 plans[{prev_plan}].days[{prev_day}] "
                        f"和 {prefix} 锁定，locked_pois 必须跨天唯一"
                    )
                all_locked[poi] = (plan_idx, day_idx)
```

### 5.3 Phase 3 Skeleton Prompt 改造

在 skeleton 子阶段 prompt 的"最小结构化字段"部分更新为：

```
每套骨架的 **最小结构化字段**（写入 `skeleton_plans` 时必须包含）：
- `id`：唯一标识符（如 `"plan_A"`）
- `name`：方案显示名称
- `days`：list，每天必须包含：
  - `area_cluster`：当天主区域列表（如 `["浅草", "上野"]`）
  - `theme`：当天主题
  - `locked_pois`：该天独占的强锚点列表（如 `["浅草寺"]`）。
    - 每个 POI 只能被一天 lock，不允许跨天重复
    - 可以为空列表（表示该天没有强锚点）
  - `candidate_pois`：该天允许使用的候选 POI 池（如 `["仲见世商店街", "上野公园"]`）
  - `core_activities`：核心活动
  - `fatigue_level`：疲劳等级（low / medium / high）
  - `budget_level`：预算等级（low / medium / high）
- `tradeoffs`：保留了什么、放弃了什么

可选字段（有则更好，没有时系统自动推导）：
- `excluded_pois`：该天显式排除的 POI
- `date_role`：`"arrival_day"` / `"departure_day"` / `"full_day"`
- `mobility_envelope`：`{ "max_cross_area_hops": 1, "max_transit_leg_min": 35 }`
- `fallback_slots`：`[{ "replace_if_unavailable": "浅草寺", "alternatives": ["今户神社"] }]`
```

新增 Red Flag：
```
- **locked_pois 跨天重复**——同一个 POI 被两天同时 lock，这会导致 Phase 5 并行 Worker 产生冲突
- **candidate_pois 为空**——Phase 5 Worker 没有候选池就只能凭空创造，容易偏离骨架意图
```

---

## 6. 模块 D：回退协议

### 6.1 Worker 结构化失败

Worker 新增错误码 `NEEDS_PHASE3_REPLAN`，在以下条件触发：

1. `locked_pois` 全部不可行 + `fallback_slots` 全部耗尽
2. 即使删除所有非 locked 活动，仍超出 `mobility_envelope`
3. `date_role` 为 arrival/departure 时，大交通时间使当天无法放置任何有效活动

Worker 返回：
```python
DayWorkerResult(
    day=task.day,
    success=False,
    dayplan=None,
    error="locked_pois ['浅草寺'] 全部不可行，fallback 也无法满足",
    error_code="NEEDS_PHASE3_REPLAN",
)
```

### 6.2 Orchestrator 回退决策

```python
# 在 run() 的结果收集阶段
replan_days = [r for r in results if r.error_code == "NEEDS_PHASE3_REPLAN"]
if replan_days:
    reason_parts = [f"Day {r.day}: {r.error}" for r in replan_days]
    reason = "骨架分配失败，以下天数无法按当前骨架展开:\n" + "\n".join(reason_parts)
    # 触发 backtrack
    yield LLMChunk(
        type=ChunkType.TEXT_DELTA,
        content=f"\n\n⚠️ {reason}\n需要回退到 Phase 3 重新调整骨架方案。\n",
    )
    # 调用 request_backtrack
    await self._trigger_backtrack(reason)
    return
```

### 6.3 回退范围

- 回退目标：Phase 3 skeleton 子步骤
- 保留：trip_brief、candidate_pool、shortlist
- 清除：skeleton_plans、selected_skeleton_id、daily_plans
- 回退上下文注入：将失败原因作为系统消息注入，引导 Phase 3 针对性调整

---

## 7. 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `backend/agent/orchestrator.py` | 修改 | 增强 `_global_validate`（4 项新校验）；新增 `_compile_day_tasks`；re-dispatch 逻辑；回退触发 |
| `backend/agent/worker_prompt.py` | 修改 | 升级 `DayTask` dataclass；`build_day_suffix` 追加约束块；`split_skeleton_to_day_tasks` 提取新字段 |
| `backend/agent/day_worker.py` | 修改 | 新增 `NEEDS_PHASE3_REPLAN` 错误码；约束遵守检查 |
| `backend/tools/plan_tools/phase3_tools.py` | 修改 | 升级 `_SET_SKELETON_PLANS_PARAMS` schema；新增 `_validate_skeleton_days` |
| `backend/phase/prompts.py` | 修改 | skeleton 子阶段 prompt 更新最小结构化字段说明和 Red Flags |
| `backend/harness/validator.py` | 不变 | 复用已有 `_time_to_minutes` 等辅助函数（import） |
| `backend/tests/` | 新增/修改 | 各模块对应的单元测试 |

## 8. 测试策略

### 8.1 Orchestrator 校验测试

- 时间冲突：构造相邻活动时间重叠的 dayplan，验证检出
- 首尾日衔接：构造 Day 1 首活动早于到达时间的 case
- 语义去重：构造坐标 < 200m + 名称相似的跨天 POI
- pace 校验：构造超出活动数限制的 dayplan
- re-dispatch：mock Worker，验证 error 级 issue 触发重跑，且 repair_hints 正确注入

### 8.2 编译层测试

- POI 归属表：验证 locked_pois 正确映射到 forbidden_pois
- mobility_envelope 推导：验证不同 pace 的默认值
- date_role 推导：验证首尾日标记

### 8.3 Phase 3 Schema 测试

- 缺少 area_cluster/locked_pois/candidate_pois 时报错
- locked_pois 跨天重复时报错
- 合法输入正常通过

### 8.4 回退协议测试

- Worker 返回 NEEDS_PHASE3_REPLAN 时 Orchestrator 触发 backtrack
- 回退后保留 trip_brief/candidate_pool，清除 skeleton/daily_plans
