# Trace & Memory 数据管道补全实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补全 P1 评估中识别的 7 个缺口，让 TraceViewer 和 MemoryCenter 的数据通道完全打通。

**Architecture:** "丰富 Stats 层，Trace 层只做读取"——在数据产生点（工具调用、钩子、loop）就地记录到 SessionStats，build_trace 纯粹做数据组装，前端纯粹做数据渲染。

**Tech Stack:** Python 3.12 dataclasses / FastAPI SSE / React 18 TypeScript / CSS Variables

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `backend/telemetry/stats.py` | Modify | ToolCallRecord 新增 4 字段, 新增 MemoryHitRecord, SessionStats.memory_hits |
| `backend/tools/update_plan_state.py` | Modify | 返回 previous_value |
| `backend/harness/guardrail.py` | Modify | 住宿 price 别名兼容 |
| `backend/memory/manager.py` | Modify | generate_context 返回 tuple[str, list[str]] |
| `backend/main.py` | Modify | 钩子写入 stats 新字段, memory_hits 记录, memory_recall SSE, trace 端点传 compression_events |
| `backend/agent/loop.py` | Modify | 并行组计数器 + metadata 透传 |
| `backend/api/trace.py` | Modify | 消费新字段, 3 个辅助函数 |
| `frontend/src/types/trace.ts` | Modify | TraceToolCall 3 字段, MemoryHit, TraceIteration.memory_hits |
| `frontend/src/components/TraceViewer.tsx` | Modify | 6 个新渲染区域 |
| `frontend/src/components/MemoryCenter.tsx` | Modify | 命中记忆高亮 |
| `frontend/src/components/ChatPanel.tsx` | Modify | memory_recall SSE 事件处理 |
| `frontend/src/components/SessionSidebar.tsx` | Modify | 透传 recalledIds |
| `frontend/src/App.tsx` | Modify | recalledIds state lifting |
| `frontend/src/styles/trace-viewer.css` | Modify | 新增样式 |
| `backend/tests/test_trace_api.py` | Modify | 扩展 trace 测试 |
| `backend/tests/test_guardrail.py` | Modify | 价格别名测试 |
| `backend/tests/test_memory_manager.py` | Modify | generate_context 返回值测试 |
| `backend/tests/test_agent_loop.py` | Modify | FakeMemoryManager 签名更新 |
| `backend/tests/test_telemetry_agent_loop.py` | Modify | _MemoryManager 签名更新 |
| `backend/tests/test_memory_integration.py` | Modify | monkeypatch 签名更新 |

---

### Task 1: Stats 层扩展 — ToolCallRecord 新增字段 + MemoryHitRecord

**Files:**
- Modify: `backend/telemetry/stats.py:53-104`
- Test: `backend/tests/test_trace_api.py` (新增测试函数)

- [ ] **Step 1: 写失败测试 — ToolCallRecord 新字段**

在 `backend/tests/test_trace_api.py` 末尾追加：

```python
def test_tool_call_record_new_fields():
    """ToolCallRecord accepts state_changes, parallel_group, validation_errors, judge_scores."""
    from telemetry.stats import ToolCallRecord
    rec = ToolCallRecord(
        tool_name="update_plan_state",
        duration_ms=50.0,
        status="ok",
        error_code=None,
        phase=1,
        state_changes=[{"field": "destination", "before": None, "after": "东京"}],
        parallel_group=1,
        validation_errors=["时间冲突"],
        judge_scores={"pace": 4, "geography": 5},
    )
    assert rec.state_changes == [{"field": "destination", "before": None, "after": "东京"}]
    assert rec.parallel_group == 1
    assert rec.validation_errors == ["时间冲突"]
    assert rec.judge_scores == {"pace": 4, "geography": 5}


def test_tool_call_record_defaults_none():
    """New fields default to None for backward compatibility."""
    from telemetry.stats import ToolCallRecord
    rec = ToolCallRecord(
        tool_name="web_search", duration_ms=100.0, status="ok",
        error_code=None, phase=1,
    )
    assert rec.state_changes is None
    assert rec.parallel_group is None
    assert rec.validation_errors is None
    assert rec.judge_scores is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest backend/tests/test_trace_api.py::test_tool_call_record_new_fields backend/tests/test_trace_api.py::test_tool_call_record_defaults_none -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'state_changes'`

- [ ] **Step 3: 实现 ToolCallRecord 新字段**

在 `backend/telemetry/stats.py` 中，修改 `ToolCallRecord`：

```python
@dataclass
class ToolCallRecord:
    tool_name: str
    duration_ms: float
    status: str
    error_code: str | None
    phase: int
    timestamp: float = field(default_factory=time.time)
    state_changes: list[dict] | None = None
    parallel_group: int | None = None
    validation_errors: list[str] | None = None
    judge_scores: dict | None = None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest backend/tests/test_trace_api.py::test_tool_call_record_new_fields backend/tests/test_trace_api.py::test_tool_call_record_defaults_none -v`
Expected: PASS

- [ ] **Step 5: 写失败测试 — MemoryHitRecord + SessionStats.memory_hits**

在 `backend/tests/test_trace_api.py` 末尾追加：

```python
def test_memory_hit_record():
    """MemoryHitRecord stores recall metadata."""
    from telemetry.stats import MemoryHitRecord
    rec = MemoryHitRecord(
        item_ids=["mem-1", "mem-2"],
        core_count=1, trip_count=0, phase_count=1,
    )
    assert rec.item_ids == ["mem-1", "mem-2"]
    assert rec.core_count == 1
    assert rec.timestamp > 0


def test_session_stats_memory_hits():
    """SessionStats has memory_hits list, defaults empty."""
    stats = SessionStats()
    assert stats.memory_hits == []


def test_session_stats_to_dict_includes_memory_hits():
    """to_dict includes memory_hits count."""
    from telemetry.stats import MemoryHitRecord
    stats = SessionStats()
    stats.memory_hits.append(MemoryHitRecord(
        item_ids=["m1"], core_count=1, trip_count=0, phase_count=0,
    ))
    d = stats.to_dict()
    assert d["memory_hit_count"] == 1
```

- [ ] **Step 6: 运行测试确认失败**

Run: `python -m pytest backend/tests/test_trace_api.py::test_memory_hit_record backend/tests/test_trace_api.py::test_session_stats_memory_hits backend/tests/test_trace_api.py::test_session_stats_to_dict_includes_memory_hits -v`
Expected: FAIL — `ImportError: cannot import name 'MemoryHitRecord'`

- [ ] **Step 7: 实现 MemoryHitRecord + SessionStats.memory_hits + to_dict 扩展**

在 `backend/telemetry/stats.py` 中，在 `ToolCallRecord` 之后添加：

```python
@dataclass
class MemoryHitRecord:
    item_ids: list[str]
    core_count: int
    trip_count: int
    phase_count: int
    timestamp: float = field(default_factory=time.time)
```

修改 `SessionStats`：

```python
@dataclass
class SessionStats:
    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    memory_hits: list[MemoryHitRecord] = field(default_factory=list)
```

在 `to_dict` 的 return 字典中添加：

```python
"memory_hit_count": len(self.memory_hits),
```

- [ ] **Step 8: 运行测试确认通过**

Run: `python -m pytest backend/tests/test_trace_api.py::test_memory_hit_record backend/tests/test_trace_api.py::test_session_stats_memory_hits backend/tests/test_trace_api.py::test_session_stats_to_dict_includes_memory_hits -v`
Expected: PASS

- [ ] **Step 9: 运行全部现有 stats 测试确保无回归**

