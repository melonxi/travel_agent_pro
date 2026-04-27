# Phase 5 Worker Artifact Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Replace Phase 5 worker final-text JSON handoff with a worker-only structured candidate submission tool backed by run-scoped JSON artifacts, while keeping Orchestrator as the only writer of final `daily_plans`.

**Architecture:** Day Workers receive a new `submit_day_plan_candidate` tool schema in addition to existing read-only tools. The tool is handled inside `run_day_worker`, validates that the submitted day matches the assigned `DayTask`, atomically writes a JSON artifact under `data/phase5_runs/{session_id}/{run_id}/`, and records the submitted dayplan as the worker result. `Phase5Orchestrator` creates a `run_id`, passes a `Phase5CandidateStore` to workers, loads latest submitted candidates after collection, then continues with existing global validation, redispatch, and `replace_all_daily_plans` final commit.

**Tech Stack:** Python 3.12, asyncio, dataclasses, pytest, existing `ToolCall` / `ToolResult` / `LLMChunk` contracts, JSON files with atomic `tmp -> replace` writes.

---

## File Structure

- Create `backend/agent/phase5/candidate_store.py`
  - Owns run-scoped artifact paths, candidate validation, atomic writes, and latest-candidate loading.
- Modify `backend/config.py`
  - Add `artifact_root` to `Phase5ParallelConfig` and parse `phase5.parallel.artifact_root`; default to `./data/phase5_runs`.
- Modify `backend/agent/phase5/worker_prompt.py`
  - Add instruction that workers should call `submit_day_plan_candidate` when the DayPlan is ready.
- Modify `backend/agent/phase5/day_worker.py`
  - Add worker-only submit tool schema, intercept submit tool calls, write candidate artifact, and return submitted candidate on final no-tool turn.
- Modify `backend/agent/phase5/orchestrator.py`
  - Create run id/store, pass store metadata to workers, and prefer artifact candidates over text-return fallback.
- Add tests in `backend/tests/test_phase5_candidate_store.py`
  - Validate artifact write/read, day mismatch rejection, and latest-attempt selection.
- Extend `backend/tests/test_day_worker.py`
  - Verify worker can submit DayPlan through the tool and complete without final JSON text.
- Extend `backend/tests/test_orchestrator.py`
  - Verify Orchestrator reads submitted artifact candidates and writes final daily plans.

---

### Task 1: Candidate Artifact Store

**Files:**
- Create: `backend/agent/phase5/candidate_store.py`
- Test: `backend/tests/test_phase5_candidate_store.py`

- [x] **Step 1: Write failing candidate store tests**

Add tests that expect this API:

```python
from pathlib import Path

import pytest

from agent.phase5.candidate_store import (
    Phase5CandidateStore,
    Phase5CandidateValidationError,
)


def _dayplan(day: int = 1) -> dict:
    return {
        "day": day,
        "date": "2026-05-01",
        "notes": "候选计划",
        "activities": [],
    }


def test_submit_candidate_writes_json_artifact(tmp_path: Path):
    store = Phase5CandidateStore(tmp_path)

    result = store.submit_candidate(
        session_id="sess_1",
        run_id="run_1",
        worker_id="day_1_attempt_1",
        expected_day=1,
        attempt=1,
        dayplan=_dayplan(1),
    )

    assert result["submitted"] is True
    path = Path(result["path"])
    assert path.exists()
    loaded = store.load_latest_candidates("sess_1", "run_1")
    assert len(loaded) == 1
    assert loaded[0]["dayplan"]["day"] == 1


def test_submit_candidate_rejects_wrong_day(tmp_path: Path):
    store = Phase5CandidateStore(tmp_path)

    with pytest.raises(Phase5CandidateValidationError, match="expected day 1"):
        store.submit_candidate(
            session_id="sess_1",
            run_id="run_1",
            worker_id="day_1_attempt_1",
            expected_day=1,
            attempt=1,
            dayplan=_dayplan(2),
        )


def test_load_latest_candidates_keeps_highest_attempt_per_day(tmp_path: Path):
    store = Phase5CandidateStore(tmp_path)
    store.submit_candidate("sess_1", "run_1", "day_1_attempt_1", 1, 1, _dayplan(1))
    latest = _dayplan(1)
    latest["notes"] = "newer"
    store.submit_candidate("sess_1", "run_1", "day_1_attempt_2", 1, 2, latest)

    loaded = store.load_latest_candidates("sess_1", "run_1")

    assert len(loaded) == 1
    assert loaded[0]["attempt"] == 2
    assert loaded[0]["dayplan"]["notes"] == "newer"
```

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
cd backend
source .venv/bin/activate
pytest tests/test_phase5_candidate_store.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent.phase5.candidate_store'`.

- [x] **Step 3: Implement candidate store**

Create `backend/agent/phase5/candidate_store.py`:

```python
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class Phase5CandidateValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Phase5CandidateStore:
    root: Path | str

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))

    def run_dir(self, session_id: str, run_id: str) -> Path:
        return Path(self.root) / session_id / run_id

    def submit_candidate(
        self,
        session_id: str,
        run_id: str,
        worker_id: str,
        expected_day: int,
        attempt: int,
        dayplan: dict[str, Any],
    ) -> dict[str, Any]:
        self._validate_dayplan(expected_day, dayplan)
        run_dir = self.run_dir(session_id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": session_id,
            "run_id": run_id,
            "worker_id": worker_id,
            "day": expected_day,
            "attempt": attempt,
            "status": "submitted",
            "dayplan": dayplan,
        }
        path = run_dir / f"day_{expected_day}_attempt_{attempt}.json"
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
        return {"submitted": True, "day": expected_day, "attempt": attempt, "path": str(path)}

    def load_latest_candidates(self, session_id: str, run_id: str) -> list[dict[str, Any]]:
        run_dir = self.run_dir(session_id, run_id)
        if not run_dir.exists():
            return []
        latest_by_day: dict[int, dict[str, Any]] = {}
        for path in sorted(run_dir.glob("day_*_attempt_*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            day = int(payload["day"])
            attempt = int(payload.get("attempt", 0))
            current = latest_by_day.get(day)
            if current is None or attempt > int(current.get("attempt", 0)):
                latest_by_day[day] = payload
        return [latest_by_day[day] for day in sorted(latest_by_day)]

    def _validate_dayplan(self, expected_day: int, dayplan: dict[str, Any]) -> None:
        if not isinstance(dayplan, dict):
            raise Phase5CandidateValidationError("dayplan must be an object")
        actual_day = dayplan.get("day")
        if actual_day != expected_day:
            raise Phase5CandidateValidationError(
                f"dayplan day {actual_day!r} does not match expected day {expected_day}"
            )
        if not isinstance(dayplan.get("date"), str) or not dayplan["date"]:
            raise Phase5CandidateValidationError("dayplan.date must be a non-empty string")
        if not isinstance(dayplan.get("activities"), list):
            raise Phase5CandidateValidationError("dayplan.activities must be a list")
```

- [x] **Step 4: Run candidate store tests**

Run:

```bash
cd backend
source .venv/bin/activate
pytest tests/test_phase5_candidate_store.py -q
```

Expected: all tests PASS.

---

### Task 2: Worker Tool Submission

**Files:**
- Modify: `backend/agent/phase5/day_worker.py`
- Modify: `backend/agent/phase5/worker_prompt.py`
- Test: `backend/tests/test_day_worker.py`

- [x] **Step 1: Write failing worker submission test**

Add a test where the LLM calls `submit_day_plan_candidate`, then emits a plain completion message without JSON. Expected result: `run_day_worker()` returns success with the submitted dayplan and the artifact exists.

- [x] **Step 2: Run the focused test and verify it fails**

Run:

```bash
cd backend
source .venv/bin/activate
pytest tests/test_day_worker.py::test_run_day_worker_accepts_submit_day_plan_candidate_tool -q
```

Expected: FAIL because the submit tool is unknown or the new optional parameters do not exist.

- [x] **Step 3: Implement worker-only submit tool**

Modify `run_day_worker()` to accept optional `candidate_store`, `run_id`, and `attempt`. Add a `submit_day_plan_candidate` schema to `worker_tools` when a store/run id is available. Intercept submit tool calls before delegating other calls to `tool_engine.execute_batch()`. Store the submitted dayplan in a local `submitted_dayplan`; on a final no-tool turn, return that submitted plan before falling back to text JSON extraction.

- [x] **Step 4: Run day worker tests**

Run:

```bash
cd backend
source .venv/bin/activate
pytest tests/test_day_worker.py -q
```

Expected: all tests PASS.

---

### Task 3: Orchestrator Reads Candidate Artifacts

**Files:**
- Modify: `backend/config.py`
- Modify: `backend/agent/phase5/orchestrator.py`
- Test: `backend/tests/test_orchestrator.py`

- [x] **Step 1: Write failing orchestrator artifact test**

Add a test that patches `run_day_worker()` to write candidate artifacts through the provided `candidate_store` and return success. The test should verify that Orchestrator loads artifact candidates and writes `plan.daily_plans`.

- [x] **Step 2: Run the focused test and verify it fails**

Run:

```bash
cd backend
source .venv/bin/activate
pytest tests/test_orchestrator.py::TestWorkerArtifactHandoff::test_orchestrator_loads_worker_candidate_artifacts -q
```

Expected: FAIL because Orchestrator does not create/pass/read candidate store yet.

- [x] **Step 3: Add config and Orchestrator integration**

Add `artifact_root: str = "./data/phase5_runs"` to `Phase5ParallelConfig` and parse `phase5.parallel.artifact_root`. In `Phase5Orchestrator.run()`, create `run_id`, instantiate `Phase5CandidateStore`, pass `candidate_store`, `run_id`, and attempt numbers to `run_day_worker()`, and after collection load `store.load_latest_candidates(session_id, run_id)`. Use loaded artifact dayplans when present; otherwise keep the existing `successes` fallback for unit tests and backward compatibility.

- [x] **Step 4: Run orchestrator tests**

Run:

```bash
cd backend
source .venv/bin/activate
pytest tests/test_orchestrator.py -q
```

Expected: all tests PASS.

---

### Task 4: Focused Regression Verification

**Files:**
- No new files.

- [x] **Step 1: Run focused Phase 5 tests**

Run:

```bash
cd backend
source .venv/bin/activate
pytest tests/test_phase5_candidate_store.py tests/test_day_worker.py tests/test_orchestrator.py tests/test_loop_phase5_routing.py -q
```

Expected: all tests PASS.

- [x] **Step 2: Run frontend build to ensure prior UI change still compiles**

Run:

```bash
cd frontend
npm run build
```

Expected: build exits 0. Existing Vite chunk-size warning is acceptable.

---

## Self-Review

- Spec coverage: The plan replaces text-only handoff with a worker-only submit tool, persists JSON artifacts in a fixed staging directory, keeps Orchestrator as final writer, and preserves fallback compatibility.
- Placeholder scan: No TBD/TODO placeholders remain.
- Type consistency: `Phase5CandidateStore`, `Phase5CandidateValidationError`, `submit_candidate()`, `load_latest_candidates()`, `candidate_store`, `run_id`, and `attempt` names are used consistently across tasks.
