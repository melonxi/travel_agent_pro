# API Orchestration Structure Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the remaining oversized API orchestration modules into focused files while preserving behavior and existing test monkeypatch contracts.

**Architecture:** Keep `backend/main.py` as the FastAPI composition root. Keep HTTP handlers in `backend/api/routes/`. Split `backend/api/orchestration/` into `agent/`, `chat/`, `memory/`, `session/`, and `common/` subpackages by request-orchestration responsibility.

**Tech Stack:** FastAPI, pytest, existing Python orchestration modules under `backend/api/orchestration`.

---

### Task 1: Split Memory Orchestration

**Files:**
- Modify: `backend/api/orchestration/memory/orchestration.py`
- Create: `backend/api/orchestration/memory/contracts.py`
- Create: `backend/api/orchestration/memory/recall_planning.py`
- Create: `backend/api/orchestration/memory/extraction.py`
- Create: `backend/api/orchestration/memory/tasks.py`
- Create: `backend/api/orchestration/memory/episodes.py`
- Modify: `backend/tests/test_main_structure.py`

- [ ] Add structure tests asserting the memory orchestration subpackage exists and `memory/orchestration.py` stays below a smaller line-count threshold.
- [ ] Run `pytest backend/tests/test_main_structure.py -q` and verify it fails before implementation.
- [ ] Move extraction prompt/save/job helpers to `memory_extraction.py`, task publishing/stream/job scheduler glue to `memory_tasks.py`, and archived trip episode/slice helpers to `memory_episodes.py`.
- [ ] Run `pytest backend/tests/test_main_structure.py backend/tests/test_memory_integration.py -q`.
- [ ] Review `git diff -- backend/api/orchestration/memory backend/tests/test_main_structure.py`.

### Task 1b: Align Memory And Internal Task API Names

**Files:**
- Modify: `backend/api/routes/memory_routes.py`
- Create: `backend/api/routes/internal_task_routes.py`
- Modify: `backend/main.py`
- Modify: `backend/tests/test_main_structure.py`

- [ ] Add structure tests asserting `internal_task_routes.py` exists and `memory_routes.py` does not own `/api/internal-tasks`.
- [ ] Move internal task snapshot/SSE endpoints into `internal_task_routes.py` and register them from `main.py`.
- [ ] Run `pytest backend/tests/test_main_structure.py backend/tests/test_memory_integration.py -q`.
- [ ] Review `git diff -- backend/api/routes/memory_routes.py backend/api/routes/internal_task_routes.py backend/main.py backend/tests/test_main_structure.py`.

### Task 2: Split Agent Builder

**Files:**
- Modify: `backend/api/orchestration/agent/builder.py`
- Create: `backend/api/orchestration/agent/tools.py`
- Create: `backend/api/orchestration/agent/hooks.py`
- Modify: `backend/tests/test_main_structure.py`

- [ ] Add structure tests asserting the agent orchestration subpackage exists and `agent/builder.py` stays below a smaller line-count threshold.
- [ ] Run `pytest backend/tests/test_main_structure.py -q` and verify it fails before implementation.
- [ ] Move tool engine assembly to `agent_tools.py` and hook callbacks/setup to `agent_hooks.py`.
- [ ] Run `pytest backend/tests/test_main_structure.py backend/tests/test_api.py backend/tests/test_quality_gate.py -q`.
- [ ] Review `git diff -- backend/api/orchestration/agent backend/tests/test_main_structure.py`.

### Task 3: Split Chat Stream

**Files:**
- Modify: `backend/api/orchestration/chat/stream.py`
- Create: `backend/api/orchestration/chat/events.py`
- Create: `backend/api/orchestration/chat/finalization.py`
- Modify: `backend/tests/test_main_structure.py`

- [ ] Add structure tests asserting the chat orchestration subpackage exists and `chat/stream.py` stays below a smaller line-count threshold.
- [ ] Run `pytest backend/tests/test_main_structure.py -q` and verify it fails before implementation.
- [ ] Move event serialization helpers to `chat_events.py` and run/session persistence finalization to `chat_finalization.py`.
- [ ] Run `pytest backend/tests/test_main_structure.py backend/tests/test_api.py backend/tests/test_memory_integration.py -q`.
- [ ] Review `git diff -- backend/api/orchestration/chat backend/tests/test_main_structure.py`.

### Task 4: Confirm Or Remove Stray Data Directory

**Files:**
- Inspect: `backend/backend/data`
- Modify: `.gitignore` only if the directory is an ignored local artifact pattern issue

- [ ] Check whether `backend/backend/data` contains tracked files.
- [ ] Search code and config for references to `backend/backend/data`.
- [ ] Remove it if it is untracked generated state; otherwise document why it must stay.
- [ ] Run `git status --short` and `git diff --check`.
- [ ] Review the resulting filesystem/status output.

### Final Verification

- [ ] Run `python -m py_compile backend/main.py backend/api/*.py backend/api/orchestration/*.py backend/api/orchestration/*/*.py backend/api/routes/*.py`.
- [ ] Run `pytest backend/tests -q`.
- [ ] Run `git diff --check`.
- [ ] Update `PROJECT_OVERVIEW.md` if orchestration directories or responsibilities changed.
