# Portfolio proof (30-second scan)

> Independent portfolio prototype · no production users · reliability via tests / evals / fault injection.

## Positioning

**Reliable Travel Agent** — long-horizon planning agent with bounded autonomy, parallel workers, partial replanning, mid-run steering, and trace-based evaluation. Travel is the validation domain; the product under review is the **Agent Runtime**.

## Architecture (one picture)

```text
Human-Agent Loop (UI / SSE / session / steer / backtrack)
        │
        ▼
Agent Loop (bounded iterations)
  LLM turn → tools → Plan Writer → phase / validation → next iter
        │
        ├── Phase 3 Orchestrator–Workers
        │     Candidate Store + shared blackboard
        │     accepted-only commit · redispatch · rollback
        └── Flight recorder (SQLite) + Trace Grader + golden evals
```

## Status table

| Item | Value | Notes |
|------|-------|--------|
| CI | `.github/workflows/ci.yml` | Backend A0 core + frontend build |
| Pytest collect | ≈2054 | Full suite; CI runs core subset |
| A0 core | **269 passed** (2026-07-17, `uv run pytest` A0 list) | steering / orchestrator / candidate / writers / … |
| Golden cases | **40 load OK** | `backend/evals/golden_cases/` |
| Dependency lock | `backend/uv.lock` | `uv sync --all-extras --frozen` |
| LICENSE | MIT | repo root |
| Baseline metrics (pass@k numbers) | **mock** 12-case pass@3 = **1.00** | See `baseline-summary.md` — not live LLM |
| Fault injection | F1–F6 mapped to tests (**PASS**); F7 partial | See `fault-injection-report.md` |
| Demo A (product UI) | scripted playback | Marked mock in README |
| Demo B (reliability) | _pending recording_ | Prefer real backend + trace |
| Interview talk track | `interview-talk-track.md` | two stories |
| Hostile Q | `hostile-questions.md` | top 10 |

## Core reliability paths covered in A0

- Mid-run steering safe drain (`tests/test_steering.py`)
- Phase 3 orchestrator / workers (`tests/test_orchestrator.py`, `tests/test_day_worker.py`)
- Candidate store versioning (`tests/test_phase3_candidate_store.py`)
- Plan Writer single-write path (`tests/test_plan_writers.py`)
- Phase router (`tests/test_phase_router.py`)
- Eval pipeline + quality gate (`tests/test_eval_pipeline.py`, `tests/test_quality_gate.py`)
- Trace API + session persistence (`tests/test_trace_api.py`, `tests/test_session_persistence.py`)

## Three-minute start

```bash
cd backend && uv sync --all-extras --frozen
uv run pytest -q tests/test_steering.py tests/test_orchestrator.py \
  tests/test_phase3_candidate_store.py tests/test_plan_writers.py

cd ../frontend && npm ci && npm run build
```

## Next evidence to fill

1. 12 high-value golden cases with pass@3 → `baseline-results.json`
2. ≥5 fault-injection scenarios → `fault-injection-report.md`
3. One failure→recovery trace story under `traces/`
4. Demo B recording linked from README
