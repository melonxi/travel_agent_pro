# Plan JSON 字段去重重构 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 Plan JSON 中的字段语义重叠：trip_brief 瘦身、删除 destination_candidates 字段及对应工具、天数字段加注释。

**Architecture:** 自底向上修改——先删底层 writer 函数和数据模型字段，再删工具层和注册层，最后更新 prompt 和测试。每个 Task 产出可独立验证的变更。

**Tech Stack:** Python 3.12, pytest, dataclasses

---

### Task 1: 删除 destination_candidates 底层 writer 函数

**Files:**
- Modify: `backend/state/plan_writers.py:228-237`

- [ ] **Step 1: 删除 `append_destination_candidate` 和 `replace_destination_candidates` 函数**

从 `backend/state/plan_writers.py` 中删除以下代码（第 228-237 行）：

```python
def append_destination_candidate(plan: TravelPlanState, item: dict) -> None:
    """Append a single destination candidate."""
    assert isinstance(item, dict), f"Expected dict, got {type(item).__name__}"
    plan.destination_candidates.append(item)


def replace_destination_candidates(plan: TravelPlanState, items: list[dict]) -> None:
    """Replace the entire destination_candidates list."""
    assert isinstance(items, list), f"Expected list, got {type(items).__name__}"
    plan.destination_candidates = items
```

- [ ] **Step 2: 运行 plan_writers 相关测试确认删除生效**

Run: `cd backend && python -m pytest tests/test_plan_writers.py -v -k "destination" 2>&1 | head -20`
Expected: 测试报错（ImportError 或 AttributeError），因为测试还在引用已删除的函数。这是预期行为，后续 Task 会清理测试。

- [ ] **Step 3: Commit**

```bash
git add backend/state/plan_writers.py
git commit -m "refactor: remove destination_candidates writer functions"
```

---

### Task 2: 删除 destination_candidates 工具函数

**Files:**
- Modify: `backend/tools/plan_tools/append_tools.py:1-11, 91-213`

- [ ] **Step 1: 删除导入**

在 `backend/tools/plan_tools/append_tools.py` 第 6-11 行，将 import 块从：

```python
from state.plan_writers import (
    append_constraints,
    append_destination_candidate,
    append_preferences,
    replace_destination_candidates,
)
```

改为：

```python
from state.plan_writers import (
    append_constraints,
    append_preferences,
)
```

- [ ] **Step 2: 删除 destination_candidate 参数 schema**

删除第 91-112 行的 `_DESTINATION_CANDIDATE_PARAMS` 和 `_SET_DESTINATION_CANDIDATES_PARAMS`：

```python
_DESTINATION_CANDIDATE_PARAMS = {
    "type": "object",
    "properties": {
        "item": {
            "type": "object",
            "description": "单个目的地候选对象",
        }
    },
    "required": ["item"],
}

_SET_DESTINATION_CANDIDATES_PARAMS = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": "object"},
            "description": "完整目的地候选列表",
        }
    },
    "required": ["items"],
}
```

- [ ] **Step 3: 删除两个工具工厂函数**

删除第 164-213 行的 `make_add_destination_candidate_tool` 和 `make_set_destination_candidates_tool` 函数。

- [ ] **Step 4: Commit**

```bash
git add backend/tools/plan_tools/append_tools.py
git commit -m "refactor: remove destination_candidates tool functions"
```

---

### Task 3: 更新工具注册和导出

**Files:**
- Modify: `backend/tools/plan_tools/__init__.py`

- [ ] **Step 1: 删除导入**

在 `backend/tools/plan_tools/__init__.py` 第 6-11 行，将 import 块从：

```python
from .append_tools import (
    make_add_constraints_tool,
    make_add_destination_candidate_tool,
    make_add_preferences_tool,
    make_set_destination_candidates_tool,
)
```

改为：

```python
from .append_tools import (
    make_add_constraints_tool,
    make_add_preferences_tool,
)
```

