# Context Runtime Restore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore persisted sessions with full append-only `history_view` available internally while giving `AgentLoop` a short, current-phase `runtime_view` that excludes stale tool results and old phase segments.

**Architecture:** Add a session-layer runtime view builder that rebuilds the current system message from real runtime dependencies, then selects the smallest safe anchor from append-only history. `SessionPersistence.restore_session()` loads full history rows, initializes `next_history_seq`, builds the agent to obtain the real `tool_engine`, and returns `messages=runtime_view` plus internal `history_messages=history_view`. Existing `/api/messages/{session_id}` remains frontend-safe and does not expose the internal full prompt restore view.

**Tech Stack:** Python 3.11, FastAPI, pytest/pytest-asyncio, SQLite storage via `MessageStore`, existing `agent.types.Message`, `TravelPlanState`, `PhaseRouter`, context manager, memory manager, and `ToolEngine`.

---

## Phase 2 Scope Boundary

This plan assumes Phase 1 has already landed append-only message history with:

- `messages.history_seq`, `messages.phase`, `messages.phase3_step`, `messages.run_id`, and `messages.trip_id`.
- `MessageStore.load_all(session_id)` returning rows ordered by `history_seq ASC, id ASC`, falling back to legacy `seq ASC, id ASC` for old rows.
- `SessionPersistence.persist_messages(...)` appending history and returning/preserving `next_history_seq`.
- Pre-rebuild flushes from phase transitions, Phase 3 substep transitions, and backtrack rebuilds.

This plan does not implement Phase 1 storage migrations or append-only writes. This plan does not implement Phase 3 `context_epoch` debug segmentation or a public history/debug API. Phase 3 is a future dependency for richer diagnostics only.

## File Structure Map

- Create `backend/api/orchestration/session/runtime_view.py`
  - Defines an internal `HistoryMessage` wrapper so persisted row metadata stays available to restore logic without relying on provider-visible `Message.to_dict()` payloads.
  - Rebuilds the current system message from `phase_router`, `context_manager`, `memory_mgr`, `memory_enabled`, `tool_engine`, `user_id`, and the current `plan`.
  - Selects safe runtime anchors from append-only history without replaying stale tool results.

- Modify `backend/api/orchestration/session/persistence.py`
  - Deserializes persisted rows into `HistoryMessage`.
  - Computes `next_history_seq = max(history_seq) + 1` with legacy fallback to `len(history_view)` when all rows are legacy.
  - Builds the agent before runtime view construction and passes `agent.tool_engine` into `build_runtime_view_for_restore(...)`.
  - Returns `messages=runtime_view`, `history_messages=history_view`, and `next_history_seq`.

- Modify `backend/api/routes/session_routes.py`
  - Keeps `/api/messages/{session_id}` frontend-safe by preserving the current filtered public shape.
  - Adds a regression test target only; do not expose `history_messages`.

- Modify `backend/tests/test_session_runtime_view.py`
  - New focused unit tests for system message rebuilding, short runtime view selection, Phase 3 substep isolation, backtrack target isolation, and legacy fallback behavior.

- Modify `backend/tests/test_session_persistence.py`
  - Restore-session tests for `history_messages`, short `messages`, `next_history_seq`, real dependency wiring, and exclusion of old tool results.

- Modify `backend/tests/test_api.py`
  - Regression test that `/api/messages/{session_id}` remains frontend-safe and does not expose internal `history_messages`.

- Modify `PROJECT_OVERVIEW.md`
  - Required by repo policy for implementation commits. Add a short Phase 2 restore note after code changes land.

## Commit Policy

`AGENTS.md` requires every commit to keep `PROJECT_OVERVIEW.md` current. Each task below includes `PROJECT_OVERVIEW.md` in its commit command. Before each commit, update the overview with the architecture that is true after that task lands; keep the edit small and focused on restore-time history/runtime view separation.

## Runtime View Rules

The builder must obey these rules:

1. Always build a fresh current system message. Never reuse a persisted system row.
2. Do not derive the system message from only `history_view + plan`. Use `phase_router.get_prompt_for_plan(plan)`, `context_manager.build_system_message(...)`, `memory_mgr.generate_context(...)`, `memory_enabled`, `tool_engine.get_tools_for_phase(...)`, and `user_id`.
3. Never include old `tool` role messages or assistant messages with `tool_calls` from history in restore runtime view.
4. Prefer a current-phase, current-Phase-3-step user anchor when available.
5. If current `plan.phase == 3`, do not include rows from previous `phase3_step` values.
6. After backtrack to an earlier phase, do not replay the old target phase segment; use the newest user anchor at or after the latest backtrack marker when available.
7. If metadata is missing or ambiguous, fall back to `[fresh_system_message, latest_user_message]`.
8. Return plain `Message` objects in `session["messages"]`; keep `HistoryMessage` only in `session["history_messages"]` for backend internals.

## Task 1: Add Runtime View Builder Unit Tests

**Files:**
- Create: `backend/tests/test_session_runtime_view.py`
- Target implementation file: `backend/api/orchestration/session/runtime_view.py`

- [ ] **Step 1: Write failing tests for short restore runtime views**

Create `backend/tests/test_session_runtime_view.py` with this content:

```python
import pytest

from agent.types import Message, Role, ToolCall, ToolResult
from api.orchestration.session.runtime_view import (
    HistoryMessage,
    build_runtime_view_for_restore,
)
from state.models import TravelPlanState


class FakePhaseRouter:
    def __init__(self) -> None:
        self.calls = 0

    def get_prompt_for_plan(self, plan):
        self.calls += 1
        return f"PROMPT phase={plan.phase} step={getattr(plan, 'phase3_step', None)}"


class FakeContextManager:
    def __init__(self) -> None:
        self.available_tools = None
        self.memory_context = None

    def build_system_message(self, plan, phase_prompt, memory_context, *, available_tools):
        self.available_tools = list(available_tools)
        self.memory_context = memory_context
        return Message(
            role=Role.SYSTEM,
            content=(
                f"fresh system | {phase_prompt} | memory={memory_context} | "
                f"tools={','.join(available_tools)}"
            ),
        )


class FakeMemoryManager:
    def __init__(self) -> None:
        self.calls = []

    async def generate_context(self, user_id, plan):
        self.calls.append((user_id, plan.phase, getattr(plan, "phase3_step", None)))
        return ("memory for restore", ["mem_1"], 1, 0, 0)


class FakeToolEngine:
    def __init__(self) -> None:
        self.calls = []

    def get_tools_for_phase(self, phase, plan):
        self.calls.append((phase, getattr(plan, "phase3_step", None)))
        return [{"name": f"phase_{phase}_tool"}, {"name": "request_backtrack"}]


def hm(
    role,
    content,
    *,
    phase=None,
    phase3_step=None,
    history_seq=0,
    tool_calls=None,
    tool_result=None,
):
    return HistoryMessage(
        message=Message(
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_result=tool_result,
        ),
        phase=phase,
        phase3_step=phase3_step,
        history_seq=history_seq,
        run_id=f"run_{history_seq}",
        trip_id="trip_1",
    )


@pytest.mark.asyncio
async def test_restore_phase5_uses_fresh_system_and_excludes_old_tool_results():
    plan = TravelPlanState(session_id="sess_1", phase=5, destination="东京")
    history = [
        hm(Role.SYSTEM, "old phase 1 system", phase=1, history_seq=0),
        hm(Role.USER, "我想去东京玩", phase=1, history_seq=1),
        hm(
            Role.ASSISTANT,
            None,
            phase=1,
            history_seq=2,
            tool_calls=[ToolCall(id="tc_old", name="update_trip_basics", arguments={})],
        ),
        hm(
            Role.TOOL,
            None,
            phase=1,
            history_seq=3,
            tool_result=ToolResult(
                tool_call_id="tc_old",
                status="success",
                data={"destination": "东京"},
            ),
        ),
        hm(Role.USER, "按这个骨架继续细化每天路线", phase=5, history_seq=9),
    ]

    phase_router = FakePhaseRouter()
    context_manager = FakeContextManager()
    memory_mgr = FakeMemoryManager()
    tool_engine = FakeToolEngine()

    runtime = await build_runtime_view_for_restore(
        history_view=history,
        plan=plan,
        user_id="user_1",
        phase_router=phase_router,
        context_manager=context_manager,
        memory_mgr=memory_mgr,
        memory_enabled=True,
        tool_engine=tool_engine,
    )

    assert [message.role for message in runtime] == [Role.SYSTEM, Role.USER]
    assert runtime[0].content.startswith("fresh system | PROMPT phase=5")
    assert runtime[1].content == "按这个骨架继续细化每天路线"
    assert len(runtime) < len(history)
    assert all(message.tool_result is None for message in runtime)
    assert all(not message.tool_calls for message in runtime)
    assert "old phase 1 system" not in [message.content for message in runtime]
    assert memory_mgr.calls == [("user_1", 5, None)]
    assert tool_engine.calls == [(5, None)]
    assert context_manager.available_tools == ["phase_5_tool", "request_backtrack"]


@pytest.mark.asyncio
async def test_restore_phase3_skeleton_does_not_replay_previous_substeps():
    plan = TravelPlanState(
        session_id="sess_2",
        phase=3,
        phase3_step="skeleton",
        destination="大阪",
    )
    history = [
        hm(Role.USER, "先确定画像", phase=3, phase3_step="brief", history_seq=1),
        hm(
            Role.TOOL,
            None,
            phase=3,
            phase3_step="brief",
            history_seq=2,
            tool_result=ToolResult(
                tool_call_id="tc_brief",
                status="success",
                data={"trip_brief": "brief old result"},
            ),
        ),
        hm(Role.USER, "给我候选池", phase=3, phase3_step="candidate", history_seq=3),
        hm(
            Role.TOOL,
            None,
            phase=3,
            phase3_step="candidate",
            history_seq=4,
            tool_result=ToolResult(
                tool_call_id="tc_candidate",
                status="success",
                data={"candidate_pool": ["old candidate"]},
            ),
        ),
        hm(Role.USER, "从短名单生成两个骨架", phase=3, phase3_step="skeleton", history_seq=5),
    ]

    runtime = await build_runtime_view_for_restore(
        history_view=history,
        plan=plan,
        user_id="user_1",
        phase_router=FakePhaseRouter(),
        context_manager=FakeContextManager(),
        memory_mgr=FakeMemoryManager(),
        memory_enabled=True,
        tool_engine=FakeToolEngine(),
    )

    assert [message.role for message in runtime] == [Role.SYSTEM, Role.USER]
    assert runtime[1].content == "从短名单生成两个骨架"
    rendered = "\n".join(str(message.content) for message in runtime)
    assert "brief old result" not in rendered
    assert "old candidate" not in rendered
    assert "先确定画像" not in rendered
    assert "给我候选池" not in rendered


@pytest.mark.asyncio
async def test_restore_after_backtrack_does_not_replay_old_target_phase_segment():
    plan = TravelPlanState(
        session_id="sess_3",
        phase=3,
        phase3_step="brief",
        destination="京都",
    )
    history = [
        hm(Role.USER, "旧的 Phase 3 画像输入", phase=3, phase3_step="brief", history_seq=1),
        hm(
            Role.TOOL,
            None,
            phase=3,
            phase3_step="brief",
            history_seq=2,
            tool_result=ToolResult(
                tool_call_id="tc_old_p3",
                status="success",
                data={"trip_brief": "old phase3 brief"},
            ),
        ),
        hm(Role.USER, "Phase 5 发现预算不合适，回到框架规划", phase=5, history_seq=10),
        hm(
            Role.TOOL,
            None,
            phase=5,
            history_seq=11,
            tool_result=ToolResult(
                tool_call_id="tc_backtrack",
                status="success",
                data={"backtracked": True, "to_phase": 3, "reason": "预算不合适"},
            ),
        ),
    ]

    runtime = await build_runtime_view_for_restore(
        history_view=history,
        plan=plan,
        user_id="user_1",
        phase_router=FakePhaseRouter(),
        context_manager=FakeContextManager(),
        memory_mgr=FakeMemoryManager(),
        memory_enabled=True,
        tool_engine=FakeToolEngine(),
    )

    assert [message.role for message in runtime] == [Role.SYSTEM, Role.USER]
    assert runtime[1].content == "Phase 5 发现预算不合适，回到框架规划"
    rendered = "\n".join(str(message.content) for message in runtime)
    assert "旧的 Phase 3 画像输入" not in rendered
    assert "old phase3 brief" not in rendered


@pytest.mark.asyncio
async def test_restore_with_legacy_rows_falls_back_to_latest_user_only():
    plan = TravelPlanState(session_id="sess_4", phase=5, destination="首尔")
    history = [
        hm(Role.SYSTEM, "legacy system", history_seq=None),
        hm(Role.USER, "第一条旧用户消息", history_seq=None),
        hm(
            Role.TOOL,
            None,
            history_seq=None,
            tool_result=ToolResult(
                tool_call_id="tc_legacy",
                status="success",
                data={"legacy": True},
            ),
        ),
        hm(Role.USER, "最新用户消息", history_seq=None),
    ]

    runtime = await build_runtime_view_for_restore(
        history_view=history,
        plan=plan,
        user_id="user_1",
        phase_router=FakePhaseRouter(),
        context_manager=FakeContextManager(),
        memory_mgr=FakeMemoryManager(),
        memory_enabled=False,
        tool_engine=FakeToolEngine(),
    )

    assert [message.role for message in runtime] == [Role.SYSTEM, Role.USER]
    assert "暂无相关用户记忆" in runtime[0].content
    assert runtime[1].content == "最新用户消息"
    assert all(message.role != Role.TOOL for message in runtime)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest backend/tests/test_session_runtime_view.py -q
```