Run: `python -m pytest backend/tests/test_trace_api.py -v`
Expected: All PASS

- [ ] **Step 10: Commit**

```bash
git add backend/telemetry/stats.py backend/tests/test_trace_api.py
git commit -m "feat(stats): extend ToolCallRecord with state_changes/parallel_group/validation_errors/judge_scores, add MemoryHitRecord"
```

---

### Task 2: 住宿 Schema price 别名兼容

**Files:**
- Modify: `backend/harness/guardrail.py:32-36, 126-145`
- Test: `backend/tests/test_guardrail.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_guardrail.py` 末尾追加：

```python
def test_output_accommodation_price_per_night_accepted(guardrail):
    """search_accommodations with price_per_night (no price) should pass."""
    result = guardrail.validate_output("search_accommodations", {
        "results": [{
            "price_per_night": 800,
            "name": "Hotel A",
            "location": "新宿",
        }]
    })
    assert result.allowed
    assert result.reason == ""


def test_output_accommodation_price_accepted(guardrail):
    """search_accommodations with price (no price_per_night) should pass."""
    result = guardrail.validate_output("search_accommodations", {
        "results": [{
            "price": 800,
            "name": "Hotel A",
            "location": "新宿",
        }]
    })
    assert result.allowed
    assert result.reason == ""


def test_output_accommodation_no_price_at_all_is_error(guardrail):
    """search_accommodations with neither price nor price_per_night → error."""
    result = guardrail.validate_output("search_accommodations", {
        "results": [{
            "name": "Hotel A",
            "location": "新宿",
        }]
    })
    assert result.level == "error"
    assert "price" in result.reason
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest backend/tests/test_guardrail.py::test_output_accommodation_price_per_night_accepted -v`
Expected: FAIL — `assert 'warn' == ''` (因为 price 不在结果中，被 missing_fields 捕获)

- [ ] **Step 3: 实现 price 别名兼容**

修改 `backend/harness/guardrail.py`：

1) 将 `_REQUIRED_RESULT_FIELDS` 中 `search_accommodations` 的 `"price"` 移除：

```python
_REQUIRED_RESULT_FIELDS: dict[str, list[str]] = {
    "search_flights": ["price", "departure_time", "arrival_time", "airline"],
    "search_accommodations": ["name", "location"],
    "search_trains": ["price", "departure_time", "arrival_time"],
}
```

2) 添加价格别名常量：

```python
_PRICE_ALIASES: dict[str, frozenset[str]] = {
    "search_accommodations": frozenset({"price", "price_per_night"}),
}
```

3) 在 `validate_output` 方法中，在 `missing_fields` 检查之后、return 之前，添加价格别名检查：

```python
        if (
            not self._is_disabled("missing_fields")
            and tool_name in _REQUIRED_RESULT_FIELDS
            and isinstance(results, list)
        ):
            required = _REQUIRED_RESULT_FIELDS[tool_name]
            aliases = _PRICE_ALIASES.get(tool_name)
            for item in results:
                if isinstance(item, dict):
                    missing = [f for f in required if f not in item]
                    # Check price aliases: if tool has aliases, require at least one present
                    if aliases and not any(alias in item for alias in aliases):
                        missing.append("price")
                    if missing:
                        level = (
                            "error"
                            if any(field in _CRITICAL_FIELDS for field in missing)
                            else "warn"
                        )
                        return GuardrailResult(
                            allowed=True,
                            reason=f"搜索结果缺少必要字段: {', '.join(missing)}",
                            level=level,
                        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest backend/tests/test_guardrail.py::test_output_accommodation_price_per_night_accepted backend/tests/test_guardrail.py::test_output_accommodation_price_accepted backend/tests/test_guardrail.py::test_output_accommodation_no_price_at_all_is_error -v`
Expected: PASS

- [ ] **Step 5: 运行全部 guardrail 测试确保无回归**

Run: `python -m pytest backend/tests/test_guardrail.py -v`
Expected: All PASS (包括原有的 `test_output_missing_accommodation_location_is_warn`)

- [ ] **Step 6: Commit**

```bash
git add backend/harness/guardrail.py backend/tests/test_guardrail.py
git commit -m "fix(guardrail): accept price_per_night as alias for price in accommodation results"
```

---

### Task 3: update_plan_state 返回 previous_value

**Files:**
- Modify: `backend/tools/update_plan_state.py:393`
- Test: `backend/tests/test_trace_api.py` (新增单元测试)

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_trace_api.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_update_plan_state_returns_previous_value():
    """update_plan_state should include previous_value in result."""
    from state.models import TravelPlanState
    from tools.update_plan_state import make_update_plan_state_tool

    plan = TravelPlanState(session_id="s1", phase=1, destination="北京")
    tool_fn = make_update_plan_state_tool(plan)
    result = await tool_fn(field="destination", value="东京")
    assert result["previous_value"] == "北京"
    assert result["updated_field"] == "destination"
    assert plan.destination == "东京"


@pytest.mark.asyncio
async def test_update_plan_state_previous_value_none_for_new_field():
    """previous_value is None when field was not previously set."""
    from state.models import TravelPlanState
    from tools.update_plan_state import make_update_plan_state_tool

    plan = TravelPlanState(session_id="s1", phase=1)
    tool_fn = make_update_plan_state_tool(plan)
    result = await tool_fn(field="destination", value="东京")
    assert result["previous_value"] is None
    assert result["updated_field"] == "destination"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest backend/tests/test_trace_api.py::test_update_plan_state_returns_previous_value -v`
Expected: FAIL — `KeyError: 'previous_value'`

- [ ] **Step 3: 实现 previous_value 返回**

修改 `backend/tools/update_plan_state.py` 中 `update_plan_state` 函数。在字段写入逻辑之前（`if field == "backtrack":` 之后、`if field == "destination":` 之前），插入旧值快照代码：

```python
        # Snapshot previous value for state_changes tracking
        previous_value = _snapshot_field(plan, field)
```

在文件中添加辅助函数（在 `_current_comparable_value` 之后）：

```python
def _snapshot_field(plan: TravelPlanState, field: str) -> Any:
    """Capture current field value before update, for state diff tracking."""
    if field == "destination":
        return plan.destination if plan.destination else None
    if field == "dates":
        return plan.dates.to_dict() if plan.dates else None
    if field == "travelers":
        return plan.travelers.to_dict() if plan.travelers else None
    if field == "budget":
        return plan.budget.to_dict() if plan.budget else None
    if field == "accommodation":
        return plan.accommodation.to_dict() if plan.accommodation else None
    if field == "phase3_step":
        return plan.phase3_step
    if field == "selected_skeleton_id":
        return plan.selected_skeleton_id
    if field == "selected_transport":
        return plan.selected_transport
    if field in ("preferences", "constraints", "daily_plans"):
        return len(getattr(plan, field, []))
    return None
```

修改 return 语句：

```python
        return {
            "updated_field": field,
            "new_value": str(value)[:200],
            "previous_value": previous_value,
        }
```

注意：backtrack 分支已经有 return，不需要改。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest backend/tests/test_trace_api.py::test_update_plan_state_returns_previous_value backend/tests/test_trace_api.py::test_update_plan_state_previous_value_none_for_new_field -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/tools/update_plan_state.py backend/tests/test_trace_api.py
git commit -m "feat(update_plan_state): return previous_value for state diff tracking"
```

---

### Task 4: generate_context 返回 tuple[str, list[str]]

