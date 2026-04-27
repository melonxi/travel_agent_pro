# Phase 5 Worker 提示词优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 优化 Phase 5 Day Worker 的提示词和独占工具 schema，解决角色错配、工具描述缺失、约束薄弱、输出歧义、认知缺失、修复失控、预算盲区、缺少负面示例、事后惩罚式收口、归因对抗和语义重叠等问题，提升并行 Worker 的 DayPlan 输出质量。

**Architecture:** 修改集中在 `worker_prompt.py`（提示词模板 + DayTask + build_shared_prefix/build_day_suffix）、`day_worker.py`（Worker 循环逻辑中的 3 个内嵌 prompt 常量 + submit schema）和 `orchestrator.py`（`_compile_day_tasks` 注入 day_budget / arrival_time / departure_time / day_constraints）。测试文件同步更新。

**Tech Stack:** Python, pytest, 无新依赖

**Commit 规则：** 每次改动都同步更新 PROJECT_OVERVIEW.md 中 Phase 5 相关部分，然后一起 commit。

---

## 涉及文件总览

| 文件 | 职责 |
|------|------|
| `backend/agent/phase5/worker_prompt.py` | 核心：提示词模板 + DayTask + build_shared_prefix/build_day_suffix + _DAYPLAN_SCHEMA |
| `backend/agent/phase5/day_worker.py` | Worker 循环逻辑 + 3 个收口 prompt 常量 + `_SUBMIT_DAY_PLAN_CANDIDATE_SCHEMA` |
| `backend/agent/phase5/orchestrator.py` | `_compile_day_tasks` 注入 day_budget / arrival_time / departure_time / day_constraints |
| `backend/tests/test_worker_prompt.py` | 提示词构建断言 |
| `backend/tests/test_day_worker.py` | Worker 循环行为断言 |
| `backend/tests/test_orchestrator.py` | Orchestrator 测试断言 |
| `PROJECT_OVERVIEW.md` | Phase 5 相关描述同步更新 |

---

### Task 0: 重构 submit_day_plan_candidate 工具描述与参数 Schema + DayPlan Schema category enum + 结构性错误

**Files:**
- Modify: `backend/agent/phase5/day_worker.py:54-71`（`_SUBMIT_DAY_PLAN_CANDIDATE_SCHEMA`）
- Modify: `backend/agent/phase5/worker_prompt.py:80-109`（`_DAYPLAN_SCHEMA` — 合并同一常量的两处编辑）
- Modify: `backend/tests/test_day_worker.py`（新增 schema 结构断言）
- Modify: `backend/tests/test_worker_prompt.py`（负面示例断言）

**问题：** 当前 `_SUBMIT_DAY_PLAN_CANDIDATE_SCHEMA` 的 `dayplan` 参数只有 `type: object` + 一句话描述，没有内嵌属性 schema。`_DAYPLAN_SCHEMA` 缺少 category enum 和常见错误。两处编辑同一文件同一常量区域，合并为一步避免冲突。

**方案：** 重写 submit schema 为完整内联 JSON Schema + 5 段式 description；同步更新 `_DAYPLAN_SCHEMA` 加 category enum + 结构性错误示例（不含业务假设）。

- [ ] **Step 1: 重写 `_SUBMIT_DAY_PLAN_CANDIDATE_SCHEMA`**