Expected result:

```text
ModuleNotFoundError: No module named 'api.orchestration.session.runtime_view'
```

- [ ] **Step 3: Commit failing tests**

```bash
git add backend/tests/test_session_runtime_view.py PROJECT_OVERVIEW.md
git commit -m "test: cover restore runtime view boundaries"
```

Expected result: commit succeeds with `PROJECT_OVERVIEW.md` updated for the newly introduced runtime-view test boundary.

## Task 2: Implement Runtime View Builder

**Files:**
- Create: `backend/api/orchestration/session/runtime_view.py`
- Test: `backend/tests/test_session_runtime_view.py`

- [ ] **Step 1: Keep Task 1 tests failing before implementation**

Run:

```bash
pytest backend/tests/test_session_runtime_view.py -q
```

Expected result:

```text
ModuleNotFoundError: No module named 'api.orchestration.session.runtime_view'
```

- [ ] **Step 2: Add runtime view builder implementation**

Create `backend/api/orchestration/session/runtime_view.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.execution.message_rebuild import copy_message, current_tool_names
from agent.types import Message, Role


@dataclass(frozen=True)
class HistoryMessage:
    message: Message
    phase: int | None = None
    phase3_step: str | None = None
    history_seq: int | None = None
    run_id: str | None = None
    trip_id: str | None = None


def _is_backtrack_result(history_message: HistoryMessage) -> bool:
    result = history_message.message.tool_result
    return (
        result is not None
        and isinstance(result.data, dict)
        and bool(result.data.get("backtracked"))
    )


def _has_reliable_phase_metadata(history_view: list[HistoryMessage]) -> bool:
    return any(
        item.phase is not None or item.phase3_step is not None
        for item in history_view
    )


def _latest_backtrack_index(history_view: list[HistoryMessage]) -> int | None:
    for index in range(len(history_view) - 1, -1, -1):
        if _is_backtrack_result(history_view[index]):
            return index
    return None


def _latest_user_after_index(
    history_view: list[HistoryMessage],
    start_index: int,
) -> Message | None:
    for item in reversed(history_view[start_index:]):
        if item.message.role == Role.USER:
            return copy_message(item.message)
    return None


def _latest_current_phase_user(
    history_view: list[HistoryMessage],
    *,
    phase: int,
    phase3_step: str | None,
) -> Message | None:
    for item in reversed(history_view):
        if item.message.role != Role.USER:
            continue
        if item.phase != phase:
            continue
        if phase == 3 and item.phase3_step != phase3_step:
            continue
        return copy_message(item.message)
    return None


def _latest_user(history_view: list[HistoryMessage]) -> Message | None:
    for item in reversed(history_view):
        if item.message.role == Role.USER:
            return copy_message(item.message)
    return None


def select_restore_anchor(
    *,
    history_view: list[HistoryMessage],
    plan: Any,
) -> Message:
    backtrack_index = _latest_backtrack_index(history_view)
    if backtrack_index is not None:
        anchor = _latest_user_after_index(history_view, backtrack_index)
        if anchor is not None:
            return anchor

    if _has_reliable_phase_metadata(history_view):
        anchor = _latest_current_phase_user(
            history_view,
            phase=plan.phase,
            phase3_step=getattr(plan, "phase3_step", None),
        )
        if anchor is not None:
            return anchor

    anchor = _latest_user(history_view)
    if anchor is not None:
        return anchor

    return Message(role=Role.USER, content="")


async def build_runtime_view_for_restore(
    *,
    history_view: list[HistoryMessage],
    plan: Any,
    user_id: str,
    phase_router: Any,
    context_manager: Any,
    memory_mgr: Any,
    memory_enabled: bool,
    tool_engine: Any,
) -> list[Message]:
    if phase_router is None or context_manager is None or plan is None:
        raise RuntimeError("Restore runtime view requires router/context/plan")
    if memory_enabled and memory_mgr is None:
        raise RuntimeError("Restore runtime view requires memory manager when enabled")
    if tool_engine is None:
        raise RuntimeError("Restore runtime view requires tool engine")

    phase_prompt = phase_router.get_prompt_for_plan(plan)
    memory_context, *_ = (
        await memory_mgr.generate_context(user_id, plan)
        if memory_enabled
        else ("暂无相关用户记忆", [], 0, 0, 0)
    )
    system_message = context_manager.build_system_message(
        plan,
        phase_prompt,
        memory_context,
        available_tools=current_tool_names(
            tool_engine=tool_engine,
            plan=plan,
            phase=plan.phase,
        ),
    )

    return [
        system_message,
        select_restore_anchor(history_view=history_view, plan=plan),
    ]
```

