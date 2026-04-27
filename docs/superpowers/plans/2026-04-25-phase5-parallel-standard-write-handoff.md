# Phase 5 Parallel Standard Write Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route successful Phase 5 parallel dayplans through the standard `replace_all_day_plans` tool path so the existing `saw_state_update -> detect_phase_transition -> Phase 7` flow runs unchanged.

**Architecture:** Day Workers continue to submit candidates to staging artifacts, and the Python Orchestrator continues to split, retry, validate, and summarize. The Orchestrator no longer writes `plan.daily_plans` directly; it exposes final validated `dayplans` as an internal handoff result. `AgentLoop` receives that result, constructs an internal `ToolCall(name="replace_all_day_plans")`, executes it through `_execute_tool_batch()`, then reuses the existing phase transition detection and message rebuild code.

**Tech Stack:** Python async generators, existing `ToolEngine`, `ToolCall`, `ToolBatchOutcome`, `PhaseRouter`, pytest async tests.

---

## Current Findings

- Serial Phase 5 reaches Phase 7 because `save_day_plan` / `replace_all_day_plans` are executed through `AgentLoop._execute_tool_batch()`.
- `_execute_tool_batch()` sets `saw_state_update=True` when a successful tool call name is in `PLAN_WRITER_TOOL_NAMES`.
- `detect_phase_transition()` calls `phase_router.check_and_apply_transition()` only when `saw_state_update=True`.
- Parallel Phase 5 currently bypasses that path by calling `replace_all_daily_plans(self.plan, dayplans)` inside `Phase5Orchestrator.run()`.
- Therefore `daily_plans` are filled but `plan.phase` remains `5`, and the next user message falls back to serial Phase 5 prompt instead of Phase 7.

## File Structure

- Modify `backend/agent/phase5/orchestrator.py`
  - Stop importing and calling `replace_all_daily_plans`.
  - Add `final_dayplans` and `final_issues` attributes to expose validated results after `run()`.
  - Keep progress and summary text generation.
  - Stop emitting `DONE`; completion should be owned by `AgentLoop` after standard write/transition.

- Modify `backend/agent/phase5/parallel.py`
  - Add a small `Phase5ParallelHandoff` dataclass.
  - Add optional `on_handoff` callback to `run_parallel_phase5_orchestrator()`.
  - After `orchestrator.run()` finishes, call `on_handoff(...)` if final dayplans exist.
  - Use handoff state, not `plan.daily_plans`, to decide internal task success.

- Modify `backend/agent/loop.py`
  - Pass a handoff callback into `_run_parallel_phase5_orchestrator()`.
  - After parallel streaming finishes, commit handoff dayplans via an internal `replace_all_day_plans` `ToolCall`.
  - Run the existing `detect_phase_transition()` and `_handle_phase_transition()` after that internal tool batch.
  - Emit one final `DONE` after commit/transition handling.

- Modify tests:
  - `backend/tests/test_orchestrator.py`
  - `backend/tests/test_loop_phase5_routing.py`

- Update documentation:
  - `PROJECT_OVERVIEW.md`

---

### Task 1: Make Orchestrator Produce Handoff Results Instead Of Writing State

**Files:**
- Modify: `backend/agent/phase5/orchestrator.py`
- Test: `backend/tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

Add this test near the other orchestrator run tests in `backend/tests/test_orchestrator.py`:

```python
@pytest.mark.asyncio
async def test_orchestrator_exposes_final_dayplans_without_writing_plan(monkeypatch):
    plan = _make_plan_with_skeleton()
    assert plan.daily_plans == []

    def activity(name: str) -> dict:
        return {
            "name": name,
            "location": {"name": name, "lat": 35.0, "lng": 139.0},
            "start_time": "09:00",
            "end_time": "10:00",
            "category": "activity",
            "cost": 0,
        }

    async def _fake_worker(**kwargs):
        day = kwargs["task"].day
        return DayWorkerResult(
            day=day,
            date=f"2026-05-0{day}",
            success=True,
            dayplan={
                "day": day,
                "date": f"2026-05-0{day}",
                "notes": f"day {day}",
                "activities": [activity(f"POI {day}")],
            },
            iterations=1,
        )

    monkeypatch.setattr("agent.phase5.orchestrator.run_day_worker", _fake_worker)

    orch = Phase5Orchestrator(
        plan=plan,
        llm=AsyncMock(),
        tool_engine=AsyncMock(),
        config=Phase5ParallelConfig(enabled=True, max_workers=3),
    )

    chunks = [chunk async for chunk in orch.run()]

    assert plan.daily_plans == []
    assert [dp["day"] for dp in orch.final_dayplans] == [1, 2, 3]
    assert any(
        c.type == ChunkType.TEXT_DELTA
        and c.content
        and "已完成 3/3 天的行程规划" in c.content
        for c in chunks
    )
    assert not any(c.type == ChunkType.DONE for c in chunks)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