- [ ] **Step 2: 从 `PLAN_WRITER_TOOL_NAMES` 中删除两个条目**

在 `PLAN_WRITER_TOOL_NAMES` set 中删除：

```python
    "add_destination_candidate",
    "set_destination_candidates",
```

删除后集合应有 17 个元素。

- [ ] **Step 3: 从 `make_all_plan_tools` 中删除两行**

删除：

```python
        make_add_destination_candidate_tool(plan),
```

和：

```python
        make_set_destination_candidates_tool(plan),
```

- [ ] **Step 4: 从 `__all__` 中删除两个条目**

删除：

```python
    "make_add_destination_candidate_tool",
```

和：

```python
    "make_set_destination_candidates_tool",
```

- [ ] **Step 5: Commit**

```bash
git add backend/tools/plan_tools/__init__.py
git commit -m "refactor: unregister destination_candidates tools (19→17)"
```

---

### Task 4: 从 main.py 删除 tool→field 映射

**Files:**
- Modify: `backend/main.py:287-294`

- [ ] **Step 1: 删除映射条目**

在 `backend/main.py` 第 287-294 行，删除：

```python
        "add_destination_candidate": (
            "destination_candidates",
            arguments.get("item"),
        ),
        "set_destination_candidates": (
            "destination_candidates",
            arguments.get("items"),
        ),
```

- [ ] **Step 2: Commit**

```bash
git add backend/main.py
git commit -m "refactor: remove destination_candidates from tool→field mapping"
```

---

### Task 5: 从数据模型中删除 destination_candidates 字段

**Files:**
- Modify: `backend/state/models.py:278, 315, 377, 414, 461`

- [ ] **Step 1: 从 `_PHASE_DOWNSTREAM` 中删除**

在第 278 行，删除 phase 1 下游列表中的：

```python
        "destination_candidates",
```

- [ ] **Step 2: 从 `_FIELD_DEFAULTS` 中删除**

在第 315 行，删除：

```python
    "destination_candidates": [],
```

- [ ] **Step 3: 从 `TravelPlanState` dataclass 中删除字段**

在第 377 行，删除：

```python
    destination_candidates: list[dict] = field(default_factory=list)
```

- [ ] **Step 4: 从 `to_dict()` 中删除**

在第 414 行，删除：

```python
            "destination_candidates": self.destination_candidates,
```

- [ ] **Step 5: 从 `from_dict()` 中删除**

在第 461 行，删除：

```python
            destination_candidates=d.get("destination_candidates", []),
```

- [ ] **Step 6: Commit**

```bash
git add backend/state/models.py
git commit -m "refactor: remove destination_candidates field from TravelPlanState"
```

---

### Task 6: 清理测试 — destination_candidates 相关

**Files:**
- Modify: `backend/tests/test_plan_writers.py:399-413`
- Modify: `backend/tests/test_plan_tools/test_append_tools.py:1-12, 204-275`
- Modify: `backend/tests/test_plan_tools/test_init.py:9-29, 39-42`
- Modify: `backend/tests/test_phase1_tool_boundaries.py:21-23, 317-335`
- Modify: `backend/tests/test_plan_tools/test_backtrack.py:15, 86, 101`
- Modify: `backend/tests/test_phase_integration.py:555`
- Modify: `backend/tests/test_error_paths.py:151`

- [ ] **Step 1: 删除 test_plan_writers.py 中的两个测试类**

删除 `TestAppendDestinationCandidate`（第 399-404 行）和 `TestReplaceDestinationCandidates`（第 407-413 行）两个类。

- [ ] **Step 2: 清理 test_append_tools.py**

a) 更新导入（第 7-12 行），从：

```python
from tools.plan_tools.append_tools import (
    make_add_constraints_tool,
    make_add_destination_candidate_tool,
    make_add_preferences_tool,
    make_set_destination_candidates_tool,
)
```