- [ ] **Step 3: Run runtime view unit tests**

Run:

```bash
pytest backend/tests/test_session_runtime_view.py -q
```

Expected result:

```text
4 passed
```

- [ ] **Step 4: Run related message rebuild tests**

Run:

```bash
pytest backend/tests/test_phase_transition_event.py backend/tests/test_agent_phase_transition.py -q
```

Expected result:

```text
passed
```

- [ ] **Step 5: Commit runtime view builder**

```bash
git add backend/api/orchestration/session/runtime_view.py backend/tests/test_session_runtime_view.py PROJECT_OVERVIEW.md
git commit -m "feat: build short runtime view on restore"
```

Expected result: commit succeeds.

## Task 3: Wire Restore Session To History View And Runtime View

**Files:**
- Modify: `backend/api/orchestration/session/persistence.py`
- Test: `backend/tests/test_session_persistence.py`

- [ ] **Step 1: Add failing restore-session tests**

Append these tests to `backend/tests/test_session_persistence.py`:

```python
class _RestoreSessionStore:
    async def load(self, session_id):
        return {
            "session_id": session_id,
            "user_id": "user_restore",
            "status": "active",
        }


class _RestoreStateManager:
    async def load(self, session_id):
        return TravelPlanState(session_id=session_id, phase=5, destination="东京")


class _RestorePhaseRouter:
    def sync_phase_state(self, plan):
        plan.phase = 5

    def get_prompt_for_plan(self, plan):
        return f"restore prompt phase={plan.phase}"


class _RestoreContextManager:
    def build_system_message(self, plan, phase_prompt, memory_context, *, available_tools):
        return Message(
            role=Role.SYSTEM,
            content=(
                f"rebuilt system {phase_prompt} {memory_context} "
                f"{','.join(available_tools)}"
            ),
        )


class _RestoreMemoryManager:
    async def generate_context(self, user_id, plan):
        return ("restore memory", [], 0, 0, 0)


class _RestoreToolEngine:
    def get_tools_for_phase(self, phase, plan):
        return [{"name": "save_day_plan"}, {"name": "request_backtrack"}]


class _RestoreAgent:
    def __init__(self):
        self.tool_engine = _RestoreToolEngine()


class _RestoreMessageStore:
    async def load_all(self, session_id):
        return [
            {
                "role": "system",
                "content": "old persisted system",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 0,
                "history_seq": 0,
                "phase": 1,
                "phase3_step": None,
                "run_id": "run_old",
                "trip_id": "trip_1",
            },
            {
                "role": "user",
                "content": "我想去东京",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 1,
                "history_seq": 1,
                "phase": 1,
                "phase3_step": None,
                "run_id": "run_old",
                "trip_id": "trip_1",
            },
            {
                "role": "tool",
                "content": serialize_tool_result(
                    ToolResult(
                        tool_call_id="tc_old",
                        status="success",
                        data={"destination": "东京"},
                    )
                ),
                "tool_calls": None,
                "tool_call_id": "tc_old",
                "provider_state": None,
                "seq": 2,
                "history_seq": 2,
                "phase": 1,
                "phase3_step": None,
                "run_id": "run_old",
                "trip_id": "trip_1",
            },
            {
                "role": "user",
                "content": "继续细化每天路线",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 3,
                "history_seq": 9,
                "phase": 5,
                "phase3_step": None,
                "run_id": "run_new",
                "trip_id": "trip_1",
            },
        ]


@pytest.mark.asyncio
async def test_restore_session_returns_short_runtime_and_internal_history():
    built_agents = []

    def build_agent(plan, user_id, *, compression_events=None):
        agent = _RestoreAgent()
        built_agents.append((agent, plan, user_id, compression_events))
        return agent

    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=_RestoreSessionStore(),
        message_store=_RestoreMessageStore(),
        archive_store=None,
        state_mgr=_RestoreStateManager(),
        phase_router=_RestorePhaseRouter(),
        build_agent=build_agent,
        context_manager=_RestoreContextManager(),
        memory_mgr=_RestoreMemoryManager(),
        memory_enabled=True,
    )

    restored = await persistence.restore_session("sess_restore")

    assert restored is not None
    assert len(restored["history_messages"]) == 4
    assert len(restored["messages"]) == 2
    assert len(restored["messages"]) < len(restored["history_messages"])
    assert restored["next_history_seq"] == 10
    assert restored["messages"][0].role == Role.SYSTEM
    assert restored["messages"][0].content.startswith("rebuilt system restore prompt phase=5")
    assert "save_day_plan" in restored["messages"][0].content
    assert restored["messages"][1].role == Role.USER
    assert restored["messages"][1].content == "继续细化每天路线"
    assert all(message.role != Role.TOOL for message in restored["messages"])
    assert all(message.tool_result is None for message in restored["messages"])
    assert restored["history_messages"][2].message.tool_result.data == {"destination": "东京"}
    assert built_agents[0][2] == "user_restore"
    assert restored["agent"] is built_agents[0][0]


class _LegacyRestoreMessageStore:
    async def load_all(self, session_id):
        return [
            {
                "role": "user",
                "content": "legacy one",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 0,
                "history_seq": None,
                "phase": None,
                "phase3_step": None,
                "run_id": None,
                "trip_id": None,
            },
            {
                "role": "user",
                "content": "legacy two",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 1,
                "history_seq": None,
                "phase": None,
                "phase3_step": None,
                "run_id": None,
                "trip_id": None,
            },
        ]


@pytest.mark.asyncio
async def test_restore_session_legacy_history_seq_falls_back_to_history_length():
    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=_RestoreSessionStore(),
        message_store=_LegacyRestoreMessageStore(),
        archive_store=None,
        state_mgr=_RestoreStateManager(),
        phase_router=_RestorePhaseRouter(),
        build_agent=lambda *args, **kwargs: _RestoreAgent(),
        context_manager=_RestoreContextManager(),
        memory_mgr=_RestoreMemoryManager(),
        memory_enabled=False,
    )

    restored = await persistence.restore_session("sess_legacy")

    assert restored is not None
    assert restored["next_history_seq"] == 2
    assert [message.role for message in restored["messages"]] == [Role.SYSTEM, Role.USER]
    assert restored["messages"][1].content == "legacy two"
```