```python
_SUBMIT_DAY_PLAN_CANDIDATE_SCHEMA = {
    "name": "submit_day_plan_candidate",
    "description": (
        "提交你这一天的最终 DayPlan 候选给 Orchestrator。这是你完成本任务的唯一交付动作。\n"
        "\n"
        "【何时调用】\n"
        "- 当天活动序列已确定，所有 locked POI 已包含\n"
        "- 已用 get_poi_info 补齐你引用的 POI 信息（无法补齐的字段写 notes）\n"
        "- 时间表已留出交通/缓冲，活动数符合 pace 要求\n"
        "\n"
        "【何时不要调用】\n"
        "- 仍有 locked POI 未纳入活动\n"
        "- start_time/end_time 还未定（不要提交占位符）\n"
        "- 同一 POI 在你的活动列表中重复出现\n"
        "\n"
        "【提交后】\n"
        "- 此次提交是候选，Orchestrator 会做跨天校验，可能要求你修复重新提交\n"
        "- 提交成功后只输出一句确认（如：「已提交第 N 天」），不要粘贴整个 JSON\n"
        "- 提交失败时，根据 error_code 修正后最多再调一次；仍失败则在最终文本输出合法 JSON 兜底\n"
        "\n"
        "【错误码 → 动作】\n"
        "- INVALID_DAYPLAN（day 不匹配）→ 把 dayplan.day 改为当前任务天数\n"
        "- INVALID_DAYPLAN（字段缺失）→ 补齐 day/date/activities，每个 activity 含 name/location/start_time/end_time/category/cost\n"
        "- INVALID_DAYPLAN（location 非对象）→ location 必须是 {name, lat, lng}，不是字符串\n"
        "- SUBMIT_UNAVAILABLE → 此运行未注入 candidate_store，改为在最终文本输出合法 DayPlan JSON（用 ```json 代码块包裹）"
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "dayplan": {
                "type": "object",
                "description": "完整 DayPlan。day 必须等于你当前任务的天数；activities 至少 2 项；所有时间用 24 小时 HH:MM 格式。",
                "additionalProperties": False,
                "required": ["day", "date", "activities"],
                "properties": {
                    "day": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "天数（1-based），必须等于当前任务的 day。",
                    },
                    "date": {
                        "type": "string",
                        "pattern": r"^\d{4}-\d{2}-\d{2}$",
                        "description": "ISO 日期，YYYY-MM-DD。",
                    },
                    "notes": {
                        "type": "string",
                        "description": "当天补充说明（可选）。无法从工具确认的事实写在这里或活动 notes 里。",
                    },
                    "activities": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "name",
                                "location",
                                "start_time",
                                "end_time",
                                "category",
                                "cost",
                            ],
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "活动/POI 名称。",
                                },
                                "location": {
                                    "type": "object",
                                    "required": ["name", "lat", "lng"],
                                    "properties": {
                                        "name": {"type": "string"},
                                        "lat": {"type": "number", "minimum": -90, "maximum": 90},
                                        "lng": {"type": "number", "minimum": -180, "maximum": 180},
                                    },
                                    "description": "必须是对象 {name, lat, lng}，不能是字符串。lat/lng 来自 get_poi_info 返回值。",
                                },
                                "start_time": {
                                    "type": "string",
                                    "pattern": r"^\d{2}:\d{2}$",
                                    "description": "24 小时制 HH:MM。",
                                },
                                "end_time": {
                                    "type": "string",
                                    "pattern": r"^\d{2}:\d{2}$",
                                    "description": "晚于 start_time。",
                                },
                                "category": {
                                    "type": "string",
                                    "enum": [
                                        "shrine", "museum", "food", "transport",
                                        "activity", "shopping", "park",
                                        "viewpoint", "experience",
                                    ],
                                    "description": "活动类别枚举。餐饮使用 food。",
                                },
                                "cost": {
                                    "type": "number",
                                    "minimum": 0,
                                    "description": "人民币数字；免费写 0；估算时取保守上限。",
                                },
                                "transport_from_prev": {
                                    "type": "string",
                                    "description": "从上一活动到本活动的交通方式（步行/地铁/出租/巴士等）。",
                                },
                                "transport_duration_min": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "description": "上一活动到本活动的交通时长（分钟）。优先使用 calculate_route 返回值。",
                                },
                                "notes": {
                                    "type": "string",
                                    "description": "可选。无法确认的信息写在这里，例如「需提前预约（未确认链接）」。",
                                },
                            },
                        },
                    },
                },
            }
        },
        "required": ["dayplan"],
    },
}
```

- [ ] **Step 2: 同步更新 `_DAYPLAN_SCHEMA`，加 category enum + 结构性错误示例 + 指向 submit schema 为单一事实源**

```python
_DAYPLAN_SCHEMA = """## DayPlan 结构要求

无论是调用 `submit_day_plan_candidate`，还是在工具不可用时通过最终文本兜底输出，都必须使用以下结构：

```json
{
  "day": <天数>,
  "date": "<YYYY-MM-DD>",
  "notes": "<当天补充说明>",
  "activities": [
    {
      "name": "<活动名称>",
      "location": {"name": "<地点名>", "lat": <纬度>, "lng": <经度>},
      "start_time": "HH:MM",
      "end_time": "HH:MM",
      "category": "<类别>",
      "cost": <人民币数字>,
      "transport_from_prev": "<从上一地点的交通方式>",
      "transport_duration_min": <分钟数>,
      "notes": "<可选备注>"
    }
  ]
}
```

硬约束：
- location 必须是 dict（含 name, lat, lng），不能是字符串
- start_time / end_time 必须是 "HH:MM" 格式，且 end_time > start_time
- cost 是数字（人民币），没有时填 0；不能是字符串如 "100元"
- category 必须是以下枚举之一：shrine, museum, food, transport, activity, shopping, park, viewpoint, experience

常见结构错误（绝对不允许）：
1. `"location": "浅草寺"` → 必须是 `{"name": "浅草寺", "lat": 35.7148, "lng": 139.7967}`
2. `"cost": "100元"` → 必须是数字 `100`
3. `"start_time": "09:00", "end_time": "09:00"` → end_time 必须晚于 start_time
4. `"category": "景点"` → 必须使用枚举值（如 shrine, museum, park 等）

完整字段定义和约束请以 `submit_day_plan_candidate` 工具的参数 schema 为准。"""
```

- [ ] **Step 3: 新增 schema 结构断言测试**

```python
def test_submit_schema_has_inline_properties():
    from agent.phase5.day_worker import _SUBMIT_DAY_PLAN_CANDIDATE_SCHEMA
    schema = _SUBMIT_DAY_PLAN_CANDIDATE_SCHEMA
    assert schema["name"] == "submit_day_plan_candidate"
    dayplan = schema["parameters"]["properties"]["dayplan"]
    assert dayplan["type"] == "object"
    assert "day" in dayplan["properties"]
    assert "activities" in dayplan["properties"]
    act_item = dayplan["properties"]["activities"]["items"]
    assert act_item["properties"]["location"]["type"] == "object"
    assert "lat" in act_item["properties"]["location"]["properties"]
    assert "enum" in act_item["properties"]["category"]
    desc = schema["description"]
    assert "INVALID_DAYPLAN" in desc
    assert "SUBMIT_UNAVAILABLE" in desc


