# Agent LLM Turn Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract one LLM turn from `backend/agent/loop.py` into `backend/agent/execution/llm_turn.py`, so `AgentLoop.run()` focuses on iteration flow, tool batches, and phase transitions.

**Architecture:** Introduce an execution helper that owns per-iteration pre-LLM orchestration, status/internal-task chunk emission, reflection injection, tool-choice selection, and streaming chunk parsing. `AgentLoop` remains the public compatibility facade and keeps cancellation/progress state, but delegates the LLM-turn body through injected callables and explicit dependency parameters.

**Tech Stack:** Python 3.12, async generators, dataclasses, pytest, existing `LLMChunk`, `ToolCall`, `Message`, `InternalTask`, and `IterationProgress` models.

---

## Current Scope

This plan targets the middle of `AgentLoop.run()` around current lines 212-350:

- `before_llm_call` hook execution.
- Compression internal task / context compression chunk emission.
- Agent status chunk emission with `thinking` / `summarizing`.
- Reflection prompt injection and reflection internal task emission.
- Tool choice calculation.
- `llm.chat(messages, tools=..., stream=True, tool_choice=...)` invocation.
- Stream parsing for `TEXT_DELTA`, `USAGE`, `TOOL_CALL_START`, and `DONE`.
- Human label enrichment for tool calls.
- Progress transition from `NO_OUTPUT` to `PARTIAL_TEXT` / `PARTIAL_TOOL_CALL`.

This plan does not move:

- No-tool finalization and repair hints.
- Tool batch execution.
- Phase transition handling.
- Phase 5 parallel branch.
- Compatibility wrappers at the bottom of `AgentLoop`.

## Target File Structure

```text
backend/agent/
  loop.py                         # AgentLoop facade and high-level run loop
  execution/
    llm_turn.py                    # one LLM turn: pre-call events + stream parsing
    message_rebuild.py
    repair_hints.py
    tool_batches.py
    tool_invocation.py
```

## Non-Negotiable Compatibility Constraints

- Keep `agent.loop.AgentLoop.run` monkeypatchable.
- Keep `AgentLoop.progress` semantics unchanged.
- Keep cancellation checks before LLM call and during stream consumption.
- Preserve chunk order:
  - compression pending task
  - agent status `compacting`
  - `CONTEXT_COMPRESSION` chunks
  - compression success task
  - agent status `thinking` / `summarizing`
  - optional reflection internal task
  - streamed text/tool/usage chunks
- Preserve `before_llm_call` hook timing before compression/reflection/status and before `llm.chat`.
- Preserve reflection mutation of `messages` before the LLM call.
- Preserve `self._prev_phase3_step` behavior.
- Preserve `tool_choice_decider` behavior: omit `tool_choice` when it returns `"auto"`, pass it when not `"auto"`.
- Preserve tool-call `human_label` enrichment through `tool_engine.get_tool(tool_call.name)`.
- Preserve real-time `AgentLoop.progress` observability during streaming. `backend/api/orchestration/chat/stream.py` reads `agent.progress` inside `except LLMError`; if a stream raises after a `TEXT_DELTA`, `agent.progress` must already be `PARTIAL_TEXT` so continuation can save incomplete assistant text.

## Task 1: Add Structure And Focused Turn Tests

**Files:**
- Modify: `backend/tests/test_agent_loop_structure.py`
- Modify: `backend/tests/test_agent_loop.py`
- Create: `backend/tests/test_agent_llm_turn.py`

- [ ] **Step 1: Extend structure test for `llm_turn.py`**

Add `"llm_turn.py"` to `expected_execution_modules` in `backend/tests/test_agent_loop_structure.py`:

```python
expected_execution_modules = {
    "__init__.py",
    "llm_turn.py",
    "message_rebuild.py",
    "repair_hints.py",
    "tool_invocation.py",
    "tool_batches.py",
}
```

Add this assertion to `test_agent_execution_modules_expose_expected_names()`:

```python
assert module_defines(
    "agent/execution/llm_turn.py",
    "run_llm_turn",
)
assert module_defines(
    "agent/execution/llm_turn.py",
    "LlmTurnOutcome",
)
```