pytest backend/tests/test_orchestrator.py::test_orchestrator_exposes_final_dayplans_without_writing_plan -q
```

Expected: FAIL because `Phase5Orchestrator` currently writes `plan.daily_plans`, lacks `final_dayplans`, and emits `DONE`.

- [ ] **Step 3: Add result attributes and remove direct state write**

In `backend/agent/phase5/orchestrator.py`, remove this import:

```python
from state.plan_writers import replace_all_daily_plans
```

In `Phase5Orchestrator.__init__()`, add:

```python
self.final_dayplans: list[dict[str, Any]] = []
self.final_issues: list[GlobalValidationIssue] = []
```

Replace the current write block:

```python
# 9. Write results
if dayplans:
    replace_all_daily_plans(self.plan, dayplans)
    yield self._build_progress_chunk(
        worker_statuses,
        total_days,
        f"已写入 {len(dayplans)} 天行程",
    )
```

with:

```python
# 9. Expose results for AgentLoop to commit via the standard write-tool path.
self.final_dayplans = list(dayplans)
self.final_issues = list(issues)
if dayplans:
    yield self._build_progress_chunk(
        worker_statuses,
        total_days,
        f"已生成 {len(dayplans)} 天行程，准备写入规划状态...",
    )
```

At the end of `run()`, remove:

```python
yield LLMChunk(type=ChunkType.DONE)
```

Do not replace it in the Orchestrator. `AgentLoop` will own final `DONE` after the standard write and transition check.

- [ ] **Step 4: Run targeted orchestrator tests**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
pytest backend/tests/test_orchestrator.py::test_orchestrator_exposes_final_dayplans_without_writing_plan -q
pytest backend/tests/test_orchestrator.py -q
```

Expected: the new test passes. Existing tests that expected direct `plan.daily_plans` mutation or `DONE` from `orch.run()` may fail; update only those assertions to inspect `orch.final_dayplans` and leave final `DONE` responsibility to AgentLoop.

---

### Task 2: Add Parallel Handoff Callback In The Wrapper

**Files:**
- Modify: `backend/agent/phase5/parallel.py`
- Test: `backend/tests/test_loop_phase5_routing.py`

- [ ] **Step 1: Write the failing wrapper test**

Add this test to `backend/tests/test_loop_phase5_routing.py`:

```python
@pytest.mark.asyncio
async def test_parallel_wrapper_returns_final_dayplans_via_handoff(monkeypatch):
    from agent.phase5.parallel import run_parallel_phase5_orchestrator
    from state.models import Accommodation, DateRange, TravelPlanState

    plan = TravelPlanState(session_id="s-handoff", phase=5)
    plan.dates = DateRange(start="2026-05-01", end="2026-05-01")
    plan.selected_skeleton_id = "plan_A"
    plan.skeleton_plans = [{"id": "plan_A", "days": [{}]}]
    plan.accommodation = Accommodation(area="新宿")

    final_dayplans = [{
        "day": 1,
        "date": "2026-05-01",
        "notes": "ok",
        "activities": [{
            "name": "测试活动",
            "location": {"name": "测试活动", "lat": 35.0, "lng": 139.0},
            "start_time": "09:00",
            "end_time": "10:00",
            "category": "activity",
            "cost": 0,
        }],
    }]

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            self.final_dayplans = final_dayplans
            self.final_issues = []

        async def run(self):
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="并行完成")

    monkeypatch.setattr("agent.phase5.orchestrator.Phase5Orchestrator", FakeOrchestrator)

    handoffs = []
    chunks = [
        chunk
        async for chunk in run_parallel_phase5_orchestrator(
            plan=plan,
            llm=MagicMock(),
            tool_engine=ToolEngine(),
            config=Phase5ParallelConfig(enabled=True),
            on_handoff=handoffs.append,
        )
    ]

    assert len(handoffs) == 1
    assert handoffs[0].dayplans == final_dayplans
    assert handoffs[0].issues == []
    assert plan.daily_plans == []
    success_tasks = [
        c.internal_task
        for c in chunks
        if c.type == ChunkType.INTERNAL_TASK
        and c.internal_task
        and c.internal_task.kind == "phase5_orchestration"
        and c.internal_task.status == "success"
    ]
    assert success_tasks
    assert success_tasks[-1].result == {"fallback": False}
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
pytest backend/tests/test_loop_phase5_routing.py::test_parallel_wrapper_returns_final_dayplans_via_handoff -q
```