**Files:**
- Modify: `backend/memory/manager.py:97-104`
- Modify: `backend/main.py:1379-1382`
- Modify: `backend/agent/loop.py:424-427`
- Modify: `backend/tests/test_agent_loop.py:78-79`
- Modify: `backend/tests/test_telemetry_agent_loop.py:38-39`
- Modify: `backend/tests/test_memory_integration.py:274, 315`
- Test: `backend/tests/test_memory_manager.py`

- [ ] **Step 1: 写失败测试**

修改 `backend/tests/test_memory_manager.py` 中已有的 `test_generate_context_includes_active_stored_memory`，改为验证 tuple 返回：

```python
@pytest.mark.asyncio
async def test_generate_context_includes_active_stored_memory(tmp_path: Path):
    manager = MemoryManager(data_dir=str(tmp_path))
    await manager.store.upsert_item(make_item())

    text, item_ids = await manager.generate_context("u1", TravelPlanState(session_id="s1"))

    assert "## 核心用户画像" in text
    assert "节奏轻松" in text
    assert "mem-1" in item_ids
    assert isinstance(item_ids, list)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest backend/tests/test_memory_manager.py::test_generate_context_includes_active_stored_memory -v`
Expected: FAIL — `ValueError: not enough values to unpack`

- [ ] **Step 3: 实现 generate_context tuple 返回**

修改 `backend/memory/manager.py` 中 `generate_context`：

```python
    async def generate_context(
        self, user_id: str, plan: TravelPlanState
    ) -> tuple[str, list[str]]:
        items = await self.store.list_items(user_id)
        retrieved = RetrievedMemory(
            core=self.retriever.retrieve_core_profile(items),
            trip=self.retriever.retrieve_trip_memory(items, plan),
            phase=self.retriever.retrieve_phase_relevant(items, plan, plan.phase),
        )
        item_ids = [it.id for it in retrieved.core + retrieved.trip + retrieved.phase]
        return format_memory_context(retrieved), item_ids
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest backend/tests/test_memory_manager.py::test_generate_context_includes_active_stored_memory -v`
Expected: PASS

- [ ] **Step 5: 更新所有调用方 — main.py**

修改 `backend/main.py:1379-1382`：

```python
        if config.memory.enabled:
            memory_context, _recalled_ids = await memory_mgr.generate_context(
                req.user_id, plan
            )
        else:
            memory_context = "暂无相关用户记忆"
            _recalled_ids = []
```

注意：`_recalled_ids` 暂时用下划线前缀忽略，Task 8 会使用它。

- [ ] **Step 6: 更新所有调用方 — loop.py**

修改 `backend/agent/loop.py:424-427`：

```python
        memory_context, _recalled_ids = (
            await self.memory_mgr.generate_context(self.user_id, self.plan)
            if self.memory_enabled
            else ("暂无相关用户记忆", [])
        )
```

- [ ] **Step 7: 更新所有 Fake/Mock — test_agent_loop.py**

修改 `backend/tests/test_agent_loop.py:78-79`：

```python
    async def generate_context(self, user_id: str, plan: TravelPlanState) -> tuple[str, list[str]]:
        return f"memory:{user_id}", []
```

- [ ] **Step 8: 更新所有 Fake/Mock — test_telemetry_agent_loop.py**

修改 `backend/tests/test_telemetry_agent_loop.py:38-39`：

```python
    async def generate_context(self, user_id: str, plan) -> tuple[str, list[str]]:
        return "", []
```

- [ ] **Step 9: 更新所有 Fake/Mock — test_memory_integration.py**

修改 `backend/tests/test_memory_integration.py` 中两个 `fake_generate_context`：

第一个（约274行）：
```python
    async def fake_generate_context(self, user_id: str, plan: TravelPlanState) -> tuple[str, list[str]]:
        nonlocal spy_called
        spy_called = True
        return "Fake memory context for testing", []
```

第二个（约315行）：
```python
    async def fake_generate_context(self, user_id: str, plan: TravelPlanState) -> tuple[str, list[str]]:
        raise AssertionError("generate_context should not be called when memory is disabled")
```

- [ ] **Step 10: 运行全部相关测试确认通过**

Run: `python -m pytest backend/tests/test_memory_manager.py backend/tests/test_agent_loop.py backend/tests/test_telemetry_agent_loop.py backend/tests/test_memory_integration.py -v --timeout=30`
Expected: All PASS

- [ ] **Step 11: Commit**

```bash
git add backend/memory/manager.py backend/main.py backend/agent/loop.py \
  backend/tests/test_memory_manager.py backend/tests/test_agent_loop.py \
  backend/tests/test_telemetry_agent_loop.py backend/tests/test_memory_integration.py
git commit -m "feat(memory): generate_context returns tuple[str, list[str]] with recalled item IDs"
```

---

### Task 5: on_validate 记录 state_changes + validation_errors

**Files:**
- Modify: `backend/main.py:394-414` (on_validate hook)
- Modify: `backend/main.py:210-232` (_record_tool_result_stats)
- Test: `backend/tests/test_realtime_validation_hook.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_realtime_validation_hook.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_on_validate_records_state_changes_to_stats(app, sessions):
    """update_plan_state should write state_changes to the latest ToolCallRecord."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]
        plan.phase = 1
        plan.destination = "北京"

        agent = session["agent"]

        async def fake_chat(messages, tools=None, stream=True):
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_dest",
                    name="update_plan_state",
                    arguments={"field": "destination", "value": "东京"},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat

        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "去东京"},
        )

    assert resp.status_code == 200
    stats = session["stats"]
    ups_records = [r for r in stats.tool_calls if r.tool_name == "update_plan_state"]
    assert len(ups_records) >= 1
    rec = ups_records[-1]
    assert rec.state_changes is not None
    assert len(rec.state_changes) == 1
    assert rec.state_changes[0]["field"] == "destination"
    assert rec.state_changes[0]["before"] == "北京"
    assert rec.state_changes[0]["after"] == "东京"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest backend/tests/test_realtime_validation_hook.py::test_on_validate_records_state_changes_to_stats -v`
Expected: FAIL — `assert rec.state_changes is not None` fails (it's None)

- [ ] **Step 3: 修改 _record_tool_result_stats 支持新字段**

在 `backend/main.py` 中，修改 `_record_tool_result_stats` 来接受新的可选参数，并传递给 `record_tool_call`：

```python
def _record_tool_result_stats(
    *,
    stats: SessionStats | None,
    tool_call_names: dict[str, str],
    result: ToolResult,
    phase: int,
) -> None:
    if stats is None:
        return
    tool_name = tool_call_names.get(result.tool_call_id)
    if not tool_name:
        return
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    duration = metadata.get("duration_ms", 0.0)
    if not isinstance(duration, (int, float)):
        duration = 0.0
    stats.record_tool_call(
        tool_name=tool_name,
        duration_ms=float(duration),
        status=result.status,
        error_code=result.error_code,
        phase=phase,
    )
```

同时修改 `SessionStats.record_tool_call` 来接受可选参数（`backend/telemetry/stats.py`）：

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
    ) -> None:
        self.tool_calls.append(ToolCallRecord(
            tool_name=tool_name,
            duration_ms=duration_ms,
            status=status,
            error_code=error_code,
            phase=phase,
            parallel_group=parallel_group,
        ))