- [ ] **Step 2: Add a focused test for the new helper's public contract**

Create `backend/tests/test_agent_llm_turn.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

import pytest

from agent.execution.llm_turn import LlmTurnOutcome, run_llm_turn
from agent.hooks import HookManager
from agent.internal_tasks import InternalTask
from agent.types import Message, Role, ToolCall
from llm.types import ChunkType, LLMChunk
from llm.errors import LLMError, LLMErrorCode
from run import IterationProgress


@dataclass
class _ToolDef:
    human_label: str


class _ToolEngine:
    def get_tool(self, name: str):
        if name == "search":
            return _ToolDef(human_label="搜索")
        return None


class _Plan:
    phase = 1
    phase3_step = None
    destination = None


class _Reflection:
    def check_and_inject(self, messages, plan, previous_step):
        return "reflection message"


class _ToolChoiceDecider:
    def decide(self, plan, messages, phase):
        return {"type": "tool", "name": "search"}


class _LLM:
    def __init__(self):
        self.kwargs = None

    async def chat(self, messages, **kwargs):
        self.kwargs = kwargs
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="hello")
        yield LLMChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call=ToolCall(id="tc1", name="search", arguments={}),
        )
        yield LLMChunk(type=ChunkType.USAGE, usage_info={"total_tokens": 3})
        yield LLMChunk(type=ChunkType.DONE)


@pytest.mark.asyncio
async def test_run_llm_turn_emits_status_reflection_and_collects_outcome():
    llm = _LLM()
    hooks = HookManager()
    messages = [Message(role=Role.USER, content="hi")]
    compression_events = [{"before": 10, "after": 5}]
    cancelled_checks = 0
    observed_progress: list[IterationProgress] = []

    def check_cancelled() -> None:
        nonlocal cancelled_checks
        cancelled_checks += 1

    chunks = []
    outcome = None
    async for item in run_llm_turn(
        llm=llm,
        tool_engine=_ToolEngine(),
        hooks=hooks,
        messages=messages,
        tools=[{"name": "search"}],
        current_phase=1,
        plan=_Plan(),
        reflection=_Reflection(),
        tool_choice_decider=_ToolChoiceDecider(),
        compression_events=compression_events,
        iteration_idx=0,
        previous_iteration_had_tools=False,
        phase_changed_in_previous_iteration=False,
        previous_phase3_step=None,
        check_cancelled=check_cancelled,
        update_progress=observed_progress.append,
    ):
        if isinstance(item, LLMChunk):
            chunks.append(item)
        else:
            outcome = item

    assert isinstance(outcome, LlmTurnOutcome)
    assert outcome.text_chunks == ["hello"]
    assert len(outcome.tool_calls) == 1
    assert outcome.tool_calls[0].human_label == "搜索"
    assert outcome.progress == IterationProgress.PARTIAL_TOOL_CALL
    assert observed_progress == [
        IterationProgress.PARTIAL_TEXT,
        IterationProgress.PARTIAL_TOOL_CALL,
    ]
    assert outcome.next_iteration_idx == 1
    assert outcome.previous_phase3_step is None
    assert compression_events == []
    assert messages[-1] == Message(role=Role.SYSTEM, content="reflection message")
    assert llm.kwargs == {
        "tools": [{"name": "search"}],
        "stream": True,
        "tool_choice": {"type": "tool", "name": "search"},
    }
    assert [chunk.type for chunk in chunks] == [
        ChunkType.INTERNAL_TASK,
        ChunkType.AGENT_STATUS,
        ChunkType.CONTEXT_COMPRESSION,
        ChunkType.INTERNAL_TASK,
        ChunkType.AGENT_STATUS,
        ChunkType.INTERNAL_TASK,
        ChunkType.TEXT_DELTA,
        ChunkType.TOOL_CALL_START,
        ChunkType.USAGE,
    ]
    assert cancelled_checks >= 4


@pytest.mark.asyncio
async def test_run_llm_turn_uses_summarizing_stage_after_tools():
    class _EmptyLLM:
        async def chat(self, messages, **kwargs):
            yield LLMChunk(type=ChunkType.DONE)

    chunks = []
    outcome = None
    async for item in run_llm_turn(
        llm=_EmptyLLM(),
        tool_engine=_ToolEngine(),
        hooks=HookManager(),
        messages=[Message(role=Role.USER, content="hi")],
        tools=[],
        current_phase=1,
        plan=None,
        reflection=None,
        tool_choice_decider=None,
        compression_events=[],
        iteration_idx=3,
        previous_iteration_had_tools=True,
        phase_changed_in_previous_iteration=False,
        previous_phase3_step=None,
        check_cancelled=lambda: None,
        update_progress=lambda progress: None,
    ):
        if isinstance(item, LLMChunk):
            chunks.append(item)
        else:
            outcome = item

    assert isinstance(outcome, LlmTurnOutcome)
    status_chunks = [chunk for chunk in chunks if chunk.type == ChunkType.AGENT_STATUS]
    assert status_chunks[0].agent_status["stage"] == "summarizing"
    assert status_chunks[0].agent_status["iteration"] == 3
    assert outcome.next_iteration_idx == 4


@pytest.mark.asyncio
async def test_run_llm_turn_updates_progress_before_stream_error():
    class _FailingLLM:
        async def chat(self, messages, **kwargs):
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="partial")
            raise LLMError(
                code=LLMErrorCode.TRANSIENT,
                message="stream failed",
                retryable=True,
                provider="test",
                model="fake",
                failure_phase="streaming",
            )

    observed_progress: list[IterationProgress] = []

    with pytest.raises(LLMError):
        async for _item in run_llm_turn(
            llm=_FailingLLM(),
            tool_engine=_ToolEngine(),
            hooks=HookManager(),
            messages=[Message(role=Role.USER, content="hi")],
            tools=[],
            current_phase=1,
            plan=None,
            reflection=None,
            tool_choice_decider=None,
            compression_events=[],
            iteration_idx=0,
            previous_iteration_had_tools=False,
            phase_changed_in_previous_iteration=False,
            previous_phase3_step=None,
            check_cancelled=lambda: None,
            update_progress=observed_progress.append,
        ):
            pass

    assert observed_progress == [IterationProgress.PARTIAL_TEXT]
```

