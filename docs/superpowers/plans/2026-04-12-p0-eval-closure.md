# P0 Eval Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the P0 eval upgrade executable and reportable, and fix the remaining stats/documentation credibility gaps.

**Architecture:** Extend the existing eval runner instead of replacing it. Add a small executor protocol, report serialization helpers, suite metrics, and a main-level LLM stats helper for measurable latency.

**Tech Stack:** Python dataclasses, pytest, PyYAML, FastAPI session code, Markdown docs.

---

### Task 1: Executable Eval Runner

**Files:**
- Modify: `backend/evals/models.py`
- Modify: `backend/evals/runner.py`
- Test: `backend/tests/test_eval_pipeline.py`

- [ ] Add tests for `EvalExecution`, `run_case()`, `run_suite()`, and JSON report output.
- [ ] Run targeted eval tests and confirm they fail because the new APIs do not exist.
- [ ] Implement the minimal models and runner functions.
- [ ] Run targeted eval tests and confirm they pass.

### Task 2: LLM Latency Stats

**Files:**
- Modify: `backend/main.py`
- Test: `backend/tests/test_quality_gate.py`

- [ ] Add a failing test for a helper that records usage with positive wall-clock duration.
- [ ] Implement the helper and wire the SSE usage branch to it.
- [ ] Run the targeted quality gate/stat tests and confirm they pass.

### Task 3: Documentation Consistency

**Files:**
- Modify: `README.md`
- Modify: `PROJECT_OVERVIEW.md`

- [ ] Replace conflicting 7-phase wording with the actual 1/3/5/7 production path wording.
- [ ] Describe eval as executable golden-case runner plus report output, without claiming pass@k or 30+ cases.
- [ ] Verify references to test/eval counts remain accurate.

### Task 4: Verification

**Files:**
- No production edits.

- [ ] Run targeted tests for eval, stats, quality gate, and harness modules.
- [ ] Run a broader backend test subset that covers API/session/eval behavior.
- [ ] Inspect `git diff --stat` and summarize changed files.

