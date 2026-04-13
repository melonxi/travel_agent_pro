# pass@k 稳定性评估 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add pass@k stability evaluation — run each golden case k times, compute consistency metrics, generate reports.

**Architecture:** A new `stability.py` module in `backend/evals/` computes per-case and suite-level stability metrics by calling the executor directly (not `runner.run_case`) to access `EvalExecution.tool_calls` and per-assertion results. A CLI script in `scripts/` wraps this with argument parsing and a self-contained HTTP executor.

**Tech Stack:** Python 3.12, dataclasses, statistics stdlib, httpx (CLI only), pytest

**Spec:** `docs/superpowers/specs/2026-04-13-pass-at-k-stability-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `backend/evals/models.py` | Modify (append) | `StabilityMetrics` + `StabilitySuiteResult` dataclasses |
| `backend/evals/stability.py` | Create | `run_stability`, `run_stability_suite`, `save_stability_report` |
| `backend/tests/test_stability.py` | Create | All unit tests for stability computation + report generation |
| `scripts/eval-stability.py` | Create | CLI entry point with self-contained HTTP executor |

**Not touched:** `backend/evals/runner.py`, `backend/harness/`, `backend/tools/`, `backend/main.py`, `frontend/`

---

### Task 1: Add StabilityMetrics and StabilitySuiteResult data models

**Files:**
- Modify: `backend/evals/models.py:79` (append after line 79)

- [ ] **Step 1: Append dataclasses to models.py**

Add at end of `backend/evals/models.py`:

```python
@dataclass
class StabilityMetrics:
    """Aggregated stability metrics for a single case over k runs."""

    case_id: str
    k: int
    pass_rate: float
    assertion_consistency: dict[str, float]
    tool_overlap_ratio: float
    cost_stats: dict[str, float]
    duration_stats: dict[str, float]
    runs: list[CaseResult] = field(default_factory=list)


@dataclass
class StabilitySuiteResult:
    """Stability results for all cases."""

    total_cases: int = 0
    k: int = 3
    results: list[StabilityMetrics] = field(default_factory=list)
    overall_pass_rate: float = 0.0
    unstable_cases: list[str] = field(default_factory=list)
    highly_unstable_cases: list[str] = field(default_factory=list)
```

- [ ] **Step 2: Verify import works**

Run: `cd backend && python -c "from evals.models import StabilityMetrics, StabilitySuiteResult; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add backend/evals/models.py
git commit -m "feat(evals): add StabilityMetrics and StabilitySuiteResult dataclasses"
```

---

### Task 2: Write failing tests for run_stability core logic

**Files:**
- Create: `backend/tests/test_stability.py`

- [ ] **Step 1: Write all unit tests for run_stability**

Create `backend/tests/test_stability.py`:

```python
"""Tests for pass@k stability evaluation."""
from __future__ import annotations

import math

import pytest

from evals.models import (
    Assertion,
    AssertionType,
    CaseResult,
    EvalExecution,
    GoldenCase,
    StabilityMetrics,
    StabilitySuiteResult,
)
from evals.stability import run_stability, run_stability_suite


def _make_case(
    case_id: str = "test-case",
    difficulty: str = "easy",
    assertions: list[Assertion] | None = None,
) -> GoldenCase:
    return GoldenCase(
        id=case_id,
        name=f"Test {case_id}",
        description="",
        difficulty=difficulty,
        messages=[{"role": "user", "content": "hello"}],
        assertions=assertions
        or [
            Assertion(type=AssertionType.STATE_FIELD_SET, target="destination", value="东京"),
            Assertion(type=AssertionType.TOOL_CALLED, target="search_flights"),
        ],
    )


def _make_executor(executions: list[EvalExecution]):
    """Return an executor that yields pre-defined executions in order."""
    it = iter(executions)
    def executor(case: GoldenCase) -> EvalExecution:
        return next(it)
    return executor