- [ ] **Step 3: Add a characterization test for stream-error progress**

Add this test near `test_progress_tracks_partial_text()` in `backend/tests/test_agent_loop.py`:

```python
@pytest.mark.asyncio
async def test_progress_tracks_partial_text_when_llm_stream_errors():
    async def fake_chat(messages, **kwargs):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="hello")
        raise LLMError(
            code=LLMErrorCode.TRANSIENT,
            message="stream failed",
            retryable=True,
            provider="test",
            model="fake",
            failure_phase="streaming",
        )

    mock_llm = MagicMock()
    mock_llm.provider_name = "openai"
    mock_llm.model = "gpt-4o"
    mock_llm.chat = fake_chat
    engine = ToolEngine()
    hooks = HookManager()

    loop = AgentLoop(llm=mock_llm, tool_engine=engine, hooks=hooks)
    messages = [Message(role=Role.USER, content="hi")]
    with pytest.raises(LLMError):
        async for _ in loop.run(messages, phase=1):
            pass

    assert loop.progress == IterationProgress.PARTIAL_TEXT
```

This is a characterization test, not a new-behavior red test. It should PASS on the current implementation and must remain PASS after the extraction. It protects `backend/api/orchestration/chat/stream.py` continuation behavior.

- [ ] **Step 4: Run the focused tests and verify expected state**

Run:

```bash
pytest backend/tests/test_agent_loop.py::test_progress_tracks_partial_text_when_llm_stream_errors -q
pytest backend/tests/test_agent_loop_structure.py backend/tests/test_agent_llm_turn.py -q
```

Expected: the new `test_progress_tracks_partial_text_when_llm_stream_errors` PASSES on current code. The structure/helper command FAILS because `backend/agent/execution/llm_turn.py` does not exist yet.

## Task 2: Implement `agent/execution/llm_turn.py`

