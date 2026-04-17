# Phase 5 状态契约修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Phase 5 计划工具错误事故中暴露的状态契约不一致、工具参数错误不可诊断、上下文信号冲突、持久化不完整四类问题。

**Architecture:** 修改范围集中在 6 个后端文件：`state/models.py`（天数语义）、`phase/router.py`（门控校验 + trip_brief 修复）、`tools/engine.py`（TypeError 结构化）、`context/manager.py`（trip_brief 过滤）、`main.py`（增量持久化）。每个修复独立可测试，按优先级 A→B→C 排序。

**Tech Stack:** Python 3.12+, pytest, pytest-asyncio

**Postmortem Reference:** `docs/postmortems/2026-04-17-phase5-plan-tool-errors.md`

---

### Task 1: 修复 DateRange.total_days 为 inclusive 自然日语义

**优先级：A**

**Files:**
- Modify: `backend/state/models.py:45-51`
- Modify: `backend/tests/test_state_models.py:28-29`

**背景：** 当前 `total_days = (end - start).days`，对 `2026-05-24` 到 `2026-05-30` 返回 6。但旅行规划中"5/24 到 5/30"覆盖 7 个自然日（24、25、26、27、28、29、30），骨架生成器也按此语义生成了 7 天。改为 `(end - start).days + 1` 统一语义。

- [ ] **Step 1: 更新 test_state_models.py 中的 total_days 断言**

文件：`backend/tests/test_state_models.py:28-29`

将：
```python
dr = DateRange(start="2026-04-10", end="2026-04-15")
assert dr.total_days == 5
```

改为：
```python
dr = DateRange(start="2026-04-10", end="2026-04-15")
assert dr.total_days == 6  # inclusive: 10,11,12,13,14,15 = 6 天
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_state_models.py::test_date_range_total_days -v`（在 `backend/` 目录下）
Expected: FAIL，`assert 5 == 6`

- [ ] **Step 3: 修改 DateRange.total_days 实现**

文件：`backend/state/models.py:45-51`

将：
```python
@property
def total_days(self) -> int:
    from datetime import date as dt_date

    s = dt_date.fromisoformat(self.start)
    e = dt_date.fromisoformat(self.end)
    return (e - s).days
```

改为：
```python
@property
def total_days(self) -> int:
    """覆盖 start 到 end 的自然日数量（inclusive 两端）。"""
    from datetime import date as dt_date

    s = dt_date.fromisoformat(self.start)
    e = dt_date.fromisoformat(self.end)
    return (e - s).days + 1
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_state_models.py::test_date_range_total_days -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/state/models.py backend/tests/test_state_models.py
git commit -m "fix: DateRange.total_days 改为 inclusive 自然日语义（+1）"
```

---

### Task 2: 更新因 total_days +1 导致失败的全部测试

**优先级：A**

**Files:**
- Modify: `backend/tests/test_phase_router.py`
- Modify: `backend/tests/test_plan_writers.py`
- Modify: `backend/tests/test_e2e_golden_path.py`
- Modify: `backend/tests/test_plan_tools/test_daily_plans.py`
- Modify: `backend/tests/test_appendix_issues.py`
- Modify: `backend/tests/test_phase_integration.py`
- 可能还有其他文件（见 Step 1 的全量测试运行结果）

**策略：** 先全量运行测试找出所有失败，再逐个修复。修复方式分两类：
1. **直接断言 total_days 数值的测试**：更新断言值（+1）。
2. **通过 daily_plans 数量判断 phase 的测试**：增加 daily_plans 数量以匹配新的 total_days，或调整 DateRange 使 total_days 保持原值。

- [ ] **Step 1: 全量运行测试，收集所有失败**

Run: `pytest tests/ -x --tb=short 2>&1 | head -100`（在 `backend/` 目录下）

记录所有失败的测试用例名和文件位置。

- [ ] **Step 2: 修复 test_phase_router.py**

需要修复的关键位置：

**Line 62-67**（`test_infer_phase_plans_complete`）：当前 `DateRange(start="2026-04-10", end="2026-04-15")` 总天数从 5 变为 6，需要 6 个 DayPlan 才能进入 phase 7：

