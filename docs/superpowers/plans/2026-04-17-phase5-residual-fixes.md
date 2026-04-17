# Phase 5 残留问题修复实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Phase 5 复测中发现的三个残留问题——工具参数预校验、validator 时间冲突闭环、session meta 增量更新，以及 trace 错误结构暴露。

**Architecture:** 在 ToolEngine 层增加基于 schema required 的预校验拦截缺参调用；在 daily_plans 工具层写入后即时检测时间冲突并返回给模型；在 main.py 增量持久化路径同步更新 session meta；在 trace 构建中补全 error_code 和 suggestion 字段。

**Tech Stack:** Python 3.12, pytest, FastAPI, SQLite

---

## 文件变更总览

| 文件 | 操作 | 职责 |
|------|------|------|
| `backend/tools/engine.py` | 修改 L202 | execute() 增加 required 预校验 |
| `backend/tools/plan_tools/daily_plans.py` | 修改 L174-259 | 工具返回增加冲突检测结果 |
| `backend/harness/validator.py` | 修改 L125-151 | 新增按天过滤的公开方法 |
| `backend/phase/prompts.py` | 修改 L665 | Phase 5 prompt 增加冲突处理指令 |
| `backend/telemetry/stats.py` | 修改 L54-129 | ToolCallRecord 增加 suggestion 字段 |
| `backend/api/trace.py` | 修改 L139-151 | build_trace 输出 error_code + suggestion |
| `backend/main.py` | 修改 L345-380, L1749, L1974 | record_stats 传递 suggestion；增量更新 meta；finally 加日志 |
| `backend/tests/test_tool_engine.py` | 修改 | 新增预校验测试 |
| `backend/tests/test_plan_tools/test_daily_plans.py` | 修改 | 新增冲突检测测试 |
| `backend/tests/test_trace_api.py` | 修改 | 新增 error_code/suggestion 测试 |
| `backend/tests/test_quality_gate.py` | 修改 | 新增 suggestion 记录测试 |

---

### Task 1: ToolEngine 参数 schema 预校验

**优先级：A — 阻断空参数调用**

**Files:**
- Modify: `backend/tools/engine.py:202-205`
- Test: `backend/tests/test_tool_engine.py`

- [ ] **Step 1: 写失败测试 — 缺参返回 INVALID_ARGUMENTS**

在 `backend/tests/test_tool_engine.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_execute_missing_required_param_returns_invalid_arguments():
    """缺少 required 参数时，预校验直接返回 INVALID_ARGUMENTS，不进入函数调用。"""
    engine = ToolEngine()

    @tool(
        name="needs_param",
        description="test",
        phases=[1],
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name", "age"],
        },
    )
    async def needs_param(name: str, age: int) -> dict:
        return {"ok": True}

    engine.register(needs_param)
    call = ToolCall(id="c1", name="needs_param", arguments={"name": "Alice"})
    result = await engine.execute(call)
    assert result.status == "error"
    assert result.error_code == "INVALID_ARGUMENTS"
    assert "age" in result.error


@pytest.mark.asyncio
async def test_execute_empty_args_returns_invalid_arguments():
    """空参数 {} 时，预校验返回 INVALID_ARGUMENTS。"""
    engine = ToolEngine()

    @tool(
        name="needs_days",
        description="test",
        phases=[1],
        parameters={
            "type": "object",
            "properties": {"days": {"type": "array"}},
            "required": ["days"],
        },
    )
    async def needs_days(days: list) -> dict:
        return {"ok": True}

    engine.register(needs_days)
    call = ToolCall(id="c2", name="needs_days", arguments={})
    result = await engine.execute(call)
    assert result.status == "error"
    assert result.error_code == "INVALID_ARGUMENTS"
    assert "days" in result.error


@pytest.mark.asyncio
async def test_execute_no_required_field_skips_prevalidation():
    """schema 无 required 字段时，不做预校验，正常执行。"""
    engine = ToolEngine()

    @tool(
        name="optional_tool",
        description="test",
        phases=[1],
        parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
    )
    async def optional_tool(x: int = 0) -> dict:
        return {"x": x}

    engine.register(optional_tool)
    call = ToolCall(id="c3", name="optional_tool", arguments={})
    result = await engine.execute(call)
    assert result.status == "success"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_tool_engine.py::test_execute_missing_required_param_returns_invalid_arguments tests/test_tool_engine.py::test_execute_empty_args_returns_invalid_arguments tests/test_tool_engine.py::test_execute_no_required_field_skips_prevalidation -v`