```

- [ ] **Step 4: 修改 on_validate 钩子写入 state_changes 和 validation_errors**

在 `backend/main.py` 中，修改 `on_validate`：

```python
        async def on_validate(**kwargs):
            if kwargs.get("tool_name") == "update_plan_state":
                tc = kwargs.get("tool_call")
                result = kwargs.get("result")
                arguments = tc.arguments if tc and tc.arguments else {}
                field = arguments.get("field", "")
                value = arguments.get("value")

                # Record state_changes from previous_value in tool result
                if (
                    result
                    and result.status == "success"
                    and isinstance(result.data, dict)
                    and stats.tool_calls
                ):
                    prev_val = result.data.get("previous_value")
                    stats.tool_calls[-1].state_changes = [
                        {"field": field, "before": prev_val, "after": value}
                    ]

                errors = validate_incremental(plan, field, value)
                if field in ("selected_transport", "accommodation"):
                    errors.extend(validate_lock_budget(plan))

                if errors:
                    # Record validation_errors to stats
                    if stats.tool_calls:
                        stats.tool_calls[-1].validation_errors = errors
                    session = sessions.get(plan.session_id)
                    if session:
                        session["messages"].append(
                            Message(
                                role=Role.SYSTEM,
                                content="[实时约束检查]\n"
                                + "\n".join(f"- {error}" for error in errors),
                            )
                        )
```

注意：需要在 `on_validate` 的闭包中能访问到 `stats`。查看上下文，`stats` 已经在 `_build_agent` 的闭包作用域中，通过 `session["stats"]` 访问。需要确认。

实际上 `on_validate` 在 `_build_agent` 内部定义，但 `stats` 不是直接的局部变量。需要从 session 中获取：

```python
        async def on_validate(**kwargs):
            if kwargs.get("tool_name") == "update_plan_state":
                tc = kwargs.get("tool_call")
                result = kwargs.get("result")
                arguments = tc.arguments if tc and tc.arguments else {}
                field = arguments.get("field", "")
                value = arguments.get("value")

                session = sessions.get(plan.session_id)
                stats: SessionStats | None = session.get("stats") if session else None

                # Record state_changes from previous_value in tool result
                if (
                    result
                    and result.status == "success"
                    and isinstance(result.data, dict)
                    and stats
                    and stats.tool_calls
                ):
                    prev_val = result.data.get("previous_value")
                    stats.tool_calls[-1].state_changes = [
                        {"field": field, "before": prev_val, "after": value}
                    ]

                errors = validate_incremental(plan, field, value)
                if field in ("selected_transport", "accommodation"):
                    errors.extend(validate_lock_budget(plan))

                if errors:
                    if stats and stats.tool_calls:
                        stats.tool_calls[-1].validation_errors = errors
                    if session:
                        session["messages"].append(
                            Message(
                                role=Role.SYSTEM,
                                content="[实时约束检查]\n"
                                + "\n".join(f"- {error}" for error in errors),
                            )
                        )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest backend/tests/test_realtime_validation_hook.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add backend/main.py backend/telemetry/stats.py
git commit -m "feat(hooks): on_validate records state_changes and validation_errors to ToolCallRecord"
```

---

### Task 6: on_soft_judge 记录 judge_scores

**Files:**
- Modify: `backend/main.py:525-553` (on_soft_judge hook)

- [ ] **Step 1: 修改 on_soft_judge 将评分写入 stats**

在 `backend/main.py` 中，修改 `on_soft_judge` 函数。在 `score = parse_judge_response(...)` 之后、`if score.suggestions:` 之前，添加：

```python
            # Record judge scores to stats
            session_stats: SessionStats | None = session.get("stats")
            if session_stats and session_stats.tool_calls:
                session_stats.tool_calls[-1].judge_scores = {
                    "overall": score.overall,
                    "suggestions_count": len(score.suggestions),
                }
```

注意：`parse_judge_response` 返回的 `JudgeScore` 对象的字段需要确认。查看 `score.overall` 和 `score.suggestions` 在当前代码中已被使用（main.py:546-547），所以确认可用。

- [ ] **Step 2: 运行现有 judge 测试确认无回归**

Run: `python -m pytest backend/tests/ -k "judge" -v --timeout=30`
Expected: PASS (或无匹配测试)

- [ ] **Step 3: Commit**

```bash
git add backend/main.py
git commit -m "feat(hooks): on_soft_judge records judge_scores to ToolCallRecord"
```

---

### Task 7: 并行组标记

**Files:**
- Modify: `backend/agent/loop.py:192-248`
- Modify: `backend/main.py:210-232`

- [ ] **Step 1: 在 AgentLoop 添加并行组计数器**

在 `backend/agent/loop.py` 的 `AgentLoop.__init__` 中添加：

```python
        self._parallel_group_counter: int = 0
```

- [ ] **Step 2: 并行批处理时设置 parallel_group 到 ToolResult.metadata**

修改 `backend/agent/loop.py` 中并行批处理逻辑。在 `batch_results = await self.tool_engine.execute_batch(...)` 之前，增加计数器递增：

```python
                            self._parallel_group_counter += 1
                            current_group = self._parallel_group_counter
```

在 `for (batch_idx, batch_tc), batch_result in zip(...)` 循环内，yield `LLMChunk` 之前，向 `batch_result.metadata` 注入 `parallel_group`：

```python
                                if batch_result.metadata is None:
                                    batch_result.metadata = {}
                                batch_result.metadata["parallel_group"] = current_group
```

- [ ] **Step 3: _record_tool_result_stats 读取 parallel_group**

修改 `backend/main.py` 中 `_record_tool_result_stats`，在 `stats.record_tool_call(...)` 调用中传入 `parallel_group`：

```python
    parallel_group = metadata.get("parallel_group")
    stats.record_tool_call(
        tool_name=tool_name,
        duration_ms=float(duration),
        status=result.status,
        error_code=result.error_code,
        phase=phase,
        parallel_group=parallel_group,
    )
```

- [ ] **Step 4: 运行相关测试确认无回归**

Run: `python -m pytest backend/tests/test_agent_loop.py backend/tests/test_trace_api.py -v --timeout=30`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/loop.py backend/main.py
git commit -m "feat(loop): track parallel tool execution groups via ToolResult.metadata"
```

---

### Task 8: Memory 命中记录 + memory_recall SSE 事件

**Files:**
- Modify: `backend/main.py:1379-1382, 1402-1420`

- [ ] **Step 1: 在 chat 端点中记录 memory_hits 到 stats**

修改 `backend/main.py` 中 chat 端点（约1379行），在已有的 `generate_context` 调用处：

```python
        if config.memory.enabled:
            memory_context, recalled_ids = await memory_mgr.generate_context(
                req.user_id, plan
            )
        else:
            memory_context = "暂无相关用户记忆"
            recalled_ids = []
```

在 `sys_msg = context_mgr.build_system_message(...)` 之后，记录 memory_hits：

```python
        if recalled_ids:
            from telemetry.stats import MemoryHitRecord
            session_stats: SessionStats | None = session.get("stats")
            if session_stats is not None:
                # Count by category (approximation: core items come first in retriever)
                items = await memory_mgr.store.list_items(req.user_id) if config.memory.enabled else []
                recalled_set = set(recalled_ids)
                core_ids = {it.id for it in memory_mgr.retriever.retrieve_core_profile(items)} if items else set()
                trip_ids = {it.id for it in memory_mgr.retriever.retrieve_trip_memory(items, plan)} if items else set()
                phase_ids = {it.id for it in memory_mgr.retriever.retrieve_phase_relevant(items, plan, plan.phase)} if items else set()
                session_stats.memory_hits.append(MemoryHitRecord(
                    item_ids=recalled_ids,
                    core_count=len(recalled_set & core_ids),
                    trip_count=len(recalled_set & trip_ids),
                    phase_count=len(recalled_set & phase_ids),
                ))
```

实际上，上面的实现会重新执行 retriever 逻辑，浪费性能。更好的做法是让 `generate_context` 直接返回分类计数。让我简化——在 `generate_context` 中直接返回计数信息：

