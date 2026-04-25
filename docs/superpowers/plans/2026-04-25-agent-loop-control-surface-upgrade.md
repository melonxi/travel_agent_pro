# Agent Loop Control Surface Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce `backend/agent/loop.py` control-flow debt without changing AgentLoop runtime behavior, by making phase transitions, iteration limits, repair hints, constructor dependencies, and Phase 5 boundary checks explicit.

**Architecture:** Keep `AgentLoop` as the facade and streaming coordinator. Move narrow decision logic into focused `agent/execution/*` modules, preserve public compatibility where tests or callers still rely on existing constructor names, and verify each task with targeted tests before moving to the next one.

**Tech Stack:** Python 3.12 async/await, pytest, pytest-asyncio, existing `agent.types`, `llm.types`, `run.IterationProgress`, and `config.Phase5ParallelConfig`.

---

## Scope

This plan upgrades five parts of the current AgentLoop control surface:

1. Unify phase transition detection into one helper.
2. Split `max_retries` semantics into clearer loop iteration and LLM error concepts while preserving the old keyword.
3. Move repair hint mutation ownership from builder functions to the loop.
4. Group AgentLoop constructor dependencies into dataclasses while preserving the existing constructor API.
5. Name and isolate the two Phase 5 boundary checks.

This plan intentionally does not redesign the memory Stage 1-4 pipeline. That path is system-wide and should get a separate plan because it crosses `backend/api/orchestration/memory/*`, `backend/memory/*`, trace, config, and tests.

## File Structure

- Create: `backend/agent/execution/phase_transition.py`
  - Owns transition detection only: backtrack, direct plan phase writes, and router-driven transitions.
  - Returns a pure detection result plus internal task events; it does not rebuild messages or emit chunks.

- Create: `backend/agent/execution/limits.py`
  - Owns AgentLoop iteration limit naming and backward-compatible normalization of `max_retries`.

- Modify: `backend/agent/execution/repair_hints.py`
  - Stop mutating `repair_hints_used` directly.
  - Return `RepairHintOutcome(message, key)` so AgentLoop decides when a hint has been consumed.

- Modify: `backend/agent/execution/llm_turn.py`
  - Optional Task 2 hook only if LLM error counting is added at the AgentLoop boundary. No behavior change to streaming progress.

- Modify: `backend/agent/loop.py`
  - Use the new helpers.
  - Keep `AgentLoop(...)` old keyword arguments working.
  - Keep compatibility methods currently asserted by `backend/tests/test_agent_loop_structure.py`.

- Create: `backend/agent/execution/loop_config.py`
  - Owns grouped `AgentLoopDeps` and `AgentLoopConfig` dataclasses.
  - `backend/agent/loop.py` imports and re-exports them for the public constructor API.

- Modify: `backend/tests/test_agent_loop_structure.py`
  - Add structure assertions for the new modules and tighter line-count guard.

- Add: `backend/tests/test_agent_phase_transition.py`
  - Unit tests for `detect_phase_transition(...)`.

- Add: `backend/tests/test_agent_loop_limits.py`
  - Tests for `AgentLoopLimits` and backward compatibility of `max_retries`.

- Add: `backend/tests/test_agent_loop_config.py`
  - Tests grouped constructor API and legacy constructor compatibility.

- Add or modify: `backend/tests/test_agent_repair_hints.py`
  - Tests for new repair hint ownership contract.

- Modify existing focused tests:
  - `backend/tests/test_phase_transition_event.py`
  - `backend/tests/test_loop_phase5_routing.py`
  - `backend/tests/test_agent_loop.py`

---

## Task 1: Unified Phase Transition Detection

**Files:**
- Create: `backend/agent/execution/phase_transition.py`
- Modify: `backend/agent/loop.py`
- Modify: `backend/tests/test_agent_loop_structure.py`
- Add: `backend/tests/test_agent_phase_transition.py`
- Verify existing: `backend/tests/test_phase_transition_event.py`

### Intent

Today `AgentLoop.run()` has three different phase transition branches:

- `needs_rebuild` from a backtrack-style tool result.
- Direct `plan.phase` mutation from a write tool.
- `phase_router.check_and_apply_transition(...)` after state updates.

The logic is correct, but it is spread across the loop body. This task creates one decision point that answers: "Did the tool batch cause a phase transition, and why?"

- [ ] **Step 1: Write unit tests for transition detection**

Create `backend/tests/test_agent_phase_transition.py`:

```python
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agent.execution.phase_transition import detect_phase_transition
from agent.execution.tool_batches import ToolBatchOutcome
from agent.internal_tasks import InternalTask
from agent.types import ToolResult
from run import IterationProgress


class _Plan:
    def __init__(self, *, phase: int, phase3_step: str | None = None):
        self.phase = phase
        self.phase3_step = phase3_step


@pytest.mark.asyncio
async def test_detects_backtrack_before_router_check():
    plan = _Plan(phase=1)
    router = AsyncMock()
    router.check_and_apply_transition = AsyncMock(return_value=True)
    result = ToolResult(tool_call_id="tc1", status="success")
    batch = ToolBatchOutcome(
        progress=IterationProgress.TOOLS_WITH_WRITES,
        saw_state_update=True,
        needs_rebuild=True,
        rebuild_result=result,
        next_parallel_group_counter=0,
    )

    detection = await detect_phase_transition(
        plan=plan,
        phase_router=router,
        hooks=None,
        batch_outcome=batch,
        phase_before_batch=5,
        phase3_step_before_batch=None,
        current_phase=5,
        drain_internal_task_events=lambda: [],
    )

    assert detection.request is not None
    assert detection.request.reason == "backtrack"
    assert detection.request.from_phase == 5
    assert detection.request.to_phase == 1
    assert detection.request.result is result
    router.check_and_apply_transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_detects_direct_plan_phase_write_before_router_check():
    plan = _Plan(phase=3)
    router = AsyncMock()
    router.check_and_apply_transition = AsyncMock(return_value=True)
    batch = ToolBatchOutcome(
        progress=IterationProgress.TOOLS_WITH_WRITES,
        saw_state_update=True,
        needs_rebuild=False,
        rebuild_result=None,
        next_parallel_group_counter=0,
    )

    detection = await detect_phase_transition(
        plan=plan,
        phase_router=router,
        hooks=None,
        batch_outcome=batch,
        phase_before_batch=1,
        phase3_step_before_batch=None,
        current_phase=1,
        drain_internal_task_events=lambda: [],
    )

    assert detection.request is not None
    assert detection.request.reason == "plan_tool_direct"
    assert detection.request.from_phase == 1
    assert detection.request.to_phase == 3
    router.check_and_apply_transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_detects_router_transition_and_returns_internal_tasks_first():
    plan = _Plan(phase=1)
    task = InternalTask(
        id="quality_gate:1",
        kind="quality_gate",
        label="Quality gate",
        status="success",
    )

    async def _promote(_plan, hooks=None):
        _plan.phase = 3
        return True

    router = AsyncMock()
    router.check_and_apply_transition = AsyncMock(side_effect=_promote)
    batch = ToolBatchOutcome(
        progress=IterationProgress.TOOLS_WITH_WRITES,
        saw_state_update=True,
        needs_rebuild=False,
        rebuild_result=None,
        next_parallel_group_counter=0,
    )

    detection = await detect_phase_transition(
        plan=plan,
        phase_router=router,
        hooks="hooks",
        batch_outcome=batch,
        phase_before_batch=1,
        phase3_step_before_batch=None,
        current_phase=1,
        drain_internal_task_events=lambda: [task],
    )

    assert detection.internal_tasks == [task]
    assert detection.request is not None
    assert detection.request.reason == "check_and_apply_transition"
    assert detection.request.to_phase == 3
    router.check_and_apply_transition.assert_awaited_once_with(plan, hooks="hooks")


@pytest.mark.asyncio
async def test_returns_phase3_step_after_batch_without_phase_transition():
    plan = _Plan(phase=3, phase3_step="candidate")
    router = AsyncMock()
    router.check_and_apply_transition = AsyncMock(return_value=False)
    batch = ToolBatchOutcome(
        progress=IterationProgress.TOOLS_WITH_WRITES,
        saw_state_update=False,
        needs_rebuild=False,
        rebuild_result=None,
        next_parallel_group_counter=0,
    )

    detection = await detect_phase_transition(
        plan=plan,
        phase_router=router,
        hooks=None,
        batch_outcome=batch,
        phase_before_batch=3,
        phase3_step_before_batch="brief",
        current_phase=3,
        drain_internal_task_events=lambda: [],
    )

    assert detection.request is None
    assert detection.internal_tasks == []
    assert detection.phase3_step_after_batch == "candidate"
    router.check_and_apply_transition.assert_not_awaited()
```

- [ ] **Step 2: Run the new tests and confirm they fail**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_agent_phase_transition.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'agent.execution.phase_transition'
```

- [ ] **Step 3: Implement `phase_transition.py`**

Create `backend/agent/execution/phase_transition.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent.execution.tool_batches import ToolBatchOutcome
from agent.internal_tasks import InternalTask
from agent.types import ToolResult


@dataclass(frozen=True)
class PhaseTransitionRequest:
    from_phase: int
    to_phase: int
    from_step: Any
    reason: str
    result: ToolResult


@dataclass(frozen=True)
class PhaseTransitionDetection:
    request: PhaseTransitionRequest | None
    internal_tasks: list[InternalTask]
    phase3_step_after_batch: Any


