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
| One-shot verify | `./scripts/run-a0-core.sh` | no API keys |
| Pytest full unit | **2054 passed** (2026-07-17, `OTEL_SDK_DISABLED=true pytest -m "not integration"`) | A1 local baseline |
| A0 core | **269 passed** | steering / orchestrator / candidate / writers / … |
| Golden cases | **40 load OK** | `backend/evals/golden_cases/` |
| Dependency lock | `backend/uv.lock` | `uv sync --all-extras --frozen` |
| LICENSE | MIT | repo root |
| Baseline metrics (pass@k numbers) | **mock** 12-case pass@3 = **1.00** | See `baseline-summary.md` — not live LLM |
| Fault injection | F1–F6 mapped to tests (**PASS**); F7 partial | See `fault-injection-report.md` |
| Demo A (product UI) | scripted playback | Marked mock in README |
| Demo B (reliability) | _pending recording_ | Prefer real backend + trace |
| Interview talk track | `interview-talk-track.md` | two stories |
| Hostile Q | `hostile-questions.md` | top 10 |
| Release notes | `RELEASE_NOTES_v1.0-portfolio.md` | tag after CI green |

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
./scripts/run-a0-core.sh
```

## Next evidence to fill

1. ~~12-case mock pass@3~~ done → live pass@k when budget allows
2. ~~Fault-injection test map~~ done → attach one redacted real-run trace for Demo B
3. Demo B recording linked from README
4. GitHub About description/topics + `v1.0-portfolio` release (manual / `gh release`)