**Files:**
- Create: `backend/agent/execution/llm_turn.py`

- [ ] **Step 1: Add the helper module**

Create `backend/agent/execution/llm_turn.py`:

```python
from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from agent.internal_tasks import InternalTask
from agent.narration import compute_narration
from agent.types import Message, Role, ToolCall
from llm.types import ChunkType, LLMChunk
from run import IterationProgress


@dataclass
class LlmTurnOutcome:
    text_chunks: list[str]
    tool_calls: list[ToolCall]
    progress: IterationProgress
    next_iteration_idx: int
    previous_phase3_step: str | None


async def run_llm_turn(
    *,
    llm: Any,
    tool_engine: Any,
    hooks: Any,
    messages: list[Message],
    tools: list[dict],
    current_phase: int,
    plan: Any | None,
    reflection: Any | None,
    tool_choice_decider: Any | None,
    compression_events: list[dict],
    iteration_idx: int,
    previous_iteration_had_tools: bool,
    phase_changed_in_previous_iteration: bool,
    previous_phase3_step: str | None,
    check_cancelled: Callable[[], None],
    update_progress: Callable[[IterationProgress], None],
) -> AsyncIterator[LLMChunk | LlmTurnOutcome]:
    await hooks.run(
        "before_llm_call",
        messages=messages,
        phase=current_phase,
        tools=tools,
    )

    if compression_events:
        compaction_started_at = time.time()
        yield LLMChunk(
            type=ChunkType.INTERNAL_TASK,
            internal_task=InternalTask(
                id=f"context_compaction:{iteration_idx}",
                kind="context_compaction",
                label="上下文整理",
                status="pending",
                message="正在整理上下文以控制提示词长度…",
                blocking=True,
                scope="turn",
                started_at=compaction_started_at,
            ),
        )
        yield LLMChunk(
            type=ChunkType.AGENT_STATUS,
            agent_status={"stage": "compacting"},
        )
    else:
        compaction_started_at = None

    while compression_events:
        info = compression_events.pop(0)
        yield LLMChunk(
            type=ChunkType.CONTEXT_COMPRESSION,
            compression_info=info,
        )

    if compaction_started_at is not None:
        yield LLMChunk(
            type=ChunkType.INTERNAL_TASK,
            internal_task=InternalTask(
                id=f"context_compaction:{iteration_idx}",
                kind="context_compaction",
                label="上下文整理",
                status="success",
                message="上下文整理完成",
                blocking=True,
                scope="turn",
                started_at=compaction_started_at,
                ended_at=time.time(),
            ),
        )

    stage = (
        "summarizing"
        if previous_iteration_had_tools and not phase_changed_in_previous_iteration
        else "thinking"
    )
    hint = compute_narration(plan) if plan else None
    yield LLMChunk(
        type=ChunkType.AGENT_STATUS,
        agent_status={
            "stage": stage,
            "iteration": iteration_idx,
            "hint": hint,
        },
    )
    next_iteration_idx = iteration_idx + 1

    next_previous_phase3_step = previous_phase3_step
    if reflection is not None and plan is not None:
        reflection_msg = reflection.check_and_inject(
            messages,
            plan,
            previous_phase3_step,
        )
        if reflection_msg:
            messages.append(Message(role=Role.SYSTEM, content=reflection_msg))
            now = time.time()
            yield LLMChunk(
                type=ChunkType.INTERNAL_TASK,
                internal_task=InternalTask(
                    id=f"reflection:{next_iteration_idx - 1}",
                    kind="reflection",
                    label="反思注入",
                    status="success",
                    message="已注入阶段自检提示",
                    blocking=False,
                    scope="turn",
                    result={"message": reflection_msg},
                    started_at=now,
                    ended_at=now,
                ),
            )
        next_previous_phase3_step = getattr(plan, "phase3_step", None)

    tool_choice = "auto"
    if tool_choice_decider is not None and plan is not None:
        tool_choice = tool_choice_decider.decide(
            plan,
            messages,
            current_phase,
        )

    chat_kwargs: dict[str, Any] = {
        "tools": tools,
        "stream": True,
    }
    if tool_choice != "auto":
        chat_kwargs["tool_choice"] = tool_choice

    tool_calls: list[ToolCall] = []
    text_chunks: list[str] = []
    progress = IterationProgress.NO_OUTPUT

    async for chunk in llm.chat(messages, **chat_kwargs):
        check_cancelled()
        if chunk.type == ChunkType.TEXT_DELTA:
            if progress == IterationProgress.NO_OUTPUT:
                progress = IterationProgress.PARTIAL_TEXT
                update_progress(progress)
            text_chunks.append(chunk.content or "")
            yield chunk
        elif chunk.type == ChunkType.USAGE:
            yield chunk
        elif chunk.type == ChunkType.TOOL_CALL_START and chunk.tool_call:
            progress = IterationProgress.PARTIAL_TOOL_CALL
            update_progress(progress)
            if chunk.tool_call.human_label is None:
                tool_def = tool_engine.get_tool(chunk.tool_call.name)
                if tool_def is not None:
                    chunk.tool_call.human_label = tool_def.human_label
            tool_calls.append(chunk.tool_call)
            yield chunk
        elif chunk.type == ChunkType.DONE:
            pass

    yield LlmTurnOutcome(
        text_chunks=text_chunks,
        tool_calls=tool_calls,
        progress=progress,
        next_iteration_idx=next_iteration_idx,
        previous_phase3_step=next_previous_phase3_step,
    )
```

