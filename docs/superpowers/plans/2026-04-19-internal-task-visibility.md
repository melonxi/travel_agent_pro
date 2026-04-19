# Internal Task Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a unified `internal_task` lifecycle so every internal runtime task appears in the chat stream as a system task card instead of silently blocking or being confused with real tool execution.

**Architecture:** Introduce an `InternalTask` model and `ChunkType.INTERNAL_TASK`, then stream it through the existing SSE path. Keep true tool calls as tool cards; render internal tasks as distinct system task cards that update in place by `task.id`. Move slow post-tool evaluation onto `after_tool_result` so tool cards can end before internal quality review starts.

**Tech Stack:** Python 3.12, FastAPI SSE, pytest/pytest-asyncio, React 19, TypeScript, Vite.

---

## File Structure

- Create `backend/agent/internal_tasks.py`: focused dataclass and helpers for internal task construction and serialization.
- Modify `backend/llm/types.py`: add `ChunkType.INTERNAL_TASK` and `LLMChunk.internal_task`.
- Modify `backend/agent/loop.py`: emit `after_tool_result`, stream queued internal tasks while slow post-tool hooks run, map existing compaction/reflection/orchestration moments to internal tasks, and preserve existing tool result ordering.
- Modify `backend/main.py`: serialize `internal_task` SSE events, pass an internal task event queue into `AgentLoop`, instrument soft judge, quality gate, memory recall, memory extraction, and context compaction.
- Do not modify `backend/agent/hooks.py`: existing event-name registration supports `after_tool_result`.
- Modify `frontend/src/types/plan.ts`: add `InternalTaskEvent` and include `internal_task` in `SSEEvent`.
- Modify `frontend/src/components/ChatPanel.tsx`: merge lifecycle events by `task.id` into one chat message.
- Modify `frontend/src/components/MessageBubble.tsx`: render system task cards.
- Modify `frontend/src/styles/index.css`: style `.system-internal-task` cards with state variants.
- Keep `frontend/src/components/ParallelProgress.tsx` unchanged in this plan; Phase 5 worker progress remains the detailed view while the new `phase5_orchestration` internal task provides the chat-level summary.
- Add/modify backend tests:
  - `backend/tests/test_types.py`
  - `backend/tests/test_agent_loop.py`
  - `backend/tests/test_api.py`
  - `backend/tests/test_realtime_validation_hook.py`
  - `backend/tests/test_memory_integration.py`
- Add frontend verification via `cd frontend && npm run build`.
- Add E2E regression in `e2e-waiting-experience.spec.ts` for slow soft judge visibility.

---

### Task 1: Define Internal Task Types

**Files:**
- Create: `backend/agent/internal_tasks.py`
- Modify: `backend/llm/types.py`
- Test: `backend/tests/test_types.py`

- [ ] **Step 1: Write failing tests for internal task serialization**

Append these tests to `backend/tests/test_types.py`:

```python
def test_internal_task_to_dict_omits_none_fields():
    from agent.internal_tasks import InternalTask

    task = InternalTask(
        id="soft_judge:tc_1",
        kind="soft_judge",
        label="行程质量评审",
        status="pending",
        message="正在检查行程质量…",
        blocking=True,
        scope="turn",
        related_tool_call_id="tc_1",
        started_at=100.0,
    )

    assert task.to_dict() == {
        "id": "soft_judge:tc_1",
        "kind": "soft_judge",
        "label": "行程质量评审",
        "status": "pending",
        "message": "正在检查行程质量…",
        "blocking": True,
        "scope": "turn",
        "related_tool_call_id": "tc_1",
        "started_at": 100.0,
    }


def test_internal_task_requires_known_status_and_scope():
    import pytest
    from agent.internal_tasks import InternalTask

    with pytest.raises(ValueError, match="status"):
        InternalTask(
            id="bad",
            kind="soft_judge",
            label="行程质量评审",
            status="running",
        )

    with pytest.raises(ValueError, match="scope"):
        InternalTask(
            id="bad",
            kind="soft_judge",
            label="行程质量评审",
            status="pending",
            scope="global",
        )


def test_llm_chunk_accepts_internal_task():
    from agent.internal_tasks import InternalTask
    from llm.types import ChunkType, LLMChunk

    task = InternalTask(
        id="quality_gate:5:7",
        kind="quality_gate",
        label="阶段推进检查",
        status="success",
        message="可以进入下一阶段",
    )
    chunk = LLMChunk(type=ChunkType.INTERNAL_TASK, internal_task=task)

    assert ChunkType.INTERNAL_TASK.value == "internal_task"
    assert chunk.internal_task is task
```

- [ ] **Step 2: Run the type tests and verify they fail**

Run:

```bash
cd backend
pytest tests/test_types.py::test_internal_task_to_dict_omits_none_fields tests/test_types.py::test_internal_task_requires_known_status_and_scope tests/test_types.py::test_llm_chunk_accepts_internal_task -v
```

Expected: FAIL because `agent.internal_tasks` does not exist and `ChunkType.INTERNAL_TASK` is missing.

- [ ] **Step 3: Create `backend/agent/internal_tasks.py`**

Create:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


VALID_INTERNAL_TASK_STATUSES = {"pending", "success", "warning", "error", "skipped"}
VALID_INTERNAL_TASK_SCOPES = {"turn", "background", "session"}