```python
def test_infer_phase_plans_complete(router):
    plan = TravelPlanState(
        session_id="s1",
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        selected_skeleton_id="balanced",
        accommodation=Accommodation(area="祇園"),
        daily_plans=[DayPlan(day=i + 1, date=f"2026-04-{10 + i}") for i in range(6)],
    )
    assert router.infer_phase(plan) == 7
```

注意：原来用 `range(5)` 生成 day=0..4，改为 `range(6)` 生成 day=1..6（用 `i + 1` 确保 day 从 1 开始）。

**Line 100-104**（`test_sync_phase_state_hydrates_minimal_trip_brief_from_explicit_state`）：

```python
assert plan.trip_brief["total_days"] == 6  # 原来是 5
```

- [ ] **Step 3: 修复 test_plan_writers.py**

**Line 254, 261**：`total_days == 4` → `total_days == 5`。

找到使用 `DateRange` 的上下文，确认 dates 是什么值，然后更新断言。

- [ ] **Step 4: 修复 test_e2e_golden_path.py**

**Line 163**：`assert plan.dates.total_days == 5` → `== 6`
**Line 167**：`assert plan.trip_brief["total_days"] == 5` → `== 6`

需要确认 golden path 中构建的 daily_plans 数量是否也需要同步增加。

- [ ] **Step 5: 修复 test_plan_tools/test_daily_plans.py**

**Line 159-167**（`test_append_day_plan_rejects_day_beyond_trip_length`）：
`DateRange(start='2026-05-01', end='2026-05-04')` 从 3 天变为 4 天，`day=4` 不再被拒绝。改为 `day=5`：

```python
async def test_append_day_plan_rejects_day_beyond_trip_length(self):
    plan = _make_plan()
    plan.dates = DateRange(start='2026-05-01', end='2026-05-04')
    tool_fn = make_append_day_plan_tool(plan)

    with pytest.raises(ToolError, match='day') as exc_info:
        await tool_fn(day=5, date='2026-05-05', activities=[_sample_activity()])

    assert exc_info.value.error_code == 'INVALID_VALUE'
```

**Line 303-318**（`test_replace_daily_plans_rejects_day_beyond_trip_length`）：
同理，将 `day=4` 改为 `day=5`：

```python
async def test_replace_daily_plans_rejects_day_beyond_trip_length(self):
    plan = _make_plan()
    plan.dates = DateRange(start='2026-05-01', end='2026-05-04')
    tool_fn = make_replace_daily_plans_tool(plan)

    with pytest.raises(ToolError, match='day') as exc_info:
        await tool_fn(
            days=[
                {'day': 1, 'date': '2026-05-01', 'activities': [_sample_activity()]},
                {'day': 2, 'date': '2026-05-02', 'activities': [_sample_activity()]},
                {'day': 5, 'date': '2026-05-05', 'activities': [_sample_activity()]},
            ]
        )

    assert exc_info.value.error_code == 'INVALID_VALUE'
```

- [ ] **Step 6: 修复 test_appendix_issues.py 和 test_phase_integration.py**

根据 Step 1 的测试失败结果修复。核心模式相同：
- 如果测试依赖 `len(daily_plans) >= total_days` 进入 phase 7，增加 daily_plans 数量。
- 如果测试直接断言 total_days 数值，+1。

- [ ] **Step 7: 修复其余失败测试**

根据 Step 1 的完整失败列表，逐一修复。每个修复遵循同样的模式。

- [ ] **Step 8: 全量测试验证**

Run: `pytest tests/ -v --tb=short`（在 `backend/` 目录下）
Expected: ALL PASS

- [ ] **Step 9: Commit**

```bash
git add backend/tests/
git commit -m "test: 更新所有测试适配 inclusive total_days 语义"
```

---

### Task 3: Phase 3→5 门控校验骨架天数一致性

**优先级：A**

**Files:**
- Modify: `backend/phase/router.py:64-72`
- Modify: `backend/tests/test_phase_router.py`

**背景：** 当前 `infer_phase()` 在 `selected_skeleton_id` 存在且 `accommodation` 已设置时直接进入 Phase 5，不校验骨架天数是否等于 `total_days`。需要增加门控：骨架天数不一致时保留在 Phase 3 并记录警告。

- [ ] **Step 1: 在 test_phase_router.py 添加骨架天数不一致的测试**

在文件末尾添加：