Also add this import near the top:

```python
from state.models import TravelPlanState
```

- [ ] **Step 2: Run restore tests to verify they fail**

Run:

```bash
pytest backend/tests/test_session_persistence.py::test_restore_session_returns_short_runtime_and_internal_history backend/tests/test_session_persistence.py::test_restore_session_legacy_history_seq_falls_back_to_history_length -q
```

Expected result:

```text
TypeError: SessionPersistence.__init__() got an unexpected keyword argument 'context_manager'
```

- [ ] **Step 3: Modify `SessionPersistence` dependencies and restore flow**

In `backend/api/orchestration/session/persistence.py`, update imports:

```python
from api.orchestration.session.runtime_view import (
    HistoryMessage,
    build_runtime_view_for_restore,
)
```

Update the dataclass fields:

```python
@dataclass
class SessionPersistence:
    ensure_storage_ready: Callable[[], Awaitable[None]]
    db: object
    session_store: object
    message_store: object
    archive_store: object
    state_mgr: object
    phase_router: object
    build_agent: Callable[..., object]
    context_manager: object
    memory_mgr: object
    memory_enabled: bool
```

Add helper functions below `deserialize_tool_result(...)`:

```python
def deserialize_history_message(row: dict[str, object]) -> HistoryMessage:
    role = Role(row["role"])
    tool_calls = None
    if row.get("tool_calls"):
        tool_calls = [
            ToolCall(
                id=payload["id"],
                name=payload["name"],
                arguments=payload["arguments"],
                human_label=payload.get("human_label"),
            )
            for payload in json.loads(row["tool_calls"])
        ]

    tool_result = None
    if row.get("tool_call_id"):
        tool_result = deserialize_tool_result(
            str(row["tool_call_id"]),
            row.get("content"),
        )
    provider_state = None
    if row.get("provider_state"):
        provider_state = json.loads(row["provider_state"])

    raw_history_seq = row.get("history_seq")
    history_seq = int(raw_history_seq) if raw_history_seq is not None else None
    raw_phase = row.get("phase")
    phase = int(raw_phase) if raw_phase is not None else None

    return HistoryMessage(
        message=Message(
            role=role,
            content=row.get("content") if tool_result is None else None,
            tool_calls=tool_calls,
            tool_result=tool_result,
            provider_state=provider_state,
        ),
        phase=phase,
        phase3_step=(
            str(row["phase3_step"])
            if row.get("phase3_step") is not None
            else None
        ),
        history_seq=history_seq,
        run_id=str(row["run_id"]) if row.get("run_id") is not None else None,
        trip_id=str(row["trip_id"]) if row.get("trip_id") is not None else None,
    )


def next_history_seq_from_history(history_view: list[HistoryMessage]) -> int:
    seq_values = [
        item.history_seq
        for item in history_view
        if item.history_seq is not None
    ]
    if seq_values:
        return max(seq_values) + 1
    return len(history_view)
```

Replace the message deserialization block inside `restore_session(...)` with:

```python
        history_view = [
            deserialize_history_message(row)
            for row in await self.message_store.load_all(session_id)
        ]
        next_history_seq = next_history_seq_from_history(history_view)

        self.phase_router.sync_phase_state(plan)
        compression_events: list[dict] = []
        agent = self.build_agent(
            plan,
            meta["user_id"],
            compression_events=compression_events,
        )
        runtime_view = await build_runtime_view_for_restore(
            history_view=history_view,
            plan=plan,
            user_id=meta["user_id"],
            phase_router=self.phase_router,
            context_manager=self.context_manager,
            memory_mgr=self.memory_mgr,
            memory_enabled=self.memory_enabled,
            tool_engine=agent.tool_engine,
        )
        return {
            "plan": plan,
            "messages": runtime_view,
            "history_messages": history_view,
            "next_history_seq": next_history_seq,
            "agent": agent,
            "needs_rebuild": False,
            "user_id": meta["user_id"],
            "compression_events": compression_events,
            "stats": SessionStats(),
            "_pending_system_notes": [],
        }
```

- [ ] **Step 4: Update `SessionPersistence` construction sites**

Find constructor calls:

```bash
rg -n "SessionPersistence\\(" backend
```

For application wiring, pass the existing context manager, memory manager, and memory config:

```python
persistence = SessionPersistence(
    ensure_storage_ready=ensure_storage_ready,
    db=db,
    session_store=session_store,
    message_store=message_store,
    archive_store=archive_store,
    state_mgr=state_mgr,
    phase_router=phase_router,
    build_agent=build_agent,
    context_manager=context_mgr,
    memory_mgr=memory_mgr,
    memory_enabled=config.memory.enabled,
)
```

For existing unit tests that do not call `restore_session(...)`, pass inert values:

```python
context_manager=None,
memory_mgr=None,
memory_enabled=False,
```

- [ ] **Step 5: Run targeted restore tests**

Run:

```bash
pytest backend/tests/test_session_persistence.py::test_restore_session_returns_short_runtime_and_internal_history backend/tests/test_session_persistence.py::test_restore_session_legacy_history_seq_falls_back_to_history_length -q
```

Expected result:

```text
2 passed
```

- [ ] **Step 6: Run session persistence suite**

Run:

```bash
pytest backend/tests/test_session_persistence.py backend/tests/test_session_runtime_view.py -q
```

Expected result:

```text
passed
```

- [ ] **Step 7: Commit restore wiring**

```bash
git add backend/api/orchestration/session/persistence.py backend/tests/test_session_persistence.py PROJECT_OVERVIEW.md
git commit -m "feat: restore sessions with separate history and runtime views"
```

Expected result: commit succeeds.

## Task 4: Preserve Frontend-Safe `/api/messages` Behavior

**Files:**
- Modify: `backend/tests/test_api.py`
- Modify: `backend/api/routes/session_routes.py`

- [ ] **Step 1: Add failing API regression test**

Add this test to `backend/tests/test_api.py`:

```python
@pytest.mark.asyncio
async def test_get_messages_does_not_expose_internal_restore_history(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        assert session_resp.status_code == 200
        session_id = session_resp.json()["session_id"]

        response = await client.get(f"/api/messages/{session_id}")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert all("history_messages" not in item for item in payload)
    assert all("history_seq" not in item for item in payload)
    assert all("phase" not in item for item in payload)
    assert all("phase3_step" not in item for item in payload)
    assert all("run_id" not in item for item in payload)
    assert all("trip_id" not in item for item in payload)
```

Add this second regression test to prove persisted internal history rows still serialize publicly without leaking internal columns:

```python
@pytest.mark.asyncio
async def test_get_messages_keeps_public_shape_for_append_only_rows(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        assert session_resp.status_code == 200
        session_id = session_resp.json()["session_id"]

    sessions = _get_sessions(app)
    session = sessions[session_id]
    message_store = None
    session_store = None
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not hasattr(endpoint, "__closure__"):
            continue
        free_vars = getattr(endpoint.__code__, "co_freevars", ())
        for name, cell in zip(free_vars, endpoint.__closure__ or ()):
            if name == "message_store":
                message_store = cell.cell_contents
            if name == "session_store":
                session_store = cell.cell_contents
    assert message_store is not None
    assert session_store is not None

    await message_store.append_batch(
        session_id,
        [
            {
                "role": "system",
                "content": "internal system",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 0,
                "history_seq": 0,
                "phase": session["plan"].phase,
                "phase3_step": session["plan"].phase3_step,
                "run_id": "run_public_shape",
                "trip_id": session["plan"].trip_id,
            },
            {
                "role": "user",
                "content": "你好",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 1,
                "history_seq": 1,
                "phase": session["plan"].phase,
                "phase3_step": session["plan"].phase3_step,
                "run_id": "run_public_shape",
                "trip_id": session["plan"].trip_id,
            },
        ],
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/messages/{session_id}")

    assert response.status_code == 200
    assert response.json() == [
        {
            "role": "system",
            "content": "internal system",
            "tool_calls": None,
            "tool_call_id": None,
            "seq": 0,
        },
        {
            "role": "user",
            "content": "你好",
            "tool_calls": None,
            "tool_call_id": None,
            "seq": 1,
        },
    ]
```

- [ ] **Step 2: Run API regression test**

Run:

```bash
pytest backend/tests/test_api.py::test_get_messages_does_not_expose_internal_restore_history backend/tests/test_api.py::test_get_messages_keeps_public_shape_for_append_only_rows -q
```

Expected result after Phase 1 and before the route guard is tightened:

```text
FAIL with an assertion diff showing leaked public response keys such as phase, phase3_step, history_seq, run_id, or trip_id.
```

- [ ] **Step 3: Keep route response public and compact**

Ensure `backend/api/routes/session_routes.py` keeps the public response shape restricted to the existing fields:

```python
    @app.get("/api/messages/{session_id}")
    async def get_messages(session_id: str):
        await ensure_storage_ready()
        meta = await session_store.load(session_id)
        if meta is None or meta["status"] == "deleted":
            raise HTTPException(status_code=404, detail="Session not found")
        rows = await message_store.load_all(session_id)
        return [
            {
                "role": row["role"],
                "content": row["content"],
                "tool_calls": (
                    json.loads(row["tool_calls"]) if row.get("tool_calls") else None
                ),
                "tool_call_id": row.get("tool_call_id"),
                "seq": row["seq"],
            }
            for row in rows
        ]
```

Do not return `history_messages`, `history_seq`, `phase`, `phase3_step`, `run_id`, or `trip_id` from this route.

- [ ] **Step 4: Run API tests**

Run:

```bash
pytest backend/tests/test_api.py backend/tests/test_session_restore.py -q
```

Expected result:

```text
passed
```

- [ ] **Step 5: Commit API safety test**

```bash
git add backend/tests/test_api.py backend/api/routes/session_routes.py PROJECT_OVERVIEW.md
git commit -m "test: keep message history API frontend safe"
```

Expected result: commit succeeds.

## Task 5: Add Backtrack And Substep Restore Integration Coverage

**Files:**
- Modify: `backend/tests/test_session_persistence.py`
- Test: `backend/api/orchestration/session/runtime_view.py`

- [ ] **Step 1: Add failing persistence tests for Phase 3 substeps and backtrack restore**

Append these message stores and tests to `backend/tests/test_session_persistence.py`:

```python
class _NoopSyncPhaseRouter(_RestorePhaseRouter):
    def sync_phase_state(self, plan):
        return None


class _Phase3SkeletonStateManager:
    async def load(self, session_id):
        return TravelPlanState(
            session_id=session_id,
            phase=3,
            phase3_step="skeleton",
            destination="大阪",
        )


class _Phase3SkeletonMessageStore:
    async def load_all(self, session_id):
        return [
            {
                "role": "user",
                "content": "画像输入",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 0,
                "history_seq": 0,
                "phase": 3,
                "phase3_step": "brief",
                "run_id": "run_brief",
                "trip_id": "trip_1",
            },
            {
                "role": "tool",
                "content": serialize_tool_result(
                    ToolResult(
                        tool_call_id="tc_brief",
                        status="success",
                        data={"trip_brief": "old brief"},
                    )
                ),
                "tool_calls": None,
                "tool_call_id": "tc_brief",
                "provider_state": None,
                "seq": 1,
                "history_seq": 1,
                "phase": 3,
                "phase3_step": "brief",
                "run_id": "run_brief",
                "trip_id": "trip_1",
            },
            {
                "role": "user",
                "content": "生成骨架",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 2,
                "history_seq": 2,
                "phase": 3,
                "phase3_step": "skeleton",
                "run_id": "run_skeleton",
                "trip_id": "trip_1",
            },
        ]


@pytest.mark.asyncio
async def test_restore_session_phase3_substep_keeps_previous_substeps_out_of_runtime():
    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=_RestoreSessionStore(),
        message_store=_Phase3SkeletonMessageStore(),
        archive_store=None,
        state_mgr=_Phase3SkeletonStateManager(),
        phase_router=_NoopSyncPhaseRouter(),
        build_agent=lambda *args, **kwargs: _RestoreAgent(),
        context_manager=_RestoreContextManager(),
        memory_mgr=_RestoreMemoryManager(),
        memory_enabled=True,
    )

    restored = await persistence.restore_session("sess_phase3")

    assert restored is not None
    assert len(restored["history_messages"]) == 3
    assert len(restored["messages"]) == 2
    rendered = "\n".join(str(message.content) for message in restored["messages"])
    assert "生成骨架" in rendered
    assert "画像输入" not in rendered
    assert "old brief" not in rendered
    assert all(message.role != Role.TOOL for message in restored["messages"])


class _BacktrackToPhase3StateManager:
    async def load(self, session_id):
        return TravelPlanState(
            session_id=session_id,
            phase=3,
            phase3_step="brief",
            destination="京都",
        )


class _BacktrackToPhase3MessageStore:
    async def load_all(self, session_id):
        return [
            {
                "role": "user",
                "content": "老 Phase 3 输入",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 0,
                "history_seq": 0,
                "phase": 3,
                "phase3_step": "brief",
                "run_id": "run_old_phase3",
                "trip_id": "trip_1",
            },
            {
                "role": "tool",
                "content": serialize_tool_result(
                    ToolResult(
                        tool_call_id="tc_old_phase3",
                        status="success",
                        data={"trip_brief": "old target phase segment"},
                    )
                ),
                "tool_calls": None,
                "tool_call_id": "tc_old_phase3",
                "provider_state": None,
                "seq": 1,
                "history_seq": 1,
                "phase": 3,
                "phase3_step": "brief",
                "run_id": "run_old_phase3",
                "trip_id": "trip_1",
            },
            {
                "role": "user",
                "content": "预算太高，回到框架规划",
                "tool_calls": None,
                "tool_call_id": None,
                "provider_state": None,
                "seq": 2,
                "history_seq": 8,
                "phase": 5,
                "phase3_step": None,
                "run_id": "run_backtrack",
                "trip_id": "trip_1",
            },
            {
                "role": "tool",
                "content": serialize_tool_result(
                    ToolResult(
                        tool_call_id="tc_backtrack",
                        status="success",
                        data={
                            "backtracked": True,
                            "to_phase": 3,
                            "reason": "预算太高",
                        },
                    )
                ),
                "tool_calls": None,
                "tool_call_id": "tc_backtrack",
                "provider_state": None,
                "seq": 3,
                "history_seq": 9,
                "phase": 5,
                "phase3_step": None,
                "run_id": "run_backtrack",
                "trip_id": "trip_1",
            },
        ]


@pytest.mark.asyncio
async def test_restore_session_after_backtrack_does_not_replay_old_target_phase():
    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=_RestoreSessionStore(),
        message_store=_BacktrackToPhase3MessageStore(),
        archive_store=None,
        state_mgr=_BacktrackToPhase3StateManager(),
        phase_router=_NoopSyncPhaseRouter(),
        build_agent=lambda *args, **kwargs: _RestoreAgent(),
        context_manager=_RestoreContextManager(),
        memory_mgr=_RestoreMemoryManager(),
        memory_enabled=True,
    )

    restored = await persistence.restore_session("sess_backtrack")

    assert restored is not None
    assert len(restored["messages"]) == 2
    rendered = "\n".join(str(message.content) for message in restored["messages"])
    assert "预算太高，回到框架规划" in rendered
    assert "老 Phase 3 输入" not in rendered
    assert "old target phase segment" not in rendered
    assert all(message.role != Role.TOOL for message in restored["messages"])
```