def test_dayplan_schema_has_category_enum_and_structural_errors():
    plan = _make_plan()
    prefix = build_shared_prefix(plan)
    assert "枚举之一" in prefix
    assert "常见结构错误" in prefix
    assert "location" in prefix
```

- [ ] **Step 4: 运行测试验证**

Run: `cd backend && python -m pytest tests/test_day_worker.py tests/test_worker_prompt.py -v`

- [ ] **Step 5: Commit（含 PROJECT_OVERVIEW 同步）**

```bash
git add backend/agent/phase5/day_worker.py backend/agent/phase5/worker_prompt.py backend/tests/test_day_worker.py backend/tests/test_worker_prompt.py PROJECT_OVERVIEW.md
git commit -m "feat(phase5): inline JSON Schema for submit tool, category enum, structural error examples"
```

---

### Task 1: Worker 专属角色 + 并发语境 + 无用户交互声明（合并原 Task 1 & 5）

**Files:**
- Modify: `backend/agent/phase5/worker_prompt.py:37-78`（`_WORKER_ROLE` 常量）
- Modify: `backend/agent/phase5/worker_prompt.py:112-167`（`build_shared_prefix` 函数，移除 `_load_soul()`）
- Delete: `backend/agent/phase5/worker_prompt.py:35,112-115`（`_SOUL_PATH` 和 `_load_soul` 函数）
- Modify: `backend/tests/test_worker_prompt.py:36-71`（断言更新）

**问题：** soul.md 定义面向用户的对话人格，Worker 没有用户交互通道却注入了这些指令。Worker 也不理解 forbidden_pois 的跨天语义、Orchestrator 角色、以及应该"完成优于完美"。

**方案：** 移除 soul.md 注入；重写 `_WORKER_ROLE` 为 Worker 专属角色定义，包含无用户交互声明、并发语境、完成优于完美原则、优先级。

- [ ] **Step 1: 删除 `_SOUL_PATH` 和 `_load_soul`，修改 `build_shared_prefix` 开头**

```python
def build_shared_prefix(plan: TravelPlanState) -> str:
    """Build the shared prefix for all Day Workers.

    This prefix is identical across all workers to maximize KV-Cache hit rate.
    Do NOT include any per-day information here.
    """
    parts = []

    # 旅行上下文（只读）
    parts.append("## 旅行上下文\n")
    # ... rest unchanged, just no _load_soul() at start
```

删除 `_SOUL_PATH = Path(...)` 和 `def _load_soul():` 函数。

- [ ] **Step 2: 重写 `_WORKER_ROLE` 常量**

```python
_WORKER_ROLE = """## 角色

你是单日行程落地规划师，由主 Agent 派发的并行子任务执行者。
你是 N 个并行 Worker 之一——其他 Worker 正在规划其他天的行程，你只负责指定的一天。

## 无用户交互

你与用户没有任何交互通道。不要提问、不要请求确认、不要给出 2-3 个选项让人选。
所有判断由你独立做出，通过 `submit_day_plan_candidate` 提交结果。

## 完成优于完美

一个覆盖所有硬约束的 70 分保守 DayPlan 远胜于一个无限搜索未完成的计划。
优先提交，Orchestrator 会做全局验证和修补。

## 优先级（冲突时）

1. 当前 DayTask 的硬约束（locked / forbidden / area_cluster / mobility）
2. 骨架的 area / theme / core_activities（方向性参考）
3. 通用旅行规划常识

## 硬法则

- 严格基于骨架安排展开，不要偷偷替换区域或主题。
- 区域连续性优先于景点密度——同一天的活动应在地理上聚拢。
- 时间安排必须留出现实缓冲（交通延误、排队、休息），不要把活动首尾无缝拼死。
- 用 get_poi_info 补齐缺失的坐标、票价、开放时间。
- 用 optimize_day_route 优化活动顺序。
- 用 calculate_route 验证关键移动是否可行。
- 餐饮可作为活动（category="food"），安排在合理时段。

## 你与全局的关系

- `forbidden_pois` 中的景点是其他天已经锁定的核心景点——使用它们会导致跨天 POI 重复，触发 Orchestrator 重新分配（计为你的失败）。
- 你提交的 DayPlan 由 Orchestrator 做跨天 POI 去重、时间冲突、预算检查等全局校验。
- 如果你的输出有局部问题，Orchestrator 会发回修复要求（repair_hints），你只需修正指定问题，不需要重做整天。
- 预算分配参考：每天大致均分总预算即可。

## 工具回退策略

- 当专项工具返回无效信息时，可以进行有限次补救，但不要围绕同一 POI 或同一问题无限搜索。
- 如果已经具备区域、主题、核心活动和基本时间结构，应优先输出保守版 DayPlan。
- 当工具仍无法补齐细节时，可以基于骨架、区域连续性和常识性节奏完成保守安排。
- 不得编造具体营业时间、具体票价、明确预约要求；无法确认的事实写入 notes。
- 当系统提示进入收口模式时，必须停止继续调工具并直接提交 DayPlan。

## 交付方式（唯一合法路径）

你完成单日规划后，**必须**调用 `submit_day_plan_candidate` 工具提交 DayPlan。
这是提交 DayPlan 的唯一方式。

提交成功后，只输出一句简短确认："已提交第 N 天计划。"

❌ 不要在自然语言正文中输出完整 DayPlan JSON。
❌ 不要绕过工具直接输出 JSON。

唯一例外：如果 `submit_day_plan_candidate` 返回 `SUBMIT_UNAVAILABLE` 错误（工具不可用），
才可以在最终文本中输出 DayPlan JSON 作为系统故障兜底。

如果 `submit_day_plan_candidate` 返回其他错误：
- 根据错误信息修正 DayPlan 后再次提交（最多 1 次）
- 如果错误说明 day 不匹配，必须把 day 改为当前任务天数
- 如果错误说明字段缺失，必须补齐字段

## 状态写入边界

`submit_day_plan_candidate` 只提交候选 DayPlan 给 Orchestrator 校验。
它不会直接写入最终行程状态。
你不能假设提交后计划已经最终确认。
Orchestrator 会统一做跨天校验、必要重派和最终写入。"""
```

- [ ] **Step 3: 更新测试**

```python
def test_build_shared_prefix_contains_role():
    plan = _make_plan()
    prefix = build_shared_prefix(plan)
    assert "单日行程落地规划师" in prefix
    assert "完成优于完美" in prefix
    assert "无用户交互" in prefix
    assert "forbidden_pois" in prefix
    assert "唯一合法路径" in prefix
    # soul.md content should NOT appear
    assert "一次只问一个问题" not in prefix