- [ ] **Step 2: Run focused helper tests**

Run:

```bash
pytest backend/tests/test_agent_llm_turn.py -q
```

Expected: PASS.

## Task 3: Wire `AgentLoop.run()` To `run_llm_turn`

**Files:**
- Modify: `backend/agent/loop.py`

- [ ] **Step 1: Import the new helper**

In `backend/agent/loop.py`, add:

```python
from agent.execution.llm_turn import LlmTurnOutcome, run_llm_turn
```

Remove imports that become unused after the wiring:

```python
import time
from agent.narration import compute_narration
```

Keep `asyncio`, `suppress`, and `InternalTask`; they are still used outside the LLM turn body.

Before deleting `import time`, verify it is no longer used in `loop.py`:

```bash
rg -n "\\btime\\." backend/agent/loop.py
```

Expected after the replacement: no matches.

- [ ] **Step 2: Replace the per-turn LLM body**

In `AgentLoop.run()`, replace the block from:

```python
await self.hooks.run(
    "before_llm_call",
    messages=messages,
    phase=current_phase,
    tools=tools,
)
```

through the `async for chunk in self.llm.chat(...)` block with:

```python
turn_outcome: LlmTurnOutcome | None = None
async for turn_item in run_llm_turn(
    llm=self.llm,
    tool_engine=self.tool_engine,
    hooks=self.hooks,
    messages=messages,
    tools=tools,
    current_phase=current_phase,
    plan=self.plan,
    reflection=self.reflection,
    tool_choice_decider=self.tool_choice_decider,
    compression_events=self.compression_events,
    iteration_idx=iteration_idx,
    previous_iteration_had_tools=prev_iteration_had_tools,
    phase_changed_in_previous_iteration=phase_changed_in_prev_iteration,
    previous_phase3_step=self._prev_phase3_step,
    check_cancelled=self._check_cancelled,
    update_progress=lambda progress: setattr(self, "_progress", progress),
):
    if isinstance(turn_item, LLMChunk):
        yield turn_item
    else:
        turn_outcome = turn_item

if turn_outcome is None:
    raise RuntimeError("LLM turn finished without an outcome")

iteration_idx = turn_outcome.next_iteration_idx
self._prev_phase3_step = turn_outcome.previous_phase3_step
self._progress = turn_outcome.progress
prev_iteration_had_tools = False
phase_changed_in_prev_iteration = False
tool_calls = turn_outcome.tool_calls
text_chunks = turn_outcome.text_chunks
```

Keep the existing no-tool finalization and tool-call handling after this replacement unchanged.

The existing reset lines:

```python
prev_iteration_had_tools = False
phase_changed_in_prev_iteration = False
```