Expected: FAIL because `run_parallel_phase5_orchestrator()` has no `on_handoff` parameter or handoff dataclass, and because the final internal task currently determines `success` from `plan.daily_plans` instead of Orchestrator `final_dayplans`.

- [ ] **Step 3: Implement handoff dataclass and callback**

In `backend/agent/phase5/parallel.py`, update imports:

```python
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable
```

Add above `should_use_parallel_phase5()`:

```python
@dataclass(frozen=True)
class Phase5ParallelHandoff:
    dayplans: list[dict[str, Any]]
    issues: list[Any]
```

Change the function signature:

```python
async def run_parallel_phase5_orchestrator(
    *,
    plan: Any,
    llm: Any,
    tool_engine: Any,
    config: Phase5ParallelConfig | None,
    on_handoff: Callable[[Phase5ParallelHandoff], None] | None = None,
) -> AsyncIterator[LLMChunk]:
```

After the `async for chunk in orchestrator.run(): yield chunk` block, add:

```python
    final_dayplans = list(getattr(orchestrator, "final_dayplans", []) or [])
    final_issues = list(getattr(orchestrator, "final_issues", []) or [])
    if final_dayplans and on_handoff is not None:
        on_handoff(
            Phase5ParallelHandoff(
                dayplans=final_dayplans,
                issues=final_issues,
            )
        )
```

Replace:

```python
completed = bool(getattr(plan, "daily_plans", None))
```

with:

```python
completed = bool(final_dayplans)
```