Expected: 前两个 FAIL（当前走 TypeError 路径，error_code 可能是 INVALID_ARGUMENTS 但错误消息不含"缺少必填参数"），第三个 PASS。

- [ ] **Step 3: 实现预校验**

在 `backend/tools/engine.py` 的 `execute()` 方法中，`try:` 块之前（L202 `try:` 之后、L204 `start_time = time.monotonic()` 之前），插入预校验逻辑：

```python
            # --- 预校验 required 参数 ---
            params_schema = tool_def.parameters or {}
            required_params = params_schema.get("required", [])
            if required_params:
                missing = [p for p in required_params if p not in call.arguments]
                if missing:
                    span.set_attribute(TOOL_NAME, call.name)
                    span.set_attribute(TOOL_STATUS, "error")
                    span.set_attribute(TOOL_ERROR_CODE, "INVALID_ARGUMENTS")
                    error_msg = f"缺少必填参数: {', '.join(missing)}"
                    suggestion = f"请提供以下参数: {', '.join(missing)}"
                    span.add_event(
                        EVENT_TOOL_OUTPUT,
                        {"error": error_msg, "error_code": "INVALID_ARGUMENTS"},
                    )
                    return ToolResult(
                        tool_call_id=call.id,
                        status="error",
                        error=error_msg,
                        error_code="INVALID_ARGUMENTS",
                        suggestion=suggestion,
                    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_tool_engine.py -v -q`

Expected: 所有新增测试 PASS，已有测试不退化。

- [ ] **Step 5: 提交**

```bash
git add backend/tools/engine.py backend/tests/test_tool_engine.py
git commit -m "fix: ToolEngine 增加 required 参数预校验，拦截空参数调用"
```

---

### Task 2: validator 暴露按天过滤的时间冲突检测

**优先级：A — 为工具层冲突检测提供基础**

**Files:**
- Modify: `backend/harness/validator.py:125-151`
- Test: `backend/tests/test_harness_validator.py`

- [ ] **Step 1: 写失败测试 — 按天过滤冲突**

在 `backend/tests/test_harness_validator.py` 末尾追加：

```python
def test_validate_day_conflicts_filters_by_day():
    """validate_day_conflicts 只返回指定天数的时间冲突。"""
    from harness.validator import validate_day_conflicts

    plan = TravelPlanState(
        session_id="s1",
        dates=DateRange(start="2026-05-01", end="2026-05-02"),
        daily_plans=[
            DayPlan(
                day=1,
                date="2026-05-01",
                activities=[
                    _make_activity("A", "09:00", "12:00", cost=0),
                    _make_activity("B", "11:00", "13:00", cost=0),  # 冲突
                ],
            ),
            DayPlan(
                day=2,
                date="2026-05-02",
                activities=[
                    _make_activity("C", "09:00", "10:00", cost=0),
                    _make_activity("D", "11:00", "12:00", cost=0),  # 无冲突
                ],
            ),
        ],
    )
    # 只查 day 1
    result = validate_day_conflicts(plan, [1])
    assert len(result["conflicts"]) == 1
    assert "Day 1" in result["conflicts"][0]
    assert result["has_severe_conflicts"] is True  # 11:00 < 12:00

    # 只查 day 2
    result2 = validate_day_conflicts(plan, [2])
    assert len(result2["conflicts"]) == 0
    assert result2["has_severe_conflicts"] is False


def test_validate_day_conflicts_detects_zero_gap():
    """零间隔也算严重冲突。"""
    from harness.validator import validate_day_conflicts

    plan = TravelPlanState(
        session_id="s1",
        daily_plans=[
            DayPlan(
                day=1,
                date="2026-05-01",
                activities=[
                    _make_activity("A", "09:00", "10:00", cost=0),
                    _make_activity("B", "10:00", "11:00", cost=0),  # 0 gap, transport_duration_min defaults to 0
                ],
            ),
        ],
    )
    result = validate_day_conflicts(plan, [1])
    # transport_duration_min 默认 0，10:00 + 0 = 10:00，不 > 10:00，所以不冲突
    assert result["has_severe_conflicts"] is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_validator.py::test_validate_day_conflicts_filters_by_day tests/test_harness_validator.py::test_validate_day_conflicts_detects_zero_gap -v`