@dataclass
class InternalTask:
    id: str
    kind: str
    label: str
    status: str
    message: str | None = None
    blocking: bool = True
    scope: str = "turn"
    related_tool_call_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: float | None = None
    ended_at: float | None = None

    def __post_init__(self) -> None:
        if self.status not in VALID_INTERNAL_TASK_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(VALID_INTERNAL_TASK_STATUSES)}, got {self.status!r}"
            )
        if self.scope not in VALID_INTERNAL_TASK_SCOPES:
            raise ValueError(
                f"scope must be one of {sorted(VALID_INTERNAL_TASK_SCOPES)}, got {self.scope!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "blocking": self.blocking,
            "scope": self.scope,
        }
        optional_fields = {
            "message": self.message,
            "related_tool_call_id": self.related_tool_call_id,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }
        for key, value in optional_fields.items():
            if value is not None:
                payload[key] = value
        return payload
```

- [ ] **Step 4: Extend `backend/llm/types.py`**

Modify imports:

```python
from agent.internal_tasks import InternalTask
from agent.types import ToolCall, ToolResult
```

Add enum value:

```python
    INTERNAL_TASK = "internal_task"
```

Add dataclass field:

```python
    internal_task: InternalTask | None = None
```

- [ ] **Step 5: Run the type tests and verify they pass**

Run:

```bash
cd backend
pytest tests/test_types.py::test_internal_task_to_dict_omits_none_fields tests/test_types.py::test_internal_task_requires_known_status_and_scope tests/test_types.py::test_llm_chunk_accepts_internal_task -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/agent/internal_tasks.py backend/llm/types.py backend/tests/test_types.py
git commit -m "feat: add internal task event type"
```

---

### Task 2: Stream Internal Task SSE Events

**Files:**
- Modify: `backend/main.py`
- Test: `backend/tests/test_api.py`

- [ ] **Step 1: Write failing SSE serialization test**

Append to `backend/tests/test_api.py`:

```python
@pytest.mark.asyncio
async def test_chat_stream_emits_internal_task_event(app):
    from agent.internal_tasks import InternalTask
    from llm.types import ChunkType, LLMChunk

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(
            type=ChunkType.INTERNAL_TASK,
            internal_task=InternalTask(
                id="soft_judge:tc_1",
                kind="soft_judge",
                label="行程质量评审",
                status="pending",
                message="正在检查行程质量…",
                related_tool_call_id="tc_1",
                started_at=100.0,
            ),
        )
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]

            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "继续"},
            )

    assert resp.status_code == 200
    assert '"type": "internal_task"' in resp.text
    assert '"kind": "soft_judge"' in resp.text
    assert '"label": "行程质量评审"' in resp.text
    assert '"status": "pending"' in resp.text
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
cd backend
pytest tests/test_api.py::test_chat_stream_emits_internal_task_event -v
```

Expected: FAIL because `_run_agent_stream()` does not serialize `ChunkType.INTERNAL_TASK`.

- [ ] **Step 3: Add internal task serialization in `_run_agent_stream()`**

In `backend/main.py`, inside `_run_agent_stream()` before the generic `event_type` block, add:

```python
                    if (
                        chunk.type == ChunkType.INTERNAL_TASK
                        and chunk.internal_task is not None
                    ):
                        yield json.dumps(
                            {
                                "type": "internal_task",
                                "task": chunk.internal_task.to_dict(),
                            },
                            ensure_ascii=False,
                        )
                        continue
```

- [ ] **Step 4: Run test and verify it passes**

Run:

```bash
cd backend
pytest tests/test_api.py::test_chat_stream_emits_internal_task_event -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_api.py
git commit -m "feat: stream internal task events"
```

---

### Task 3: Add `after_tool_result` and Fix Soft Judge Ordering

**Files:**
- Modify: `backend/agent/loop.py`
- Modify: `backend/main.py`
- Test: `backend/tests/test_agent_loop.py`
- Test: `backend/tests/test_realtime_validation_hook.py`

- [ ] **Step 1: Write failing ordering test**

Append to `backend/tests/test_agent_loop.py`:

```python
@pytest.mark.asyncio
async def test_tool_result_emitted_before_slow_after_tool_result_hook(agent, mock_llm, hooks):
    hook_started = asyncio.Event()
    release_hook = asyncio.Event()

    async def slow_hook(**kwargs):
        hook_started.set()
        await release_hook.wait()

    hooks.register("after_tool_result", slow_hook)

    call_count = 0

    async def mock_chat(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(id="tc_1", name="greet", arguments={"name": "X"}),
            )
            yield LLMChunk(type=ChunkType.DONE)
        else:
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
            yield LLMChunk(type=ChunkType.DONE)

    mock_llm.chat = mock_chat

    messages = [Message(role=Role.USER, content="hi")]
    stream = agent.run(messages, phase=1)

    try:
        seen_types = []
        while True:
            chunk = await asyncio.wait_for(stream.__anext__(), timeout=0.2)
            seen_types.append(chunk.type)
            if chunk.type == ChunkType.TOOL_RESULT:
                assert chunk.tool_result is not None
                assert chunk.tool_result.status == "success"
                break

        assert ChunkType.TOOL_CALL_START in seen_types
        assert hook_started.is_set() is False
    finally:
        release_hook.set()
        await stream.aclose()
```

- [ ] **Step 2: Run ordering test and verify it fails**

Run:

```bash
cd backend
pytest tests/test_agent_loop.py::test_tool_result_emitted_before_slow_after_tool_result_hook -v
```

Expected: FAIL because `after_tool_result` is not invoked after `TOOL_RESULT`.

- [ ] **Step 3: Run `after_tool_result` after every `TOOL_RESULT` and stream queued internal tasks while it runs**

In `backend/agent/loop.py`, import:

```python
from contextlib import suppress
from agent.internal_tasks import InternalTask
```

Extend `AgentLoop.__init__` with:

```python
        internal_task_events: list[InternalTask] | None = None,
```

Store it:

```python
        self.internal_task_events = (
            internal_task_events if internal_task_events is not None else []
        )