- [ ] **Step 4: Run wrapper test**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
pytest backend/tests/test_loop_phase5_routing.py::test_parallel_wrapper_returns_final_dayplans_via_handoff -q
```

Expected: PASS.

---

### Task 3: Commit Handoff Dayplans Through Standard Tool Batch In AgentLoop

**Files:**
- Modify: `backend/agent/loop.py`
- Test: `backend/tests/test_loop_phase5_routing.py`

- [ ] **Step 1: Write the failing integration test**

Add this test to `backend/tests/test_loop_phase5_routing.py`:

```python
@pytest.mark.asyncio
async def test_parallel_phase5_handoff_commits_via_standard_tool_and_transitions(monkeypatch):
    from phase.router import PhaseRouter
    from state.models import Accommodation, DateRange, TravelPlanState
    from tests.helpers.register_plan_tools import register_all_plan_tools

    plan = TravelPlanState(session_id="s-parallel-commit", phase=5)
    plan.dates = DateRange(start="2026-05-01", end="2026-05-01")
    plan.selected_skeleton_id = "plan_A"
    plan.skeleton_plans = [{"id": "plan_A", "days": [{}]}]
    plan.accommodation = Accommodation(area="新宿")

    final_dayplans = [{
        "day": 1,
        "date": "2026-05-01",
        "notes": "并行生成",
        "activities": [{
            "name": "测试活动",
            "location": {"name": "测试活动", "lat": 35.0, "lng": 139.0},
            "start_time": "09:00",
            "end_time": "10:00",
            "category": "activity",
            "cost": 0,
        }],
    }]

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            self.final_dayplans = final_dayplans
            self.final_issues = []

        async def run(self):
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="并行摘要")

    monkeypatch.setattr("agent.phase5.orchestrator.Phase5Orchestrator", FakeOrchestrator)

    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    agent = AgentLoop(
        llm=MagicMock(),
        tool_engine=engine,
        hooks=HookManager(),
        phase_router=PhaseRouter(),
        context_manager=_StubContextManager(),
        plan=plan,
        memory_mgr=_StubMemoryManager(),
        user_id="u",
        phase5_parallel_config=Phase5ParallelConfig(enabled=True),
    )

    chunks = [
        chunk
        async for chunk in agent.run(
            [Message(role=Role.USER, content="继续")],
            phase=5,
        )
    ]

    assert plan.phase == 7
    assert len(plan.daily_plans) == 1
    assert plan.daily_plans[0].day == 1
    assert any(c.type == ChunkType.TOOL_RESULT for c in chunks)
    assert any(
        c.type == ChunkType.PHASE_TRANSITION
        and c.phase_info["from_phase"] == 5
        and c.phase_info["to_phase"] == 7
        for c in chunks
    )
    assert chunks[-1].type == ChunkType.DONE
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
pytest backend/tests/test_loop_phase5_routing.py::test_parallel_phase5_handoff_commits_via_standard_tool_and_transitions -q
```

Expected: FAIL because AgentLoop does not yet commit the handoff through `_execute_tool_batch()`.

- [ ] **Step 3: Update AgentLoop imports**

In `backend/agent/loop.py`, keep the existing imports and ensure `ToolCall` is already imported from `agent.types`. No new import is needed beyond the handoff callback support in `parallel.py`.

- [ ] **Step 4: Change `_run_parallel_phase5_orchestrator()` to collect and commit handoff**

Replace the current method:

```python
async def _run_parallel_phase5_orchestrator(self) -> AsyncIterator[LLMChunk]:
    async for chunk in run_parallel_phase5_orchestrator(
        plan=self.plan,
        llm=self.llm,
        tool_engine=self.tool_engine,
        config=self.phase5_parallel_config,
    ):
        yield chunk
```

with:

```python
async def _run_parallel_phase5_orchestrator(
    self,
    *,
    messages: list[Message],
    original_user_message: Message,
) -> AsyncIterator[LLMChunk]:
    _handoff: Any | None = None

    def _capture_handoff(handoff: Any) -> None:
        nonlocal _handoff
        _handoff = handoff

    async for chunk in run_parallel_phase5_orchestrator(
        plan=self.plan,
        llm=self.llm,
        tool_engine=self.tool_engine,
        config=self.phase5_parallel_config,
        on_handoff=_capture_handoff,
    ):
        yield chunk

    if _handoff is None or not _handoff.dayplans:
        yield LLMChunk(type=ChunkType.DONE)
        return

    commit_call = ToolCall(
        id="internal_phase5_parallel_commit",
        name="replace_all_day_plans",
        arguments={"days": list(_handoff.dayplans)},
        human_label="写入并行逐日行程",
    )
    # Preserve the same message-history shape as a normal assistant tool call.
    # _execute_tool_batch() appends the matching TOOL message and then existing
    # phase-transition detection can reason over a standard write-tool batch.
    messages.append(
        Message(
            role=Role.ASSISTANT,
            content=None,
            tool_calls=[commit_call],
        )
    )

    phase_before_batch = self.plan.phase if self.plan is not None else 5
    phase3_step_before_batch = (
        getattr(self.plan, "phase3_step", None) if self.plan is not None else None
    )
    batch_outcome: ToolBatchOutcome | None = None
    async for batch_item in self._execute_tool_batch(
        tool_calls=[commit_call],
        messages=messages,
    ):
        if isinstance(batch_item, LLMChunk):
            yield batch_item
        else:
            batch_outcome = batch_item

    if batch_outcome is None:
        raise RuntimeError("Parallel Phase 5 commit finished without an outcome")

    transition_detection = await detect_phase_transition(
        plan=self.plan,
        phase_router=self.phase_router,
        hooks=self.hooks,
        batch_outcome=batch_outcome,
        phase_before_batch=phase_before_batch,
        phase3_step_before_batch=phase3_step_before_batch,
        current_phase=phase_before_batch,
        drain_internal_task_events=self._drain_internal_task_events,
    )
    for task in transition_detection.internal_tasks:
        yield LLMChunk(type=ChunkType.INTERNAL_TASK, internal_task=task)

    if transition_detection.request is not None:
        async for transition_item in self._handle_phase_transition(
            messages=messages,
            request=transition_detection.request,
            original_user_message=original_user_message,
        ):
            if isinstance(transition_item, LLMChunk):
                yield transition_item

    yield LLMChunk(type=ChunkType.DONE)