修改 `backend/memory/manager.py`，在 `generate_context` 返回更丰富的信息：

```python
    async def generate_context(
        self, user_id: str, plan: TravelPlanState
    ) -> tuple[str, list[str], tuple[int, int, int]]:
        items = await self.store.list_items(user_id)
        retrieved = RetrievedMemory(
            core=self.retriever.retrieve_core_profile(items),
            trip=self.retriever.retrieve_trip_memory(items, plan),
            phase=self.retriever.retrieve_phase_relevant(items, plan, plan.phase),
        )
        item_ids = [it.id for it in retrieved.core + retrieved.trip + retrieved.phase]
        counts = (len(retrieved.core), len(retrieved.trip), len(retrieved.phase))
        return format_memory_context(retrieved), item_ids, counts
```

不，这又要改所有调用方了。算了，保持 `tuple[str, list[str]]`，在 main.py 中用长度近似：

```python
        if recalled_ids:
            from telemetry.stats import MemoryHitRecord
            session_stats: SessionStats | None = session.get("stats")
            if session_stats is not None:
                session_stats.memory_hits.append(MemoryHitRecord(
                    item_ids=recalled_ids,
                    core_count=len(recalled_ids),  # total count, breakdown not critical
                    trip_count=0,
                    phase_count=0,
                ))
```

实际上更好的方案：直接修改返回类型为 `MemoryRecallResult` namedtuple。但这增加复杂度。

最简方案：改为返回 3 个值的 tuple。由于 Task 4 已经改了签名，这里只需要再扩展一次。

让我重新考虑。为了避免过多改动，还是保持 `tuple[str, list[str]]`，memory_hits 中的 core/trip/phase 计数设为总数（简单但够用）。TraceViewer 显示 "命中 N 条记忆" 就够了。

```python
        if recalled_ids:
            from telemetry.stats import MemoryHitRecord
            session_stats = session.get("stats")
            if session_stats is not None:
                session_stats.memory_hits.append(MemoryHitRecord(
                    item_ids=recalled_ids,
                    core_count=len(recalled_ids),
                    trip_count=0,
                    phase_count=0,
                ))
```

- [ ] **Step 2: 在 event_stream 中发送 memory_recall SSE 事件**

修改 `backend/main.py` 的 `event_stream()` 函数。在 `llm_started_at = time.monotonic()` 之后、`async for chunk in agent.run(...)` 之前，添加：

```python
            if recalled_ids:
                yield json.dumps({
                    "type": "memory_recall",
                    "item_ids": recalled_ids,
                }, ensure_ascii=False)
```

注意：`recalled_ids` 需要在 `event_stream` 闭包中可见。由于 `event_stream` 定义在 chat endpoint 内部，而 `recalled_ids` 也在同一作用域，可以直接访问。

- [ ] **Step 3: 运行 chat 相关测试确认无回归**

