# Backtrack into update_plan_state Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the LLM trigger phase backtrack via `update_plan_state(field="backtrack")` instead of relying on brittle keyword matching, while preserving keywords as a fallback.

**Architecture:** A new `BacktrackService` class centralizes the backtrack transaction (validate, record, clear downstream, switch phase). The `update_plan_state` tool gains a `backtrack` branch that calls this service. The `on_tool_call` hook detects the `backtracked` flag and skips auto-transition. The keyword-based `_detect_backtrack()` moves to a post-agent-run fallback in the chat handler.

**Tech Stack:** Python 3.11+, FastAPI, pytest, pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-04-04-backtrack-into-update-plan-state-design.md`

---

## File Structure

```
backend/phase/
    backtrack.py                    # NEW  — BacktrackService class (~28 lines)
    router.py                       # MODIFY — __init__ creates BacktrackService, prepare_backtrack delegates
backend/state/
    models.py                       # MODIFY — expand _PHASE_DOWNSTREAM from 2 entries to 5
backend/tools/
    update_plan_state.py            # MODIFY — add "backtrack" to _ALLOWED_FIELDS, new branch (lines 66-92)
backend/main.py                     # MODIFY — hook skips auto-transition on backtrack, session adds needs_rebuild,
                                    #          chat handler restructured with post-run fallback
backend/tests/
    test_backtrack_service.py       # NEW  — 6 test cases for BacktrackService
```

---

## Task 1: BacktrackService & Model Updates

**Files:**
- Create: `backend/phase/backtrack.py`
- Modify: `backend/state/models.py:208-221`
- Test: `backend/tests/test_backtrack_service.py`

- [x] **Step 1: Write failing tests for BacktrackService**

Create `backend/tests/test_backtrack_service.py` with 6 test cases:

```python
class TestBacktrackService:
    def test_normal_backtrack_phase_5_to_3(self) -> None:
        """phase 5->3: phase changed, history recorded, downstream cleared."""

    def test_illegal_backtrack_same_phase(self) -> None:
        """to_phase == plan.phase raises ValueError."""

    def test_illegal_backtrack_forward(self) -> None:
        """to_phase > plan.phase raises ValueError."""

    def test_backtrack_to_phase_2_clears_destination(self) -> None:
        """Backtrack to phase 2 clears destination but keeps destination_candidates."""

    def test_backtrack_to_phase_1_clears_all(self) -> None:
        """Backtrack to phase 1 clears all downstream fields."""

    def test_backtrack_to_phase_4_clears_accommodation_and_daily_plans(self) -> None:
        """Backtrack to phase 4 clears accommodation and daily_plans."""
```

Run: `pytest tests/test_backtrack_service.py -v`
Expected: FAIL (module not found)

- [x] **Step 2: Expand `_PHASE_DOWNSTREAM` in `models.py`**

Replace the existing 2-entry mapping with full coverage for phases 1-5:

```python
_PHASE_DOWNSTREAM: dict[int, list[str]] = {
    1: ["destination", "destination_candidates", "dates", "accommodation", "daily_plans"],
    2: ["destination", "dates", "accommodation", "daily_plans"],
    3: ["dates", "accommodation", "daily_plans"],
    4: ["accommodation", "daily_plans"],
    5: ["daily_plans"],
}
```

- [x] **Step 3: Create `BacktrackService`**

Create `backend/phase/backtrack.py`:

```python
class BacktrackService:
    def execute(self, plan: TravelPlanState, to_phase: int, reason: str, snapshot_path: str) -> None:
        if to_phase >= plan.phase:
            raise ValueError("只能回退到更早的阶段")
        plan.backtrack_history.append(BacktrackEvent(
            from_phase=plan.phase, to_phase=to_phase, reason=reason, snapshot_path=snapshot_path,
        ))
        plan.clear_downstream(from_phase=to_phase)
        plan.phase = to_phase
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_backtrack_service.py -v`
Expected: 6 PASSED

- [x] **Step 5: Refactor `PhaseRouter` to delegate to `BacktrackService`**

Modify `backend/phase/router.py`:
- Add `__init__` that creates `self._backtrack_service = BacktrackService()`
- `prepare_backtrack()` delegates to `self._backtrack_service.execute()`

Run: `pytest tests/test_phase_router.py tests/test_backtrack_service.py -v`
Expected: ALL PASSED

- [x] **Step 6: Commit Task 1**

```bash
git add backend/phase/backtrack.py backend/state/models.py backend/phase/router.py backend/tests/test_backtrack_service.py
git commit -m "feat: add BacktrackService, expand _PHASE_DOWNSTREAM, refactor PhaseRouter"
```

---

## Task 2: Extend `update_plan_state` Tool with Backtrack Field

**Files:**
- Modify: `backend/tools/update_plan_state.py`

- [x] **Step 1: Add "backtrack" to `_ALLOWED_FIELDS`**

Add `"backtrack"` to the set.

- [x] **Step 2: Update tool description and `_PARAMETERS`**

Update description to mention backtrack usage. Update `_PARAMETERS` to document the `value` format when `field="backtrack"`.

- [x] **Step 3: Add backtrack branch in the tool function**

Insert before the normal field-update logic (line 66):

```python
if field == "backtrack":
    # Validate value structure
    if not isinstance(value, dict) or "to_phase" not in value:
        raise ToolError("backtrack 的 value 必须包含 to_phase 字段", ...)
    to_phase = int(value["to_phase"])
    reason = str(value.get("reason", "用户请求回退"))
    if to_phase >= plan.phase:
        raise ToolError(f"只能回退到更早的阶段，当前阶段: {plan.phase}，目标: {to_phase}", ...)
    from_phase = plan.phase
    from phase.backtrack import BacktrackService  # lazy import to avoid circular
    service = BacktrackService()
    service.execute(plan, to_phase, reason, snapshot_path="")
    return {
        "backtracked": True,
        "from_phase": from_phase, "to_phase": to_phase, "reason": reason,
        "next_action": "请向用户确认回退结果，不要继续调用其他工具",
    }