def test_build_shared_prefix_no_soul_md():
    plan = _make_plan()
    prefix = build_shared_prefix(plan)
    assert "一次只问一个问题" not in prefix
    assert "2-3 个选项" not in prefix
```

- [ ] **Step 4: 运行测试**

Run: `cd backend && python -m pytest tests/test_worker_prompt.py tests/test_day_worker.py -v`

- [ ] **Step 5: Commit（含 PROJECT_OVERVIEW 同步）**

```bash
git add backend/agent/phase5/worker_prompt.py backend/tests/test_worker_prompt.py PROJECT_OVERVIEW.md
git commit -m "refactor(phase5): replace soul.md with worker-only identity, add no-user-interaction and concurrency context"
```

---

### Task 2: 共享前缀瘦身 + 字段稳定排序 + DayTask 扩展 + day_constraints 注入路径

**Files:**
- Modify: `backend/agent/phase5/worker_prompt.py:118-167`（`build_shared_prefix`）
- Modify: `backend/agent/phase5/worker_prompt.py:218-259`（`build_day_suffix`）
- Modify: `backend/agent/phase5/worker_prompt.py:17-32`（`DayTask` 新增字段）
- Modify: `backend/agent/phase5/orchestrator.py:170-217`（`_compile_day_tasks` 注入 day_constraints / day_budget / arrival_time / departure_time）
- Modify: `backend/tests/test_worker_prompt.py`
- Modify: `backend/tests/test_orchestrator.py`

**问题：** 1) 全量 trip_brief/preferences/constraints 稀释注意力；2) dict 字段顺序不确定破坏 KV-Cache；3) non-hard constraints 在共享前缀不必要；4) `day_constraints` 只渲染没有注入路径——需在 orchestrator 中过滤 plan.constraints 填入 DayTask。

**方案：** trip_brief 白名单过滤 + constraints 分层注入 + preferences 按 key 排序 + DayTask 新增 `day_budget`/`day_constraints`/`arrival_time`/`departure_time` + orchestrator `_compile_day_tasks` 注入所有新字段。

- [ ] **Step 1: `DayTask` 新增 4 个字段（全部有默认值，向后兼容）**

```python
@dataclass
class DayTask:
    """A single day's task extracted from the skeleton."""

    day: int
    date: str
    skeleton_slice: dict[str, Any]
    pace: str
    locked_pois: list[str] = field(default_factory=list)
    candidate_pois: list[str] = field(default_factory=list)
    forbidden_pois: list[str] = field(default_factory=list)
    area_cluster: list[str] = field(default_factory=list)
    mobility_envelope: dict[str, Any] = field(default_factory=dict)
    fallback_slots: list[dict] = field(default_factory=list)
    date_role: str = "full_day"
    repair_hints: list[str] = field(default_factory=list)
    day_budget: int | None = None
    day_constraints: list[dict[str, str]] = field(default_factory=list)
    arrival_time: str | None = None
    departure_time: str | None = None