Expected: FAIL（`validate_day_conflicts` 不存在）

- [ ] **Step 3: 实现 validate_day_conflicts**

在 `backend/harness/validator.py` 末尾追加：

```python
def validate_day_conflicts(
    plan: TravelPlanState, day_numbers: list[int]
) -> dict:
    """检查指定天数的时间冲突。

    Returns:
        {"conflicts": list[str], "has_severe_conflicts": bool}
        严重冲突定义：相邻活动间隔为负（前一个结束+交通 > 后一个开始）。
    """
    all_errors = _validate_time_conflicts(plan)
    day_set = set(day_numbers)
    relevant = [
        e for e in all_errors
        if any(f"Day {d}:" in e for d in day_set)
    ]
    # 严重冲突：间隔为负数或零（含交通后仍来不及）
    severe = [e for e in relevant if "间隔仅 -" in e or "间隔仅 0min" in e]
    return {
        "conflicts": relevant,
        "has_severe_conflicts": len(severe) > 0,
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_validator.py -v -q`

Expected: 所有测试 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/harness/validator.py backend/tests/test_harness_validator.py
git commit -m "feat: validator 新增 validate_day_conflicts 按天过滤时间冲突"
```

---

### Task 3: daily_plans 工具写入后返回冲突信息

**优先级：A — 核心闭环**

**Files:**
- Modify: `backend/tools/plan_tools/daily_plans.py:174-259`
- Test: `backend/tests/test_plan_tools/test_daily_plans.py`

- [ ] **Step 1: 写失败测试 — append_day_plan 返回冲突信息**

在 `backend/tests/test_plan_tools/test_daily_plans.py` 中：

首先在文件顶部辅助函数区域（`_sample_activity()` 之后）增加 helper：

```python
def _activity(name: str, start: str, end: str, cost: float = 0) -> dict:
    return {
        "name": name,
        "location": {"name": name, "lat": 30.0, "lng": 104.0},
        "start_time": start,
        "end_time": end,
        "category": "activity",
        "cost": cost,
    }
```

然后在文件末尾追加测试：

```python
@pytest.mark.asyncio
async def test_append_day_plan_returns_conflicts_on_time_overlap():
    """append_day_plan 写入有时间冲突的活动时，返回 conflicts 字段。"""
    plan = _make_plan()
    plan.dates = DateRange(start="2026-05-01", end="2026-05-02")
    tool_fn = make_append_day_plan_tool(plan)
    result = await tool_fn(
        day=1,
        date="2026-05-01",
        activities=[
            _activity("A", "09:00", "12:00"),
            _activity("B", "11:00", "13:00"),  # 冲突：12:00 > 11:00
        ],
    )
    assert result["action"] == "append"
    assert "conflicts" in result
    assert len(result["conflicts"]) > 0
    assert result["has_severe_conflicts"] is True


@pytest.mark.asyncio
async def test_append_day_plan_no_conflicts_when_valid():
    """append_day_plan 写入无冲突的活动时，conflicts 为空。"""
    plan = _make_plan()
    plan.dates = DateRange(start="2026-05-01", end="2026-05-02")
    tool_fn = make_append_day_plan_tool(plan)
    result = await tool_fn(
        day=1,
        date="2026-05-01",
        activities=[
            _activity("A", "09:00", "10:00"),
            _activity("B", "11:00", "12:00"),
        ],
    )
    assert result["conflicts"] == []
    assert result["has_severe_conflicts"] is False