```python
def test_infer_phase_blocks_phase5_when_skeleton_days_mismatch(router):
    """骨架天数与 total_days 不一致时，不应进入 Phase 5。"""
    plan = TravelPlanState(
        session_id="s1",
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),  # 6 天 (inclusive)
        selected_skeleton_id="plan_a",
        skeleton_plans=[{"id": "plan_a", "days": [{"day": i} for i in range(1, 8)]}],  # 7 天
        accommodation=Accommodation(area="祇園"),
    )
    assert router.infer_phase(plan) == 3  # 不进入 5


def test_infer_phase_allows_phase5_when_skeleton_days_match(router):
    """骨架天数与 total_days 一致时，正常进入 Phase 5。"""
    plan = TravelPlanState(
        session_id="s1",
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),  # 6 天 (inclusive)
        selected_skeleton_id="plan_a",
        skeleton_plans=[{"id": "plan_a", "days": [{"day": i} for i in range(1, 7)]}],  # 6 天
        accommodation=Accommodation(area="祇園"),
    )
    assert router.infer_phase(plan) == 5
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_phase_router.py::test_infer_phase_blocks_phase5_when_skeleton_days_mismatch -v`
Expected: FAIL，`assert 5 == 3`（当前没有门控，会返回 5）

- [ ] **Step 3: 实现骨架天数门控**

文件：`backend/phase/router.py:64-72`

将：
```python
def infer_phase(self, plan: TravelPlanState) -> int:
    self.sync_phase_state(plan)
    if not plan.destination:
        return 1
    if not plan.dates or not plan.selected_skeleton_id or not plan.accommodation:
        return 3
    if len(plan.daily_plans) < plan.dates.total_days:
        return 5
    return 7
```

改为：
```python
def infer_phase(self, plan: TravelPlanState) -> int:
    self.sync_phase_state(plan)
    if not plan.destination:
        return 1
    if not plan.dates or not plan.selected_skeleton_id or not plan.accommodation:
        return 3
    # 门控：骨架天数必须与权威 total_days 一致才能进入 Phase 5
    if not self._skeleton_days_match(plan):
        return 3
    if len(plan.daily_plans) < plan.dates.total_days:
        return 5
    return 7
```

在类中添加辅助方法（在 `infer_phase` 方法之后）：

```python
def _skeleton_days_match(self, plan: TravelPlanState) -> bool:
    """检查已选骨架的天数是否与 plan.dates.total_days 一致。"""
    if not plan.selected_skeleton_id or not plan.skeleton_plans or not plan.dates:
        return True  # 无法校验时放行，由其他条件控制
    for skeleton in plan.skeleton_plans:
        if not isinstance(skeleton, dict):
            continue
        if skeleton.get("id") == plan.selected_skeleton_id or skeleton.get("name") == plan.selected_skeleton_id:
            days = skeleton.get("days")
            if isinstance(days, list):
                return len(days) == plan.dates.total_days
            return True  # 骨架没有 days 字段时放行
    return True  # 未找到匹配骨架时放行
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_phase_router.py -v`
Expected: ALL PASS（包括新测试和已有测试）

- [ ] **Step 5: Commit**

```bash
git add backend/phase/router.py backend/tests/test_phase_router.py
git commit -m "fix: Phase 3→5 门控拒绝骨架天数与 total_days 不一致"
```

---

### Task 4: 修复 trip_brief 中的 stale 日期字段

**优先级：B**

**Files:**
- Modify: `backend/phase/router.py:23-49`
- Modify: `backend/tests/test_phase_router.py`

**背景：** `_hydrate_phase3_brief()` 使用 `setdefault` 注入 `dates` 和 `total_days`，这意味着如果 `trip_brief` 中已有旧值则不会更新。当用户修改日期后，trip_brief 中的日期会 stale，给模型发送冲突信号。改为强制覆盖。

- [ ] **Step 1: 添加 stale trip_brief 覆盖测试**

在 `test_phase_router.py` 末尾添加：