```

- [ ] **Step 2: 精简 `build_shared_prefix`，trip_brief 白名单 + constraints 分层 + stable ordering**

```python
def build_shared_prefix(plan: TravelPlanState) -> str:
    parts = []

    parts.append("## 旅行上下文\n")
    if plan.destination:
        parts.append(f"- 目的地：{plan.destination}")
    if plan.dates:
        parts.append(
            f"- 日期范围：{plan.dates.start} 至 {plan.dates.end}"
            f"（{plan.dates.total_days} 天）"
        )
    if plan.travelers:
        line = f"- 出行人数：{plan.travelers.adults} 成人"
        if plan.travelers.children:
            line += f"、{plan.travelers.children} 儿童"
        parts.append(line)
    if plan.trip_brief:
        _BRIEF_EXCLUDE = {"dates", "total_days", "budget_per_day"}
        _BRIEF_INCLUDE = {"goal", "pace", "departure_city", "style", "must_do", "avoid"}
        parts.append("- 旅行画像（全局）：")
        for key in sorted(plan.trip_brief.keys()):
            if key in _BRIEF_EXCLUDE:
                continue
            if key in _BRIEF_INCLUDE:
                parts.append(f"  - {key}: {plan.trip_brief[key]}")
    if plan.accommodation:
        parts.append(f"- 住宿区域：{plan.accommodation.area}")
        if plan.accommodation.hotel:
            parts.append(f"- 住宿酒店：{plan.accommodation.hotel}")
    if plan.budget:
        parts.append(f"- 总预算：{plan.budget.total} {plan.budget.currency}")
        total_days = plan.dates.total_days if plan.dates else 0
        if total_days > 0:
            daily_avg = round(plan.budget.total / total_days)
            parts.append(f"- 日均参考：约 {daily_avg} {plan.budget.currency}/天")
    if plan.preferences:
        pref_strs = sorted([f"{p.key}: {p.value}" for p in plan.preferences if p.key])
        if pref_strs:
            parts.append(f"- 用户偏好：{'; '.join(pref_strs)}")

    # Only global hard constraints in shared prefix; day-level constraints go to suffix
    if plan.constraints:
        global_constraints = sorted(
            [f"[{c.type}] {c.description}" for c in plan.constraints if c.type == "hard"]
        )
        if global_constraints:
            parts.append(f"- 全局硬约束：{'; '.join(global_constraints)}")

    parts.append("\n---\n")
    parts.append(_WORKER_ROLE)
    parts.append("\n---\n")
    parts.append(_DAYPLAN_SCHEMA)

    return "\n".join(parts)
```

- [ ] **Step 3: 修改 `build_day_suffix`，core_activities 改为方向性线索 + 显示 day_budget + day_constraints + 到达/离开时间**

```python
def build_day_suffix(task: DayTask) -> str:
    parts = [f"\n---\n\n## 你的任务：第 {task.day} 天（{task.date}）\n"]

    sk = task.skeleton_slice
    parts.append("骨架安排：")
    if "area" in sk:
        parts.append(f"- 主区域：{sk['area']}")
    if "theme" in sk:
        parts.append(f"- 主题：{sk['theme']}")
    if "core_activities" in sk:
        activities = sk["core_activities"]
        if isinstance(activities, list):
            parts.append(f"- 方向性活动线索：{'、'.join(str(a) for a in activities)}")
        else:
            parts.append(f"- 方向性活动线索：{activities}")
        parts.append("  （线索仅供参考，具体 POI 由下方 locked_pois / candidate_pois 决定）")
    if "fatigue" in sk:
        parts.append(f"- 疲劳等级：{sk['fatigue']}")
    if "budget_level" in sk:
        parts.append(f"- 预算等级：{sk['budget_level']}")
    if task.day_budget is not None:
        parts.append(f"- 建议日预算：约 {task.day_budget} 元/天（仅供参考，硬性约束以总预算为准）")

    if task.day_constraints:
        parts.append("- 天级别约束：")
        for c in task.day_constraints:
            parts.append(f"  - [{c['type']}] {c['description']}")

    pace = task.pace
    if pace == "relaxed":
        count_range = "2-3"
    elif pace == "intensive":
        count_range = "4-5"
    else:
        count_range = "3-4"
    parts.append(f"\n节奏要求：{pace} → 本天 {count_range} 个核心活动")

    constraint_block = _build_constraint_block(task)
    if constraint_block:
        parts.append(constraint_block)

    parts.append(
        "\n请执行以上 DayTask。"
        "优先补齐核心 POI 的坐标与开放时间；"
        "完成后调用 `submit_day_plan_candidate` 提交候选 DayPlan。"
    )

    return "\n".join(parts)