- [ ] **Step 2: Run targeted tests**

Run:

```bash
pytest backend/tests/test_session_persistence.py::test_restore_session_phase3_substep_keeps_previous_substeps_out_of_runtime backend/tests/test_session_persistence.py::test_restore_session_after_backtrack_does_not_replay_old_target_phase -q
```

Expected result:

```text
2 passed
```

- [ ] **Step 3: Run full restore-related tests**

Run:

```bash
pytest backend/tests/test_session_runtime_view.py backend/tests/test_session_persistence.py backend/tests/test_session_restore.py -q
```

Expected result:

```text
passed
```

- [ ] **Step 4: Commit integration coverage**

```bash
git add backend/tests/test_session_persistence.py PROJECT_OVERVIEW.md
git commit -m "test: isolate restore anchors after substep and backtrack rebuilds"
```

Expected result: commit succeeds.

## Task 6: Update Project Overview And Run Verification

**Files:**
- Modify: `PROJECT_OVERVIEW.md`
- Test: restore/session/API suites

- [ ] **Step 1: Update `PROJECT_OVERVIEW.md`**

Add a concise note near the context persistence documentation section:

```markdown
- **Context restore Phase 2**：恢复 session 时后端加载完整 append-only `history_view` 供内部诊断和后续写入游标使用，但传给 `AgentLoop` 的 `session["messages"]` 是由当前 plan、phase prompt、memory context、可用工具和最新安全 user anchor 重建的短 `runtime_view`；恢复不会 replay 旧阶段工具结果、Phase 3 旧子步骤流水账或 backtrack 前的旧目标阶段 segment。
```

- [ ] **Step 2: Run focused verification**

Run:

```bash
pytest backend/tests/test_session_runtime_view.py backend/tests/test_session_persistence.py backend/tests/test_session_restore.py backend/tests/test_api.py -q
```

Expected result:

```text
passed
```

- [ ] **Step 3: Run broader backend regression around restore and phase transitions**

Run:

```bash
pytest backend/tests/test_phase_transition_event.py backend/tests/test_agent_phase_transition.py backend/tests/test_storage_message.py backend/tests/test_storage_session.py -q
```

Expected result:

```text
passed
```

- [ ] **Step 4: Inspect git diff**

Run:

```bash
git diff -- backend/api/orchestration/session/runtime_view.py backend/api/orchestration/session/persistence.py backend/api/routes/session_routes.py backend/tests/test_session_runtime_view.py backend/tests/test_session_persistence.py backend/tests/test_api.py PROJECT_OVERVIEW.md
```

Expected result:

```text
Diff only contains Phase 2 runtime restore code, tests, and the required overview note. It does not include Phase 3 context_epoch debug segmentation, public history/debug API work, or unrelated refactors.
```

- [ ] **Step 5: Commit final verification/doc update**

```bash
git add PROJECT_OVERVIEW.md backend/api/orchestration/session/runtime_view.py backend/api/orchestration/session/persistence.py backend/api/routes/session_routes.py backend/tests/test_session_runtime_view.py backend/tests/test_session_persistence.py backend/tests/test_api.py
git commit -m "docs: document context runtime restore"
```

Expected result: commit succeeds, or reports nothing to commit if the overview was already included in earlier commits.

## Self-Review Checklist

- Phase 2 only: no `context_epoch`, no public debug API, no frontend timeline, no full history replay.
- Restore loads `history_view` and returns internal `history_messages`.
- Restore gives `AgentLoop` short `session["messages"]`.
- Runtime view builder uses real dependencies: `phase_router`, `context_manager`, `memory_mgr`, `memory_enabled`, `tool_engine`, and `user_id`.
- Tests prove restored `session["messages"]` is shorter than history and excludes old tool results.
- Tests prove Phase 3 substep restore does not replay previous substeps.
- Tests prove backtrack restore does not replay the old target phase segment.
- `/api/messages/{session_id}` remains frontend-safe and does not return internal `history_messages`.
- `PROJECT_OVERVIEW.md` is updated in the implementation branch before completion.