```python
def test_hydrate_phase3_brief_overwrites_stale_dates(router):
    """trip_brief 中的旧日期应被权威 plan.dates 覆盖。"""
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        destination="Kyoto",
        dates=DateRange(start="2026-05-24", end="2026-05-30"),
        trip_brief={
            "destination": "Kyoto",
            "dates": {"start": "2026-05-10", "end": "2026-05-16"},
            "total_days": 6,
        },
    )
    router.sync_phase_state(plan)
    assert plan.trip_brief["dates"] == {"start": "2026-05-24", "end": "2026-05-30"}
    assert plan.trip_brief["total_days"] == 7  # inclusive: 24-30
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_phase_router.py::test_hydrate_phase3_brief_overwrites_stale_dates -v`
Expected: FAIL，旧值未被覆盖

- [ ] **Step 3: 修改 _hydrate_phase3_brief 使用强制覆盖**

文件：`backend/phase/router.py:23-49`

将 `setdefault` 改为直接赋值（仅限 `dates` 和 `total_days` 这两个权威字段）：

```python
def _hydrate_phase3_brief(self, plan: TravelPlanState) -> None:
    if plan.phase < 3 or not plan.destination:
        return

    brief = dict(plan.trip_brief)
    brief.setdefault("destination", plan.destination)
    if plan.dates:
        # 权威字段：强制覆盖，防止 stale 值误导模型
        brief["dates"] = plan.dates.to_dict()
        brief["total_days"] = plan.dates.total_days
    if plan.travelers:
        brief.setdefault("travelers", plan.travelers.to_dict())
    if plan.budget:
        brief.setdefault("budget", plan.budget.to_dict())
    if plan.preferences:
        brief.setdefault(
            "preferences",
            [p.to_dict() for p in plan.preferences],
        )
    if plan.constraints:
        brief.setdefault(
            "constraints",
            [c.to_dict() for c in plan.constraints],
        )

    if brief != plan.trip_brief:
        plan.trip_brief = brief
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_phase_router.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add backend/phase/router.py backend/tests/test_phase_router.py
git commit -m "fix: trip_brief 中 dates/total_days 改用强制覆盖防止 stale"
```

---

### Task 5: 工具缺参 TypeError 转化为结构化 INVALID_ARGUMENTS

**优先级：B**

**Files:**
- Modify: `backend/tools/engine.py:249-266`
- Modify: `backend/tests/test_tool_engine.py`

**背景：** 当模型空参数调用 `replace_daily_plans` 时，Python 在函数调用层抛出 `TypeError`（缺少 positional argument），被 `ToolEngine.execute()` 的 `except Exception` 分支捕获并包装成泛化 `INTERNAL_ERROR`。模型无法从中恢复。应识别参数相关的 `TypeError` 并返回 `INVALID_ARGUMENTS`。

- [ ] **Step 1: 添加 TypeError 转化测试**

在 `test_tool_engine.py` 末尾添加：

```python
@pytest.mark.asyncio
async def test_execute_missing_args_returns_invalid_arguments():
    """缺少必填参数时应返回 INVALID_ARGUMENTS 而非 INTERNAL_ERROR。"""
    @tool(
        name="need_args",
        description="Needs args",
        phases=[1],
        parameters={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
    )
    async def need_args(x: int) -> dict:
        return {"x": x}

    eng = ToolEngine()
    eng.register(need_args)

    call = ToolCall(id="tc_missing", name="need_args", arguments={})
    result = await eng.execute(call)
    assert result.status == "error"
    assert result.error_code == "INVALID_ARGUMENTS"
    assert "x" in result.error or "required" in result.error.lower()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_tool_engine.py::test_execute_missing_args_returns_invalid_arguments -v`
Expected: FAIL，`assert "INTERNAL_ERROR" == "INVALID_ARGUMENTS"`

- [ ] **Step 3: 在 ToolEngine.execute 中识别参数 TypeError**

文件：`backend/tools/engine.py:249-266`

将：
```python
            except Exception as e:
                duration_ms = (time.monotonic() - start_time) * 1000
                span.set_attribute(TOOL_NAME, call.name)
                span.set_attribute(TOOL_STATUS, "error")
                span.set_attribute(TOOL_ERROR_CODE, "INTERNAL_ERROR")
                span.record_exception(e)
                span.add_event(
                    EVENT_TOOL_OUTPUT,
                    {
                        "error": truncate(str(e)),
                        "error_code": "INTERNAL_ERROR",
                    },
                )
                result = self._internal_error_result(call, e)
                if result.metadata is None:
                    result.metadata = {}
                result.metadata["duration_ms"] = round(duration_ms, 1)
                return result
```

