# Eval baseline summary

**Status:** mock pass@3 baseline frozen for portfolio scaffolding (not live LLM quality).

| Field | Value |
|-------|--------|
| commit_sha | `7db2bc4ce6b13b8e96c0f2585cbf7a81bb7a8f48` (pre-portfolio-commit; re-stamp after merge) |
| model | **mock executor** (deterministic, no live LLM) |
| date | 2026-07-17 |
| sample_count | 12 cases × pass@3 |
| executor | mock |
| report | `baseline-stability.md` / `baseline-stability.json` |

## Metrics

| Metric | Value | Source / caveat |
|--------|-------|-----------------|
| Golden pass@3 (12-case, mock) | **1.00** (12/12 cases 3/3) | `scripts/eval-stability.py --mock` |
| Unstable cases | 0 | same |
| Highly unstable cases | 0 | same |
| Tool-call set overlap (mean) | 1.00 | mock |
| Mean cost (mock placeholder) | ~$0.01 | **not real spend** |
| Assertion note | `easy-001` trace_grade_status:state_write_has_diff consistency 0.00 | mock path; investigate before claiming live quality |
| 40-case single regression | _not run yet_ | next step |
| Live model pass@k | _not run yet_ | requires API keys + cost |

## Cases included

`easy-001`, `easy-002`, `easy-004`, `med-001`, `med-002`, `hard-002`, `hard-003`, `failure-001`, `failure-004`, `failure-005`, `infeasible-001`, `infeasible-002`

## How to regenerate

```bash
# From repo root
uv run --directory backend python ../scripts/eval-stability.py --mock --k 3 \
  --cases easy-001,easy-002,easy-004,med-001,med-002,hard-002,hard-003,failure-001,failure-004,failure-005,infeasible-001,infeasible-002 \
  --output ../docs/evidence/baseline-stability
```

**Honesty for interviews:** mock baseline proves the eval harness is runnable and stable; it does **not** prove production LLM quality. Quote live numbers only after a live run.