async def detect_phase_transition(
    *,
    plan: Any | None,
    phase_router: Any | None,
    hooks: Any | None,
    batch_outcome: ToolBatchOutcome,
    phase_before_batch: int,
    phase3_step_before_batch: Any,
    current_phase: int,
    drain_internal_task_events: Callable[[], list[InternalTask]],
) -> PhaseTransitionDetection:
    """Detect exactly one phase transition after a completed tool batch."""

    if batch_outcome.needs_rebuild:
        phase_after_batch = plan.phase if plan is not None else current_phase
        return PhaseTransitionDetection(
            request=PhaseTransitionRequest(
                from_phase=phase_before_batch,
                to_phase=phase_after_batch,
                from_step=phase3_step_before_batch,
                reason="backtrack",
                result=batch_outcome.rebuild_result
                or ToolResult(tool_call_id="", status="success"),
            ),
            internal_tasks=[],
            phase3_step_after_batch=getattr(plan, "phase3_step", None)
            if plan is not None
            else None,
        )

    phase_after_batch = plan.phase if plan is not None else current_phase
    if phase_after_batch != phase_before_batch:
        return PhaseTransitionDetection(
            request=PhaseTransitionRequest(
                from_phase=phase_before_batch,
                to_phase=phase_after_batch,
                from_step=phase3_step_before_batch,
                reason="plan_tool_direct",
                result=ToolResult(tool_call_id="", status="success"),
            ),
            internal_tasks=[],
            phase3_step_after_batch=getattr(plan, "phase3_step", None)
            if plan is not None
            else None,
        )

    internal_tasks: list[InternalTask] = []
    if batch_outcome.saw_state_update and phase_router is not None and plan is not None:
        phase_changed = await phase_router.check_and_apply_transition(
            plan,
            hooks=hooks,
        )
        internal_tasks = drain_internal_task_events()
        phase_after_batch = plan.phase
        if phase_changed:
            return PhaseTransitionDetection(
                request=PhaseTransitionRequest(
                    from_phase=phase_before_batch,
                    to_phase=phase_after_batch,
                    from_step=phase3_step_before_batch,
                    reason="check_and_apply_transition",
                    result=ToolResult(tool_call_id="", status="success"),
                ),
                internal_tasks=internal_tasks,
                phase3_step_after_batch=getattr(plan, "phase3_step", None),
            )

    return PhaseTransitionDetection(
        request=None,
        internal_tasks=internal_tasks,
        phase3_step_after_batch=getattr(plan, "phase3_step", None)
        if plan is not None
        else None,
    )
```

- [ ] **Step 4: Replace the three transition branches in `loop.py`**

In `backend/agent/loop.py`, add imports:

```python
from agent.execution.phase_transition import (
    PhaseTransitionDetection,
    PhaseTransitionRequest,
    detect_phase_transition,
)
```

Replace the block from:

```python
saw_state_update = batch_outcome.saw_state_update
needs_rebuild = batch_outcome.needs_rebuild
rebuild_result = batch_outcome.rebuild_result

if needs_rebuild:
    ...
```

through the end of the `if saw_state_update ...` branch with:

```python
transition_detection = await detect_phase_transition(
    plan=self.plan,
    phase_router=self.phase_router,
    hooks=self.hooks,
    batch_outcome=batch_outcome,
    phase_before_batch=phase_before_batch,
    phase3_step_before_batch=phase3_step_before_batch,
    current_phase=current_phase,
    drain_internal_task_events=self._drain_internal_task_events,
)
for task in transition_detection.internal_tasks:
    yield LLMChunk(type=ChunkType.INTERNAL_TASK, internal_task=task)

if transition_detection.request is not None:
    prev_iteration_had_tools = True
    phase_changed_in_prev_iteration = True
    transition_outcome: PhaseTransitionOutcome | None = None
    async for transition_item in self._handle_phase_transition(
        messages=messages,
        request=transition_detection.request,
        original_user_message=original_user_message,
    ):
        if isinstance(transition_item, LLMChunk):
            yield transition_item
        else:
            transition_outcome = transition_item
    if transition_outcome is None:
        raise RuntimeError("Phase transition finished without an outcome")
    messages[:] = transition_outcome.messages
    current_phase = transition_outcome.current_phase
    tools = transition_outcome.tools
    continue