Run: `python -m pytest backend/tests/test_realtime_validation_hook.py backend/tests/test_trace_api.py -v --timeout=30`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add backend/main.py
git commit -m "feat(memory): record memory_hits to stats and emit memory_recall SSE event"
```

---

### Task 9: build_trace 消费新数据

**Files:**
- Modify: `backend/api/trace.py:12-115`
- Modify: `backend/main.py:1646-1653`
- Test: `backend/tests/test_trace_api.py`

- [ ] **Step 1: 写失败测试 — state_changes 和 compression_event**

在 `backend/tests/test_trace_api.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_trace_state_changes_from_stats(app):
    """state_changes populated from ToolCallRecord.state_changes."""
    sessions = _get_sessions(app)
    session_id = "test-state-changes"
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai", model="gpt-4o", input_tokens=100,
        output_tokens=50, duration_ms=200.0, phase=1, iteration=1,
    )
    stats.record_tool_call(
        tool_name="update_plan_state", duration_ms=50.0,
        status="ok", error_code=None, phase=1,
    )
    stats.tool_calls[-1].state_changes = [
        {"field": "destination", "before": None, "after": "东京"}
    ]
    sessions[session_id] = {
        "stats": stats, "messages": [], "plan": None,
        "compression_events": [],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    data = resp.json()

    assert data["iterations"][0]["state_changes"] == [
        {"field": "destination", "before": None, "after": "东京"}
    ]


@pytest.mark.asyncio
async def test_trace_compression_event(app):
    """compression_event populated from session compression_events."""
    sessions = _get_sessions(app)
    session_id = "test-compression"
    stats = SessionStats()
    ts = 1000.0
    stats.record_llm_call(
        provider="openai", model="gpt-4o", input_tokens=100,
        output_tokens=50, duration_ms=200.0, phase=1, iteration=1,
    )
    stats.llm_calls[-1].timestamp = ts
    sessions[session_id] = {
        "stats": stats, "messages": [], "plan": None,
        "compression_events": [
            {
                "timestamp": ts - 1,
                "mode": "tool_compaction",
                "reason": "test compression",
                "message_count_before": 20,
                "message_count_after": 10,
            }
        ],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    data = resp.json()

    assert data["iterations"][0]["compression_event"] is not None
    assert "tool_compaction" in data["iterations"][0]["compression_event"]


@pytest.mark.asyncio
async def test_trace_parallel_group(app):
    """parallel_group populated from ToolCallRecord."""
    sessions = _get_sessions(app)
    session_id = "test-parallel"
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai", model="gpt-4o", input_tokens=100,
        output_tokens=50, duration_ms=200.0, phase=1, iteration=1,
    )
    stats.record_tool_call(
        tool_name="web_search", duration_ms=100.0,
        status="ok", error_code=None, phase=1,
        parallel_group=1,
    )
    stats.record_tool_call(
        tool_name="search_flights", duration_ms=150.0,
        status="ok", error_code=None, phase=1,
        parallel_group=1,
    )
    sessions[session_id] = {
        "stats": stats, "messages": [], "plan": None,
        "compression_events": [],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    data = resp.json()

    tools = data["iterations"][0]["tool_calls"]
    assert tools[0]["parallel_group"] == 1
    assert tools[1]["parallel_group"] == 1


@pytest.mark.asyncio
async def test_trace_memory_hits(app):
    """memory_hits populated from SessionStats.memory_hits."""
    from telemetry.stats import MemoryHitRecord
    sessions = _get_sessions(app)
    session_id = "test-memory-hits"
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai", model="gpt-4o", input_tokens=100,
        output_tokens=50, duration_ms=200.0, phase=1, iteration=1,
    )
    stats.memory_hits.append(MemoryHitRecord(
        item_ids=["m1", "m2"], core_count=1, trip_count=1, phase_count=0,
        timestamp=stats.llm_calls[-1].timestamp,
    ))
    sessions[session_id] = {
        "stats": stats, "messages": [], "plan": None,
        "compression_events": [],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    data = resp.json()

    hits = data["iterations"][0]["memory_hits"]
    assert hits is not None
    assert hits["item_ids"] == ["m1", "m2"]
    assert hits["core"] == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest backend/tests/test_trace_api.py::test_trace_state_changes_from_stats -v`
Expected: FAIL — state_changes 仍然是 `[]`

- [ ] **Step 3: 重构 build_trace 消费新数据**

修改 `backend/api/trace.py`：

```python
from __future__ import annotations
from telemetry.stats import SessionStats, ToolCallRecord, MemoryHitRecord, LLMCallRecord, lookup_pricing

# Fallback write-effect tools — used when no ToolEngine is available
_WRITE_TOOLS = frozenset({
    "update_plan_state",
    "assemble_day_plan",
    "generate_summary",
})


def _collect_state_changes(tool_calls: list[ToolCallRecord]) -> list[dict]:
    """Extract state_changes from update_plan_state ToolCallRecords."""
    changes = []
    for tc in tool_calls:
        if tc.state_changes:
            changes.extend(tc.state_changes)
    return changes


def _match_compression_event(
    llm_rec: LLMCallRecord,
    events: list[dict] | None,
) -> str | None:
    """Find compression event that occurred just before this LLM call."""
    if not events:
        return None
    best = None
    for evt in events:
        evt_ts = evt.get("timestamp", 0)
        if evt_ts <= llm_rec.timestamp:
            best = evt
    if best:
        mode = best.get("mode", "unknown")
        reason = best.get("reason", "")
        return f"{mode}: {reason}"
    return None


def _match_memory_hits(
    llm_rec: LLMCallRecord,
    hits: list[MemoryHitRecord],
) -> dict | None:
    """Find memory hit record near this LLM call timestamp."""
    if not hits:
        return None
    for hit in hits:
        if abs(hit.timestamp - llm_rec.timestamp) < 5.0:
            return {
                "item_ids": hit.item_ids,
                "core": hit.core_count,
                "trip": hit.trip_count,
                "phase": hit.phase_count,
            }
    return None


def build_trace(session_id: str, session: dict, *, tool_engine=None) -> dict:
    """Build structured trace from session's stats data."""
    stats: SessionStats = session.get("stats", SessionStats())
    compression_events: list[dict] = session.get("compression_events", [])
    summary = stats.to_dict()

    def _get_side_effect(tool_name: str) -> str:
        if tool_engine is not None:
            tool_def = tool_engine._tools.get(tool_name)
            if tool_def is not None:
                return tool_def.side_effect
        return "write" if tool_name in _WRITE_TOOLS else "read"

    # Enrich summary with cost_usd per model
    for model_name, model_data in summary.get("by_model", {}).items():
        pricing = lookup_pricing(model_name)
        if pricing:
            cost = (model_data["input_tokens"] / 1_000_000) * pricing["input"]
            cost += (model_data["output_tokens"] / 1_000_000) * pricing["output"]
            model_data["cost_usd"] = round(cost, 6)
        else:
            model_data["cost_usd"] = 0.0
        model_data.pop("duration_ms", None)

    # Enrich by_tool with avg_duration_ms and rename duration_ms to total_duration_ms
    for tool_data in summary.get("by_tool", {}).values():
        calls = tool_data.get("calls", 0)
        total_dur = tool_data.pop("duration_ms", 0.0)
        tool_data.pop("errors", None)
        tool_data["total_duration_ms"] = total_dur
        tool_data["avg_duration_ms"] = round(total_dur / calls, 1) if calls > 0 else 0.0

    # Build iterations — each LLM call starts a new iteration
    iterations = []
    llm_calls = stats.llm_calls
    tool_calls = list(stats.tool_calls)

    tool_idx = 0
    for i, llm in enumerate(llm_calls):
        next_llm_ts = llm_calls[i + 1].timestamp if i + 1 < len(llm_calls) else float("inf")
        iter_tools: list[ToolCallRecord] = []
        iter_tool_dicts = []
        while tool_idx < len(tool_calls) and tool_calls[tool_idx].timestamp < next_llm_ts:
            tc = tool_calls[tool_idx]
            iter_tools.append(tc)
            iter_tool_dicts.append({
                "name": tc.tool_name,
                "duration_ms": round(tc.duration_ms, 1),
                "status": tc.status,
                "side_effect": _get_side_effect(tc.tool_name),
                "arguments_preview": "",
                "result_preview": "",
                "parallel_group": tc.parallel_group,
                "validation_errors": tc.validation_errors,
                "judge_scores": tc.judge_scores,
            })
            tool_idx += 1

        pricing = lookup_pricing(llm.model)
        cost = 0.0
        if pricing:
            cost = (llm.input_tokens / 1_000_000) * pricing["input"]
            cost += (llm.output_tokens / 1_000_000) * pricing["output"]

        iterations.append({
            "index": i + 1,
            "phase": llm.phase,
            "llm_call": {
                "provider": llm.provider,
                "model": llm.model,
                "input_tokens": llm.input_tokens,
                "output_tokens": llm.output_tokens,
                "duration_ms": round(llm.duration_ms, 1),
                "cost_usd": round(cost, 6),
            },
            "tool_calls": iter_tool_dicts,
            "state_changes": _collect_state_changes(iter_tools),
            "compression_event": _match_compression_event(llm, compression_events),
            "memory_hits": _match_memory_hits(llm, stats.memory_hits),
        })

    # Handle remaining/orphan tool calls (no parent LLM call)
    remaining_tools: list[ToolCallRecord] = []
    remaining_tool_dicts = []
    while tool_idx < len(tool_calls):
        tc = tool_calls[tool_idx]
        remaining_tools.append(tc)
        remaining_tool_dicts.append({
            "name": tc.tool_name,
            "duration_ms": round(tc.duration_ms, 1),
            "status": tc.status,
            "side_effect": _get_side_effect(tc.tool_name),
            "arguments_preview": "",
            "result_preview": "",
            "parallel_group": tc.parallel_group,
            "validation_errors": tc.validation_errors,
            "judge_scores": tc.judge_scores,
        })
        tool_idx += 1

    if remaining_tool_dicts:
        iterations.append({
            "index": len(iterations) + 1,
            "phase": remaining_tools[0].phase if remaining_tools else 0,
            "llm_call": None,
            "tool_calls": remaining_tool_dicts,
            "state_changes": _collect_state_changes(remaining_tools),
            "compression_event": None,
            "memory_hits": None,
        })

    return {
        "session_id": session_id,
        "total_iterations": len(iterations),
        "summary": summary,
        "iterations": iterations,
    }
```

- [ ] **Step 4: 更新 trace 端点传入 compression_events**

修改 `backend/main.py:1646-1653`。当前 session dict 已经包含 `compression_events`，`build_trace` 现在从 `session` dict 中读取，所以调用方不需要改。确认现有调用 `build_trace(session_id, session, tool_engine=engine)` 中的 `session` 已包含 `compression_events` 键即可。

但需要确保不存在 `compression_events` 键时的回退。在 `build_trace` 中已有 `session.get("compression_events", [])`。

对于现有测试中的 session dict（如 `{"stats": stats, "messages": [], "plan": None}`），没有 `compression_events` 键，`build_trace` 会默认 `[]`，所以旧测试不会 break。

新测试中需要加 `"compression_events": []`（已在 Step 1 中加了）。

但需要给 compression_events 中的每个事件添加 `timestamp`。当前 `compression_events` 数据结构（main.py:442-455, 508-521）没有 `timestamp` 字段。需要在 main.py 中添加：

在 `backend/main.py` 的 `on_before_llm` 中两个 `compression_events.append(...)` 处，添加 `"timestamp": time.time()` 字段。

- [ ] **Step 5: 运行新测试确认通过**

Run: `python -m pytest backend/tests/test_trace_api.py::test_trace_state_changes_from_stats backend/tests/test_trace_api.py::test_trace_compression_event backend/tests/test_trace_api.py::test_trace_parallel_group backend/tests/test_trace_api.py::test_trace_memory_hits -v`
Expected: All PASS

- [ ] **Step 6: 运行全部 trace 测试确认无回归**

Run: `python -m pytest backend/tests/test_trace_api.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add backend/api/trace.py backend/main.py backend/tests/test_trace_api.py
git commit -m "feat(trace): build_trace consumes state_changes, compression_events, parallel_group, validation_errors, judge_scores, memory_hits"
```

---

### Task 10: 前端类型扩展

**Files:**
- Modify: `frontend/src/types/trace.ts`

- [ ] **Step 1: 扩展 TraceToolCall + MemoryHit + TraceIteration**

修改 `frontend/src/types/trace.ts`：

```typescript
export interface TraceToolCall {
  name: string
  duration_ms: number
  status: 'success' | 'error' | 'skipped'
  side_effect: 'read' | 'write'
  arguments_preview: string
  result_preview: string
  parallel_group: number | null
  validation_errors: string[] | null
  judge_scores: Record<string, number> | null
}

export interface StateChange {
  field: string
  before: unknown
  after: unknown
}

export interface MemoryHit {
  item_ids: string[]
  core: number
  trip: number
  phase: number
}

export interface TraceIteration {
  index: number
  phase: number
  llm_call: {
    provider: string
    model: string
    input_tokens: number
    output_tokens: number
    duration_ms: number
    cost_usd: number
  } | null
  tool_calls: TraceToolCall[]
  state_changes: StateChange[]
  compression_event: string | null
  memory_hits: MemoryHit | null
}

export interface TraceSummary {
  total_input_tokens: number
  total_output_tokens: number
  total_llm_duration_ms: number
  total_tool_duration_ms: number
  estimated_cost_usd: number
  llm_call_count: number
  tool_call_count: number
  by_model: Record<string, {
    calls: number
    input_tokens: number
    output_tokens: number
    cost_usd: number
  }>
  by_tool: Record<string, {
    calls: number
    total_duration_ms: number
    avg_duration_ms: number
  }>
}

export interface SessionTrace {
  session_id: string
  total_iterations: number
  summary: TraceSummary
  iterations: TraceIteration[]
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/types/trace.ts
git commit -m "feat(types): extend TraceToolCall, add MemoryHit interface, extend TraceIteration"
```

---

### Task 11: TraceViewer 渲染扩展

**Files:**
- Modify: `frontend/src/components/TraceViewer.tsx`
- Modify: `frontend/src/styles/trace-viewer.css`

- [ ] **Step 1: 更新 ToolCallRow 渲染 parallel_group, validation_errors, judge_scores**

在 `frontend/src/components/TraceViewer.tsx` 中，修改 `ToolCallRow`：

```tsx
function ToolCallRow({ tool, maxDuration }: { tool: TraceToolCall; maxDuration: number }) {
  const widthPct = maxDuration > 0 ? (tool.duration_ms / maxDuration) * 100 : 0
  return (
    <div className="trace-tool-row">
      <span className="tool-name">{tool.name}</span>
      <span className={`tool-side-effect ${tool.side_effect}`}>{tool.side_effect}</span>
      {tool.parallel_group != null && (
        <span className="tool-parallel-badge" title={`并行组 ${tool.parallel_group}`}>P</span>
      )}
      <div className="tool-bar-container">
        <div
          className={`tool-bar status-${tool.status}`}
          style={{ width: `${Math.max(widthPct, 2)}%` }}
        />
      </div>
      <span className="tool-duration">{formatDuration(tool.duration_ms)}</span>
      {tool.validation_errors && tool.validation_errors.length > 0 && (
        <div className="tool-validation-errors">
          {tool.validation_errors.map((err, i) => (
            <div key={i} className="validation-error-item">{err}</div>
          ))}
        </div>
      )}
      {tool.judge_scores && (
        <div className="tool-judge-scores">
          {Object.entries(tool.judge_scores).map(([key, val]) => (
            <span key={key} className="judge-score-tag">{key}: {val}</span>
          ))}
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 2: 更新 IterationRow 渲染 compression_event 和 memory_hits**

在 `frontend/src/components/TraceViewer.tsx` 中，在 `IterationRow` 的 header 区域添加 compression 标记，在 detail 区域添加 memory_hits 显示：

```tsx
function IterationRow({ iteration, maxLLMDuration }: { iteration: TraceIteration; maxLLMDuration: number }) {
  const [expanded, setExpanded] = useState(false)
  const llm = iteration.llm_call
  const barPct = llm && maxLLMDuration > 0 ? (llm.duration_ms / maxLLMDuration) * 100 : 0
  const maxToolDuration = Math.max(...iteration.tool_calls.map((t) => t.duration_ms), 1)

  return (
    <div className="trace-iteration">
      <div className="trace-iteration-header" onClick={() => setExpanded(!expanded)}>
        <span className="iter-index">#{iteration.index}</span>
        <span className="iter-phase">P{iteration.phase}</span>
        {iteration.compression_event && (
          <span className="iter-compression" title={iteration.compression_event}>C</span>
        )}
        {llm && (
          <>
            <div className="iter-bar-container">
              <div
                className={`iter-bar ${getProviderClass(llm.provider)}`}
                style={{ width: `${Math.max(barPct, 3)}%` }}
              />
            </div>
            <span className="iter-model">{llm.model}</span>
            <span className="iter-tokens">{formatTokens(llm.input_tokens + llm.output_tokens)}</span>
            <span className="iter-cost">{formatCost(llm.cost_usd)}</span>
          </>
        )}
        <span className={`iter-expand-icon ${expanded ? 'expanded' : ''}`}>▶</span>
      </div>
      {expanded && (
        <div className="trace-iteration-detail">
          {iteration.compression_event && (
            <div className="trace-compression-info">{iteration.compression_event}</div>
          )}
          {iteration.tool_calls.length > 0 ? (
            <div className="trace-tool-list">
              {iteration.tool_calls.map((tool, i) => (
                <ToolCallRow key={i} tool={tool} maxDuration={maxToolDuration} />
              ))}
            </div>
          ) : (
            <div className="trace-no-tools">No tool calls</div>
          )}
          <StateDiffPanel changes={iteration.state_changes} />
          {iteration.memory_hits && (
            <div className="trace-memory-hits">
              命中 {iteration.memory_hits.item_ids.length} 条记忆
              （core {iteration.memory_hits.core} / trip {iteration.memory_hits.trip} / phase {iteration.memory_hits.phase}）
            </div>
          )}
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 3: 添加新 CSS 样式**

在 `frontend/src/styles/trace-viewer.css` 末尾追加：

```css
/* Parallel group badge */
.tool-parallel-badge {
  font-size: 9px;
  padding: 1px 4px;
  border-radius: 3px;
  background: rgba(139, 92, 246, 0.15);
  color: #a78bfa;
  font-weight: 600;
}

/* Validation errors */
.tool-validation-errors {
  width: 100%;
  margin-top: 4px;
  padding-left: 106px;
}

.validation-error-item {
  font-size: 11px;
  color: var(--red, #ef4444);
  padding: 2px 6px;
  background: rgba(239, 68, 68, 0.08);
  border-radius: 3px;
  margin-bottom: 2px;
}

/* Judge scores */
.tool-judge-scores {
  display: flex;
  gap: 4px;
  margin-top: 4px;
  padding-left: 106px;
  flex-wrap: wrap;
}

.judge-score-tag {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 3px;
  background: rgba(59, 130, 246, 0.1);
  color: #60a5fa;
}

/* Compression indicator */
.iter-compression {
  font-size: 9px;
  padding: 1px 4px;
  border-radius: 3px;
  background: rgba(245, 158, 11, 0.15);
  color: #fbbf24;
  font-weight: 600;
}

.trace-compression-info {
  font-size: 11px;
  color: var(--text-secondary);
  padding: 4px 6px;
  background: rgba(245, 158, 11, 0.06);
  border-radius: 4px;
  margin-bottom: 8px;
}

/* Memory hits */
.trace-memory-hits {
  font-size: 11px;
  color: var(--accent-amber, #f59e0b);
  padding: 4px 6px;
  background: rgba(245, 158, 11, 0.06);
  border-radius: 4px;
  margin-top: 8px;
}
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/TraceViewer.tsx frontend/src/styles/trace-viewer.css
git commit -m "feat(trace-viewer): render state_changes, compression_event, validation_errors, judge_scores, parallel_group, memory_hits"
```

---

### Task 12: MemoryCenter 命中记忆高亮 + memory_recall SSE 处理

**Files:**
- Modify: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/SessionSidebar.tsx`
- Modify: `frontend/src/components/MemoryCenter.tsx`
- Modify: `frontend/src/styles/memory-center.css`

- [ ] **Step 1: ChatPanel 处理 memory_recall 事件**

在 `frontend/src/components/ChatPanel.tsx` 中：

1) 在 Props 接口中添加回调：

```typescript
interface Props {
  sessionId: string
  onPlanUpdate: (plan: TravelPlanState) => void
  onMemoryRecall?: (itemIds: string[]) => void
}
```

2) 在 `handleSend` 的 `sendMessage` 回调中，添加 memory_recall 事件处理（在 `else if (event.type === 'state_update' ...)` 之后）：

```typescript
        } else if (event.type === 'memory_recall' && event.item_ids) {
          onMemoryRecall?.(event.item_ids)
        }
```

3) 更新组件签名：

```typescript
export default function ChatPanel({ sessionId, onPlanUpdate, onMemoryRecall }: Props) {
```

4) 在 SSEEvent 类型中确保 `item_ids` 被允许。在 `frontend/src/types/plan.ts` 中确认 SSEEvent 类型定义（如果它是松散类型或 any，则无需改）。

- [ ] **Step 2: App 添加 recalledIds state 并传递**

修改 `frontend/src/App.tsx`：

1) 添加 state：

```typescript
const [recalledIds, setRecalledIds] = useState<string[]>([])
```

2) 添加回调：

```typescript
const handleMemoryRecall = useCallback((itemIds: string[]) => {
  setRecalledIds(itemIds)
}, [])
```

3) 传递给 ChatPanel：

```tsx
<ChatPanel
  key={chatKey}
  sessionId={sessionId}
  onPlanUpdate={handlePlanUpdate}
  onMemoryRecall={handleMemoryRecall}
/>
```

4) 传递给 SessionSidebar：

```tsx
<SessionSidebar
  sessions={sessionList}
  activeSessionId={sessionId}
  recalledIds={recalledIds}
  onSelectSession={(id) => { void handleSelectSession(id) }}
  onNewSession={() => { void handleNewSession() }}
  onDeleteSession={(id) => { void handleDeleteSession(id) }}
/>
```

- [ ] **Step 3: SessionSidebar 透传 recalledIds 到 MemoryCenter**

修改 `frontend/src/components/SessionSidebar.tsx`，在 props 中添加 `recalledIds` 并传递给 `MemoryCenter`：

```tsx
// SessionSidebar props interface 中添加：
recalledIds?: string[]

// MemoryCenter 调用处添加 recalledIds：
<MemoryCenter
  open={memoryOpen}
  onClose={() => setMemoryOpen(false)}
  memory={memory}
  recalledIds={recalledIds}
/>
```

- [ ] **Step 4: MemoryCenter 高亮命中记忆**

修改 `frontend/src/components/MemoryCenter.tsx`：

1) 在 props 中添加：