```

- [ ] **Step 5: Pass messages into both parallel entry points**

In `AgentLoop.run()`, replace the loop-top branch:

```python
async for chunk in self._run_parallel_phase5_orchestrator():
    yield chunk
return
```

with:

```python
async for chunk in self._run_parallel_phase5_orchestrator(
    messages=messages,
    original_user_message=original_user_message,
):
    yield chunk
return
```

Make the same replacement in the boundary branch near the safety-limit fallback.

- [ ] **Step 6: Run the new AgentLoop test**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
pytest backend/tests/test_loop_phase5_routing.py::test_parallel_phase5_handoff_commits_via_standard_tool_and_transitions -q
```

Expected: PASS.

---

### Task 4: Preserve Fallback, Non-Handoff, And Integration Behavior

**Files:**
- Modify: `backend/tests/test_loop_phase5_routing.py`
- Modify: `backend/tests/test_parallel_phase5_integration.py`
- Modify only if needed: `backend/agent/loop.py`

- [ ] **Step 1: Add a no-handoff regression test**

Add this test to `backend/tests/test_loop_phase5_routing.py`:

```python
@pytest.mark.asyncio
async def test_parallel_phase5_without_handoff_does_not_commit_or_transition(monkeypatch):
    from phase.router import PhaseRouter
    from state.models import Accommodation, DateRange, TravelPlanState
    from tests.helpers.register_plan_tools import register_all_plan_tools

    plan = TravelPlanState(session_id="s-no-handoff", phase=5)
    plan.dates = DateRange(start="2026-05-01", end="2026-05-01")
    plan.selected_skeleton_id = "plan_A"
    plan.skeleton_plans = [{"id": "plan_A", "days": [{}]}]
    plan.accommodation = Accommodation(area="新宿")

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            self.final_dayplans = []
            self.final_issues = []

        async def run(self):
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="并行失败，等待串行")

    monkeypatch.setattr("agent.phase5.orchestrator.Phase5Orchestrator", FakeOrchestrator)

    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    agent = AgentLoop(
        llm=MagicMock(),
        tool_engine=engine,
        hooks=HookManager(),
        phase_router=PhaseRouter(),
        context_manager=_StubContextManager(),
        plan=plan,
        memory_mgr=_StubMemoryManager(),
        user_id="u",
        phase5_parallel_config=Phase5ParallelConfig(enabled=True),
    )

    chunks = [
        chunk
        async for chunk in agent.run(
            [Message(role=Role.USER, content="继续")],
            phase=5,
        )
    ]

    assert plan.phase == 5
    assert plan.daily_plans == []
    assert not any(c.type == ChunkType.TOOL_RESULT for c in chunks)
    assert not any(c.type == ChunkType.PHASE_TRANSITION for c in chunks)
    assert chunks[-1].type == ChunkType.DONE
```

- [ ] **Step 2: Run the no-handoff test**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
pytest backend/tests/test_loop_phase5_routing.py::test_parallel_phase5_without_handoff_does_not_commit_or_transition -q
```

Expected: PASS.

- [ ] **Step 3: Audit and update parallel integration tests for the new ownership boundary**

Open `backend/tests/test_parallel_phase5_integration.py` and update any assertion that expects `Phase5Orchestrator.run()` itself to mutate `plan.daily_plans` or emit final `DONE`.

Use this rule when updating tests:

```python
# Orchestrator-level tests should assert handoff output, not state mutation.
assert plan.daily_plans == []
assert [dp["day"] for dp in orch.final_dayplans] == [1, 2, 3]
assert not any(c.type == ChunkType.DONE for c in chunks)
```

If a test is intended to verify end-to-end state mutation, move that assertion to an `AgentLoop`-level test that executes the internal `replace_all_day_plans` commit through `_execute_tool_batch()`, like `test_parallel_phase5_handoff_commits_via_standard_tool_and_transitions`.

- [ ] **Step 4: Run Phase 5 routing and integration tests**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
pytest backend/tests/test_loop_phase5_routing.py -q
pytest backend/tests/test_parallel_phase5_integration.py -q
```