```

Add these helpers to `AgentLoop`:

```python
    def _drain_internal_task_events(self) -> list[InternalTask]:
        events = list(self.internal_task_events)
        self.internal_task_events.clear()
        return events

    async def _run_after_tool_result_hook(
        self,
        *,
        tool_name: str,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> AsyncIterator[LLMChunk]:
        hook_task = asyncio.create_task(
            self.hooks.run(
                "after_tool_result",
                tool_name=tool_name,
                tool_call=tool_call,
                result=result,
            )
        )
        try:
            while not hook_task.done():
                for task in self._drain_internal_task_events():
                    yield LLMChunk(type=ChunkType.INTERNAL_TASK, internal_task=task)
                await asyncio.sleep(0.05)
            await hook_task
        except Exception:
            hook_task.cancel()
            with suppress(asyncio.CancelledError):
                await hook_task
            raise
        for task in self._drain_internal_task_events():
            yield LLMChunk(type=ChunkType.INTERNAL_TASK, internal_task=task)
```

After each existing:

```python
yield LLMChunk(
    type=ChunkType.TOOL_RESULT,
    tool_result=result,
)
```

add:

```python
async for hook_chunk in self._run_after_tool_result_hook(
    tool_name=tc.name,
    tool_call=tc,
    result=result,
):
    yield hook_chunk
```

For the parallel read batch branch, use `batch_tc` instead of `tc`:

```python
async for hook_chunk in self._run_after_tool_result_hook(
    tool_name=batch_tc.name,
    tool_call=batch_tc,
    result=result,
):
    yield hook_chunk
```

Do not move the existing `after_tool_call` calls. They must remain before `TOOL_RESULT` so `state_changes`, `validation_errors`, and existing stats attachment continue to work.

- [ ] **Step 4: Move soft judge registration to `after_tool_result`**

In `backend/main.py`, change:

```python
hooks.register("after_tool_call", on_soft_judge)
```

to:

```python
hooks.register("after_tool_result", on_soft_judge)
```

Keep `on_tool_call` and `on_validate` on `after_tool_call`.

- [ ] **Step 5: Run ordering and stats tests**

Run:

```bash
cd backend
pytest tests/test_agent_loop.py::test_tool_result_emitted_before_slow_after_tool_result_hook tests/test_realtime_validation_hook.py::test_on_validate_records_state_changes_to_stats -v
```

Expected: PASS. The realtime validation stats test proves the existing `after_tool_call` stats behavior did not regress.

- [ ] **Step 6: Commit**

```bash
git add backend/agent/loop.py backend/main.py backend/tests/test_agent_loop.py
git commit -m "fix: separate post-tool internal work from tool results"
```

---

### Task 4: Instrument Soft Judge Internal Task

**Files:**
- Modify: `backend/main.py`
- Test: `backend/tests/test_realtime_validation_hook.py`

- [ ] **Step 1: Write failing soft judge visibility test**

Append to `backend/tests/test_realtime_validation_hook.py`:

```python
@pytest.mark.asyncio
async def test_save_day_plan_emits_soft_judge_internal_task_events(app, sessions):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]
        plan.phase = 5
        plan.destination = "东京"
        plan.dates = DateRange(start="2026-05-01", end="2026-05-01")
        plan.budget = Budget(total=10_000)
        plan.accommodation = Accommodation(area="新宿", hotel="A")

        agent = session["agent"]

        async def fake_chat(messages, tools=None, stream=True, **kw):
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_day",
                    name="save_day_plan",
                    arguments={
                        "mode": "create",
                        "day": 1,
                        "date": "2026-05-01",
                        "activities": [
                            {
                                "name": "浅草寺",
                                "location": {"name": "浅草寺", "lat": 35.7148, "lng": 139.7967},
                                "start_time": "09:00",
                                "end_time": "10:00",
                                "category": "景点",
                                "cost": 0,
                            }
                        ],
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat

        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "安排第一天"},
        )

    assert resp.status_code == 200
    tool_result_index = resp.text.index('"type": "tool_result"')
    pending_index = resp.text.index('"id": "soft_judge:tc_day"')
    assert tool_result_index < pending_index
    assert '"kind": "soft_judge"' in resp.text
    assert '"status": "pending"' in resp.text
    assert '"status": "success"' in resp.text
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
cd backend
pytest tests/test_realtime_validation_hook.py::test_save_day_plan_emits_soft_judge_internal_task_events -v
```

Expected: FAIL because `on_soft_judge` does not emit internal task events.

- [ ] **Step 3: Emit pending/success/warning/error in `on_soft_judge`**

In `backend/main.py`, import:

```python
from agent.internal_tasks import InternalTask
```

Inside `on_soft_judge`, get the tool call:

```python
tc = kwargs.get("tool_call")
result = kwargs.get("result")
if not (result and result.status == "success"):
    return
