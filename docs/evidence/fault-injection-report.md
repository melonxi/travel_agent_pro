# Fault injection report

**Last verified:** 2026-07-17  
**How verified:** unit/integration-style pytest under `backend/` with `OTEL_SDK_DISABLED=true` (no API keys).

```bash
cd backend
uv sync --all-extras --frozen
export OTEL_SDK_DISABLED=true
uv run pytest -q \
  tests/test_steering.py \
  tests/test_orchestrator.py \
  tests/test_phase3_candidate_store.py \
  tests/test_day_worker.py \
  --tb=short
```

## Scenario matrix

| ID | Scenario | Expected behavior | Primary tests | Result |
|----|----------|-------------------|---------------|--------|
| F1 | Worker output rejected / candidate lifecycle | Rejected or superseded is explicit; only accepted commits | `tests/test_phase3_candidate_store.py` (suite) | **PASS** (suite green in A0) |
| F2 | Redispatch failure | Restore previous version; **no silent day loss** | `test_phase3_steer_redispatch_failure_restores_previous_version`, `test_phase3_late_steer_redispatch_failure_restores_previous_version` in `tests/test_steering.py`; redispatch paths in `tests/test_orchestrator.py` | **PASS** |
| F3 | Cross-day / uniqueness constraints | Blackboard / store enforces uniqueness | `tests/test_phase3_candidate_store.py` + related uniqueness tests | **PASS** (store suite in A0) |
| F4 | Tool empty / invalid schema | Fail closed or recover; no dirty final write via Plan Writer | `tests/test_plan_writers.py`, `tests/test_quality_gate.py`, harness paths | **PASS** (A0 includes writers + quality gate) |
| F5 | Steering during tool protocol window | Drain only at safe boundaries; no break of tool_call/result pairing | `tests/test_steering.py` drain + Phase3 steer series | **PASS** |
| F6 | Run end with residual steering | Terminal ack / clear HTTP semantics (`409` no active run, terminal ack at stream end) | `test_steer_endpoint_no_active_run_returns_409`, `test_stream_terminally_acks_steering_left_at_run_end`, `test_phase3_steer_during_attempt5_gets_terminal_ack` | **PASS** |
| F7 | Phase backtrack vs deliverables | Stale deliverables must not stay authoritative | Covered partially via session/plan persistence + deliverable tests (expand if needed) | **PARTIAL** — A0 has session persistence; dedicated deliverable backtrack case still to highlight |

## Interview story (recommended): F2 silent day loss

1. Day workers do **not** write final `TravelPlanState` directly.  
2. Outputs land as **candidates** with `accepted` / `rejected` / `superseded`.  
3. Shared blackboard enforces POI / budget / cross-day constraints.  
4. Final plan only materializes **accepted** versions.  
5. If redispatch fails, previous version is **restored** (see steering tests above).  
6. Point to test names, not slideware.

## Interview story (secondary): F5/F6 mid-run steering

1. `POST /api/chat/{session_id}/steer` enqueues guidance.  
2. Loop/orchestrator drains only at safe points.  
3. Must not insert between assistant `tool_call` and `tool_result`.  
4. Can target a single day for redispatch.  
5. Leftover queue at run end → **terminal ack**, not silent drop.

## Trace attachments

Optional redacted exports: `docs/evidence/traces/`.  
Raw SQLite DBs stay under `backend/data/` (gitignored).

## Remaining work

- [ ] Attach one redacted real-run trace for F2 (Demo B material)
- [ ] Mark F7 with an explicit named test once identified/added
- [ ] Optional: live fault-analysis script output under `scripts/failure-analysis/results/` (local only)