```

Then change `_handle_phase_transition(...)` signature to accept the request:

```python
async def _handle_phase_transition(
    self,
    *,
    messages: list[Message],
    request: PhaseTransitionRequest,
    original_user_message: Message,
) -> AsyncIterator[LLMChunk | PhaseTransitionOutcome]:
```

Inside `_handle_phase_transition`, replace references:

```python
from_phase=request.from_phase
to_phase=request.to_phase
from_step=request.from_step
reason=request.reason
result=request.result
```

Keep the yielded `PHASE_TRANSITION` chunk before message rebuilding.

- [ ] **Step 5: Preserve phase3 step rebuild behavior**

In `backend/agent/loop.py`, replace:

```python
phase3_step_after_batch = (
    getattr(self.plan, "phase3_step", None)
    if self.plan is not None
    else None
)
```

with:

```python
phase3_step_after_batch = transition_detection.phase3_step_after_batch
```

Do not move Phase 3 step rebuild into `detect_phase_transition(...)`; it is a different behavior from phase transition and should remain visible in `AgentLoop.run()`.

- [ ] **Step 6: Update structure tests**

Modify `backend/tests/test_agent_loop_structure.py` expected execution modules:

```python
expected_execution_modules = {
    "__init__.py",
    "llm_turn.py",
    "message_rebuild.py",
    "phase_transition.py",
    "repair_hints.py",
    "tool_invocation.py",
    "tool_batches.py",
}
```

Add:

```python
assert module_defines(
    "agent/execution/phase_transition.py",
    "detect_phase_transition",
)
assert module_defines(
    "agent/execution/phase_transition.py",
    "PhaseTransitionRequest",
)
```

Tighten the temporary size guard only after the refactor passes:

```python
assert line_count("agent/loop.py") < 630
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_agent_phase_transition.py tests/test_phase_transition_event.py tests/test_agent_loop_structure.py -q
```

Expected:

```text
passed
```

- [ ] **Step 8: Review checkpoint**

Review these invariants manually:

- Backtrack still wins over direct plan phase writes and router checks.
- Direct `plan.phase` write still wins over router checks.
- Internal task chunks from `check_and_apply_transition(...)` are yielded before the phase transition chunk.
- Phase 3 step-only rebuild still runs when there is no phase change.

---

## Task 2: Clarify Loop Limits Without Breaking `max_retries`

**Files:**
- Create: `backend/agent/execution/limits.py`
- Modify: `backend/agent/loop.py`
- Modify: `backend/tests/test_agent_loop_structure.py`
- Add: `backend/tests/test_agent_loop_limits.py`
- Verify existing: `backend/tests/test_appendix_issues.py`, `backend/tests/test_agent_loop.py`

### Intent

`max_retries` currently means "maximum AgentLoop iterations", not "retry count after failure." Rename the internal meaning to `max_iterations`, preserve `max_retries` as a backward-compatible constructor alias, and introduce `max_llm_errors` as an explicit future/now-ready error budget.

- [ ] **Step 1: Write tests for limit normalization**

Create `backend/tests/test_agent_loop_limits.py`:

```python
from __future__ import annotations

from agent.execution.limits import AgentLoopLimits
from agent.loop import AgentLoop
from agent.hooks import HookManager
from tools.engine import ToolEngine


def test_limits_keep_max_retries_as_compatibility_alias():
    limits = AgentLoopLimits.from_constructor_args(
        max_iterations=None,
        max_retries=7,
        max_llm_errors=None,
    )

    assert limits.max_iterations == 7
    assert limits.max_llm_errors == 1


def test_max_iterations_takes_precedence_over_compatibility_alias():
    limits = AgentLoopLimits.from_constructor_args(
        max_iterations=4,
        max_retries=9,
        max_llm_errors=2,
    )

    assert limits.max_iterations == 4
    assert limits.max_llm_errors == 2


def test_agent_loop_exposes_legacy_max_retries_value():
    agent = AgentLoop(
        llm=object(),
        tool_engine=ToolEngine(),
        hooks=HookManager(),
        max_retries=5,
    )

    assert agent.max_iterations == 5
    assert agent.max_retries == 5
    assert agent.limits.max_llm_errors == 1
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_agent_loop_limits.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'agent.execution.limits'
```

- [ ] **Step 3: Implement `limits.py`**

Create `backend/agent/execution/limits.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentLoopLimits:
    max_iterations: int = 3
    max_llm_errors: int = 1

    @classmethod
    def from_constructor_args(
        cls,
        *,
        max_iterations: int | None,
        max_retries: int | None,
        max_llm_errors: int | None,
    ) -> "AgentLoopLimits":
        effective_iterations = (
            max_iterations
            if max_iterations is not None
            else max_retries
            if max_retries is not None
            else cls.max_iterations
        )
        effective_llm_errors = (
            max_llm_errors if max_llm_errors is not None else cls.max_llm_errors
        )
        if effective_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        if effective_llm_errors < 0:
            raise ValueError("max_llm_errors must be >= 0")
        return cls(
            max_iterations=effective_iterations,
            max_llm_errors=effective_llm_errors,
        )