class TestRunStabilityAllPass:
    """k=3, all runs pass all assertions."""

    def test_pass_rate_is_one(self):
        case = _make_case()
        execs = [
            EvalExecution(
                state={"destination": "东京"},
                tool_calls=["search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.10},
            )
            for _ in range(3)
        ]
        result = run_stability(case, _make_executor(execs), k=3)

        assert result.pass_rate == 1.0
        assert result.k == 3
        assert result.case_id == "test-case"

    def test_assertion_consistency_all_one(self):
        case = _make_case()
        execs = [
            EvalExecution(
                state={"destination": "东京"},
                tool_calls=["search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.10},
            )
            for _ in range(3)
        ]
        result = run_stability(case, _make_executor(execs), k=3)

        assert result.assertion_consistency["state_field_set:destination"] == 1.0
        assert result.assertion_consistency["tool_called:search_flights"] == 1.0

    def test_tool_overlap_is_one(self):
        case = _make_case()
        execs = [
            EvalExecution(
                state={"destination": "东京"},
                tool_calls=["search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.10},
            )
            for _ in range(3)
        ]
        result = run_stability(case, _make_executor(execs), k=3)

        assert result.tool_overlap_ratio == 1.0


class TestRunStabilityPartialPass:
    """k=5, 3 pass + 2 fail."""

    def test_pass_rate_is_0_6(self):
        case = _make_case()
        passing = EvalExecution(
            state={"destination": "东京"},
            tool_calls=["search_flights"],
            responses=["ok"],
            stats={"estimated_cost_usd": 0.10},
        )
        failing = EvalExecution(
            state={"destination": "大阪"},
            tool_calls=["search_flights"],
            responses=["ok"],
            stats={"estimated_cost_usd": 0.15},
        )
        execs = [passing, passing, passing, failing, failing]
        result = run_stability(case, _make_executor(execs), k=5)

        assert result.pass_rate == pytest.approx(0.6)

    def test_assertion_consistency_reflects_difference(self):
        case = _make_case()
        passing = EvalExecution(
            state={"destination": "东京"},
            tool_calls=["search_flights"],
            responses=["ok"],
            stats={"estimated_cost_usd": 0.10},
        )
        failing = EvalExecution(
            state={"destination": "大阪"},
            tool_calls=["search_flights"],
            responses=["ok"],
            stats={"estimated_cost_usd": 0.15},
        )
        execs = [passing, passing, passing, failing, failing]
        result = run_stability(case, _make_executor(execs), k=5)

        # destination assertion: 3/5 pass (东京 matches 3 times)
        assert result.assertion_consistency["state_field_set:destination"] == pytest.approx(0.6)
        # tool_called: all 5 have search_flights
        assert result.assertion_consistency["tool_called:search_flights"] == 1.0


class TestRunStabilitySingleRun:
    """k=1 edge case."""

    def test_k1_pass(self):
        case = _make_case()
        execs = [
            EvalExecution(
                state={"destination": "东京"},
                tool_calls=["search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.10},
            )
        ]
        result = run_stability(case, _make_executor(execs), k=1)

        assert result.pass_rate == 1.0
        assert result.cost_stats["stddev"] == 0.0
        assert result.duration_stats["stddev"] == 0.0

    def test_k1_fail(self):
        case = _make_case()
        execs = [
            EvalExecution(
                state={},
                tool_calls=[],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.05},
            )
        ]
        result = run_stability(case, _make_executor(execs), k=1)

        assert result.pass_rate == 0.0


class TestToolOverlap:
    """Tool overlap ratio edge cases."""

    def test_identical_tools(self):
        case = _make_case()
        execs = [
            EvalExecution(
                state={"destination": "东京"},
                tool_calls=["web_search", "update_plan_state"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.10},
            )
            for _ in range(3)
        ]
        result = run_stability(case, _make_executor(execs), k=3)
        assert result.tool_overlap_ratio == 1.0

    def test_disjoint_tools(self):
        case = _make_case()
        execs = [
            EvalExecution(
                state={"destination": "东京"},
                tool_calls=["web_search"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.10},
            ),
            EvalExecution(
                state={"destination": "东京"},
                tool_calls=["web_search", "search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.12},
            ),
            EvalExecution(
                state={"destination": "东京"},
                tool_calls=["search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.11},
            ),
        ]
        result = run_stability(case, _make_executor(execs), k=3)
        # intersection = {} (no tool is in ALL 3), union = {web_search, search_flights}
        assert result.tool_overlap_ratio == 0.0

    def test_no_tools_any_run(self):
        case = _make_case(assertions=[])
        execs = [
            EvalExecution(state={}, tool_calls=[], responses=["ok"], stats={})
            for _ in range(3)
        ]
        result = run_stability(case, _make_executor(execs), k=3)
        # Both intersection and union are empty — defined as 1.0 (vacuously consistent)
        assert result.tool_overlap_ratio == 1.0


class TestCostAndDurationStats:
    def test_cost_stats(self):
        case = _make_case(assertions=[])
        execs = [
            EvalExecution(state={}, tool_calls=[], responses=["ok"], stats={"estimated_cost_usd": 0.1}),
            EvalExecution(state={}, tool_calls=[], responses=["ok"], stats={"estimated_cost_usd": 0.2}),
            EvalExecution(state={}, tool_calls=[], responses=["ok"], stats={"estimated_cost_usd": 0.3}),
        ]
        result = run_stability(case, _make_executor(execs), k=3)

        assert result.cost_stats["min"] == pytest.approx(0.1)
        assert result.cost_stats["max"] == pytest.approx(0.3)
        assert result.cost_stats["mean"] == pytest.approx(0.2)
        assert result.cost_stats["stddev"] == pytest.approx(0.1)

    def test_missing_cost_defaults_to_zero(self):
        case = _make_case(assertions=[])
        execs = [
            EvalExecution(state={}, tool_calls=[], responses=["ok"], stats={}),
            EvalExecution(state={}, tool_calls=[], responses=["ok"], stats={}),
        ]
        result = run_stability(case, _make_executor(execs), k=2)
        assert result.cost_stats["mean"] == 0.0


class TestRunStabilitySuite:
    def test_suite_aggregation(self):
        case1 = _make_case(case_id="stable-case")
        case2 = _make_case(case_id="unstable-case")

        # case1: 3/3 pass → pass_rate=1.0
        execs1 = [
            EvalExecution(
                state={"destination": "东京"},
                tool_calls=["search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.1},
            )
            for _ in range(3)
        ]
        # case2: 1/3 pass → pass_rate≈0.33
        execs2 = [
            EvalExecution(
                state={"destination": "东京"},
                tool_calls=["search_flights"],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.1},
            ),
            EvalExecution(
                state={},
                tool_calls=[],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.2},
            ),
            EvalExecution(
                state={},
                tool_calls=[],
                responses=["ok"],
                stats={"estimated_cost_usd": 0.15},
            ),
        ]

        all_execs = execs1 + execs2
        executor = _make_executor(all_execs)

        suite = run_stability_suite([case1, case2], executor, k=3)

        assert suite.total_cases == 2
        assert suite.k == 3
        assert suite.overall_pass_rate == pytest.approx((1.0 + 1 / 3) / 2)
        assert "unstable-case" in suite.unstable_cases
        assert "unstable-case" in suite.highly_unstable_cases
        assert "stable-case" not in suite.unstable_cases

    def test_suite_empty_cases(self):
        suite = run_stability_suite([], _make_executor([]), k=3)
        assert suite.total_cases == 0
        assert suite.overall_pass_rate == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_stability.py -v`
Expected: All tests fail with `ModuleNotFoundError: No module named 'evals.stability'`

- [ ] **Step 3: Commit failing tests**

```bash
git add backend/tests/test_stability.py
git commit -m "test(evals): add failing tests for pass@k stability computation"
```

---

### Task 3: Implement run_stability and run_stability_suite

**Files:**
- Create: `backend/evals/stability.py`

- [ ] **Step 1: Create stability.py with core computation logic**

Create `backend/evals/stability.py`:

```python
"""pass@k stability evaluation — run cases k times and compute consistency metrics."""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from evals.models import (
    CaseResult,
    EvalExecution,
    GoldenCase,
    StabilityMetrics,
    StabilitySuiteResult,
)
from evals.runner import GoldenCaseExecutor, evaluate_assertion, run_case_offline


def _compute_stats(values: list[float]) -> dict[str, float]:
    """Compute min/max/mean/stddev for a list of floats."""
    if not values:
        return {"min": 0.0, "max": 0.0, "mean": 0.0, "stddev": 0.0}
    return {
        "min": min(values),
        "max": max(values),
        "mean": statistics.mean(values),
        "stddev": statistics.stdev(values) if len(values) >= 2 else 0.0,
    }


def _compute_tool_overlap(tool_call_sets: list[set[str]]) -> float:
    """Compute intersection/union ratio across k runs' tool call sets."""
    if not tool_call_sets:
        return 1.0
    union = set()
    intersection: set[str] | None = None
    for s in tool_call_sets:
        union |= s
        intersection = s if intersection is None else intersection & s
    if not union:
        return 1.0
    return len(intersection or set()) / len(union)


def run_stability(
    case: GoldenCase,
    executor: GoldenCaseExecutor,
    k: int = 3,
) -> StabilityMetrics:
    """Run a single case k times and compute stability metrics."""
    executions: list[EvalExecution] = []
    case_results: list[CaseResult] = []

    for _ in range(k):
        start = time.monotonic()
        execution = executor(case)
        executions.append(execution)
        result = run_case_offline(
            case,
            execution.state,
            execution.tool_calls,
            execution.responses,
            stats=execution.stats,
        )
        result.duration_ms = (time.monotonic() - start) * 1000
        case_results.append(result)

    # pass_rate
    pass_count = sum(1 for r in case_results if r.passed)
    pass_rate = pass_count / k

    # assertion_consistency — per-assertion pass rate across k runs
    assertion_consistency: dict[str, float] = {}
    for assertion in case.assertions:
        key = f"{assertion.type.value}:{assertion.target}"
        passes = 0
        for exc in executions:
            ok, _ = evaluate_assertion(
                assertion, exc.state, exc.tool_calls, exc.responses
            )
            if ok:
                passes += 1
        assertion_consistency[key] = passes / k

    # tool_overlap_ratio
    tool_sets = [set(exc.tool_calls) for exc in executions]
    tool_overlap_ratio = _compute_tool_overlap(tool_sets)

    # cost_stats and duration_stats
    costs = [r.stats.get("estimated_cost_usd", 0.0) for r in case_results]
    durations = [r.duration_ms for r in case_results]

    return StabilityMetrics(
        case_id=case.id,
        k=k,
        pass_rate=pass_rate,
        assertion_consistency=assertion_consistency,
        tool_overlap_ratio=tool_overlap_ratio,
        cost_stats=_compute_stats(costs),
        duration_stats=_compute_stats(durations),
        runs=case_results,
    )


def run_stability_suite(
    cases: list[GoldenCase],
    executor: GoldenCaseExecutor,
    k: int = 3,
) -> StabilitySuiteResult:
    """Run all cases through stability evaluation and aggregate results."""
    if not cases:
        return StabilitySuiteResult(total_cases=0, k=k)

    results: list[StabilityMetrics] = []
    for case in cases:
        metrics = run_stability(case, executor, k=k)
        results.append(metrics)

    rates = [m.pass_rate for m in results]
    overall = statistics.mean(rates) if rates else 0.0
    unstable = [m.case_id for m in results if m.pass_rate < 1.0]
    highly_unstable = [m.case_id for m in results if m.pass_rate < 0.6]

    return StabilitySuiteResult(
        total_cases=len(cases),
        k=k,
        results=results,
        overall_pass_rate=overall,
        unstable_cases=unstable,
        highly_unstable_cases=highly_unstable,
    )
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_stability.py -v`
Expected: All tests PASS

- [ ] **Step 3: Run full regression**

Run: `cd backend && python -m pytest tests/ -q`
Expected: All existing tests still pass, no new failures

- [ ] **Step 4: Commit**

```bash
git add backend/evals/stability.py
git commit -m "feat(evals): implement run_stability and run_stability_suite"
```

---

### Task 4: Write failing tests for save_stability_report

**Files:**
- Modify: `backend/tests/test_stability.py` (append)

- [ ] **Step 1: Append report tests to test_stability.py**

Add to end of `backend/tests/test_stability.py`:

```python
import json
from evals.stability import save_stability_report


class TestSaveStabilityReport:
    def _make_suite(self) -> StabilitySuiteResult:
        """Build a minimal suite result for report testing."""
        metrics = StabilityMetrics(
            case_id="easy-001",
            k=3,
            pass_rate=1.0,
            assertion_consistency={"state_field_set:destination": 1.0, "tool_called:search_flights": 1.0},
            tool_overlap_ratio=1.0,
            cost_stats={"min": 0.08, "max": 0.12, "mean": 0.10, "stddev": 0.02},
            duration_stats={"min": 100.0, "max": 300.0, "mean": 200.0, "stddev": 100.0},
            runs=[],
        )
        unstable_metrics = StabilityMetrics(
            case_id="hard-001",
            k=3,
            pass_rate=0.33,
            assertion_consistency={"tool_called:search_flights": 0.33},
            tool_overlap_ratio=0.5,
            cost_stats={"min": 0.20, "max": 0.30, "mean": 0.25, "stddev": 0.05},
            duration_stats={"min": 500.0, "max": 900.0, "mean": 700.0, "stddev": 200.0},
            runs=[],
        )
        return StabilitySuiteResult(
            total_cases=2,
            k=3,
            results=[metrics, unstable_metrics],
            overall_pass_rate=0.665,
            unstable_cases=["hard-001"],
            highly_unstable_cases=["hard-001"],
        )

    def test_json_report_written(self, tmp_path):
        suite = self._make_suite()
        json_path, md_path = save_stability_report(suite, tmp_path / "report")

        assert json_path.exists()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["k"] == 3
        assert data["total_cases"] == 2
        assert data["overall_pass_rate"] == pytest.approx(0.665)
        assert len(data["results"]) == 2
        assert data["results"][0]["case_id"] == "easy-001"

    def test_markdown_report_written(self, tmp_path):
        suite = self._make_suite()
        json_path, md_path = save_stability_report(suite, tmp_path / "report")

        assert md_path.exists()
        content = md_path.read_text(encoding="utf-8")
        assert "pass@k 稳定性评估报告" in content
        assert "easy-001" in content
        assert "hard-001" in content
        assert "0.33" in content

    def test_markdown_contains_high_variance_section(self, tmp_path):
        suite = self._make_suite()
        _, md_path = save_stability_report(suite, tmp_path / "report")

        content = md_path.read_text(encoding="utf-8")
        assert "高方差断言" in content
        assert "tool_called:search_flights" in content

    def test_creates_parent_directories(self, tmp_path):
        suite = self._make_suite()
        nested = tmp_path / "a" / "b" / "report"
        json_path, md_path = save_stability_report(suite, nested)
        assert json_path.exists()
        assert md_path.exists()
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `cd backend && python -m pytest tests/test_stability.py::TestSaveStabilityReport -v`
Expected: FAIL with `ImportError: cannot import name 'save_stability_report'`

- [ ] **Step 3: Commit failing tests**

```bash
git add backend/tests/test_stability.py
git commit -m "test(evals): add failing tests for stability report generation"
```

---

### Task 5: Implement save_stability_report

**Files:**
- Modify: `backend/evals/stability.py` (append)

- [ ] **Step 1: Append save_stability_report to stability.py**

Add to end of `backend/evals/stability.py`:

```python
def _stability_metrics_to_dict(m: StabilityMetrics) -> dict[str, Any]:
    """Serialize a StabilityMetrics to a JSON-safe dict."""
    return {
        "case_id": m.case_id,
        "k": m.k,
        "pass_rate": m.pass_rate,
        "assertion_consistency": m.assertion_consistency,
        "tool_overlap_ratio": m.tool_overlap_ratio,
        "cost_stats": m.cost_stats,
        "duration_stats": m.duration_stats,
        "runs": [
            {
                "case_id": r.case_id,
                "passed": r.passed,
                "assertions_passed": r.assertions_passed,
                "assertions_total": r.assertions_total,
                "failures": r.failures,
                "duration_ms": round(r.duration_ms, 1),
                "error": r.error,
                "stats": r.stats,
            }
            for r in m.runs
        ],
    }


def save_stability_report(
    suite: StabilitySuiteResult,
    output_path: str | Path,
) -> tuple[Path, Path]:
    """Write JSON + Markdown stability reports.

    Returns (json_path, md_path).
    """
    base = Path(output_path)
    base.parent.mkdir(parents=True, exist_ok=True)

    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")

    # --- JSON ---
    payload = {
        "total_cases": suite.total_cases,
        "k": suite.k,
        "overall_pass_rate": suite.overall_pass_rate,
        "unstable_cases": suite.unstable_cases,
        "highly_unstable_cases": suite.highly_unstable_cases,
        "results": [_stability_metrics_to_dict(m) for m in suite.results],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # --- Markdown ---
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = [
        "# pass@k 稳定性评估报告",
        "",
        f"- 运行时间：{now}",
        f"- k = {suite.k}",
        f"- 覆盖 case 数：{suite.total_cases}",
        "",
        "## 总览",
        "",
        "| 指标 | 值 |",
        "|------|-----|",
        f"| 总体 pass@k | {suite.overall_pass_rate:.2f} |",
        f"| 不稳定 case（pass_rate < 1.0） | {len(suite.unstable_cases)} |",
        f"| 高度不稳定 case（pass_rate < 0.6） | {len(suite.highly_unstable_cases)} |",
        "",
        "## 按 Case 详情",
        "",
        f"| Case | 难度 | pass@{suite.k} | 不稳定断言 | 工具一致性 | 平均成本 |",
        "|------|------|--------|-----------|-----------|---------|",
    ]

    for m in suite.results:
        difficulty = ""
        if m.runs:
            difficulty = m.runs[0].difficulty
        pass_count = int(m.pass_rate * m.k + 0.5)
        unstable_assertions = [
            f"{key} ({val:.1f})"
            for key, val in m.assertion_consistency.items()
            if val < 1.0
        ]
        unstable_str = ", ".join(unstable_assertions) if unstable_assertions else "—"
        cost_mean = m.cost_stats.get("mean", 0.0)
        lines.append(
            f"| {m.case_id} | {difficulty} | {pass_count}/{m.k} "
            f"| {unstable_str} | {m.tool_overlap_ratio:.2f} | ${cost_mean:.2f} |"
        )

    # High variance assertions section
    high_var: list[tuple[str, str, float]] = []
    for m in suite.results:
        for key, val in m.assertion_consistency.items():
            if val < 0.6:
                high_var.append((m.case_id, key, val))

    if high_var:
        lines.extend([
            "",
            "## 高方差断言（一致性 < 0.6）",
            "",
            "| Case | 断言 | 一致性 | 说明 |",
            "|------|------|--------|------|",
        ])
        for case_id, key, val in high_var:
            k = suite.k
            pass_n = int(val * k + 0.5)
            lines.append(f"| {case_id} | {key} | {val:.2f} | {pass_n}/{k} 次通过 |")

    # Cost & latency table
    lines.extend([
        "",
        "## 成本与延迟统计",
        "",
        "| Case | 成本 min | 成本 max | 成本 stddev | 延迟 mean | 延迟 stddev |",
        "|------|---------|---------|------------|----------|------------|",
    ])
    for m in suite.results:
        cs = m.cost_stats
        ds = m.duration_stats
        lines.append(
            f"| {m.case_id} | ${cs['min']:.2f} | ${cs['max']:.2f} "
            f"| ${cs['stddev']:.2f} | {ds['mean']:.0f}ms | {ds['stddev']:.0f}ms |"
        )
    lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")

    return json_path, md_path
```

- [ ] **Step 2: Run report tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_stability.py::TestSaveStabilityReport -v`
Expected: All 4 tests PASS

- [ ] **Step 3: Run full test suite**

Run: `cd backend && python -m pytest tests/ -q`
Expected: All pass, no regressions

- [ ] **Step 4: Commit**

```bash
git add backend/evals/stability.py
git commit -m "feat(evals): implement save_stability_report with JSON + Markdown output"
```

---

### Task 6: Create CLI script

**Files:**
- Create: `scripts/eval-stability.py`

- [ ] **Step 1: Create the CLI script**

Create `scripts/eval-stability.py`:

```python
#!/usr/bin/env python3
"""pass@k stability evaluation — run golden cases multiple times and report consistency.

Usage:
    python scripts/eval-stability.py --k 3 --base-url http://127.0.0.1:8000
    python scripts/eval-stability.py --cases easy-001,hard-001 --k 5
    python scripts/eval-stability.py --difficulty easy,medium --k 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from evals.models import EvalExecution, GoldenCase
from evals.runner import load_golden_cases
from evals.stability import run_stability_suite, save_stability_report

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
GOLDEN_CASES_DIR = BACKEND_DIR / "evals" / "golden_cases"


def create_session(client: httpx.Client) -> str:
    resp = client.post("/api/sessions")
    resp.raise_for_status()
    return resp.json()["session_id"]


def send_message(client: httpx.Client, session_id: str, message: str) -> list[str]:
    """Send a chat message via SSE and collect response chunks."""
    responses: list[str] = []
    with client.stream(
        "POST",
        f"/api/chat/{session_id}",
        json={"message": message},
        timeout=180.0,
    ) as stream:
        stream.raise_for_status()
        for line in stream.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            etype = event.get("type", "")
            if etype in {"text", "text_delta"} and event.get("content"):
                responses.append(event["content"])
    return responses


def get_plan_state(client: httpx.Client, session_id: str) -> dict:
    resp = client.get(f"/api/plan/{session_id}")
    resp.raise_for_status()
    return resp.json()


def get_session_stats(client: httpx.Client, session_id: str) -> dict:
    resp = client.get(f"/api/sessions/{session_id}/stats")
    resp.raise_for_status()
    return resp.json()


def get_messages(client: httpx.Client, session_id: str) -> list[dict]:
    resp = client.get(f"/api/messages/{session_id}")
    resp.raise_for_status()
    return resp.json()


def extract_tool_calls(messages: list[dict]) -> list[str]:
    """Extract unique tool names from stored messages."""
    tool_names: list[str] = []
    for msg in messages:
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                name = tc.get("function", {}).get("name") or tc.get("name", "")
                if name and name not in tool_names:
                    tool_names.append(name)
    return tool_names


def make_live_executor(client: httpx.Client) -> callable:
    """Build an executor that runs a case against the live backend."""

    def executor(case: GoldenCase) -> EvalExecution:
        session_id = create_session(client)
        all_responses: list[str] = []

        for msg in case.messages:
            if msg["role"] == "user":
                chunks = send_message(client, session_id, msg["content"])
                all_responses.extend(chunks)

        state = get_plan_state(client, session_id)
        try:
            stats = get_session_stats(client, session_id)
        except Exception:
            stats = {}
        try:
            messages = get_messages(client, session_id)
        except Exception:
            messages = []

        tool_calls = extract_tool_calls(messages)
        full_text = " ".join(all_responses)

        return EvalExecution(
            state=state,
            tool_calls=tool_calls,
            responses=[full_text] if full_text else [],
            stats=stats,
        )

    return executor


def ensure_backend_ready(client: httpx.Client, base_url: str) -> None:
    try:
        health = client.get("/health")
        health.raise_for_status()
        if health.json().get("status") != "ok":
            raise RuntimeError("health endpoint did not return status=ok")
    except (httpx.RequestError, httpx.HTTPStatusError, RuntimeError) as exc:
        print(f"ERROR: Backend not ready at {base_url}")
        print(f"Reason: {exc}")
        print("Start with: scripts/dev.sh")
        sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="pass@k stability evaluation for golden cases"
    )
    parser.add_argument("--k", type=int, default=3, help="runs per case (default: 3)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="backend URL")
    parser.add_argument("--cases", default="", help="comma-separated case IDs (default: all)")
    parser.add_argument("--difficulty", default="", help="comma-separated difficulties to filter")
    parser.add_argument(
        "--output",
        default="docs/eval-stability-report",
        help="output path prefix (default: docs/eval-stability-report)",
    )
    args = parser.parse_args()

    all_cases = load_golden_cases(str(GOLDEN_CASES_DIR))
    print(f"Loaded {len(all_cases)} golden cases")

    # Filter by case IDs
    if args.cases:
        ids = {c.strip() for c in args.cases.split(",")}
        all_cases = [c for c in all_cases if c.id in ids]

    # Filter by difficulty
    if args.difficulty:
        diffs = {d.strip() for d in args.difficulty.split(",")}
        all_cases = [c for c in all_cases if c.difficulty in diffs]

    if not all_cases:
        print("ERROR: No cases match the given filters")
        return 1

    print(f"Running stability evaluation: {len(all_cases)} cases × k={args.k}")

    with httpx.Client(base_url=args.base_url, timeout=30.0) as client:
        ensure_backend_ready(client, args.base_url)
        executor = make_live_executor(client)
        suite = run_stability_suite(all_cases, executor, k=args.k)

    json_path, md_path = save_stability_report(suite, args.output)
    print(f"\nJSON report: {json_path}")
    print(f"Markdown report: {md_path}")

    # Summary
    print(f"\n{'=' * 60}")
    print(f"pass@{args.k} SUMMARY")
    print(f"{'=' * 60}")
    print(f"Overall pass rate: {suite.overall_pass_rate:.2f}")
    print(f"Unstable cases: {len(suite.unstable_cases)}")
    print(f"Highly unstable cases: {len(suite.highly_unstable_cases)}")
    for m in suite.results:
        pass_n = int(m.pass_rate * m.k + 0.5)
        emoji = "✅" if m.pass_rate == 1.0 else ("⚠️" if m.pass_rate >= 0.6 else "❌")
        print(f"  {emoji} {m.case_id}: {pass_n}/{m.k} (cost: ${m.cost_stats['mean']:.2f})")

    if suite.highly_unstable_cases:
        print(f"\n❌ {len(suite.highly_unstable_cases)} highly unstable case(s) — exit 1")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Verify --help works**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro && python scripts/eval-stability.py --help`
Expected: Usage text with --k, --base-url, --cases, --difficulty, --output flags

- [ ] **Step 3: Commit**

```bash
git add scripts/eval-stability.py
git commit -m "feat(scripts): add eval-stability.py CLI for pass@k evaluation"
```

---

### Task 7: Final verification and cleanup

- [ ] **Step 1: Run full backend test suite**

Run: `cd backend && python -m pytest tests/ -q`
Expected: All tests pass, including new stability tests

- [ ] **Step 2: Verify --help output**

Run: `python scripts/eval-stability.py --help`
Expected: Clean help text

- [ ] **Step 3: Update PROJECT_OVERVIEW.md**

Add a brief section about stability evaluation under the evals section of `PROJECT_OVERVIEW.md`:

> **pass@k 稳定性评估**：`backend/evals/stability.py` 提供 `run_stability` / `run_stability_suite` 函数，对同一 golden case 运行 k 次，计算 pass_rate、断言一致性、工具重叠率、成本/延迟统计。CLI 入口 `scripts/eval-stability.py`。

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "docs: update PROJECT_OVERVIEW.md with pass@k stability evaluation"
```
