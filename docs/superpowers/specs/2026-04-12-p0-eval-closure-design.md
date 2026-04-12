# P0 Eval Closure Design

## Goal

Close the largest remaining P0 gap: turn the eval package from a YAML/offline assertion scaffold into an executable evaluation harness with report output, while making session LLM latency stats meaningful and keeping documentation honest.

## Scope

- Add an executor protocol to `backend/evals/runner.py` so tests and future CLI/API adapters can run a `GoldenCase` end to end.
- Preserve the existing offline evaluator for pre-collected traces.
- Add JSON report generation with suite, difficulty, assertion, infeasible, and stats summaries.
- Record non-zero LLM call duration in `backend/main.py` when usage chunks are observed.
- Align README and `PROJECT_OVERVIEW.md` around the actual 1/3/5/7 production path and executable eval wording.

## Out Of Scope

- Real LLM calls inside unit tests.
- A full CLI command for eval execution.
- pass@k orchestration.
- New golden cases beyond the existing 15.

## Architecture

`GoldenCaseExecutor` is a callable protocol: `executor(case) -> EvalExecution`. `EvalExecution` contains final state, called tools, response text, and optional stats. `run_case()` uses the executor, then delegates assertion checking to the existing evaluator. `run_suite()` loops over cases, aggregates metrics, and returns a richer `SuiteResult`.

Report generation is deterministic and file-system local. `save_report()` writes JSON to `backend/evals/reports/` or a caller-provided directory. Tests use fake executors and temporary directories.

## Testing

Tests are added before implementation:

- executable runner calls the executor and evaluates assertions
- suite metrics include difficulty and infeasible breakdowns
- report writer emits JSON with metrics and case results
- LLM stats helper records positive wall-clock duration

