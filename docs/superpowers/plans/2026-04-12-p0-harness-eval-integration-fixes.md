# P0 Harness & Eval Integration Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Repair the P0 harness/eval integration gaps so feasibility gating, session stats, and golden eval assertions work on the real application path.

**Architecture:** Keep the existing P0 modules and wire them through the AgentLoop and FastAPI streaming boundary. Add focused regression tests that reproduce each reviewed failure before touching production code.

**Tech Stack:** Python 3.12+, pytest, pytest-asyncio, FastAPI app internals, YAML golden cases.

---

## Spec

- Phase 1→3 feasibility gating must not crash when `TravelPlanState.dates` is a `DateRange`.
- `AgentLoop.run()` must forward `ChunkType.USAGE` chunks so `main.py` can record token/cost stats.
- Tool call duration metadata must be recorded into `SessionStats.record_tool_call()` when tool results stream back to the API layer.
- Golden case tool assertions must use real registered tool names.

## Root Cause Summary

- `main.py` used `DateRange.start_date/end_date`, but `DateRange` exposes `start/end`.
- `AgentLoop.run()` only forwarded text, tool call, tool result, compression, keepalive, and done chunks; usage chunks from providers were dropped.
- `ToolEngine` attached `duration_ms` to `ToolResult.metadata`, but `main.py` did not record that metadata into `SessionStats`.
- Two golden YAML files asserted `search_hotels`, while the project tool is named `search_accommodations`.

## Files

- Modify: `backend/main.py`
- Modify: `backend/agent/loop.py`
- Modify: `backend/evals/golden_cases/med-001-paris-honeymoon.yaml`
- Modify: `backend/evals/golden_cases/med-004-dubai-luxury.yaml`
- Modify: `backend/tests/test_agent_loop.py`
- Modify: `backend/tests/test_eval_pipeline.py`
- Modify: `backend/tests/test_quality_gate.py`
- Modify: `backend/tests/test_xiaohongshu_search.py`

---

### Task 1: Feasibility Gate DateRange Regression

**Files:**
- Modify: `backend/tests/test_quality_gate.py`
- Modify: `backend/main.py`

- [x] **Step 1: Write failing test**

Add `test_phase1_to_phase3_feasibility_gate_handles_daterange()` to prove the date helper accepts the real `DateRange(start, end)` model.

- [x] **Step 2: Run test and verify RED**

Run:

```bash
cd backend && python -m pytest tests/test_quality_gate.py::test_phase1_to_phase3_feasibility_gate_handles_daterange -q
```

Observed RED: failed because `_days_count_from_dates` did not exist.

- [x] **Step 3: Implement minimal fix**

In `backend/main.py`, add `_days_count_from_dates()` and use it in the Phase 1→3 feasibility gate.

- [x] **Step 4: Run test and verify GREEN**

Run:

```bash
cd backend && python -m pytest tests/test_quality_gate.py::test_phase1_to_phase3_feasibility_gate_handles_daterange -q
```

Observed GREEN: PASS.

---

### Task 2: Usage Chunk Forwarding

**Files:**
- Modify: `backend/tests/test_agent_loop.py`
- Modify: `backend/agent/loop.py`

- [x] **Step 1: Write failing test**

Add `test_agent_loop_forwards_usage_chunks()` with a fake LLM that yields `TEXT_DELTA`, `USAGE`, and `DONE`.

- [x] **Step 2: Run test and verify RED**

Run:

```bash
cd backend && python -m pytest tests/test_agent_loop.py::test_agent_loop_forwards_usage_chunks -q
```

Observed RED: failed because the output chunk types omitted `usage`.

- [x] **Step 3: Implement minimal fix**

In `backend/agent/loop.py`, yield `ChunkType.USAGE` chunks unchanged inside the LLM streaming loop.

- [x] **Step 4: Run test and verify GREEN**

Run:

```bash
cd backend && python -m pytest tests/test_agent_loop.py::test_agent_loop_forwards_usage_chunks -q
```

Observed GREEN: PASS.

---

### Task 3: Tool Duration SessionStats Wiring

**Files:**
- Modify: `backend/tests/test_quality_gate.py`
- Modify: `backend/main.py`

- [x] **Step 1: Write failing test**

Add `test_record_tool_result_stats_records_duration()` around a helper that records `ToolResult.metadata["duration_ms"]` into `SessionStats`.

- [x] **Step 2: Run test and verify RED**

Run:

```bash
cd backend && python -m pytest tests/test_quality_gate.py::test_record_tool_result_stats_records_duration -q
```

Observed RED: failed because `_record_tool_result_stats` did not exist.

- [x] **Step 3: Implement minimal fix**

Extract `_record_tool_result_stats()` in `backend/main.py` and call it when streaming each `TOOL_RESULT`.

- [x] **Step 4: Run test and verify GREEN**

Run:

```bash
cd backend && python -m pytest tests/test_quality_gate.py::test_record_tool_result_stats_records_duration -q
```

Observed GREEN: PASS.

---

### Task 4: Golden Case Tool Names

**Files:**
- Modify: `backend/tests/test_eval_pipeline.py`
- Modify: `backend/evals/golden_cases/med-001-paris-honeymoon.yaml`
- Modify: `backend/evals/golden_cases/med-004-dubai-luxury.yaml`

- [x] **Step 1: Write failing test**

Add `test_golden_cases_use_registered_tool_names()` to load golden cases and assert every tool assertion references a known registered tool.

- [x] **Step 2: Run test and verify RED**

Run:

```bash
cd backend && python -m pytest tests/test_eval_pipeline.py::TestGoldenCaseLoader::test_golden_cases_use_registered_tool_names -q
```

Observed RED: failed with `search_hotels` in `med-001` and `med-004`.

- [x] **Step 3: Implement minimal fix**

Replace `search_hotels` with `search_accommodations` in the two YAML files.

- [x] **Step 4: Run test and verify GREEN**

Run:

```bash
cd backend && python -m pytest tests/test_eval_pipeline.py::TestGoldenCaseLoader::test_golden_cases_use_registered_tool_names -q
```

Observed GREEN: PASS.

---

## Final Verification

Run:

```bash
cd backend && python -m pytest tests/test_agent_loop.py tests/test_quality_gate.py tests/test_eval_pipeline.py tests/test_stats.py tests/test_feasibility.py -q
```

Observed: `55 passed`.

Run:

```bash
cd backend && python -m pytest tests/test_guardrail.py tests/test_harness_validator.py tests/test_harness_judge.py tests/test_stats.py tests/test_feasibility.py tests/test_eval_pipeline.py -q
```

Observed: `70 passed`.

Run:

```bash
cd backend && python -m pytest -q
```

Observed: `594 passed` printed. The pytest process did not exit cleanly after summary because telemetry/gRPC background threads remained alive even with Jaeger available, so the process was killed after test completion.