@pytest.mark.asyncio
async def test_replace_daily_plans_returns_conflicts():
    """replace_daily_plans 写入后也返回冲突信息。"""
    plan = _make_plan()
    plan.dates = DateRange(start="2026-05-01", end="2026-05-02")
    tool_fn = make_replace_daily_plans_tool(plan)
    result = await tool_fn(
        days=[
            {
                "day": 1,
                "date": "2026-05-01",
                "activities": [
                    _activity("A", "09:00", "12:00"),
                    _activity("B", "11:00", "13:00"),  # 冲突
                ],
            },
        ],
    )
    assert "conflicts" in result
    assert len(result["conflicts"]) > 0
    assert result["has_severe_conflicts"] is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_plan_tools/test_daily_plans.py::test_append_day_plan_returns_conflicts_on_time_overlap tests/test_plan_tools/test_daily_plans.py::test_append_day_plan_no_conflicts_when_valid tests/test_plan_tools/test_daily_plans.py::test_replace_daily_plans_returns_conflicts -v`

Expected: FAIL（返回的 dict 中没有 conflicts 字段）

- [ ] **Step 3: 实现冲突检测返回**

修改 `backend/tools/plan_tools/daily_plans.py`：

在文件顶部增加导入：
```python
from harness.validator import validate_day_conflicts
```

修改 `make_append_day_plan_tool` 中的 `append_day_plan` 函数，在 `return` 之前增加冲突检测：

```python
        # 原有 return 替换为：
        conflict_info = validate_day_conflicts(plan, [day])
        return {
            "updated_field": "daily_plans",
            "action": "append",
            "day": day,
            "date": date,
            "activity_count": len(activities),
            "total_days": len(plan.daily_plans),
            "previous_days": previous_count,
            **conflict_info,
        }
```

修改 `make_replace_daily_plans_tool` 中的 `replace_daily_plans` 函数，在 `return` 之前增加冲突检测：

```python
        # 原有 return 替换为：
        replaced_days = [dp["day"] for dp in days]
        conflict_info = validate_day_conflicts(plan, replaced_days)
        return {
            "updated_field": "daily_plans",
            "action": "replace",
            "total_days": len(plan.daily_plans),
            "previous_days": previous_count,
            **conflict_info,
        }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_plan_tools/test_daily_plans.py -v -q`

Expected: 所有新增测试 PASS，已有测试不退化。

- [ ] **Step 5: 提交**

```bash
git add backend/tools/plan_tools/daily_plans.py backend/tests/test_plan_tools/test_daily_plans.py
git commit -m "feat: daily_plans 工具写入后返回时间冲突检测结果"
```

---

### Task 4: Phase 5 prompt 增加时间冲突处理指令

**优先级：B — 引导模型行为**

**Files:**
- Modify: `backend/phase/prompts.py:665`

- [ ] **Step 1: 修改 prompt**

在 `backend/phase/prompts.py` 的 `PHASE5_PROMPT` 中，在 `## 完成 Gate` 段落之前（L665 之前）插入新段落：

```python
## 时间冲突处理

append_day_plan / replace_daily_plans 返回结果中可能包含 `conflicts` 和 `has_severe_conflicts` 字段。

- 如果 `has_severe_conflicts` 为 true：**必须立即**使用 replace_daily_plans 修复该天的时间安排，确保相邻活动之间留有合理的交通缓冲时间（至少等于 transport_duration_min）。不要继续追加后续天数。
- 如果有 `conflicts` 但 `has_severe_conflicts` 为 false：记录在心，可继续追加，在全部天数写完后统一用 replace_daily_plans 优化。
- 没有 conflicts：正常继续。

常见冲突原因：
1. 前一个活动的 end_time + transport_duration_min > 下一个活动的 start_time
2. 跨城交通后直接安排活动，没留缓冲
3. 两个活动时间重叠

修复方法：调整 start_time/end_time，增加活动间的时间间隔，或删除不必要的活动。

```

- [ ] **Step 2: 运行已有 prompt 相关测试确认不退化**

Run: `cd backend && python -m pytest tests/test_phase_router.py -v -q`

Expected: 除预先存在的 2 个 prompt 文案断言失败外，其余全部 PASS。

- [ ] **Step 3: 提交**

```bash
git add backend/phase/prompts.py
git commit -m "feat: Phase 5 prompt 增加时间冲突处理指令，引导模型修复冲突"
```

---

### Task 5: trace 暴露 error_code 和 suggestion

**优先级：B — 可观测性**

