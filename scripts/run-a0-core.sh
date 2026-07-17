#!/usr/bin/env bash
# A0 portfolio core verification — no API keys required.
# Matches .github/workflows/ci.yml backend-core job.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"

export OTEL_SDK_DISABLED="${OTEL_SDK_DISABLED:-true}"

echo "==> uv sync --all-extras --frozen"
uv sync --all-extras --frozen

echo "==> load golden cases (expect 40)"
uv run python -c "
from pathlib import Path
from evals.runner import load_golden_cases
n = len(load_golden_cases(str(Path('evals/golden_cases'))))
assert n == 40, n
print(f'golden_cases={n}')
"

echo "==> A0 core pytest suite"
uv run pytest -q \
  tests/test_steering.py \
  tests/test_orchestrator.py \
  tests/test_phase3_candidate_store.py \
  tests/test_day_worker.py \
  tests/test_plan_writers.py \
  tests/test_phase_router.py \
  tests/test_eval_pipeline.py \
  tests/test_quality_gate.py \
  tests/test_trace_api.py \
  tests/test_session_persistence.py \
  --tb=line

echo "==> frontend build"
cd "$ROOT/frontend"
npm ci
npm run build

echo "A0 core verification passed."