tool_call_id = tc.id if tc else "unknown"
task_id = f"soft_judge:{tool_call_id}"
started_at = time.time()
```

In `_build_agent()` in `backend/main.py`, create and pass the shared list that Task 3 added to `AgentLoop`. Put this near the `hooks = HookManager()` line:

```python
session = sessions.get(plan.session_id)
internal_task_events = (
    session.setdefault("_internal_task_events", []) if session is not None else []
)
```

Pass it into `AgentLoop(...)`:

```python
internal_task_events=internal_task_events,
```

In `on_soft_judge`, bind the list near `session = sessions.get(plan.session_id)`:

```python
internal_task_events = session.setdefault("_internal_task_events", [])
```

Append pending before the LLM call:

```python
internal_task_events.append(
    InternalTask(
        id=task_id,
        kind="soft_judge",
        label="行程质量评审",
        status="pending",
        message="正在检查节奏、地理顺路性、连贯性和个性化匹配…",
        blocking=True,
        scope="turn",
        related_tool_call_id=tool_call_id,
        started_at=started_at,
    )
)
```

For final status:

```python
status = "warning" if score.suggestions else "success"
internal_task_events.append(
    InternalTask(
        id=task_id,
        kind="soft_judge",
        label="行程质量评审",
        status=status,
        message=(
            f"评分 {score.overall:.1f}/5，发现 {len(score.suggestions)} 条建议"
            if score.suggestions
            else f"评分 {score.overall:.1f}/5，未发现需要立即修正的问题"
        ),
        blocking=True,
        scope="turn",
        related_tool_call_id=tool_call_id,
        result={
            "overall": score.overall,
            "pace": score.pace,
            "geography": score.geography,
            "coherence": score.coherence,
            "personalization": score.personalization,
            "suggestions": score.suggestions,
        },
        started_at=started_at,
        ended_at=time.time(),
    )
)
```

Wrap the LLM call in `asyncio.wait_for(..., timeout=20)` and append `error` final task on exception:

```python
internal_task_events.append(
    InternalTask(
        id=task_id,
        kind="soft_judge",
        label="行程质量评审",
        status="error",
        message="行程质量评审失败，已跳过，不影响行程保存",
        error=str(exc),
        blocking=True,
        scope="turn",
        related_tool_call_id=tool_call_id,
        started_at=started_at,
        ended_at=time.time(),
    )
)
```

- [ ] **Step 4: Run soft judge test**

Run:

```bash
cd backend
pytest tests/test_realtime_validation_hook.py::test_save_day_plan_emits_soft_judge_internal_task_events -v
```

Expected: PASS.

- [ ] **Step 5: Run focused backend regression**

Run:

```bash
cd backend
pytest tests/test_agent_loop.py tests/test_realtime_validation_hook.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/agent/loop.py backend/main.py backend/tests/test_realtime_validation_hook.py
git commit -m "feat: show soft judge as internal task"
```

---

### Task 5: Instrument Quality Gate

**Files:**
- Modify: `backend/main.py`
- Test: `backend/tests/test_api.py`

- [ ] **Step 1: Write failing quality gate visibility test**

Append to `backend/tests/test_api.py`:

```python
@pytest.mark.asyncio
async def test_quality_gate_emits_internal_task_when_blocking(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    data_dir = tmp_path / "data"
    config_file.write_text(
        f"""
llm:
  provider: openai
  model: gpt-4o
data_dir: "{data_dir}"
flyai:
  enabled: false
quality_gate:
  threshold: 4.5
  max_retries: 1
memory_extraction:
  enabled: false
telemetry:
  enabled: false
""",
        encoding="utf-8",
    )

    class LowScoreProvider:
        async def chat(self, *args, **kwargs):
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA,
                content='{"overall":3.0,"pace":3,"geography":3,"coherence":3,"personalization":3,"suggestions":["补强路线顺路性"]}',
            )
            yield LLMChunk(type=ChunkType.DONE)

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    monkeypatch.setattr("main.create_llm_provider", lambda _config: LowScoreProvider())
    app = create_app(str(config_file))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]

    session = _get_sessions(app)[session_id]
    plan = session["plan"]
    plan.phase = 5
    plan.destination = "京都"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.daily_plans = [
        DayPlan(day=1, date="2026-05-01"),
        DayPlan(day=2, date="2026-05-02"),
        DayPlan(day=3, date="2026-05-03"),
    ]

    agent = session["agent"]

    async def fake_chat(messages, tools=None, stream=True, **kw):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="准备进入下一阶段")
        yield LLMChunk(type=ChunkType.DONE)

    agent.llm.chat = fake_chat

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "继续"},
        )

    assert resp.status_code == 200
    assert '"kind": "quality_gate"' in resp.text
    assert '"label": "阶段推进检查"' in resp.text
    assert '"status": "pending"' in resp.text
    assert '"status": "warning"' in resp.text
    assert "补强路线顺路性" in resp.text