```

- [x] **Step 4: Verify existing tests still pass**

Run: `pytest tests/ -v`
Expected: ALL PASSED (no existing tests break)

- [x] **Step 5: Commit Task 2**

```bash
git add backend/tools/update_plan_state.py
git commit -m "feat: extend update_plan_state with backtrack field support"
```

---

## Task 3: Hook & Chat Handler Changes in `main.py`

**Files:**
- Modify: `backend/main.py`

- [x] **Step 1: Update `on_tool_call` hook to skip auto-transition on backtrack**

When `update_plan_state` returns `{"backtracked": True}`, set `session["needs_rebuild"] = True` and `return` early (skip `check_and_apply_transition`):

```python
async def on_tool_call(**kwargs):
    if kwargs.get("tool_name") == "update_plan_state":
        result = kwargs.get("result")
        if result and result.data and result.data.get("backtracked"):
            session = sessions.get(plan.session_id)
            if session:
                session["needs_rebuild"] = True
            return
        phase_router.check_and_apply_transition(plan)
```

- [x] **Step 2: Add `needs_rebuild` flag to session initialization**

In `create_session()`:
```python
sessions[plan.session_id] = {
    "plan": plan, "messages": [], "agent": agent, "needs_rebuild": False,
}
```

- [x] **Step 3: Add `needs_rebuild=False` to backtrack endpoint**

In `POST /api/backtrack`, after rebuilding agent:
```python
session["needs_rebuild"] = False
```

- [x] **Step 4: Restructure chat handler with post-run fallback**

1. At start of chat handler, check `needs_rebuild` and rebuild agent if True.
2. Remove pre-agent-run `_detect_backtrack()` call.
3. Record `phase_before_run = plan.phase` before `agent.run()`.
4. After `agent.run()` completes in `event_stream()`, add fallback:

```python
if plan.phase == phase_before_run:
    backtrack_target = _detect_backtrack(req.message, plan)
    if backtrack_target is not None:
        snapshot_path = await state_mgr.save_snapshot(plan)
        phase_router.prepare_backtrack(plan, backtrack_target, f"fallback回退：{req.message[:50]}", snapshot_path)
        session["needs_rebuild"] = True
```

- [x] **Step 5: Verify all tests pass**

Run: `pytest tests/ -v`
Expected: ALL PASSED

- [x] **Step 6: Commit Task 3**

```bash
git add backend/main.py
git commit -m "feat: hook skips auto-transition on backtrack, chat handler uses keyword fallback"
```

---

## Task 4: Integration Verification

- [x] **Step 1: Run full test suite**

Run: `pytest tests/ -q`
Expected: 199 passed

- [x] **Step 2: Verify spec test requirements coverage**

| Spec requirement | Covered by |
|---|---|
| `update_plan_state(field="backtrack")` normal backtrack | `test_backtrack_service.py::test_normal_backtrack_phase_5_to_3` |
| Illegal `to_phase` rejected | `test_backtrack_service.py::test_illegal_backtrack_same_phase`, `test_illegal_backtrack_forward` |
| Hook skips auto-transition on backtrack | `on_tool_call` hook checks `backtracked` flag |
| API and tool backtrack use same service | Both call `BacktrackService.execute()` |
| `_PHASE_DOWNSTREAM` clears correct fields | `test_backtrack_service.py::test_backtrack_to_phase_2_clears_destination`, `test_backtrack_to_phase_1_clears_all`, `test_backtrack_to_phase_4_clears_accommodation_and_daily_plans` |
| Fallback keyword detection still works | `chat()` post-run fallback with `_detect_backtrack()` |