```typescript
interface MemoryCenterProps {
  open: boolean
  onClose: () => void
  memory: UseMemoryReturn
  recalledIds?: string[]
}
```

2) 在组件中接收：

```typescript
export default function MemoryCenter({
  open,
  onClose,
  memory,
  recalledIds = [],
}: MemoryCenterProps) {
```

3) 在 MemoryCard 中传递 recalled 状态：

```tsx
<MemoryCard
  key={item.id}
  item={item}
  recalled={recalledIds.includes(item.id)}
  onConfirm={confirmMemory}
  onReject={rejectMemory}
  onDelete={deleteMemory}
/>
```

4) 在 MemoryCard 组件中接收并显示：

```typescript
function MemoryCard({
  item,
  recalled,
  onConfirm,
  onReject,
  onDelete,
}: {
  item: MemoryItem
  recalled?: boolean
  onConfirm?: (id: string) => void
  onReject?: (id: string) => void
  onDelete?: (id: string) => void
}) {
  // ...

  const cardClass = [
    'memory-card',
    item.status === 'pending' && 'is-pending',
    (item.status === 'rejected' || item.status === 'obsolete') && 'is-archived',
    recalled && 'is-recalled',
  ]
    .filter(Boolean)
    .join(' ')
```

- [ ] **Step 5: 添加高亮 CSS**