```

Add this import to `backend/tests/test_api.py` with the existing state model imports:

```python
from state.models import DayPlan
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
cd backend
pytest tests/test_api.py::test_quality_gate_emits_internal_task_when_blocking -v
```

Expected: FAIL because quality gate does not emit internal task events.

- [ ] **Step 3: Add quality gate pending/final events**

In `on_before_phase_transition`, get session internal events:

```python
internal_task_events = session.setdefault("_internal_task_events", []) if session else []
task_id = f"quality_gate:{target_plan.session_id}:{from_phase}:{to_phase}"
started_at = time.time()
internal_task_events.append(
    InternalTask(
        id=task_id,
        kind="quality_gate",
        label="阶段推进检查",
        status="pending",
        message=f"正在判断 Phase {from_phase} 是否可以进入 Phase {to_phase}…",
        blocking=True,
        scope="turn",
        result={"from_phase": from_phase, "to_phase": to_phase},
        started_at=started_at,
    )
)
```

When hard constraints fail, append:

```python
internal_task_events.append(
    InternalTask(
        id=task_id,
        kind="quality_gate",
        label="阶段推进检查",
        status="warning",
        message="发现硬约束冲突，暂不推进阶段",
        blocking=True,
        scope="turn",
        result={"errors": errors},
        started_at=started_at,
        ended_at=time.time(),
    )
)
```

When score passes:

```python
internal_task_events.append(
    InternalTask(
        id=task_id,
        kind="quality_gate",
        label="阶段推进检查",
        status="success",
        message=f"评分 {score.overall:.1f}/5，可以进入 Phase {to_phase}",
        blocking=True,
        scope="turn",
        result={"overall": score.overall},
        started_at=started_at,
        ended_at=time.time(),
    )
)
```

When score blocks:

```python
internal_task_events.append(
    InternalTask(
        id=task_id,
        kind="quality_gate",
        label="阶段推进检查",
        status="warning",
        message=f"评分 {score.overall:.1f}/5，低于阈值 {config.quality_gate.threshold:.1f}",
        blocking=True,
        scope="turn",
        result={"overall": score.overall, "suggestions": suggestions},
        started_at=started_at,
        ended_at=time.time(),
    )
)
```

When judge fails and gate allows:

```python
internal_task_events.append(
    InternalTask(
        id=task_id,
        kind="quality_gate",
        label="阶段推进检查",
        status="skipped",
        message="阶段推进检查不可用，已跳过并允许主流程继续",
        blocking=True,
        scope="turn",
        error=str(exc),
        started_at=started_at,
        ended_at=time.time(),
    )
)
```

Also drain internal task events after `phase_router.check_and_apply_transition(...)` in `AgentLoop` so gate events appear even when the transition check does not produce another tool result. Add a drain loop immediately after `check_and_apply_transition` returns.

- [ ] **Step 4: Run quality gate test**

Run:

```bash
cd backend
pytest tests/test_api.py::test_quality_gate_emits_internal_task_when_blocking -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/agent/loop.py backend/tests/test_api.py
git commit -m "feat: show quality gate internal tasks"
```

---

### Task 6: Frontend Types and Chat State

**Files:**
- Modify: `frontend/src/types/plan.ts`
- Modify: `frontend/src/components/ChatPanel.tsx`

- [ ] **Step 1: Add TypeScript event type**

In `frontend/src/types/plan.ts`, add:

```ts
export interface InternalTaskEvent {
  id: string
  kind: string
  label: string
  status: 'pending' | 'success' | 'warning' | 'error' | 'skipped'
  message?: string
  blocking: boolean
  scope: 'turn' | 'background' | 'session'
  related_tool_call_id?: string | null
  result?: unknown
  error?: string | null
  started_at?: number
  ended_at?: number
}
```

Add to `BaseSSEEvent`:

```ts
  task?: InternalTaskEvent
```

Add `'internal_task'` to `GenericSSEEvent.type`.

- [ ] **Step 2: Extend ChatMessage**

In `frontend/src/components/ChatPanel.tsx`, import type:

```ts
import type { InternalTaskEvent, ParallelWorkerStatus, PhaseTransitionEvent, SSEEvent, TravelPlanState } from '../types/plan'
```

Add to `ChatMessage`:

```ts
  internalTask?: InternalTaskEvent
```

Add to `EventHandlerState`:

```ts
  internalTaskMessageIds: Map<string, string>
