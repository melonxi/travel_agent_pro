# Harness Validation Forward Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move harness validation from phase-transition-only feedback to immediate post-state-write feedback.

**Architecture:** Keep `validate_hard_constraints()` as the full `before_phase_transition` safety net. Add focused incremental checks in `backend/harness/validator.py`, strengthen search-result field severity in `backend/harness/guardrail.py`, and update the `update_plan_state` after-tool hook in `backend/main.py` to inject non-blocking real-time system feedback.

**Tech Stack:** Python 3.12 dataclasses, pytest, existing FastAPI app factory and agent hook system.

---

### Task 1: Incremental Validator

**Files:**
- Create: `backend/tests/test_incremental_validator.py`
- Modify: `backend/harness/validator.py`

- [x] **Step 1: Write failing tests**

Cover budget write pass, budget write with existing activity overrun, dates feasibility for Tokyo one-day trip, daily plan time conflict, and unmonitored field no-op.

- [x] **Step 2: Verify RED**

Run: `cd backend && pytest tests/test_incremental_validator.py -v`

Expected before implementation: import error for missing `validate_incremental`.

- [x] **Step 3: Implement minimal code**

Add `_coerce_budget`, `_coerce_dates`, `_activity_total_cost`, `_validate_time_conflicts`, and `validate_incremental(plan, field, value)`. Refactor `validate_hard_constraints()` to reuse the time-conflict and activity-cost helpers without changing its public behavior.

- [x] **Step 4: Verify GREEN**

Run: `cd backend && pytest tests/test_incremental_validator.py -v`

Expected after implementation: all tests pass.

### Task 2: Lock Budget Gate

**Files:**
- Create: `backend/tests/test_lock_budget_gate.py`
- Modify: `backend/harness/validator.py`

- [x] **Step 1: Write failing tests**

Cover 60% allowed, 85% warning, 110% error, no budget skip, no locked item skip, and transport-only 80% warning.

- [x] **Step 2: Verify RED**

Run: `cd backend && pytest tests/test_lock_budget_gate.py -v`

Expected before implementation: import error for missing `validate_lock_budget`.

- [x] **Step 3: Implement minimal code**

Add `_LOCK_BUDGET_RATIO = 0.8`, numeric price parsing, selected transport cost extraction, trip-night calculation, selected accommodation nightly-price lookup from `accommodation_options`, and `validate_lock_budget(plan)`.

- [x] **Step 4: Verify GREEN**

Run: `cd backend && pytest tests/test_lock_budget_gate.py -v`

Expected after implementation: all tests pass.

### Task 3: Guardrail Result Severity

**Files:**
- Modify: `backend/tests/test_guardrail.py`
- Modify: `backend/harness/guardrail.py`

- [x] **Step 1: Write failing tests**

Cover missing flight `price` as `error`, missing flight `airline` as `warn`, and missing accommodation `location` as `warn`. Update existing "complete result" tests to include the expanded required fields.

- [x] **Step 2: Verify RED**

Run: `cd backend && pytest tests/test_guardrail.py -v`

Expected before implementation: missing `price` still warns and expanded non-critical fields are not checked.

- [x] **Step 3: Implement minimal code**

Expand `_REQUIRED_RESULT_FIELDS`, add `_CRITICAL_FIELDS = {"price"}`, and set missing-field result severity to `error` only when a missing field is critical.

- [x] **Step 4: Verify GREEN**

Run: `cd backend && pytest tests/test_guardrail.py -v`

Expected after implementation: all tests pass.

### Task 4: Hook Wiring

**Files:**
- Create: `backend/tests/test_realtime_validation_hook.py`
- Modify: `backend/main.py`

- [x] **Step 1: Write failing test**

Simulate `update_plan_state(field="daily_plans")` writing a time conflict through the app/agent path. Assert the session receives a `[实时约束检查]` system message containing `时间冲突`.

- [x] **Step 2: Verify RED**

Run: `cd backend && pytest tests/test_realtime_validation_hook.py -v`

Expected before implementation: only the old hard-constraint message is injected, so the realtime assertion fails.

- [x] **Step 3: Implement hook change**

Import `validate_incremental` and `validate_lock_budget`, call incremental validation using `tool_call.arguments`, call lock-budget validation for `selected_transport` and `accommodation`, and inject `[实时约束检查]` feedback without blocking tool execution.

- [x] **Step 4: Verify GREEN**

Run: `cd backend && pytest tests/test_realtime_validation_hook.py -v`

Expected after implementation: test passes. Confirm `before_phase_transition` still calls `validate_hard_constraints(target_plan)`.

### Task 5: Verification And Docs

**Files:**
- Modify: `PROJECT_OVERVIEW.md`

- [x] **Step 1: Run targeted tests**

Run: `cd backend && pytest tests/test_incremental_validator.py tests/test_lock_budget_gate.py tests/test_guardrail.py tests/test_realtime_validation_hook.py -v`

Expected: all targeted tests pass.

- [x] **Step 2: Run full backend regression**

Run: `cd backend && pytest`

Expected: all backend tests pass.

- [x] **Step 3: Update overview**

Document incremental validation, lock budget feedback, and guardrail critical-field severity in `PROJECT_OVERVIEW.md`.

- [x] **Step 4: Final sanity checks**

Run: `git diff --check`

Expected: no whitespace errors.