Expected: PASS. If an existing fake orchestrator mutates `plan.daily_plans`, update it to expose `final_dayplans` instead when the test is exercising the new handoff flow.

---

### Task 5: Full Regression And Documentation

**Files:**
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: Update architecture docs**

In `PROJECT_OVERVIEW.md`, update the Phase 5 parallel description so it says the Orchestrator returns validated final dayplans to AgentLoop, and AgentLoop commits them with `replace_all_day_plans` through the standard write-tool path. Replace wording that says the Orchestrator “最后统一写入 `replace_all_daily_plans`”.

Use this wording:

```markdown
Orchestrator 收集结果后读取 artifact 候选并做全局验证（POI 去重 / 预算检查 / 天数覆盖 / 时间冲突 / 语义去重 / 交通衔接 / 节奏匹配），error 级问题触发最多 1 轮 re-dispatch（注入 repair_hints 重跑受影响天）。验证后的 final dayplans 不由 Orchestrator 直接写入 `TravelPlanState`；而是作为内部 handoff 交还给 AgentLoop，由 AgentLoop 构造内部 `replace_all_day_plans` 工具调用并走标准 `_execute_tool_batch -> detect_phase_transition` 链路，从而复用 Phase 5 → Phase 7 的现有阶段推进、hook、telemetry 和工具结果事件。
```

- [ ] **Step 2: Run targeted test suite**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
pytest backend/tests/test_orchestrator.py backend/tests/test_loop_phase5_routing.py backend/tests/test_phase_router.py backend/tests/test_plan_tools/test_daily_plans.py -q
```

Expected: PASS.

- [ ] **Step 3: Run broader backend tests relevant to AgentLoop**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
pytest backend/tests/test_agent_loop.py backend/tests/test_parallel_phase5_integration.py -q
```

Expected: PASS. `test_parallel_phase5_integration.py` should already reflect the new boundary from Task 4; this run is a regression sweep, not the first place to discover required test rewrites.

- [ ] **Step 4: Manual behavior check**

Run the app or an integration harness with a Phase 5 plan that satisfies parallel eligibility:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
pytest backend/tests/test_loop_phase5_routing.py::test_parallel_phase5_handoff_commits_via_standard_tool_and_transitions -q
```

Expected observable sequence in chunks:

```text
internal_task pending: Phase 5 并行编排
agent_status: parallel_progress
text_delta: 并行摘要 / 已完成 X/Y 天的行程规划
internal_task success: Phase 5 并行编排
tool_result: replace_all_day_plans success
phase_transition: 5 -> 7
done
```

---

## Self-Review

**Spec coverage:** The plan implements the requested architecture exactly: workers only generate candidates, Orchestrator validates and returns final dayplans, AgentLoop commits through `replace_all_day_plans`, and existing transition logic advances Phase 5 to Phase 7.

**Placeholder scan:** No task uses “TBD”, “TODO”, “implement later”, or vague “add tests” language. Each task includes exact files, code snippets, and commands.

**Type consistency:** `Phase5ParallelHandoff.dayplans` is used consistently as `list[dict[str, Any]]`. AgentLoop keeps the complete handoff object in a `nonlocal _handoff` variable rather than mutating a captured list, so `issues` and future fields remain available with clear ownership. The internal commit uses existing `ToolCall`, `ToolBatchOutcome`, `detect_phase_transition()`, and `_handle_phase_transition()` APIs.

**Message-history contract:** `_run_parallel_phase5_orchestrator()` intentionally appends an assistant message containing the internal `replace_all_day_plans` tool call before `_execute_tool_batch()` runs. This side effect mirrors the serial LLM tool-call path; removing it would leave the subsequent TOOL message without a matching assistant tool call in the in-memory history.

**Risk notes:** This changes the order of externally visible events: the summary text may appear before the `replace_all_day_plans` tool result and phase transition event. That is acceptable for preserving the existing Orchestrator summary, but if product wants write-before-summary, move summary text generation from Orchestrator to AgentLoop after commit in a follow-up.
