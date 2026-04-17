# Plan JSON 字段去重重构

## 背景

Plan JSON（`TravelPlanState`）中存在三组字段重叠问题：

1. `trip_brief` 内部的 `must_do`/`avoid`/`budget_note` 与顶层 `preferences`/`constraints`/`budget` 语义重复，导致同一信息被写入两处
2. `destination_candidates`（Phase 1 候选目的地）与 `candidate_pool`（Phase 3 候选活动）命名混淆，且前者几乎无下游消费
3. 天数信息散布在 `dates.total_days`、`skeleton_plans[].days`、`daily_plans` 三处（分析后认定不是真正的数据冗余）

## 设计原则

**单一数据源**：每条信息只在一个字段中存储。需要聚合视图的场景由读取侧动态组装（如 `_hydrate_phase3_brief`），不在写入侧重复存储。

**不需要向后兼容**：已有会话数据可以丢弃。

---

## 重构 1：trip_brief 瘦身

### 目标

将 `trip_brief` 从"自由结构混合体"精简为"LLM 分析产出的高层画像身份证"，只保留其他字段无法表达的信息。

### 字段变更

| 字段 | 动作 | 去向 | 理由 |
|------|------|------|------|
| `goal` | 保留 | — | trip_brief 独有，描述旅行目标（如"亲子度假""美食探索"） |
| `pace` | 保留 | — | trip_brief 独有，描述节奏偏好（relaxed/balanced/intensive） |
| `departure_city` | 保留 | — | trip_brief 独有，出发城市 |
| `must_do` | **移除** | `add_preferences(key="must_do", value="...")` | 与 preferences 语义重复 |
| `avoid` | **移除** | `add_constraints(type="hard", description="不安排...")` | 与 constraints 语义重复 |
| `budget_note` | **移除** | `update_trip_basics(budget=...)` + 可选 `add_constraints` | 与 budget 结构化字段重复 |

### 影响范围

#### `set_trip_brief` 工具（`backend/tools/plan_tools/phase3_tools.py`）

更新工具 description，说明：
- 只接受 `goal`、`pace`、`departure_city` 三个标准字段
- must_do 应使用 `add_preferences`
- avoid 应使用 `add_constraints`
- budget 相关信息应使用 `update_trip_basics`

#### Phase 3 brief 子阶段 prompt（`backend/phase/prompts.py`）

更新"状态写入"部分：
- `set_trip_brief(fields={goal, pace, departure_city})` — 只写画像核心
- `add_preferences(key="must_do", value="...")` — 写入 must_do 项
- `add_constraints(type="hard", description="...")` — 写入 avoid 项
- `update_trip_basics(budget=...)` — 写入预算

更新"工具策略"中 trip_brief 的标准字段说明，移除 must_do/avoid/budget_note。

#### PHASE3_BASE_PROMPT 工具职责对照表

新增两行：
- "记录用户必去/必体验项目" → `add_preferences` ✗ `set_trip_brief`
- "记录用户不想要的体验" → `add_constraints` ✗ `set_trip_brief`

#### `_hydrate_phase3_brief`（`backend/phase/router.py`）

**不需要改动**。该函数已经把 preferences/constraints/budget/dates 合并到 trip_brief 视图中注入系统提示。trip_brief 瘦身后，合并逻辑照常工作，LLM 看到的仍然是完整画像。

#### `backend/context/manager.py`

**不需要改动**。已经分别注入 trip_brief KV、preferences 列表、constraints 列表。

#### `backend/agent/loop.py` state repair

brief 子阶段的 repair 提示需要更新，从"调用 set_trip_brief 写入画像"改为"调用 set_trip_brief 写入 goal/pace/departure_city"。

#### 前端 `Phase3Workbench.tsx`

trip_brief 卡片展示的字段变少（goal/pace/departure_city + hydration 注入的 total_days/destination 等），must_do/avoid 不再出现在 trip_brief 卡片中，但 preferences/constraints 已有独立展示区域。无需额外改动。

#### `backend/agent/reflection.py`

Phase 3 lock 自检已经读取 preferences 和 constraints 做检查，不读 trip_brief 中的 must_do/avoid。**不需要改动**。

---