```

- [ ] **Step 4: 修改 orchestrator `_compile_day_tasks` 注入 day_budget / day_constraints / arrival_time / departure_time**

在 `_compile_day_tasks` 末尾（返回 tasks 之前）加：

```python
    # 5. Inject day budget (soft hint)
    if self.plan.budget and self.plan.dates:
        total_days = self.plan.dates.total_days
        if total_days > 0:
            daily_avg = round(self.plan.budget.total / total_days)
            for t in tasks:
                t.day_budget = daily_avg

    # 5b. Inject day-level (non-hard) constraints
    if self.plan.constraints:
        day_level = [
            {"type": c.type, "description": c.description}
            for c in self.plan.constraints
            if c.type != "hard"
        ]
        if day_level:
            for t in tasks:
                t.day_constraints = day_level

    # 6. Inject arrival/departure times from transport
    transport = self.plan.selected_transport
    if isinstance(transport, dict) and tasks:
        arrival_min = _extract_transport_time(transport, "outbound")
        departure_min = _extract_transport_time(transport, "return")
        if arrival_min is not None and tasks[0].date_role == "arrival_day":
            hh, mm = divmod(arrival_min, 60)
            tasks[0].arrival_time = f"{hh:02d}:{mm:02d}"
        if departure_min is not None and tasks[-1].date_role == "departure_day":
            hh, mm = divmod(departure_min, 60)
            tasks[-1].departure_time = f"{hh:02d}:{mm:02d}"
        # Handle arrival_departure_day (single-day trips)
        if len(tasks) == 1 and tasks[0].date_role == "arrival_departure_day":
            if arrival_min is not None:
                hh, mm = divmod(arrival_min, 60)
                tasks[0].arrival_time = f"{hh:02d}:{mm:02d}"
            if departure_min is not None:
                hh, mm = divmod(departure_min, 60)
                tasks[0].departure_time = f"{hh:02d}:{mm:02d}"
```

- [ ] **Step 5: 更新 `_build_constraint_block` 处理 `arrival_departure_day`**

在 `_build_constraint_block` 的 date_role 分支中增加：

```python
    if task.date_role == "arrival_day":
        lines.append("\n### 🛬 到达日约束")
        if task.arrival_time:
            lines.append(f"- 预计到达时间：{task.arrival_time}")
            lines.append(f"- 首活动开始时间不得早于 {task.arrival_time} + 2 小时")
        else:
            lines.append("- 首活动开始时间须留出至少 2 小时接驳缓冲")
        lines.append("- 建议首活动安排在住宿区域附近，降低接驳风险")
    elif task.date_role == "departure_day":
        lines.append("\n### 🛫 离开日约束")
        if task.departure_time:
            lines.append(f"- 预计出发时间：{task.departure_time}")
            lines.append(f"- 末活动结束时间不得晚于 {task.departure_time} 前 3 小时")
        else:
            lines.append("- 末活动结束时间须留出至少 3 小时前往交通枢纽")
        lines.append("- 建议末活动安排在交通枢纽附近")
    elif task.date_role == "arrival_departure_day":
        lines.append("\n### 🛬🛫 到达+离开日约束")
        if task.arrival_time:
            lines.append(f"- 预计到达时间：{task.arrival_time}")
            lines.append(f"- 首活动不得早于 {task.arrival_time} + 2 小时")
        else:
            lines.append("- 首活动须留出至少 2 小时接驳缓冲")
        if task.departure_time:
            lines.append(f"- 预计出发时间：{task.departure_time}")
            lines.append(f"- 末活动不得晚于 {task.departure_time} 前 3 小时")
        else:
            lines.append("- 末活动须留出至少 3 小时前往交通枢纽")
        lines.append("- 建议只安排住宿附近或交通枢纽附近的轻松活动")
```

- [ ] **Step 6: 更新测试（含 stable ordering 加强版 + day_constraints 注入断言）**

```python
def test_build_shared_prefix_stable_ordering():
    """Different insertion orders should produce identical output."""
    plan1 = _make_plan()
    plan2 = _make_plan()
    # Mutate plan2's trip_brief order by adding an extra key then removing it
    # to ensure sorted() order is deterministic
    prefix1 = build_shared_prefix(plan1)
    prefix2 = build_shared_prefix(plan2)
    assert prefix1 == prefix2


def test_build_shared_prefix_excludes_soft_constraints():
    plan = _make_plan()
    plan.constraints = [
        Constraint(type="hard", description="不去迪士尼"),
        Constraint(type="soft", description="尽量住民宿"),
    ]
    prefix = build_shared_prefix(plan)
    assert "不去迪士尼" in prefix
    assert "尽量住民宿" not in prefix


def test_day_task_new_fields_default():
    task = DayTask(day=1, date="2026-05-01", skeleton_slice={}, pace="balanced")
    assert task.day_budget is None
    assert task.day_constraints == []
    assert task.arrival_time is None
    assert task.departure_time is None


def test_core_activities_labeled_as_directional():
    task = DayTask(
        day=1, date="2026-05-01",
        skeleton_slice={"area": "新宿", "core_activities": ["购物", "美食"]},
        pace="balanced",
    )
    suffix = build_day_suffix(task)
    assert "方向性活动线索" in suffix
    assert "仅供参考" in suffix


def test_suffix_contains_arrival_departure_day():
    task = DayTask(
        day=1, date="2026-05-01", skeleton_slice={}, pace="balanced",
        date_role="arrival_departure_day",
        arrival_time="10:00",
        departure_time="18:00",
    )
    suffix = build_day_suffix(task)
    assert "到达+离开日" in suffix
    assert "10:00" in suffix
    assert "18:00" in suffix
```

在 `test_orchestrator.py` 中增加 day_constraints 注入断言：

```python
def test_compile_day_tasks_injects_day_constraints():
    plan = _make_plan()
    plan.constraints = [
        Constraint(type="hard", description="不去迪士尼"),
        Constraint(type="soft", description="尽量住民宿"),
    ]
    orch = Phase5Orchestrator(plan=plan, ...)
    tasks = orch._compile_day_tasks(base_tasks)
    for t in tasks:
        assert len(t.day_constraints) == 1
        assert t.day_constraints[0]["type"] == "soft"
        assert t.day_constraints[0]["description"] == "尽量住民宿"