改为：
```python
            except Exception as e:
                duration_ms = (time.monotonic() - start_time) * 1000
                # 识别参数相关的 TypeError（缺参、类型不匹配）
                is_arg_error = (
                    isinstance(e, TypeError)
                    and ("argument" in str(e) or "required" in str(e))
                )
                error_code = "INVALID_ARGUMENTS" if is_arg_error else "INTERNAL_ERROR"
                suggestion = (
                    f"请检查 {call.name} 的参数是否完整且类型正确"
                    if is_arg_error
                    else "An unexpected error occurred"
                )
                span.set_attribute(TOOL_NAME, call.name)
                span.set_attribute(TOOL_STATUS, "error")
                span.set_attribute(TOOL_ERROR_CODE, error_code)
                span.record_exception(e)
                span.add_event(
                    EVENT_TOOL_OUTPUT,
                    {
                        "error": truncate(str(e)),
                        "error_code": error_code,
                    },
                )
                result = ToolResult(
                    tool_call_id=call.id,
                    status="error",
                    error=str(e),
                    error_code=error_code,
                    suggestion=suggestion,
                )
                if result.metadata is None:
                    result.metadata = {}
                result.metadata["duration_ms"] = round(duration_ms, 1)
                return result
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_tool_engine.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add backend/tools/engine.py backend/tests/test_tool_engine.py
git commit -m "fix: 工具缺参 TypeError 返回 INVALID_ARGUMENTS 而非 INTERNAL_ERROR"
```

---

### Task 6: Phase 5 上下文过滤 trip_brief 中的冗余日期字段

**优先级：B**

**Files:**
- Modify: `backend/context/manager.py:156-166`
- Modify: `backend/tests/test_context_manager.py`

**背景：** Phase 5+ 上下文会完整注入 `trip_brief` 所有字段，其中 `dates` 和 `total_days` 与 `plan.dates` 重复。虽然 Task 4 已修复 stale 问题，但仍存在同一信息出现两次的冗余。在上下文注入时过滤这两个字段，减少模型混淆。

- [ ] **Step 1: 添加 trip_brief 日期过滤测试**

找到 `test_context_manager.py` 中 Phase 5 trip_brief 注入的现有测试，添加新测试：

```python
def test_phase5_context_excludes_trip_brief_dates_and_total_days(ctx_manager):
    """Phase 5 上下文注入 trip_brief 时应排除 dates 和 total_days（已由 plan.dates 提供）。"""
    plan = TravelPlanState(
        session_id="s1",
        phase=5,
        destination="成都",
        dates=DateRange(start="2026-05-24", end="2026-05-30"),
        selected_skeleton_id="plan_a",
        accommodation=Accommodation(area="春熙路"),
        trip_brief={
            "destination": "成都",
            "dates": {"start": "2026-05-24", "end": "2026-05-30"},
            "total_days": 7,
            "goal": "休闲度假",
        },
    )
    context = ctx_manager.build_context(plan)
    # dates 和 total_days 不应在 trip_brief 区域重复出现
    lines = context.split("\n")
    brief_section = False
    for line in lines:
        if "旅行画像" in line:
            brief_section = True
            continue
        if brief_section and line.startswith("- ") and not line.startswith("  -"):
            brief_section = False
        if brief_section:
            assert "dates" not in line.split(":")[0], f"trip_brief 不应包含 dates: {line}"
            assert "total_days" not in line.split(":")[0], f"trip_brief 不应包含 total_days: {line}"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_context_manager.py::test_phase5_context_excludes_trip_brief_dates_and_total_days -v`
Expected: FAIL

- [ ] **Step 3: 在上下文注入时过滤 trip_brief 的 dates/total_days**

文件：`backend/context/manager.py:156-166`

将：
```python
        if plan.trip_brief:
            if plan.phase >= 5 or (
                plan.phase == 3
                and plan.phase3_step in ("candidate", "skeleton", "lock")
            ):
                parts.append("- 旅行画像：")
                for key, val in plan.trip_brief.items():
                    parts.append(f"  - {key}: {val}")
```