**Files:**
- Modify: `backend/telemetry/stats.py:54-129`
- Modify: `backend/api/trace.py:139-151`
- Modify: `backend/main.py:345-380`
- Test: `backend/tests/test_trace_api.py`
- Test: `backend/tests/test_quality_gate.py`

- [ ] **Step 1: 写失败测试 — trace 包含 error_code 和 suggestion**

在 `backend/tests/test_trace_api.py` 末尾追加：

```python
def test_trace_includes_error_code_and_suggestion():
    """Error 状态的 tool call 在 trace 中包含 error_code 和 suggestion。"""
    from api.trace import build_trace
    from telemetry.stats import SessionStats

    stats = SessionStats()
    stats.record_llm_call(
        provider="test", model="test", input_tokens=10, output_tokens=5,
        duration_ms=100, phase=5, iteration=1,
    )
    stats.record_tool_call(
        tool_name="replace_daily_plans",
        duration_ms=5.0,
        status="error",
        error_code="INVALID_ARGUMENTS",
        phase=5,
        arguments_preview="{}",
        result_preview="ERROR: 缺少必填参数: days",
        suggestion="请提供以下参数: days",
    )

    result = build_trace("test_session", {"stats": stats})
    tc = result["iterations"][0]["tool_calls"][0]
    assert tc["error_code"] == "INVALID_ARGUMENTS"
    assert tc["suggestion"] == "请提供以下参数: days"


def test_trace_success_tool_has_null_error_code():
    """成功的 tool call 的 error_code 和 suggestion 为 None。"""
    from api.trace import build_trace
    from telemetry.stats import SessionStats

    stats = SessionStats()
    stats.record_llm_call(
        provider="test", model="test", input_tokens=10, output_tokens=5,
        duration_ms=100, phase=5, iteration=1,
    )
    stats.record_tool_call(
        tool_name="append_day_plan",
        duration_ms=10.0,
        status="success",
        error_code=None,
        phase=5,
    )

    result = build_trace("test_session", {"stats": stats})
    tc = result["iterations"][0]["tool_calls"][0]
    assert tc["error_code"] is None
    assert tc["suggestion"] is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_trace_api.py::test_trace_includes_error_code_and_suggestion tests/test_trace_api.py::test_trace_success_tool_has_null_error_code -v`

Expected: FAIL — `record_tool_call` 不接受 `suggestion` 参数；`build_trace` 输出中没有 `error_code` 和 `suggestion` 字段。

- [ ] **Step 3: ToolCallRecord 增加 suggestion 字段**

修改 `backend/telemetry/stats.py`：

在 `ToolCallRecord` dataclass（L54-66）中追加字段：

```python
    suggestion: str | None = None
```

位置：在 `judge_scores` 字段之后。

修改 `record_tool_call` 方法（L107-129），增加 `suggestion` 参数和传递：

```python
    def record_tool_call(
        self,
        *,
        tool_name: str,
        duration_ms: float,
        status: str,
        error_code: str | None,
        phase: int,
        parallel_group: int | None = None,
        arguments_preview: str = "",
        result_preview: str = "",
        suggestion: str | None = None,
    ) -> None:
        self.tool_calls.append(
            ToolCallRecord(
                tool_name=tool_name,
                duration_ms=duration_ms,
                status=status,
                error_code=error_code,
                phase=phase,
                arguments_preview=arguments_preview,
                result_preview=result_preview,
                parallel_group=parallel_group,
                suggestion=suggestion,
            )
        )
```

- [ ] **Step 4: build_trace 输出 error_code 和 suggestion**

修改 `backend/api/trace.py` L139-151 的 `iter_tool_dicts.append(...)` 块，增加两个字段：

```python
            iter_tool_dicts.append(
                {
                    "name": tc.tool_name,
                    "duration_ms": round(tc.duration_ms, 1),
                    "status": tc.status,
                    "side_effect": _get_side_effect(tc.tool_name),
                    "arguments_preview": tc.arguments_preview,
                    "result_preview": tc.result_preview,
                    "error_code": tc.error_code,
                    "suggestion": tc.suggestion,
                    "parallel_group": tc.parallel_group,
                    "validation_errors": tc.validation_errors,
                    "judge_scores": tc.judge_scores,
                }
            )
```