```

- [ ] **Step 7: 运行测试**

Run: `cd backend && python -m pytest tests/test_worker_prompt.py tests/test_day_worker.py tests/test_orchestrator.py -v`

- [ ] **Step 8: Commit（含 PROJECT_OVERVIEW 同步）**

```bash
git add backend/agent/phase5/worker_prompt.py backend/agent/phase5/orchestrator.py backend/tests/test_worker_prompt.py backend/tests/test_day_worker.py backend/tests/test_orchestrator.py PROJECT_OVERVIEW.md
git commit -m "refactor(phase5): slim shared prefix, stable ordering, day_constraints injection, DayTask expansion, arrival_departure_day"
```

---

### Task 3: 约束语义分层 — ⛔必须 / ✅优先 / 🚫禁止 + 违反后果 + repair 聚焦 + 禁止来源说明

**Files:**
- Modify: `backend/agent/phase5/worker_prompt.py:170-215`（`_build_constraint_block` 函数）

**问题：** locked_pois/candidate_pois/forbidden_pois 渲染为同一级别 bullet points，无违反后果声明。repair_hints 无聚焦指令。

**方案：** 一次性重写 `_build_constraint_block`，涵盖分层渲染 + 违反后果 + repair 聚焦 + forbidden 来源说明。注意 arrival_departure_day 分支已在 Task 2 Step 5 中添加。

- [ ] **Step 1: 重写 `_build_constraint_block`**

内容与 Task 2 Step 5 中的代码一致（已在上面完整给出）。确认包含：
- ⛔ 必须包含（违反 = DayPlan 无效）
- ✅ 优先选取（候选池）
- 🚫 禁止使用（违反 = 跨天 POI 重复，触发 Orchestrator 重新分配）+ 来源说明
- date_role 四个分支（arrival_day / departure_day / arrival_departure_day / full_day）
- repair_hints 加粗聚焦指令

- [ ] **Step 2: 更新测试断言**

已在 Task 2 Step 6 中给出完整的 `TestDayTaskConstraints` 更新。额外增加：

```python
def test_forbidden_pois_explains_why():
    task = DayTask(
        day=2, date="2026-05-02", skeleton_slice={}, pace="balanced",
        forbidden_pois=["明治神宫"],
    )
    suffix = build_day_suffix(task)
    assert "跨天 POI 重复" in suffix or "已被其他天锁定" in suffix
```

- [ ] **Step 3: 运行测试**

Run: `cd backend && python -m pytest tests/test_worker_prompt.py -v`

- [ ] **Step 4: Commit（含 PROJECT_OVERVIEW 同步）**

```bash
git add backend/agent/phase5/worker_prompt.py backend/tests/test_worker_prompt.py PROJECT_OVERVIEW.md
git commit -m "feat(phase5): hierarchical constraints with violation consequences, repair focus, forbidden source explanation"
```

---

### Task 4: 收口控制改进 — 预防式预算 + 降级模板（不造假坐标）+ 事实性描述 + JSON 修复引导

**Files:**
- Modify: `backend/agent/phase5/day_worker.py:40-52`（3 个收口 prompt 常量）
- Modify: `backend/agent/phase5/day_worker.py:176-179`（initial messages 构建，注入迭代预算）
- Modify: `backend/tests/test_day_worker.py`

**问题：** 1) 前 60% 迭代没有"你有 N 轮预算"预防信息；2) `_FORCED_EMIT_PROMPT` 诊断性描述 + 降级模板教模型写 0,0 造假坐标；3) `_JSON_REPAIR_PROMPT` 没引导复用历史。

**方案：** 1) 初始 user message 注入迭代预算；2) forced emit 改为事实性描述 + 安全降级（不造假坐标）；3) JSON 修复引导复用对话历史 + 优先 submit。

- [ ] **Step 1: 重写 3 个收口 prompt 常量**

```python
_FORCED_EMIT_PROMPT = (
    "同一查询已达到重复上限（2次）或补救链已耗尽（3次）。"
    "请立即停止所有工具调用，基于已有信息提交 DayPlan。\n"
    "若信息确实不全：\n"
    "- 只保留已拿到坐标的 POI，缺少的 POI 不纳入\n"
    "- 缺营业时间：在 notes 标注「请出行前确认营业时间」\n"
    "- 缺票价：cost 写 0，在 notes 标注「票价以现场为准」\n"
    "- 绝不在 location 中填入 0,0 假坐标\n"
    "不要再为了「再查一次」而调用任何工具。"
)

_LATE_EMIT_PROMPT = (
    "你已使用大部分工具调用预算。"
    "请在下一轮提交 DayPlan；如还需 1-2 个工具补齐核心信息可继续，但不要超过 2 个调用就必须提交。"
    "无法确认的事实写入 notes 字段。"
)