```

Initialize in both `startMessageStream()` and `handleContinue()`:

```ts
internalTaskMessageIds: new Map<string, string>(),
```

- [ ] **Step 3: Handle `internal_task` SSE**

In `createEventHandler`, before `agent_status`, add:

```ts
    } else if (event.type === 'internal_task' && event.task) {
      const task = event.task
      const existingMessageId = state.internalTaskMessageIds.get(task.id)
      if (!existingMessageId) {
        const id = createMessageId()
        state.internalTaskMessageIds.set(task.id, id)
        setMessages((prev) =>
          insertBeforeAssistant(prev, state.currentAssistantId, {
            id,
            role: 'system',
            content: '',
            internalTask: task,
          }),
        )
      } else {
        setMessages((prev) =>
          prev.map((message) =>
            message.id === existingMessageId
              ? { ...message, internalTask: task }
              : message,
          ),
        )
      }
```

- [ ] **Step 4: Pass `internalTask` into MessageBubble**

In the render call:

```tsx
internalTask={m.internalTask}
```

- [ ] **Step 5: Run TypeScript build and verify failure**

Run:

```bash
cd frontend
npm run build
```

Expected: FAIL because `MessageBubble` does not accept `internalTask` yet. This confirms the event state path is wired to a missing renderer.

- [ ] **Step 6: Commit after Task 7 renderer passes**

Do not commit yet; Task 7 completes the frontend build.

---

### Task 7: Render System Task Cards

**Files:**
- Modify: `frontend/src/components/MessageBubble.tsx`
- Modify: `frontend/src/styles/index.css`

- [ ] **Step 1: Add MessageBubble prop**

In `frontend/src/components/MessageBubble.tsx`, import:

```ts
import type { InternalTaskEvent } from '../types/plan'
```

Add to `Props`:

```ts
  internalTask?: InternalTaskEvent
```

Destructure:

```ts
  internalTask,
```

- [ ] **Step 2: Add timer for pending internal tasks**

Update the existing timer effect condition:

```ts
  useEffect(() => {
    const shouldTick =
      (role === 'tool' && toolStatus === 'pending') ||
      (role === 'system' && internalTask?.status === 'pending')
    if (!shouldTick) return undefined

    const timer = window.setInterval(() => {
      setNow(Date.now())
    }, 500)

    return () => window.clearInterval(timer)
  }, [role, toolStatus, internalTask?.status])
```

- [ ] **Step 3: Render internal task card**

Before the `phaseTransition` branch, add:

```tsx
  if (role === 'system' && internalTask) {
    const startedAtMs = internalTask.started_at ? internalTask.started_at * 1000 : undefined
    const endedAtMs = internalTask.ended_at ? internalTask.ended_at * 1000 : undefined
    const elapsedMs = startedAtMs
      ? Math.max(0, (endedAtMs ?? (internalTask.status === 'pending' ? now : Date.now())) - startedAtMs)
      : null
    const elapsedLabel = elapsedMs !== null ? `${(elapsedMs / 1000).toFixed(1)}s` : null
    const hasDetails = internalTask.result !== undefined || internalTask.error

    return (
      <div className={`message system-internal-task ${internalTask.status}`}>
        <div className="internal-task-card">
          <div className="internal-task-header">
            <div className="internal-task-title-block">
              <span className="internal-task-kicker">系统任务</span>
              <span className="internal-task-title">{internalTask.label}</span>
            </div>
            <div className="internal-task-actions">
              {elapsedLabel && <span className="internal-task-elapsed">{elapsedLabel}</span>}
              <span className={`internal-task-status ${internalTask.status}`}>
                {internalTask.status === 'pending'
                  ? '执行中'
                  : internalTask.status === 'success'
                    ? '完成'
                    : internalTask.status === 'warning'
                      ? '需关注'
                      : internalTask.status === 'error'
                        ? '失败'
                        : '已跳过'}
              </span>
              {hasDetails && (
                <button
                  type="button"
                  className="tool-details-toggle"
                  onClick={() => setDetailsExpanded((value) => !value)}
                  aria-expanded={detailsExpanded}
                >
                  详情{detailsExpanded ? '收起' : '展开'}
                </button>
              )}
            </div>
          </div>
          {internalTask.message && (
            <div className="internal-task-message">{internalTask.message}</div>
          )}
          {internalTask.status === 'pending' && <div className="internal-task-progress" />}
          {detailsExpanded && hasDetails && (
            <div className="tool-section">
              {internalTask.result !== undefined && (
                <div className="tool-section-detail">
                  <div className="tool-section-title">结果</div>
                  <pre className="tool-json">{formatJson(internalTask.result)}</pre>
                </div>
              )}
              {internalTask.error && (
                <div className="tool-section-detail">
                  <div className="tool-section-title">错误</div>
                  <pre className="tool-json">{internalTask.error}</pre>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    )
  }
```

- [ ] **Step 4: Add CSS**

Append to `frontend/src/styles/index.css`:

```css
.message.system-internal-task {
  align-self: stretch;
  max-width: min(760px, 100%);
}

.internal-task-card {
  border: 1px solid rgba(137, 160, 190, 0.26);
  border-left: 4px solid rgba(137, 160, 190, 0.82);
  border-radius: 16px;
  padding: 12px 14px;
  background: rgba(15, 23, 42, 0.56);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.18);
}

.system-internal-task.pending .internal-task-card {
  border-left-color: #6aa9ff;
}

.system-internal-task.success .internal-task-card {
  border-left-color: #61d394;
}

.system-internal-task.warning .internal-task-card {
  border-left-color: #f2b84b;
}

.system-internal-task.error .internal-task-card {
  border-left-color: #ff6b6b;
}

.system-internal-task.skipped .internal-task-card {
  opacity: 0.78;
}

.internal-task-header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
}

.internal-task-title-block {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.internal-task-kicker {
  color: var(--text-muted);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.internal-task-title {
  color: var(--text-primary);
  font-weight: 700;
}

.internal-task-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.internal-task-elapsed {
  color: var(--text-muted);
  font-size: 12px;
}

.internal-task-status {
  border-radius: 999px;
  padding: 3px 8px;
  font-size: 12px;
  background: rgba(255, 255, 255, 0.08);
}

.internal-task-status.pending {
  color: #9cc8ff;
}

.internal-task-status.success {
  color: #8be0ad;
}

.internal-task-status.warning {
  color: #ffd083;
}

.internal-task-status.error {
  color: #ff9a9a;
}

.internal-task-message {
  margin-top: 8px;
  color: var(--text-secondary);
  line-height: 1.5;
}

.internal-task-progress {
  margin-top: 10px;
  height: 2px;
  border-radius: 999px;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.08);
}

.internal-task-progress::after {
  content: "";
  display: block;
  width: 36%;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, transparent, #6aa9ff, transparent);
  animation: internal-task-sweep 1.2s ease-in-out infinite;
}

@keyframes internal-task-sweep {
  from { transform: translateX(-100%); }
  to { transform: translateX(300%); }
}
```

- [ ] **Step 5: Run frontend build**

Run:

```bash
cd frontend
npm run build
```

Expected: PASS.

- [ ] **Step 6: Commit Tasks 6 and 7**

```bash
git add frontend/src/types/plan.ts frontend/src/components/ChatPanel.tsx frontend/src/components/MessageBubble.tsx frontend/src/styles/index.css
git commit -m "feat: render internal task cards"
```

---

### Task 8: Show Context, Memory, Reflection, and Phase 5 Orchestration Tasks

**Files:**
- Modify: `backend/agent/loop.py`
- Modify: `backend/main.py`
- Test: `backend/tests/test_agent_status_event.py`
- Test: `backend/tests/test_memory_integration.py`
- Test: `backend/tests/test_orchestrator.py`

- [ ] **Step 1: Write backend tests for additional task kinds**

Add to `backend/tests/test_agent_status_event.py`:

```python
@pytest.mark.asyncio
async def test_reflection_emits_internal_task_when_message_injected(engine, hooks):
    plan = TravelPlanState(session_id="s1", phase=3, phase3_step="lock")

    class FakeReflection:
        def check_and_inject(self, messages, plan_arg, prev_step):
            return "[自检] 请检查交通住宿"

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="ok")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=hooks,
        plan=plan,
        reflection=FakeReflection(),
    )

    chunks = [chunk async for chunk in agent.run([Message(role=Role.USER, content="继续")], phase=3)]
    tasks = [c.internal_task for c in chunks if c.type == ChunkType.INTERNAL_TASK]

    assert any(t and t.kind == "reflection" and t.status == "success" for t in tasks)
```

Add this test to `backend/tests/test_memory_integration.py`:

```python
@pytest.mark.asyncio
async def test_chat_stream_emits_memory_recall_internal_task(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]

        sessions = _get_sessions(app)
        session = sessions[session_id]
        session["memory_context"] = "用户偏好：喜欢轻松行程"
        session["recalled_ids"] = ["mem_1"]

        async def fake_run(messages, phase, tools_override=None):
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="继续")
            yield LLMChunk(type=ChunkType.DONE)

        session["agent"].run = fake_run

        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "继续"},
        )

    assert resp.status_code == 200
    assert '"type": "internal_task"' in resp.text
    assert '"kind": "memory_recall"' in resp.text
    assert '"status": "success"' in resp.text