改为：

```python
from tools.plan_tools.append_tools import (
    make_add_constraints_tool,
    make_add_preferences_tool,
)
```

b) 删除 `TestAddDestinationCandidateTool` 类（第 204-232 行）和 `TestSetDestinationCandidatesTool` 类（第 235-275 行）。

- [ ] **Step 3: 更新 test_init.py**

a) 从 `EXPECTED_TOOL_NAMES` 列表中删除 `"add_destination_candidate"` 和 `"set_destination_candidates"`（第 11, 13 行）。

b) 将工具总数断言从 19 改为 17（第 39, 41 行）：

```python
    assert len(tools) == 17
    ...
    assert len({tool.name for tool in tools}) == 17
```

- [ ] **Step 4: 清理 test_phase1_tool_boundaries.py**

a) 删除导入（第 21-23 行）：

```python
from tools.plan_tools.append_tools import (
    make_set_destination_candidates_tool,
)
```

b) 删除整个 `test_destination_candidates_append_or_replace` 测试函数（第 317-335 行）。

- [ ] **Step 5: 清理 test_backtrack.py**

a) 在 `_make_plan` fixture（第 15 行）中删除：

```python
        destination_candidates=[{"name": "Tokyo"}, {"name": "Osaka"}],
```

b) 在 `test_request_backtrack_success_clears_downstream_and_records_history`（第 86 行）中删除断言：

```python
    assert plan.destination_candidates == [{"name": "Tokyo"}, {"name": "Osaka"}]
```

c) 在 `test_request_backtrack_adjusts_phase_two_to_phase_one`（第 101 行）中删除断言：

```python
    assert plan.destination_candidates == []
```

- [ ] **Step 6: 清理 test_phase_integration.py**

删除第 555 行：

```python
    assert plan_data["destination_candidates"] == []
```

- [ ] **Step 7: 清理 test_error_paths.py**

删除第 151 行：

```python
    assert updated_plan.destination_candidates == []
```

- [ ] **Step 8: 运行全部测试**

Run: `cd backend && python -m pytest tests/ -v --tb=short 2>&1 | tail -30`
Expected: 所有测试通过，无 destination_candidates 相关错误。

- [ ] **Step 9: Commit**

```bash
git add backend/tests/
git commit -m "test: remove all destination_candidates test cases"
```

---

### Task 7: trip_brief 瘦身 — 更新 set_trip_brief 工具描述

**Files:**
- Modify: `backend/tools/plan_tools/phase3_tools.py:756-785`

- [ ] **Step 1: 更新 `make_set_trip_brief_tool` 的 description**

在 `backend/tools/plan_tools/phase3_tools.py` 第 759-763 行，将 description 从：

```python
        description=(
            "更新旅行画像（增量合并到现有 trip_brief）。\n"
            "触发条件：收集到用户的旅行目标、节奏偏好、出发城市、必去/不去等画像信息后必须立即调用。\n"
            "禁止行为：此工具只用于写旅行画像，不要用它记录骨架选择（应用 select_skeleton）、候选池（应用 set_candidate_pool）或其他非画像信息。\n"
            "写入后效果：trip_brief 增量合并，brief 子阶段完成的必要条件。"
        ),
```

改为：

```python
        description=(
            "更新旅行画像（增量合并到现有 trip_brief）。\n"
            "标准字段：goal（旅行目标）、pace（relaxed/balanced/intensive）、departure_city（出发城市）。\n"
            "禁止写入：must_do/avoid 应使用 add_preferences / add_constraints；预算应使用 update_trip_basics。\n"
            "禁止行为：不要用此工具记录骨架选择（应用 select_skeleton）、候选池（应用 set_candidate_pool）或其他非画像信息。\n"
            "写入后效果：trip_brief 增量合并，brief 子阶段完成的必要条件。"
        ),
```

- [ ] **Step 2: Commit**