must remain in `AgentLoop.run()` after `run_llm_turn()` returns. `run_llm_turn()` only reads the previous values to choose `thinking` versus `summarizing`; it does not own those loop-control flags.

- [ ] **Step 3: Run focused AgentLoop tests**

Run:

```bash
python -m py_compile backend/agent/loop.py backend/agent/execution/llm_turn.py
pytest backend/tests/test_agent_llm_turn.py backend/tests/test_agent_loop.py backend/tests/test_phase_transition_event.py backend/tests/test_tool_human_label.py -q
```

Expected: PASS.

- [ ] **Step 4: Verify stream-error continuation progress**

Run the existing continuation-sensitive tests plus the new helper test:

```bash
pytest backend/tests/test_agent_llm_turn.py backend/tests/test_agent_loop.py::test_progress_tracks_partial_text backend/tests/test_agent_loop.py::test_progress_tracks_partial_text_when_llm_stream_errors -q
```

Expected: PASS. The new `test_run_llm_turn_updates_progress_before_stream_error` proves progress is updated before an `LLMError` can escape the LLM stream.

## Task 4: Tighten Structure Guard And Update Architecture Docs

**Files:**
- Modify: `backend/tests/test_agent_loop_structure.py`
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: Measure `loop.py`**

Run:

```bash
wc -l backend/agent/loop.py backend/agent/execution/llm_turn.py
```

Expected: `backend/agent/loop.py` should drop materially from the current 754 lines. If it is below 650, set the structure guard to `< 650`. If compatibility wrappers keep it above 650, set the guard to the measured count plus 50 and add a comment explaining that wrappers remain intentionally.

- [ ] **Step 2: Update structure test threshold**

Update `test_agent_loop_public_surface_and_size_guard()` in `backend/tests/test_agent_loop_structure.py` with the new threshold.

- [ ] **Step 3: Update `PROJECT_OVERVIEW.md`**

Update the `agent/` description or Agent data-flow section to mention:

```text
agent/execution/llm_turn.py
```

Describe it as the per-iteration LLM call and stream parsing helper.

- [ ] **Step 4: Run focused validation**

Run:

```bash
pytest backend/tests/test_agent_loop_structure.py backend/tests/test_agent_llm_turn.py backend/tests/test_agent_loop.py backend/tests/test_phase_transition_event.py backend/tests/test_parallel_tool_call_sequence.py -q
```

Expected: PASS.

## Task 5: Full Verification

**Files:**
- No additional implementation files unless prior tasks reveal a focused fix.

- [ ] **Step 1: Run compilation**

Run:

```bash
python -m py_compile backend/agent/*.py backend/agent/execution/*.py backend/agent/phase5/*.py backend/main.py backend/api/*.py backend/api/orchestration/*.py backend/api/orchestration/*/*.py backend/api/routes/*.py
```

Expected: exit code 0.

- [ ] **Step 2: Run full backend tests**

Run:

```bash
pytest backend/tests -q
```

Expected: all tests pass. Existing OTEL `localhost:4317` warnings are local collector noise when the pytest exit code is 0.

- [ ] **Step 3: Run diff hygiene checks**

Run:

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 4: Final review checklist**

Confirm each item:

- `agent.loop.AgentLoop.run` still exists and can be monkeypatched.
- `AgentLoop.progress` still reports partial text while streaming text and tool progress after tool batches.
- Compression chunks still appear before normal `thinking` / `summarizing` status.
- Reflection messages are still appended before the LLM call.
- Tool calls still receive `human_label`.
- No-tool repair-hint behavior remains in `AgentLoop.run()`.
- Tool batch execution and phase transition behavior are unchanged.
- `PROJECT_OVERVIEW.md` reflects `agent/execution/llm_turn.py`.

## Execution Notes

- Execute serially. This is a narrow refactor, so do not introduce subpackages beyond `agent/execution/llm_turn.py`.
- Do not delete compatibility wrappers in this plan.
- Do not make commits unless the user explicitly asks. If committing later, keep `PROJECT_OVERVIEW.md` in the same commit as the architecture change.