```

Add this compression assertion test to `backend/tests/test_agent_status_event.py`:

```python
@pytest.mark.asyncio
async def test_context_compaction_emits_internal_task(engine, hooks):
    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="ok")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=hooks,
        compression_events=[
            {
                "message_count_before": 10,
                "message_count_after": 6,
                "must_keep_count": 2,
                "compressed_count": 4,
                "estimated_tokens_before": 12000,
                "reason": "test compaction",
            }
        ],
    )

    chunks = [chunk async for chunk in agent.run([Message(role=Role.USER, content="继续")], phase=1)]
    tasks = [c.internal_task for c in chunks if c.type == ChunkType.INTERNAL_TASK]

    assert any(t and t.kind == "context_compaction" and t.status == "pending" for t in tasks)
    assert any(t and t.kind == "context_compaction" and t.status == "success" for t in tasks)
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
cd backend
pytest tests/test_agent_status_event.py::test_reflection_emits_internal_task_when_message_injected tests/test_agent_status_event.py::test_context_compaction_emits_internal_task tests/test_memory_integration.py::test_chat_stream_emits_memory_recall_internal_task -v
```

Expected: FAIL because reflection, context compaction, and memory recall do not emit internal task events.

- [ ] **Step 3: Emit reflection internal tasks**

In `backend/agent/loop.py`, after appending a reflection message, yield:

```python
yield LLMChunk(
    type=ChunkType.INTERNAL_TASK,
    internal_task=InternalTask(
        id=f"reflection:{iteration_idx}",
        kind="reflection",
        label="反思注入",
        status="success",
        message="已注入阶段自检提示",
        blocking=False,
        scope="turn",
        result={"message": reflection_msg},
        started_at=time.time(),
        ended_at=time.time(),
    ),
)
```

Add `import time` and `from agent.internal_tasks import InternalTask`.

- [ ] **Step 4: Convert context compression to internal tasks**

When `self.compression_events` is present, emit:

```python
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
        started_at=time.time(),
    ),
)
```

After draining compression events, emit final success:

```python
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
        ended_at=time.time(),
    ),
)
```

Keep existing `CONTEXT_COMPRESSION` events for backward compatibility.

- [ ] **Step 5: Emit memory recall internal tasks**

In `backend/main.py`, move `memory_mgr.generate_context(...)` into `event_stream()` so the stream can show memory recall as pending before awaiting the retrieval. Emit this pending task before the await:

```python
memory_recall_task_id = f"memory_recall:{plan.session_id}:{int(time.time())}"
memory_recall_started_at = time.time()
yield json.dumps(
    {
        "type": "internal_task",
        "task": InternalTask(
            id=memory_recall_task_id,
            kind="memory_recall",
            label="记忆召回",
            status="pending",
            message="正在检索本轮可用旅行记忆…",
            blocking=True,
            scope="turn",
            started_at=memory_recall_started_at,
        ).to_dict(),
    },
    ensure_ascii=False,
)
```

After `memory_mgr.generate_context(...)` returns, emit final success or skipped:

```python
yield json.dumps(
    {
        "type": "internal_task",
        "task": InternalTask(
            id=memory_recall_task_id,
            kind="memory_recall",
            label="记忆召回",
            status="success" if recalled_ids else "skipped",
            message=(
                f"本轮使用 {len(recalled_ids)} 条旅行记忆"
                if recalled_ids
                else "未找到本轮可用记忆"
            ),
            blocking=True,
            scope="turn",
            result={"item_ids": recalled_ids, "count": len(recalled_ids)},
            started_at=memory_recall_started_at,
            ended_at=time.time(),
        ).to_dict(),
    },
    ensure_ascii=False,
)
```

- [ ] **Step 6: Emit memory extraction internal tasks**

In `_start_memory_extraction`, append a background pending event to a session-level list and append a final event in `_on_done`. The current stream can already be closed when `_on_done` fires, so the implementation emits queued background task events at the beginning of the next request.

```python
session.setdefault("_background_internal_tasks", []).append(task)
```

At the top of `event_stream()`, before memory recall, drain `_background_internal_tasks` and yield each as `internal_task`.

Use task ids:

```python
id=f"memory_extraction:{session_id}:{int(time.time())}"
```

Final statuses:

- `success` with `result={"item_ids": item_ids, "count": len(item_ids)}`
- `skipped` when extraction disabled or no user messages
- `error` when exception occurs

- [ ] **Step 7: Emit Phase 5 orchestration internal tasks**

In `backend/agent/loop.py`, at the beginning of `_run_parallel_phase5_orchestrator()`, emit:

```python
yield LLMChunk(
    type=ChunkType.INTERNAL_TASK,
    internal_task=InternalTask(
        id=f"phase5_orchestration:{self.plan.session_id}",
        kind="phase5_orchestration",
        label="Phase 5 并行编排",
        status="pending",
        message="正在拆分每日任务并并行生成行程…",
        blocking=True,
        scope="turn",
        started_at=time.time(),
    ),
)
```

At the successful return point of `_run_parallel_phase5_orchestrator()`, emit:

```python
yield LLMChunk(
    type=ChunkType.INTERNAL_TASK,
    internal_task=InternalTask(
        id=f"phase5_orchestration:{self.plan.session_id}",
        kind="phase5_orchestration",
        label="Phase 5 并行编排",
        status="success",
        message="并行逐日行程生成完成",
        blocking=True,
        scope="turn",
        ended_at=time.time(),
    ),
)
```

At the fallback path, emit `status="warning"` with message `"并行生成未完全成功，已降级到串行模式"` and include `result={"fallback": True}`.

- [ ] **Step 8: Run focused tests**

Run:

```bash
cd backend
pytest tests/test_agent_status_event.py tests/test_memory_integration.py tests/test_orchestrator.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add backend/agent/loop.py backend/main.py backend/tests/test_agent_status_event.py backend/tests/test_memory_integration.py backend/tests/test_orchestrator.py
git commit -m "feat: show internal runtime tasks"
```

---

### Task 9: E2E Regression for Slow Soft Judge

**Files:**
- Modify: `e2e-waiting-experience.spec.ts`

- [ ] **Step 1: Add E2E test scenario**

Append this Playwright test inside `test.describe('Agent waiting experience', ...)`. It reuses the existing `installDeterministicWaitingMock()` helper:

```ts
test('shows internal task instead of leaving tool card pending during soft judge', async ({ page }) => {
  const scenario: MockScenario = {
    expectedMessage: '修改问题',
    events: [
      {
        delayMs: 100,
        payload: {
          type: 'tool_call',
          tool_call: {
            id: 'tc_day',
            name: 'save_day_plan',
            human_label: '保存单日行程',
            arguments: { day: 3 },
          },
        },
      },
      {
        delayMs: 300,
        payload: {
          type: 'tool_result',
          tool_result: {
            tool_call_id: 'tc_day',
            status: 'success',
            data: { day: 3 },
          },
        },
      },
      {
        delayMs: 350,
        payload: {
          type: 'internal_task',
          task: {
            id: 'soft_judge:tc_day',
            kind: 'soft_judge',
            label: '行程质量评审',
            status: 'pending',
            message: '正在检查行程质量…',
            blocking: true,
            scope: 'turn',
            related_tool_call_id: 'tc_day',
            started_at: Date.now() / 1000,
          },
        },
      },
      {
        delayMs: 900,
        payload: {
          type: 'internal_task',
          task: {
            id: 'soft_judge:tc_day',
            kind: 'soft_judge',
            label: '行程质量评审',
            status: 'warning',
            message: '评分 3.5/5，发现 2 条建议',
            blocking: true,
            scope: 'turn',
            related_tool_call_id: 'tc_day',
            result: { overall: 3.5, suggestions: ['统一交通方式', '调整午餐时间'] },
            ended_at: Date.now() / 1000,
          },
        },
      },
      { delayMs: 950, payload: { type: 'done' } },
    ],
  }

  await installDeterministicWaitingMock(page, scenario)
  await openChatAndSend(page, scenario.expectedMessage)

  await expect(page.getByText('保存单日行程')).toBeVisible()
  await expect(page.getByText('成功')).toBeVisible()
  await expect(page.getByText('行程质量评审')).toBeVisible()
  await expect(page.getByText('评分 3.5/5，发现 2 条建议')).toBeVisible()
})
```

- [ ] **Step 2: Run E2E test and verify it passes**

Run:

```bash
npx playwright test e2e-waiting-experience.spec.ts -g "shows internal task"
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add e2e-waiting-experience.spec.ts
git commit -m "test: cover internal task chat visibility"
```

---

### Task 10: Final Verification and Documentation Update

**Files:**
- Modify: `PROJECT_OVERVIEW.md`
- Modify: `docs/superpowers/specs/2026-04-19-internal-task-visibility-design.md`

- [ ] **Step 1: Update project overview**

In `PROJECT_OVERVIEW.md`, update the data flow and frontend component sections to mention `internal_task` SSE and system task cards.

Add to SSE data flow section:

```markdown
    ├─ Internal Task Events → soft judge / quality gate / memory / compaction / reflection lifecycle