```

- [ ] **Step 4: Wire limits into `AgentLoop.__init__`**

In `backend/agent/loop.py`, import:

```python
from agent.execution.limits import AgentLoopLimits
```

Change constructor parameters from:

```python
max_retries: int = 3,
```

to:

```python
max_retries: int | None = 3,
max_iterations: int | None = None,
max_llm_errors: int | None = None,
```

In `__init__`, replace:

```python
self.max_retries = max_retries
```

with:

```python
self.limits = AgentLoopLimits.from_constructor_args(
    max_iterations=max_iterations,
    max_retries=max_retries,
    max_llm_errors=max_llm_errors,
)
self.max_iterations = self.limits.max_iterations
self.max_retries = self.limits.max_iterations
```

Replace the loop:

```python
for iteration in range(self.max_retries):
```

with:

```python
for iteration in range(self.max_iterations):
```

Keep the fallback user-facing text unchanged for this task.

- [ ] **Step 5: Add structure test assertion**

In `backend/tests/test_agent_loop_structure.py`, add `limits.py` to `expected_execution_modules` and assert:

```python
assert module_defines(
    "agent/execution/limits.py",
    "AgentLoopLimits",
)
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_agent_loop_limits.py tests/test_appendix_issues.py::TestA3MaxRetriesUnused::test_max_retries_controls_loop_iterations tests/test_agent_loop_structure.py -q
```

Expected:

```text
passed
```

- [ ] **Step 7: Review checkpoint**

Confirm:

- Existing code passing `max_retries=...` still works.
- New code can pass `max_iterations=...`.
- The loop count behavior is unchanged.
- `max_llm_errors` exists as a named budget, but no new retry policy has been introduced yet.

---

## Task 3: Make Repair Hint Ownership Explicit

**Files:**
- Modify: `backend/agent/execution/repair_hints.py`
- Modify: `backend/agent/loop.py`
- Add or modify: `backend/tests/test_agent_repair_hints.py`
- Verify existing: `backend/tests/test_agent_loop.py`

### Intent

Today `repair_hints_used` is owned by `AgentLoop`, but builder functions mutate the set internally. This task changes repair builders to return the key they want to consume. `AgentLoop` then records that key after appending the repair system message.

- [ ] **Step 1: Write repair hint outcome tests**

Create `backend/tests/test_agent_repair_hints.py` if it does not exist:

```python
from __future__ import annotations

from agent.execution.repair_hints import (
    RepairHintOutcome,
    build_phase3_state_repair_message,
)


class _Plan:
    destination = "成都"
    phase3_step = "brief"
    trip_brief = None


def test_phase3_repair_returns_key_without_mutating_used_set():
    used: set[str] = set()

    outcome = build_phase3_state_repair_message(
        plan=_Plan(),
        current_phase=3,
        assistant_text="这是一次完整的旅行画像说明，包含偏好、预算、日期和旅行目标。",
        repair_hints_used=used,
    )

    assert isinstance(outcome, RepairHintOutcome)
    assert outcome.key == "p3_brief"
    assert "trip_brief" in outcome.message
    assert used == set()


def test_phase3_repair_respects_already_used_keys():
    used = {"p3_brief", "p3_brief_retry"}

    outcome = build_phase3_state_repair_message(
        plan=_Plan(),
        current_phase=3,
        assistant_text="这是一次完整的旅行画像说明，包含偏好、预算、日期和旅行目标。",
        repair_hints_used=used,
    )

    assert outcome is None
    assert used == {"p3_brief", "p3_brief_retry"}
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_agent_repair_hints.py -q
```

Expected:

```text
ImportError: cannot import name 'RepairHintOutcome'
```

- [ ] **Step 3: Add `RepairHintOutcome`**

In `backend/agent/execution/repair_hints.py`, add:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class RepairHintOutcome:
    message: str
    key: str
```

Change both builders' return annotations:

```python
) -> RepairHintOutcome | None:
```

- [ ] **Step 4: Stop mutating `repair_hints_used` inside builders**

For each branch that currently does:

```python
repair_hints_used.add(repair_key)
return (
    "[状态同步提醒]\n"
    "..."
)
```

change it to:

```python
return RepairHintOutcome(
    key=repair_key,
    message=(
        "[状态同步提醒]\n"
        "..."
    ),
)
```

Do this for every branch in both `build_phase3_state_repair_message(...)` and `build_phase5_state_repair_message(...)`.

Do not change the early-return checks that read `repair_hints_used`; only move mutation out of this module.

- [ ] **Step 5: Update `AgentLoop.run()` to consume repair outcome**

In `backend/agent/loop.py`, add the type import:

```python
from agent.execution.repair_hints import (
    RepairHintOutcome,
    build_phase3_state_repair_message,
    build_phase5_state_repair_message,
)
```