在 `frontend/src/styles/memory-center.css` 末尾追加：

```css
/* Recalled memory highlight */
.memory-card.is-recalled {
  border-left: 3px solid var(--accent-amber, #f59e0b);
  background: rgba(245, 158, 11, 0.04);
}

.memory-card.is-recalled::after {
  content: '本轮命中';
  position: absolute;
  top: 6px;
  right: 6px;
  font-size: 9px;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--accent-amber, #f59e0b);
  color: #000;
  font-weight: 600;
}
```

注意：需要确保 `.memory-card` 有 `position: relative`。检查现有 CSS，如果没有则添加。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/ChatPanel.tsx frontend/src/App.tsx \
  frontend/src/components/SessionSidebar.tsx frontend/src/components/MemoryCenter.tsx \
  frontend/src/styles/memory-center.css
git commit -m "feat(memory-center): highlight recalled memories with SSE event, pass through App → SessionSidebar → MemoryCenter"
```

---

### Task 13: 全量回归测试 + PROJECT_OVERVIEW 更新

**Files:**
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: 运行全部后端测试**

Run: `python -m pytest backend/tests/ -v --timeout=60`
Expected: All PASS

- [ ] **Step 2: 检查前端编译**

Run: `cd frontend && npm run build`
Expected: Build successful, no type errors

- [ ] **Step 3: 更新 PROJECT_OVERVIEW.md**

在 `PROJECT_OVERVIEW.md` 中，更新以下内容：
- Stats 层新增字段的说明
- TraceViewer 数据通道打通的说明
- MemoryCenter 命中记忆高亮功能
- 住宿 price 别名兼容

- [ ] **Step 4: Final commit**

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: update PROJECT_OVERVIEW with trace & memory pipeline improvements"
```

---

## 依赖关系

```
Task 1 (stats 扩展) ─────┬─→ Task 5 (on_validate)
                          ├─→ Task 6 (on_soft_judge)
                          ├─→ Task 7 (parallel_group)
Task 3 (previous_value) ──┘
Task 4 (generate_context) ───→ Task 8 (memory_hits)
Task 2 (guardrail) ──────────→ (独立)
Tasks 5-8 ────────────────────→ Task 9 (build_trace)
Task 9 ──────────────────────→ Task 10 (前端类型)
Task 10 ─────────────────────→ Task 11 (TraceViewer)
Task 8 + Task 10 ────────────→ Task 12 (MemoryCenter)
Tasks 1-12 ──────────────────→ Task 13 (回归 + 文档)
```

Tasks 1, 2, 3, 4 可以并行执行（无依赖）。
Tasks 5, 6, 7 可以并行执行（都依赖 Task 1，但互不依赖）。
Tasks 10, 11, 12 必须顺序执行。
