# Trace Events Verification Matrix

## Fast PR-Safe Tests

- `pytest backend/tests/test_trace_redaction.py`
- `pytest backend/tests/test_trace_recorder.py`
- `pytest backend/tests/test_storage_database.py backend/tests/test_trace_store.py`
- `pytest backend/tests/test_agent_llm_turn.py`
- `pytest backend/tests/test_parallel_tools.py`
- `pytest backend/tests/test_agent_loop.py`
- `pytest backend/tests/test_memory_turn_trace.py`
- `pytest backend/tests/test_day_worker.py::test_run_day_worker_emits_worker_trace_events backend/tests/test_orchestrator.py::test_orchestrator_emits_phase3_trace_events`
- `pytest backend/tests/test_deliverable_trace.py`
- `pytest backend/tests/test_trace_api.py`
- `pytest backend/tests/test_trace_grader.py`
- `pytest backend/tests/test_failure_report.py backend/tests/test_canary_scripts.py backend/tests/test_eval_pipeline.py`

## Unit Tests By Module

- Redaction policy: `backend/tests/test_trace_redaction.py`
- Recorder API/artifacts/failure swallowing: `backend/tests/test_trace_recorder.py`
- Database migrations and trace store CRUD: `backend/tests/test_storage_database.py`, `backend/tests/test_trace_store.py`
- LLM call/output trace capture: `backend/tests/test_agent_llm_turn.py`
- Tool call/result/state diff/validation/soft judge trace capture: `backend/tests/test_parallel_tools.py`
- Phase gate/transition/run snapshots/context rebuild trace capture: `backend/tests/test_agent_loop.py`
- Memory recall/hit trace capture: `backend/tests/test_memory_turn_trace.py`
- Phase 3 worker/orchestrator trace capture: `backend/tests/test_day_worker.py`, `backend/tests/test_orchestrator.py`
- Deliverable draft/finalize trace capture: `backend/tests/test_deliverable_trace.py`
- API/grader/eval/failure-analysis bridge: `backend/tests/test_trace_api.py`, `backend/tests/test_trace_grader.py`, `backend/tests/test_eval_pipeline.py`, `backend/tests/test_failure_report.py`, `backend/tests/test_canary_scripts.py`

## Runtime Path Integration Tests

- Chat route start/end trace: `backend/tests/test_trace_persistence.py`
- LLM context/call/output trace: `backend/tests/test_agent_llm_turn.py`
- Tool call/result/state diff trace: `backend/tests/test_parallel_tools.py`
- Phase gate/transition trace: `backend/tests/test_agent_loop.py`
- Memory recall/hit trace: `backend/tests/test_memory_turn_trace.py`
- API compatibility and artifacts: `backend/tests/test_trace_api.py`
- Phase 3 worker/orchestrator trace: `backend/tests/test_parallel_phase3_integration.py`
- Deliverable draft/finalize trace: `backend/tests/test_deliverable_trace.py`, `backend/tests/test_phase_integration.py`

## Live / Canary Tests

- `python scripts/eval-stability.py --cases regression-trace-005 --k 1`
- `python scripts/eval-stability.py --cases regression-trace-001,regression-trace-002,regression-trace-003,regression-trace-004,regression-trace-006 --k 1`
- `python scripts/run-full-phase-canary.py --start-backend --strict`
- `python scripts/run-adaptive-canary.py --start-backend --strict`
- `python scripts/failure-analysis/run_and_analyze.py`

These require a running backend and configured provider/API keys.

## Manual Verification Commands

- `curl http://127.0.0.1:8000/api/traces/<run_id>`
- `curl -X POST http://127.0.0.1:8000/api/traces/<run_id>/grade`
- `sqlite3 data/sessions.db "select event_type,count(*) from trace_events where run_id='<run_id>' group by event_type;"`
- `sqlite3 data/sessions.db "select kind,count(*) from trace_artifacts where run_id='<run_id>' group by kind;"`

## Minimum Passing Criteria

- Old `/api/sessions/{session_id}/trace` still returns the legacy TraceViewer shape.
- `/api/traces/{run_id}` returns parsed payloads, common fields, and artifact metadata.
- Realtime recorder failures are swallowed and do not fail chat.
- Sensitive tokens are redacted in payload/artifact content.
- A Phase 1 -> Phase 4 run contains `llm_call`, `llm_output`, `tool_call`, `tool_result`, `state_diff`, `phase_gate`, `phase_transition`, `memory_recall`, `context_build`, and `deliverable_finalize` when those paths execute.
- Trace grader fail results include `evidence_event_ids`.

## Nightly Criteria

- Full/adaptive canary reaches Phase 4 deliverables.
- Canary report includes persisted trace event family counts.
- At least one run includes Phase 3 worker events when parallel Phase 3 is enabled.
- Failure report includes trace grade failures and top event ids.

## Rollback Criteria

- Chat execution fails because trace persistence/artifact writing fails.
- Trace schema migration breaks existing SQLite databases.
- Old TraceViewer API response changes shape incompatibly.
- Redaction tests fail for token/cookie/bearer/xsec patterns.