```bash
git add backend/tools/plan_tools/phase3_tools.py
git commit -m "refactor: narrow set_trip_brief to goal/pace/departure_city only"
```

---

### Task 8: trip_brief 瘦身 — 更新 brief 子阶段 prompt

**Files:**
- Modify: `backend/phase/prompts.py:262-296`

- [ ] **Step 1: 更新"状态写入"部分**

在 `backend/phase/prompts.py` 的 `PHASE3_STEP_PROMPTS["brief"]` 中，将第 262-274 行的"状态写入"部分从：

```python
## 状态写入

- 用户明确表达的日期、预算、人数、偏好、约束，必须立即写入对应状态字段。
- 当你已经拿到足够信息形成旅行画像后，调用 `set_trip_brief(fields={...})` 写入 brief。
- trip_brief 写入时，使用以下标准字段名（前端和后续阶段依赖这些 key 稳定消费）：
  - `goal`：旅行目标（如"亲子度假""美食探索"）
  - `pace`：节奏偏好（`relaxed` / `balanced` / `intensive`）
  - `departure_city`：出发城市
  - `must_do`：必去/必体验项目
  - `avoid`：不想要的体验
  - `budget_note`：预算相关说明
  不要用 `from_city`、`depart_from`、`出发地` 等自创字段名替代上述标准名。
- brief 形成后，系统会自动推进到 `candidate` 子阶段，你不需要手动更新 `phase3_step`。
```

改为：

```python
## 状态写入

- 用户明确表达的日期、预算、人数、偏好、约束，必须立即写入对应状态字段。
- 当你已经拿到足够信息形成旅行画像后，按以下分工写入：
  - `set_trip_brief(fields={goal, pace, departure_city})` — 只写画像核心三字段
  - `add_preferences(items=[{key: "must_do", value: "..."}])` — 写入必去/必体验项目
  - `add_constraints(items=[{type: "hard", description: "不要..."}])` — 写入不去/不想要的体验
  - `update_trip_basics(budget=...)` — 写入预算
- trip_brief 标准字段名（前端和后续阶段依赖这些 key 稳定消费）：
  - `goal`：旅行目标（如"亲子度假""美食探索"）
  - `pace`：节奏偏好（`relaxed` / `balanced` / `intensive`）
  - `departure_city`：出发城市
  不要用 `from_city`、`depart_from`、`出发地` 等自创字段名替代上述标准名。
  不要把 must_do、avoid、budget_note 写进 trip_brief — 它们有各自的专用字段。
- brief 形成后，系统会自动推进到 `candidate` 子阶段，你不需要手动更新 `phase3_step`。
```

- [ ] **Step 2: Commit**

```bash
git add backend/phase/prompts.py
git commit -m "refactor: update brief prompt to split must_do/avoid/budget out of trip_brief"
```

---

### Task 9: 更新 PHASE3_BASE_PROMPT 工具职责对照表

**Files:**
- Modify: `backend/phase/prompts.py:181-194`

- [ ] **Step 1: 在工具职责对照表中新增两行**

在 `backend/phase/prompts.py` 第 194 行（`| 锁定用户确认的住宿 | set_accommodation | ✗ set_accommodation_options |` 之后），追加：

```
| 记录用户必去/必体验项目 | add_preferences | ✗ set_trip_brief |
| 记录用户不想要的体验 | add_constraints | ✗ set_trip_brief |
```

- [ ] **Step 2: Commit**

```bash
git add backend/phase/prompts.py
git commit -m "refactor: add must_do/avoid routing to tool responsibility table"
```

---

### Task 10: 更新 loop.py state repair 提示

**Files:**
- Modify: `backend/agent/loop.py:736-741`

- [ ] **Step 1: 更新 brief repair 提示文本**

在 `backend/agent/loop.py` 第 736-742 行，将 repair 提示从：