## 重构 2：删除 destination_candidates

### 目标

移除 `destination_candidates` 字段和对应的两个写入工具，消除与 `candidate_pool` 的命名混淆。

### 理由

消费链分析显示该字段几乎无下游消费：
- 前端没有类型定义
- 路由不读取它
- 上下文不注入它
- 仅有 backtrack 清除和 to_dict 序列化

Phase 1 的候选目的地跟踪通过对话上下文完成，不需要持久化到 plan state。

### 影响范围

#### `backend/state/models.py`

- 移除 `destination_candidates: list[dict] = field(default_factory=list)` 字段
- 从 `_PHASE_DOWNSTREAM` 中移除
- 从 `_FIELD_DEFAULTS` 中移除
- 从 `to_dict()` / `from_dict()` 中移除

#### `backend/tools/plan_tools/append_tools.py`

- 移除 `add_destination_candidate` 工具函数
- 移除 `set_destination_candidates` 工具函数

#### `backend/tools/plan_tools/__init__.py`

- 从 `make_all_plan_tools()` 和 `PLAN_WRITER_TOOL_NAMES` 中移除这两个工具
- 工具总数从 19 个降到 17 个

#### `backend/main.py`

- 从 tool→field 映射表中移除 `add_destination_candidate` / `set_destination_candidates` 条目

#### `backend/state/plan_writers.py`

- 移除 `append_destination_candidate` 和 `replace_destination_candidates` 两个底层写入函数

#### Phase 1 prompt（`backend/phase/prompts.py`）

- 当前 Phase 1 prompt 没有提及这两个工具名，不需要改动。

#### `PROJECT_OVERVIEW.md`

- 更新工具清单，移除这两个工具
- 更新工具总数描述（19 → 17）

#### 测试文件清理

| 文件 | 改动 |
|------|------|
| `backend/tests/test_plan_writers.py` | 删除 destination_candidates 追加/替换测试用例 |
| `backend/tests/test_phase1_tool_boundaries.py` | 删除 `make_set_destination_candidates_tool` 导入和 `test_destination_candidates_append_or_replace` |
| `backend/tests/test_plan_tools/test_backtrack.py` | 清理含 destination_candidates 的 fixture |
| `backend/tests/test_phase_integration.py` | 删除 `destination_candidates == []` 断言 |
| `backend/tests/test_error_paths.py` | 删除 `destination_candidates == []` 断言 |

---

## 重构 3：天数信息 — 仅加注释

### 结论

`dates.total_days`（计算属性）、`skeleton_plans[].days`（骨架排布）、`daily_plans`（详细行程）不是数据冗余，而是同一概念在不同阶段的不同粒度表达。现有一致性校验已覆盖。

### 唯一改动

在 `_hydrate_phase3_brief` 中注入 `total_days` 的位置加注释：

```python
# 视图聚合：权威来源是 dates.total_days，此处仅为 LLM 上下文便利注入
brief["total_days"] = plan.dates.total_days
```

---

## 不改动的部分

| 模块 | 理由 |
|------|------|
| `context/manager.py` 系统提示构建 | 已经分别注入 trip_brief、preferences、constraints，不受影响 |
| `_hydrate_phase3_brief` 合并逻辑 | trip_brief 瘦身后合并照常工作 |
| `harness/validator.py` | 只读 budget、dates，不读 trip_brief |
| `agent/reflection.py` | 读 preferences/constraints，不读 trip_brief 中的重叠字段 |
| 前端 Phase3Workbench | trip_brief 卡片自动适应字段变少；preferences/constraints 有独立展示 |
| `dates`/`budget`/`travelers` 顶层字段 | 权威数据源，不动 |
| `candidate_pool`/`shortlist` | 语义清晰，不动 |

---

## 测试策略

1. 现有 `backend/tests/test_plan_tools/` 中的测试需要更新：移除 destination_candidates 相关测试用例
2. `infer_phase3_step_from_state` 测试不受影响（不依赖 destination_candidates）
3. 新增测试：验证 `set_trip_brief` 只接受 goal/pace/departure_city（拒绝 must_do/avoid/budget_note）
4. 端到端验证：通过一次完整 Phase 1→3 对话确认流程正常