_JSON_REPAIR_PROMPT = (
    "你刚才的回复没有触发 submit_day_plan_candidate，也未输出可解析的 DayPlan JSON。\n"
    "请基于上文中已收集的 POI 信息和路线，立即调用 submit_day_plan_candidate 提交。\n"
    "若提交工具返回 SUBMIT_UNAVAILABLE，则在文本里输出符合 schema 的 DayPlan JSON（用 ```json 代码块包裹），"
    "必须包含 day、date、activities 字段。"
)
```

- [ ] **Step 2: 在 `run_day_worker` 中注入迭代预算声明**

```python
    day_suffix = build_day_suffix(task)
    iteration_note = (
        f"\n\n你的工具调用预算：同一查询最多 {_MAX_SAME_QUERY} 次，"
        f"同一 POI 信息最多 {_MAX_POI_RECOVERY} 次，"
        f"总迭代上限 {max_iterations} 轮。"
        f"优先补齐核心 POI 的坐标与开放时间，无需为每个细节反复搜索。"
    )

    messages: list[Message] = [
        Message(role=Role.SYSTEM, content=shared_prefix),
        Message(role=Role.USER, content=day_suffix + iteration_note),
    ]
```

- [ ] **Step 3: 更新测试断言**

```python
# test_run_day_worker_puts_day_task_in_user_message — check iteration budget
def test_run_day_worker_puts_day_task_in_user_message():
    # ... existing setup ...
    result = await run_day_worker(...)
    first_call_messages = llm.calls[0]
    user_content = first_call_messages[1].content
    assert "第 1 天" in user_content
    assert "工具调用预算" in user_content
    assert "请执行以上 DayTask" in user_content
```

- [ ] **Step 4: 运行测试**

Run: `cd backend && python -m pytest tests/test_day_worker.py -v`

- [ ] **Step 5: Commit（含 PROJECT_OVERVIEW 同步）**

```bash
git add backend/agent/phase5/day_worker.py backend/tests/test_day_worker.py PROJECT_OVERVIEW.md
git commit -m "feat(phase5): proactive convergence — iteration budget, safe degrade (no fake coords), factual forced emit, json repair references history"
```

---

### Task 5: 全量回归测试 + 整合验证 + PROJECT_OVERVIEW 最终同步

**Files:**
- All modified files
- `PROJECT_OVERVIEW.md`

- [ ] **Step 1: 运行全部 Phase 5 相关测试**

Run: `cd backend && python -m pytest tests/test_worker_prompt.py tests/test_day_worker.py tests/test_orchestrator.py tests/test_phase5_candidate_store.py tests/test_config_parallel.py -v`

- [ ] **Step 2: 运行 Agent 循环结构测试**

Run: `cd backend && python -m pytest tests/test_agent_loop_structure.py -v`

- [ ] **Step 3: 检查整合正确性**

- `_compile_day_tasks` 中 new fields 注入不冲突（day_budget / day_constraints / arrival_time / departure_time / arrival_departure_day）
- `DayTask` default values 向后兼容
- `_DAYPLAN_SCHEMA` category enum 与 `_SUBMIT_DAY_PLAN_CANDIDATE_SCHEMA` 一致
- `_DAYPLAN_SCHEMA` 负面示例只含结构错误（location 字符串 / cost 字符串 / end_time ≤ start_time / category 非枚举）
- `build_shared_prefix` 不再调用 `_load_soul()`
- preferences/trip_brief 按 sorted key 序列化
- `_FORCED_EMIT_PROMPT` 不教模型写 0,0 假坐标
- `arrival_departure_day` 已在 `_build_constraint_block` 和 `_compile_day_tasks` 中处理

- [ ] **Step 4: 最终同步 PROJECT_OVERVIEW.md**

更新 Phase 5 Worker 提示词部分，补充全部优化要点。

- [ ] **Step 5: Final commit**

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: finalize PROJECT_OVERVIEW sync with phase5 worker prompt optimization"
```

---

## 自查清单

| # | 检查项 | 状态 |
|---|--------|------|
| 1 | 每个 Task 有对应代码变更 | ✅ |
| 2 | 每个 Task 有对应测试 | ✅ |
| 3 | 无 TODO/TBD/placeholder | ✅ |
| 4 | `DayTask` 新字段有默认值，向后兼容 | ✅ |
| 5 | `_build_constraint_block` 覆盖 4 种 date_role（arrival/departure/arrival_departure/full） | ✅ |
| 6 | `_compile_day_tasks` 注入 `day_constraints`（non-hard 约束） | ✅ |
| 7 | `_FORCED_EMIT_PROMPT` 不教模型写 0,0 假坐标 | ✅ |
| 8 | 负面示例只含结构性错误 | ✅ |
| 9 | 每次 commit 同步更新 PROJECT_OVERVIEW.md | ✅ |
| 10 | `_SUBMIT_DAY_PLAN_CANDIDATE_SCHEMA` 和 `_DAYPLAN_SCHEMA` 合并在 Task 0 一起改 | ✅ |
| 11 | stable ordering 测试构造不同输入顺序 | ✅ |
| 12 | `day_constraints` 有注入路径（orchestrator `_compile_day_tasks`） | ✅ |
| 13 | 所有新测试不依赖外部 LLM 调用 | ✅ |