改为：
```python
        if plan.trip_brief:
            if plan.phase >= 5 or (
                plan.phase == 3
                and plan.phase3_step in ("candidate", "skeleton", "lock")
            ):
                # Phase 5+: 排除 dates/total_days，它们已由 plan.dates 权威提供
                skip_keys = {"dates", "total_days"} if plan.phase >= 5 else set()
                parts.append("- 旅行画像：")
                for key, val in plan.trip_brief.items():
                    if key in skip_keys:
                        continue
                    parts.append(f"  - {key}: {val}")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_context_manager.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add backend/context/manager.py backend/tests/test_context_manager.py
git commit -m "fix: Phase 5 上下文过滤 trip_brief 中的冗余 dates/total_days"
```

---

### Task 7: 增量持久化——工具写入成功后立即保存 plan 快照

**优先级：C**

**Files:**
- Modify: `backend/main.py:1748-1751` 区域
- Modify: `backend/tests/test_api.py` 或新建持久化测试

**背景：** 当前 plan 状态只在 `_run_agent_stream()` 末尾保存。如果 SSE 连接中断或前端关闭，已写入内存的状态不会落盘。在每次 plan_writer 工具成功后增加一次 `state_mgr.save(plan)` 调用。

- [ ] **Step 1: 在 state_update yield 前添加增量保存**

文件：`backend/main.py`，找到 plan_writer 工具成功后 yield `state_update` 的位置（约第 1748 行附近）。

在 `yield json.dumps({"type": "state_update", ...})` **之前**添加：

```python
                        # 增量持久化：工具写入成功后立即保存，防止 SSE 中断丢失状态
                        await state_mgr.save(plan)
```

完整上下文应变为：

```python
                        # 增量持久化：工具写入成功后立即保存，防止 SSE 中断丢失状态
                        await state_mgr.save(plan)
                        yield json.dumps(
                            {"type": "state_update", "plan": plan.to_dict()},
                            ensure_ascii=False,
                        )
```

- [ ] **Step 2: 在 finally 块中增加保底持久化**

文件：`backend/main.py`，找到 finally 块（约第 1960 行附近）。

在 `keepalive_task.cancel()` 之前添加保底保存：

```python
        finally:
            # 保底持久化：即使流异常中断，也尝试保存当前状态
            try:
                await state_mgr.save(plan)
                await session_store.update(
                    plan.session_id,
                    phase=plan.phase,
                    title=_generate_title(plan),
                    last_run_id=run.run_id,
                    last_run_status=run.status,
                    last_run_error=run.error_code,
                )
            except Exception:
                pass  # 保底保存失败不应阻塞清理
            keepalive_task.cancel()
            session.pop("_cancel_event", None)
            if not run.can_continue:
                session.pop("_current_run", None)
```

- [ ] **Step 3: 验证不会与末尾正常持久化冲突**

末尾正常保存路径（第 1917 行 `await state_mgr.save(plan)`）是幂等操作（保存最新状态），增量保存不会产生冲突。`StateManager.save()` 每次递增 version 并写入，所以多次调用安全。

- [ ] **Step 4: 运行全量测试**

Run: `pytest tests/ -v --tb=short`
Expected: ALL PASS（增量保存不应影响现有测试）

- [ ] **Step 5: Commit**

```bash
git add backend/main.py
git commit -m "fix: plan_writer 工具成功后增量持久化 + finally 保底保存"
```

---

### Task 8: 更新 postmortem 文档状态 + PROJECT_OVERVIEW

**Files:**
- Modify: `docs/postmortems/2026-04-17-phase5-plan-tool-errors.md`
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: 更新 postmortem 状态**

将文件头部的：
```
- 事故状态：已通过真实前端复现，已定位主要根因，待修复
```

改为：
```
- 事故状态：已修复（A/B 优先级项全部完成，C 优先级持久化保障已完成）
```

- [ ] **Step 2: 更新 PROJECT_OVERVIEW.md**

在相关章节记录本次修复的关键变更：
- `DateRange.total_days` 语义从 exclusive 改为 inclusive
- Phase 3→5 新增骨架天数门控
- `trip_brief` 日期字段改为强制覆盖
- 工具 TypeError 结构化为 `INVALID_ARGUMENTS`
- plan_writer 增量持久化

- [ ] **Step 3: Commit**

```bash
git add docs/postmortems/2026-04-17-phase5-plan-tool-errors.md PROJECT_OVERVIEW.md
git commit -m "docs: 更新事故复盘状态和 PROJECT_OVERVIEW"
```