```python
            return (
                "[状态同步提醒]\n"
                "你刚刚已经完成了旅行画像说明，但 `trip_brief` 仍为空。"
                "请先调用 `set_trip_brief(fields={...})`"
                " 写入结构化 brief；如果日期、预算、人数、偏好、约束是用户明确说过的，"
                "也要用 `update_trip_basics` 和 `add_preferences` / `add_constraints` 补写。"
                "写完后再继续，不要重复整段面向用户解释。"
            )
```

改为：

```python
            return (
                "[状态同步提醒]\n"
                "你刚刚已经完成了旅行画像说明，但 `trip_brief` 仍为空。"
                "请先调用 `set_trip_brief(fields={goal, pace, departure_city})`"
                " 写入画像核心字段；must_do 用 `add_preferences` 写入，"
                "avoid 用 `add_constraints` 写入，预算用 `update_trip_basics` 写入。"
                "写完后再继续，不要重复整段面向用户解释。"
            )
```

- [ ] **Step 2: Commit**

```bash
git add backend/agent/loop.py
git commit -m "refactor: update brief state repair to match trip_brief slim schema"
```

---

### Task 11: 天数字段加注释 + router.py

**Files:**
- Modify: `backend/phase/router.py:26`

- [ ] **Step 1: 在 total_days 注入位置加注释**

在 `backend/phase/router.py` 第 26 行前加注释：

```python
            # 视图聚合：权威来源是 dates.total_days，此处仅为 LLM 上下文便利注入
            brief.setdefault("total_days", plan.dates.total_days)
```

- [ ] **Step 2: Commit**

```bash
git add backend/phase/router.py
git commit -m "docs: annotate total_days injection as view-only aggregation"
```

---

### Task 12: 更新 PROJECT_OVERVIEW.md

**Files:**
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: 更新工具总数和工具清单**

a) 将所有 "19 个" 改为 "17 个"（出现在第 223、238、246、357 行附近）。

b) 在第 250 行的工具清单中，将：

```
| Phase 1 / 共用基础 | `update_trip_basics`、`add_preferences`、`add_constraints`、`add_destination_candidate`、`set_destination_candidates` |
```

改为：

```
| Phase 1 / 共用基础 | `update_trip_basics`、`add_preferences`、`add_constraints` |
```

c) 在第 238 行附近的 `tools.plan_tools.append_tools` 描述中，移除 "追加或整体替换 destination_candidates" 相关文字。

- [ ] **Step 2: Commit**

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: update PROJECT_OVERVIEW for destination_candidates removal (19→17 tools)"
```

---

### Task 13: 最终验证

**Files:** 无新改动

- [ ] **Step 1: 运行全部测试**

Run: `cd backend && python -m pytest tests/ -v --tb=short 2>&1 | tail -40`
Expected: 所有测试通过。

- [ ] **Step 2: 全局搜索 destination_candidates 残留引用**

Run: `cd backend && grep -rn "destination_candidate" --include="*.py" .`
Expected: 无输出（零残留引用）。

- [ ] **Step 3: 全局搜索 trip_brief 中的 must_do/avoid/budget_note 残留**

Run: `grep -rn "must_do\|budget_note" --include="*.py" backend/`
Expected: 仅在 prompts.py 的"不要把 must_do... 写进 trip_brief"否定指令中出现，不在工具定义或模型中出现。

- [ ] **Step 4: 确认工具总数**

Run: `cd backend && python -c "from state.models import TravelPlanState; from tools.plan_tools import make_all_plan_tools, PLAN_WRITER_TOOL_NAMES; p = TravelPlanState(session_id='verify'); tools = make_all_plan_tools(p); print(f'tools={len(tools)}, names={len(PLAN_WRITER_TOOL_NAMES)}'); assert len(tools) == 17; assert len(PLAN_WRITER_TOOL_NAMES) == 17; print('OK')"`
Expected: `tools=17, names=17` + `OK`