In `backend/agent/loop.py`, change:

```python
repair_message = self._build_phase3_state_repair_message(
    current_phase=current_phase,
    assistant_text=full_text,
    repair_hints_used=repair_hints_used,
) or self._build_phase5_state_repair_message(
    current_phase=current_phase,
    assistant_text=full_text,
    repair_hints_used=repair_hints_used,
)
```

to:

```python
repair_outcome = self._build_phase3_state_repair_message(
    current_phase=current_phase,
    assistant_text=full_text,
    repair_hints_used=repair_hints_used,
) or self._build_phase5_state_repair_message(
    current_phase=current_phase,
    assistant_text=full_text,
    repair_hints_used=repair_hints_used,
)
```

Then change:

```python
if repair_message:
    messages.append(Message(role=Role.SYSTEM, content=repair_message))
    continue
```

to:

```python
if repair_outcome:
    messages.append(Message(role=Role.SYSTEM, content=repair_outcome.message))
    repair_hints_used.add(repair_outcome.key)
    continue
```

Update wrapper return annotations in `loop.py` from `str | None` to `RepairHintOutcome | None`.

- [ ] **Step 6: Run focused tests**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_agent_repair_hints.py tests/test_agent_loop.py -q
```

Expected:

```text
passed
```

- [ ] **Step 7: Review checkpoint**

Confirm:

- `repair_hints.py` no longer contains `repair_hints_used.add(`.
- Only `AgentLoop.run()` mutates `repair_hints_used`.
- Existing repair behavior still adds at most one repair message per repair key.

Verification command:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
rg -n "repair_hints_used\\.add" backend/agent
```

Expected:

```text
backend/agent/loop.py:<line>:    repair_hints_used.add(repair_outcome.key)
```

---

## Task 4: Group AgentLoop Constructor Dependencies

**Files:**
- Modify: `backend/agent/loop.py`
- Modify: `backend/tests/test_agent_loop_structure.py`
- Add: `backend/tests/test_agent_loop_config.py`

### Intent

`AgentLoop.__init__` currently accepts many direct parameters. That is convenient for old tests, but it makes the object look like it owns too many unrelated knobs. This task introduces dataclasses for grouped dependencies and runtime options while keeping the old constructor parameters working.

- [ ] **Step 1: Write constructor grouping tests**

Create `backend/tests/test_agent_loop_config.py`:

```python
from __future__ import annotations

from agent.hooks import HookManager
from agent.loop import AgentLoop, AgentLoopConfig, AgentLoopDeps
from tools.engine import ToolEngine


def test_agent_loop_accepts_grouped_deps_and_config():
    llm = object()
    engine = ToolEngine()
    hooks = HookManager()

    agent = AgentLoop(
        deps=AgentLoopDeps(
            llm=llm,
            tool_engine=engine,
            hooks=hooks,
        ),
        config=AgentLoopConfig(
            max_iterations=4,
            max_llm_errors=2,
            user_id="u-grouped",
            memory_enabled=False,
            parallel_tool_execution=False,
        ),
    )

    assert agent.llm is llm
    assert agent.tool_engine is engine
    assert agent.hooks is hooks
    assert agent.max_iterations == 4
    assert agent.limits.max_llm_errors == 2
    assert agent.user_id == "u-grouped"
    assert agent.memory_enabled is False
    assert agent.parallel_tool_execution is False


def test_agent_loop_legacy_constructor_still_works():
    llm = object()
    engine = ToolEngine()
    hooks = HookManager()

    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=hooks,
        max_retries=6,
        user_id="legacy",
    )

    assert agent.llm is llm
    assert agent.tool_engine is engine
    assert agent.hooks is hooks
    assert agent.max_iterations == 6
    assert agent.user_id == "legacy"
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_agent_loop_config.py -q
```

Expected:

```text
ImportError: cannot import name 'AgentLoopConfig'
```

- [ ] **Step 3: Add grouped constructor dataclasses**

Create `backend/agent/execution/loop_config.py`:

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from agent.hooks import HookManager
from agent.internal_tasks import InternalTask
from config import Phase5ParallelConfig
from tools.engine import ToolEngine


@dataclass(frozen=True)
class AgentLoopDeps:
    llm: Any
    tool_engine: ToolEngine
    hooks: HookManager
    phase_router: Any | None = None
    context_manager: Any | None = None
    plan: Any | None = None
    llm_factory: Any | None = None
    memory_mgr: Any | None = None
    reflection: Any | None = None
    tool_choice_decider: Any | None = None
    guardrail: Any | None = None


@dataclass(frozen=True)
class AgentLoopConfig:
    max_iterations: int | None = None
    max_llm_errors: int | None = None
    memory_enabled: bool = True
    user_id: str = "default_user"
    compression_events: list[dict] | None = None
    parallel_tool_execution: bool = True
    cancel_event: asyncio.Event | None = None
    phase5_parallel_config: Phase5ParallelConfig | None = None
    internal_task_events: list[InternalTask] | None = None
```

Then import these names in `backend/agent/loop.py` so callers can still use `from agent.loop import AgentLoopConfig, AgentLoopDeps`:

```python
from agent.execution.loop_config import AgentLoopConfig, AgentLoopDeps
```

- [ ] **Step 4: Extend constructor without removing legacy args**

Change `AgentLoop.__init__` to accept these two optional first-class parameters:

```python
deps: AgentLoopDeps | None = None,
config: AgentLoopConfig | None = None,
```

Keep all existing legacy keyword parameters after them.

At the start of `__init__`, normalize:

```python
if deps is not None:
    llm = deps.llm
    tool_engine = deps.tool_engine
    hooks = deps.hooks
    phase_router = deps.phase_router
    context_manager = deps.context_manager
    plan = deps.plan
    llm_factory = deps.llm_factory
    memory_mgr = deps.memory_mgr
    reflection = deps.reflection
    tool_choice_decider = deps.tool_choice_decider
    guardrail = deps.guardrail

if config is not None:
    max_iterations = config.max_iterations
    max_llm_errors = config.max_llm_errors
    memory_enabled = config.memory_enabled
    user_id = config.user_id
    compression_events = config.compression_events
    parallel_tool_execution = config.parallel_tool_execution
    cancel_event = config.cancel_event
    phase5_parallel_config = config.phase5_parallel_config
    internal_task_events = config.internal_task_events

if llm is None or tool_engine is None or hooks is None:
    raise TypeError("AgentLoop requires llm, tool_engine, and hooks")
```

To support this, legacy parameters `llm`, `tool_engine`, and `hooks` need to become optional in the signature:

```python
llm: Any | None = None,
tool_engine: ToolEngine | None = None,
hooks: HookManager | None = None,
```

Do not change attribute names used elsewhere.

- [ ] **Step 5: Add structure tests**

In `backend/tests/test_agent_loop_structure.py`, assert:

```python
assert module_defines("agent/execution/loop_config.py", "AgentLoopDeps")
assert module_defines("agent/execution/loop_config.py", "AgentLoopConfig")
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_agent_loop_config.py tests/test_agent_loop_structure.py tests/test_agent_loop.py -q
```

Expected:

```text
passed
```

- [ ] **Step 7: Review checkpoint**

Confirm:

- All old tests that instantiate `AgentLoop(llm=..., tool_engine=..., hooks=...)` still pass.
- New code can instantiate through `AgentLoopDeps` and `AgentLoopConfig`.
- No caller is forced to migrate in this task.

---

## Task 5: Isolate Phase 5 Boundary Checks

**Files:**
- Modify: `backend/agent/phase5/parallel.py`
- Modify: `backend/agent/loop.py`
- Modify: `backend/tests/test_loop_phase5_routing.py`

### Intent

AgentLoop currently checks `should_use_parallel_phase5(...)` at the loop top and once after the loop limit. Both are intentional:

- Loop top: handle cold start or normal phase 5 entry.
- Loop limit boundary: handle the final iteration writing phase 5 and needing one last handoff before fallback.

This task gives those two checks explicit names so they read as policy, not accidental duplication.

- [ ] **Step 1: Write tests for named Phase 5 guards**

Modify `backend/tests/test_loop_phase5_routing.py`:

```python
from agent.phase5.parallel import (
    should_enter_parallel_phase5_now,
    should_enter_parallel_phase5_at_iteration_boundary,
)
```

Add:

```python
def test_named_phase5_guards_share_current_eligibility_rules():
    plan = _plan_ready_for_parallel_phase5()
    config = _enabled_phase5_parallel_config()

    assert should_enter_parallel_phase5_now(plan, config) is True
    assert should_enter_parallel_phase5_at_iteration_boundary(plan, config) is True
```

Use the existing helper style in this file. If helpers do not exist yet, create local helpers:

```python
def _enabled_phase5_parallel_config():
    return Phase5ParallelConfig(enabled=True, max_workers=2)


def _plan_ready_for_parallel_phase5():
    plan = MagicMock()
    plan.phase = 5
    plan.daily_plans = []
    plan.dates = MagicMock()
    plan.dates.total_days = 2
    plan.selected_skeleton_id = "s1"
    plan.skeleton_plans = [{"id": "s1", "days": [{}, {}]}]
    return plan
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_loop_phase5_routing.py -q
```

Expected:

```text
ImportError: cannot import name 'should_enter_parallel_phase5_now'
```

- [ ] **Step 3: Add named helpers**

In `backend/agent/phase5/parallel.py`, add:

```python
def should_enter_parallel_phase5_now(
    plan: Any | None,
    config: Phase5ParallelConfig | None,
) -> bool:
    return should_use_parallel_phase5(plan, config)


def should_enter_parallel_phase5_at_iteration_boundary(
    plan: Any | None,
    config: Phase5ParallelConfig | None,
) -> bool:
    return should_use_parallel_phase5(plan, config)
```

Keep `should_use_parallel_phase5(...)` unchanged for compatibility.

- [ ] **Step 4: Use named helpers in `loop.py`**

In `backend/agent/loop.py`, import:

```python
from agent.phase5.parallel import (
    run_parallel_phase5_orchestrator,
    should_enter_parallel_phase5_at_iteration_boundary,
    should_enter_parallel_phase5_now,
    should_use_parallel_phase5,
)
```

Replace loop-top check:

```python
if self.should_use_parallel_phase5(self.plan, self.phase5_parallel_config):
```

with:

```python
if should_enter_parallel_phase5_now(self.plan, self.phase5_parallel_config):
```

Replace final boundary check:

```python
if self.should_use_parallel_phase5(self.plan, self.phase5_parallel_config):
```

with:

```python
if should_enter_parallel_phase5_at_iteration_boundary(
    self.plan,
    self.phase5_parallel_config,
):
```

Keep the static method `AgentLoop.should_use_parallel_phase5(...)` for tests and external compatibility.

- [ ] **Step 5: Run focused tests**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_loop_phase5_routing.py tests/test_agent_loop_structure.py -q
```

Expected:

```text
passed
```

- [ ] **Step 6: Review checkpoint**

Confirm:

- The two checks still use identical eligibility rules.
- Comments in `loop.py` now explain timing, not the duplicated predicate itself.
- No Phase 5 orchestration behavior changed.

---

## Task 6: Final Integration Review and Test Sweep

**Files:**
- Modify: `PROJECT_OVERVIEW.md` only if this work is committed or if the architecture description becomes stale.
- No new runtime files unless a previous task revealed a necessary gap.

- [ ] **Step 1: Run static syntax check**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
python -m py_compile \
  backend/agent/loop.py \
  backend/agent/execution/*.py \
  backend/agent/phase5/*.py
```

Expected:

```text
```

No output and exit code 0.

- [ ] **Step 2: Run focused AgentLoop suite**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest \
  tests/test_agent_loop_structure.py \
  tests/test_agent_llm_turn.py \
  tests/test_agent_loop.py \
  tests/test_agent_phase_transition.py \
  tests/test_agent_loop_limits.py \
  tests/test_agent_loop_config.py \
  tests/test_agent_repair_hints.py \
  tests/test_phase_transition_event.py \
  tests/test_loop_phase5_routing.py \
  tests/test_parallel_tool_call_sequence.py \
  -q
```

Expected:

```text
passed
```

- [ ] **Step 3: Run full backend tests**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests -q
```

Expected:

```text
passed
```

OpenTelemetry warnings about `localhost:4317` are acceptable if pytest exits 0; they indicate the local collector is not running.

- [ ] **Step 4: Check diff hygiene**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
git diff --check
git status --short
```

Expected:

```text
git diff --check
```

prints no whitespace errors.

`git status --short` should show only files intentionally changed by this upgrade plus pre-existing unrelated workspace changes.

- [ ] **Step 5: Review line-count and responsibility**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
wc -l backend/agent/loop.py backend/agent/execution/*.py backend/agent/phase5/*.py
```

Expected:

- `backend/agent/loop.py` should be below the new guard from Task 1.
- `backend/agent/execution/phase_transition.py` should contain transition detection, not message rebuilding.
- `backend/agent/execution/repair_hints.py` should contain detection and message construction, not ownership mutation.
- `backend/agent/execution/limits.py` should contain naming/normalization only, not retry loops.

- [ ] **Step 6: Final review notes**

Write a short review summary with:

- Behavior preserved.
- New helper boundaries.
- Tests run.
- Any remaining intentional debt.

Remaining intentional debt after this plan:

- Memory Stage 1-4 complexity remains out of scope.
- `AgentLoop.run()` remains the streaming coordinator, so it will still be a substantial method.
- Compatibility wrappers remain until tests/callers are migrated away from private methods.

---

## Execution Recommendation

Execute in this order:

1. Task 1 only, then review and run focused tests.
2. Task 2 only, then review and run focused tests.
3. Task 3 only, then review and run focused tests.
4. Task 4 only, then review and run focused tests.
5. Task 5 only, then review and run focused tests.
6. Task 6 full sweep.

Do not batch Tasks 1-5 together. They touch the same core loop and failures are easier to diagnose one boundary at a time.