```

Add to frontend component list:

```markdown
ChatPanel / MessageBubble now render `internal_task` SSE events as system task cards, distinct from real tool cards.
```

- [ ] **Step 2: Reconcile spec with final implementation**

Open `docs/superpowers/specs/2026-04-19-internal-task-visibility-design.md` and ensure these implementation choices are recorded:

```markdown
- `AgentLoop` streams `ChunkType.INTERNAL_TASK` through the existing SSE path.
- Slow post-tool work runs after `tool_result` and streams queued internal task events while it is running.
- `memory_recall` is moved into the stream path so pending and final lifecycle events can be shown.
- Background memory extraction final events are queued and emitted at the start of the next chat stream.
```

- [ ] **Step 3: Run backend focused tests**

Run:

```bash
cd backend
pytest tests/test_types.py tests/test_agent_loop.py tests/test_api.py tests/test_realtime_validation_hook.py tests/test_agent_status_event.py tests/test_memory_integration.py tests/test_orchestrator.py -v
```

Expected: PASS.

- [ ] **Step 4: Run frontend build**

Run:

```bash
cd frontend
npm run build
```

Expected: PASS.

- [ ] **Step 5: Run E2E regression**

Run:

```bash
npx playwright test e2e-waiting-experience.spec.ts -g "shows internal task"
```

Expected: PASS.

- [ ] **Step 6: Inspect git diff**

Run:

```bash
git diff --stat
git status --short
```

Expected: only files related to internal task visibility are staged or modified. Existing unrelated dirty files should remain untouched.

- [ ] **Step 7: Commit documentation update**

```bash
git add PROJECT_OVERVIEW.md docs/superpowers/specs/2026-04-19-internal-task-visibility-design.md
git commit -m "docs: document internal task visibility runtime"
```