同样修改 L185-196 的 orphan tool_dicts 构建，增加相同字段：

```python
        remaining_tool_dicts.append(
            {
                "name": tc.tool_name,
                "duration_ms": round(tc.duration_ms, 1),
                "status": tc.status,
                "side_effect": _get_side_effect(tc.tool_name),
                "arguments_preview": "",
                "result_preview": "",
                "error_code": tc.error_code,
                "suggestion": tc.suggestion,
                "parallel_group": tc.parallel_group,
                "validation_errors": tc.validation_errors,
                "judge_scores": tc.judge_scores,
            }
        )
```

- [ ] **Step 5: _record_tool_result_stats 传递 suggestion**

修改 `backend/main.py` L371-380 的 `stats.record_tool_call(...)` 调用，增加 `suggestion=result.suggestion`：

```python
    stats.record_tool_call(
        tool_name=tool_name,
        duration_ms=float(duration),
        status=result.status,
        error_code=result.error_code,
        phase=phase,
        parallel_group=parallel_group,
        arguments_preview=arguments_preview,
        result_preview=result_preview,
        suggestion=getattr(result, "suggestion", None),
    )
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_trace_api.py tests/test_quality_gate.py tests/test_stats.py -v -q`

Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add backend/telemetry/stats.py backend/api/trace.py backend/main.py backend/tests/test_trace_api.py
git commit -m "feat: trace 暴露 error_code 和 suggestion 字段"
```

---

### Task 6: session meta 增量更新 + finally 日志

**优先级：B — 三源一致性**

**Files:**
- Modify: `backend/main.py:1749, 1974`
- Test: `backend/tests/test_quality_gate.py`（或相关集成测试）

- [ ] **Step 1: 实现增量 session meta 更新**

修改 `backend/main.py` 约 L1749 处的增量持久化段落。在 `await state_mgr.save(plan)` 之后（L1749 之后），`yield json.dumps(...)` 之前，增加 session meta 更新：

```python
                        # 增量持久化：工具写入成功后立即保存，防止 SSE 中断丢失状态
                        await state_mgr.save(plan)
                        # 同步更新 session meta，确保 plan 文件与数据库一致
                        try:
                            await session_store.update(
                                plan.session_id,
                                phase=plan.phase,
                                title=_generate_title(plan),
                            )
                        except Exception:
                            logger.warning(
                                "增量 session meta 更新失败 session=%s",
                                plan.session_id,
                                exc_info=True,
                            )
```

- [ ] **Step 2: finally 块增加日志**

修改 `backend/main.py` 约 L1974 处的 `except Exception: pass`，替换为：

```python
            except Exception:
                logger.warning(
                    "保底持久化失败 session=%s",
                    plan.session_id,
                    exc_info=True,
                )
```

- [ ] **Step 3: 运行已有测试确认不退化**

Run: `cd backend && python -m pytest tests/ -x -q --timeout=30 2>&1 | tail -20`

Expected: 不引入新的失败

- [ ] **Step 4: 提交**

```bash
git add backend/main.py
git commit -m "fix: plan writer 成功后同步更新 session meta + finally 保底保存增加日志"
```

---

### Task 7: 更新文档和 PROJECT_OVERVIEW

**优先级：C**

**Files:**
- Modify: `docs/postmortems/2026-04-17-phase5-retest-residual-issues.md`
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: 更新 postmortem 状态**

将 `docs/postmortems/2026-04-17-phase5-retest-residual-issues.md` 中：
- L9 的 `事故状态：已复测确认，主根因已修复，残留问题待修复` 改为 `事故状态：已修复`
- 在文档末尾追加修复记录段落

- [ ] **Step 2: 更新 PROJECT_OVERVIEW**

在 `PROJECT_OVERVIEW.md` 的适当位置（工具引擎 / harness 相关段落）增加：
- ToolEngine 参数预校验
- daily_plans 工具时间冲突检测
- trace error_code/suggestion 字段
- session meta 增量更新

- [ ] **Step 3: 提交**

```bash
git add docs/postmortems/2026-04-17-phase5-retest-residual-issues.md PROJECT_OVERVIEW.md
git commit -m "docs: 更新残留问题复盘状态和 PROJECT_OVERVIEW"
```
