# Trace Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an in-browser Trace Viewer that visualizes Agent execution chains (LLM calls, tool calls, state changes) in the frontend right panel, powered by a new backend Trace API.

**Architecture:** Backend exposes `GET /api/sessions/{session_id}/trace` that rebuilds execution trace from the in-memory `sessions` dict (SessionStats + messages). Frontend adds a new `TraceViewer` component as a collapsible section in the right panel, with summary bar, iteration timeline, tool waterfall, and state diff.

**Tech Stack:** FastAPI (backend), React + TypeScript + CSS (frontend). No new dependencies.

**Worktree:** `.worktrees/trace-viewer` (branch `feature/trace-viewer`)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `backend/api/__init__.py` | Create | Make `backend/api/` a Python package |
| `backend/api/trace.py` | Create | Trace API endpoint — rebuilds execution trace from session data |
| `backend/tests/test_trace_api.py` | Create | Unit tests for trace endpoint |
| `backend/main.py` | Modify (~L1643) | Register trace_router (2 lines) |
| `frontend/src/types/trace.ts` | Create | TypeScript interfaces for trace data |
| `frontend/src/hooks/useTrace.ts` | Create | React hook for fetching trace data |
| `frontend/src/components/TraceViewer.tsx` | Create | Main trace viewer component with sub-components |
| `frontend/src/styles/trace-viewer.css` | Create | Styles following Solstice design system |
| `frontend/src/App.tsx` | Modify | Add TraceViewer to right panel with tab toggle |

---

### Task 1: Backend — Trace API Types and Endpoint

**Files:**
- Create: `backend/api/__init__.py`
- Create: `backend/api/trace.py`
- Test: `backend/tests/test_trace_api.py`

- [ ] **Step 1: Create `backend/api/__init__.py`**

```python
# empty file — makes backend/api/ a Python package
```

- [ ] **Step 2: Write the failing test for trace endpoint**

Create `backend/tests/test_trace_api.py`:

```python
import pytest
from httpx import AsyncClient, ASGITransport
from main import create_app
from telemetry.stats import SessionStats


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return create_app()


def _get_sessions(app) -> dict:
    """Extract the sessions dict from app closure — same pattern as test_api.py."""
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not hasattr(endpoint, "__closure__"):
            continue
        free_vars = getattr(endpoint.__code__, "co_freevars", ())
        for name, cell in zip(free_vars, endpoint.__closure__ or ()):
            if name == "sessions":
                return cell.cell_contents
    raise RuntimeError("Cannot locate sessions dict")


@pytest.mark.asyncio
async def test_trace_not_found(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions/nonexistent/trace")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_trace_empty_session(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == session_id
    assert data["total_iterations"] == 0
    assert data["iterations"] == []
    assert data["summary"]["llm_call_count"] == 0
    assert data["summary"]["tool_call_count"] == 0


@pytest.mark.asyncio
async def test_trace_with_stats(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

    sessions = _get_sessions(app)
    stats: SessionStats = sessions[session_id]["stats"]
    stats.record_llm_call(
        provider="openai", model="gpt-4o",
        input_tokens=3200, output_tokens=450,
        duration_ms=2100.0, phase=1, iteration=0,
    )
    stats.record_tool_call(
        tool_name="web_search", duration_ms=800.0,
        status="success", error_code=None, phase=1,
    )
    stats.record_tool_call(
        tool_name="update_plan_state", duration_ms=12.0,
        status="success", error_code=None, phase=1,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == session_id
    assert data["summary"]["llm_call_count"] == 1
    assert data["summary"]["tool_call_count"] == 2
    assert data["summary"]["total_input_tokens"] == 3200
    assert data["summary"]["total_output_tokens"] == 450
    assert len(data["summary"]["by_model"]) == 1
    assert "gpt-4o" in data["summary"]["by_model"]
    assert len(data["summary"]["by_tool"]) == 2


@pytest.mark.asyncio
async def test_trace_iterations_ordered(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

    sessions = _get_sessions(app)
    stats: SessionStats = sessions[session_id]["stats"]
    # Two LLM calls = two iterations
    stats.record_llm_call(
        provider="openai", model="gpt-4o",
        input_tokens=1000, output_tokens=200,
        duration_ms=1000.0, phase=1, iteration=0,
    )
    stats.record_tool_call(
        tool_name="web_search", duration_ms=500.0,
        status="success", error_code=None, phase=1,
    )
    stats.record_llm_call(
        provider="anthropic", model="claude-sonnet-4-20250514",
        input_tokens=2000, output_tokens=400,
        duration_ms=1500.0, phase=1, iteration=1,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    data = resp.json()
    assert data["total_iterations"] == 2
    iters = data["iterations"]
    assert len(iters) == 2
    assert iters[0]["index"] == 1
    assert iters[0]["llm_call"]["model"] == "gpt-4o"
    assert iters[1]["index"] == 2
    assert iters[1]["llm_call"]["model"] == "claude-sonnet-4-20250514"


@pytest.mark.asyncio
async def test_trace_tool_side_effects(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

    sessions = _get_sessions(app)
    stats: SessionStats = sessions[session_id]["stats"]
    stats.record_llm_call(
        provider="openai", model="gpt-4o",
        input_tokens=500, output_tokens=100,
        duration_ms=800.0, phase=1, iteration=0,
    )
    stats.record_tool_call(
        tool_name="web_search", duration_ms=300.0,
        status="success", error_code=None, phase=1,
    )
    stats.record_tool_call(
        tool_name="update_plan_state", duration_ms=10.0,
        status="success", error_code=None, phase=1,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    data = resp.json()
    tools = data["iterations"][0]["tool_calls"]
    assert len(tools) == 2
    ws = next(t for t in tools if t["name"] == "web_search")
    ups = next(t for t in tools if t["name"] == "update_plan_state")
    assert ws["side_effect"] == "read"
    assert ups["side_effect"] == "write"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_trace_api.py -v --tb=short`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.trace'` or similar

- [ ] **Step 4: Implement `backend/api/trace.py`**

```python
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from telemetry.stats import SessionStats

# Known write-effect tools — matches tools/*.py where side_effect="write"
_WRITE_TOOLS = frozenset({
    "update_plan_state",
    "assemble_day_plan",
    "generate_summary",
})

trace_router = APIRouter()


def _build_trace(session_id: str, session: dict) -> dict:
    stats: SessionStats = session.get("stats", SessionStats())
    summary = stats.to_dict()

    # Enrich summary with cost per model
    for model_name, model_data in summary.get("by_model", {}).items():
        pricing = None
        from telemetry.stats import _lookup_pricing
        pricing = _lookup_pricing(model_name)
        if pricing:
            cost = (model_data["input_tokens"] / 1_000_000) * pricing["input"]
            cost += (model_data["output_tokens"] / 1_000_000) * pricing["output"]
            model_data["cost_usd"] = round(cost, 6)
        else:
            model_data["cost_usd"] = 0.0

    # Enrich by_tool with avg_duration_ms
    for tool_data in summary.get("by_tool", {}).values():
        calls = tool_data.get("calls", 0)
        total_dur = tool_data.get("duration_ms", 0.0)
        tool_data["total_duration_ms"] = total_dur
        tool_data["avg_duration_ms"] = round(total_dur / calls, 1) if calls > 0 else 0.0

    # Build iterations from LLM calls
    iterations = []
    llm_calls = stats.llm_calls
    tool_calls = list(stats.tool_calls)

    # Group tool calls by associating them with the nearest preceding LLM call
    # Each LLM call starts a new iteration; tools between two LLM calls belong
    # to the earlier one (by timestamp)
    tool_idx = 0
    for i, llm in enumerate(llm_calls):
        next_llm_ts = llm_calls[i + 1].timestamp if i + 1 < len(llm_calls) else float("inf")
        iter_tools = []
        while tool_idx < len(tool_calls) and tool_calls[tool_idx].timestamp < next_llm_ts:
            tc = tool_calls[tool_idx]
            iter_tools.append({
                "name": tc.tool_name,
                "duration_ms": round(tc.duration_ms, 1),
                "status": tc.status,
                "side_effect": "write" if tc.tool_name in _WRITE_TOOLS else "read",
                "arguments_preview": "",
                "result_preview": "",
            })
            tool_idx += 1

        pricing = None
        from telemetry.stats import _lookup_pricing
        pricing = _lookup_pricing(llm.model)
        cost = 0.0
        if pricing:
            cost = (llm.input_tokens / 1_000_000) * pricing["input"]
            cost += (llm.output_tokens / 1_000_000) * pricing["output"]

        iterations.append({
            "index": i + 1,
            "phase": llm.phase,
            "llm_call": {
                "provider": llm.provider,
                "model": llm.model,
                "input_tokens": llm.input_tokens,
                "output_tokens": llm.output_tokens,
                "duration_ms": round(llm.duration_ms, 1),
                "cost_usd": round(cost, 6),
            },
            "tool_calls": iter_tools,
            "state_changes": [],
            "compression_event": None,
        })

    # Any remaining tool calls without a preceding LLM call — shouldn't happen
    # but handle gracefully
    if tool_idx < len(tool_calls) and not iterations:
        remaining_tools = []
        while tool_idx < len(tool_calls):
            tc = tool_calls[tool_idx]
            remaining_tools.append({
                "name": tc.tool_name,
                "duration_ms": round(tc.duration_ms, 1),
                "status": tc.status,
                "side_effect": "write" if tc.tool_name in _WRITE_TOOLS else "read",
                "arguments_preview": "",
                "result_preview": "",
            })
            tool_idx += 1
        iterations.append({
            "index": 1,
            "phase": remaining_tools[0]["name"] if remaining_tools else 0,
            "llm_call": None,
            "tool_calls": remaining_tools,
            "state_changes": [],
            "compression_event": None,
        })

    return {
        "session_id": session_id,
        "total_iterations": len(iterations),
        "summary": summary,
        "iterations": iterations,
    }
```

- [ ] **Step 5: Add the route handler in `trace.py`**

Append to `backend/api/trace.py` — this is the actual route, which requires access to the `sessions` dict. Since `sessions` is a closure inside `create_app()`, we use a factory pattern:

Actually, looking at the codebase, all routes are defined inside `create_app()` as closures. The trace API must also be defined there to access `sessions`. So instead of a separate router file, we'll define the endpoint directly in `main.py` using the helper function from `api/trace.py`.

Revise: `backend/api/trace.py` exports only `_build_trace()`. The route registration happens in `main.py`.

Final `backend/api/trace.py`:

```python
from __future__ import annotations

from telemetry.stats import SessionStats, _lookup_pricing

# Known write-effect tools — matches tools/*.py where side_effect="write"
_WRITE_TOOLS = frozenset({
    "update_plan_state",
    "assemble_day_plan",
    "generate_summary",
})


def build_trace(session_id: str, session: dict) -> dict:
    """Build a structured trace from a session's stats data."""
    stats: SessionStats = session.get("stats", SessionStats())
    summary = stats.to_dict()

    # Enrich summary with cost per model
    for model_name, model_data in summary.get("by_model", {}).items():
        pricing = _lookup_pricing(model_name)
        if pricing:
            cost = (model_data["input_tokens"] / 1_000_000) * pricing["input"]
            cost += (model_data["output_tokens"] / 1_000_000) * pricing["output"]
            model_data["cost_usd"] = round(cost, 6)
        else:
            model_data["cost_usd"] = 0.0

    # Enrich by_tool with avg_duration_ms
    for tool_data in summary.get("by_tool", {}).values():
        calls = tool_data.get("calls", 0)
        total_dur = tool_data.get("duration_ms", 0.0)
        tool_data["total_duration_ms"] = total_dur
        tool_data["avg_duration_ms"] = round(total_dur / calls, 1) if calls > 0 else 0.0

    # Build iterations — each LLM call starts a new iteration
    iterations = []
    llm_calls = stats.llm_calls
    tool_calls = list(stats.tool_calls)

    tool_idx = 0
    for i, llm in enumerate(llm_calls):
        next_llm_ts = llm_calls[i + 1].timestamp if i + 1 < len(llm_calls) else float("inf")
        iter_tools = []
        while tool_idx < len(tool_calls) and tool_calls[tool_idx].timestamp < next_llm_ts:
            tc = tool_calls[tool_idx]
            iter_tools.append({
                "name": tc.tool_name,
                "duration_ms": round(tc.duration_ms, 1),
                "status": tc.status,
                "side_effect": "write" if tc.tool_name in _WRITE_TOOLS else "read",
                "arguments_preview": "",
                "result_preview": "",
            })
            tool_idx += 1

        pricing = _lookup_pricing(llm.model)
        cost = 0.0
        if pricing:
            cost = (llm.input_tokens / 1_000_000) * pricing["input"]
            cost += (llm.output_tokens / 1_000_000) * pricing["output"]

        iterations.append({
            "index": i + 1,
            "phase": llm.phase,
            "llm_call": {
                "provider": llm.provider,
                "model": llm.model,
                "input_tokens": llm.input_tokens,
                "output_tokens": llm.output_tokens,
                "duration_ms": round(llm.duration_ms, 1),
                "cost_usd": round(cost, 6),
            },
            "tool_calls": iter_tools,
            "state_changes": [],
            "compression_event": None,
        })

    return {
        "session_id": session_id,
        "total_iterations": len(iterations),
        "summary": summary,
        "iterations": iterations,
    }
```

- [ ] **Step 6: Register the trace endpoint in `backend/main.py`**

Add **before** `return app` (around line 1643):

```python
    from api.trace import build_trace

    @app.get("/api/sessions/{session_id}/trace")
    async def get_session_trace(session_id: str):
        session = sessions.get(session_id)
        if not session:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        return build_trace(session_id, session)
```

This follows the existing pattern (see `get_session_stats` at line 1187).

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_trace_api.py -v --tb=short`
Expected: All 5 tests PASS

- [ ] **Step 8: Commit**

```bash
git add backend/api/__init__.py backend/api/trace.py backend/tests/test_trace_api.py backend/main.py
git commit -m "feat(trace): add GET /api/sessions/{id}/trace endpoint

Builds structured execution trace from SessionStats data.
Returns iterations with LLM calls, tool calls, and summary stats."
```

---

### Task 2: Frontend — Type Definitions

**Files:**
- Create: `frontend/src/types/trace.ts`

- [ ] **Step 1: Create `frontend/src/types/trace.ts`**

```typescript
export interface TraceToolCall {
  name: string
  duration_ms: number
  status: 'success' | 'error' | 'skipped'
  side_effect: 'read' | 'write'
  arguments_preview: string
  result_preview: string
}

export interface StateChange {
  field: string
  before: unknown
  after: unknown
}

export interface TraceIteration {
  index: number
  phase: number
  llm_call: {
    provider: string
    model: string
    input_tokens: number
    output_tokens: number
    duration_ms: number
    cost_usd: number
  } | null
  tool_calls: TraceToolCall[]
  state_changes: StateChange[]
  compression_event: string | null
}

export interface TraceSummary {
  total_input_tokens: number
  total_output_tokens: number
  total_llm_duration_ms: number
  total_tool_duration_ms: number
  estimated_cost_usd: number
  llm_call_count: number
  tool_call_count: number
  by_model: Record<string, {
    calls: number
    input_tokens: number
    output_tokens: number
    cost_usd: number
  }>
  by_tool: Record<string, {
    calls: number
    total_duration_ms: number
    avg_duration_ms: number
  }>
}

export interface SessionTrace {
  session_id: string
  total_iterations: number
  summary: TraceSummary
  iterations: TraceIteration[]
}
```

- [ ] **Step 2: Run typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/types/trace.ts
git commit -m "feat(trace): add TypeScript type definitions for trace data"
```

---

### Task 3: Frontend — useTrace Hook

**Files:**
- Create: `frontend/src/hooks/useTrace.ts`

- [ ] **Step 1: Create `frontend/src/hooks/useTrace.ts`**

```typescript
import { useState, useEffect, useCallback } from 'react'
import type { SessionTrace } from '../types/trace'

export function useTrace(sessionId: string | null, refreshTrigger?: number) {
  const [trace, setTrace] = useState<SessionTrace | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!sessionId) {
      setTrace(null)
      return
    }
    setLoading(true)
    setError(null)
    try {
      const resp = await fetch(`/api/sessions/${sessionId}/trace`)
      if (!resp.ok) {
        throw new Error(`Failed to fetch trace: ${resp.status}`)
      }
      const data = (await resp.json()) as SessionTrace
      setTrace(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Unknown error')
      setTrace(null)
    } finally {
      setLoading(false)
    }
  }, [sessionId])

  useEffect(() => {
    void refresh()
  }, [refresh, refreshTrigger])

  return { trace, loading, error, refresh }
}
```

- [ ] **Step 2: Run typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/hooks/useTrace.ts
git commit -m "feat(trace): add useTrace hook for fetching trace data"
```

---

### Task 4: Frontend — TraceViewer Component

**Files:**
- Create: `frontend/src/components/TraceViewer.tsx`
- Create: `frontend/src/styles/trace-viewer.css`

- [ ] **Step 1: Create `frontend/src/styles/trace-viewer.css`**

```css
/* Trace Viewer — Solstice design system, dark glass style */

.trace-viewer {
  padding: 0;
}

.trace-viewer .trace-empty {
  text-align: center;
  color: var(--text-secondary);
  padding: 32px 16px;
  font-size: 13px;
}

.trace-viewer .trace-loading {
  text-align: center;
  color: var(--text-secondary);
  padding: 32px 16px;
  font-size: 13px;
}

.trace-viewer .trace-error {
  text-align: center;
  color: var(--red, #ef4444);
  padding: 16px;
  font-size: 13px;
}

/* Summary Bar */
.trace-summary {
  display: flex;
  gap: 8px;
  padding: 12px;
  flex-wrap: wrap;
}

.trace-summary-card {
  flex: 1;
  min-width: 80px;
  background: var(--bg-secondary, rgba(255, 255, 255, 0.04));
  border-radius: 8px;
  padding: 8px 10px;
  text-align: center;
}

.trace-summary-card .card-value {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}

.trace-summary-card .card-label {
  font-size: 10px;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-top: 2px;
}

/* Iteration List */
.trace-iterations {
  padding: 0 12px 12px;
}

/* Iteration Row */
.trace-iteration {
  border: 1px solid var(--border, rgba(255, 255, 255, 0.06));
  border-radius: 8px;
  margin-bottom: 6px;
  overflow: hidden;
  transition: border-color 0.15s ease;
}

.trace-iteration:hover {
  border-color: var(--border-hover, rgba(255, 255, 255, 0.12));
}

.trace-iteration-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  cursor: pointer;
  user-select: none;
}

.trace-iteration-header:hover {
  background: var(--bg-secondary, rgba(255, 255, 255, 0.02));
}

.iter-index {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  min-width: 24px;
}

.iter-phase {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--accent-amber, #f59e0b);
  color: #000;
  font-weight: 600;
}

.iter-bar-container {
  flex: 1;
  height: 6px;
  background: var(--bg-secondary, rgba(255, 255, 255, 0.04));
  border-radius: 3px;
  overflow: hidden;
}

.iter-bar {
  height: 100%;
  border-radius: 3px;
  transition: width 0.3s ease;
}

.iter-bar.provider-openai {
  background: #10a37f;
}

.iter-bar.provider-anthropic {
  background: #d4a574;
}

.iter-bar.provider-default {
  background: var(--accent-amber, #f59e0b);
}

.iter-model {
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
}

.iter-tokens {
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.iter-cost {
  font-size: 11px;
  color: var(--accent-amber, #f59e0b);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.iter-expand-icon {
  font-size: 10px;
  color: var(--text-secondary);
  transition: transform 0.15s ease;
}

.iter-expand-icon.expanded {
  transform: rotate(90deg);
}

/* Expanded detail */
.trace-iteration-detail {
  padding: 0 10px 10px;
  border-top: 1px solid var(--border, rgba(255, 255, 255, 0.06));
}

/* Tool Calls */
.trace-tool-list {
  padding: 8px 0 0;
}

.trace-tool-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 0;
  font-size: 12px;
}

.tool-name {
  font-weight: 500;
  color: var(--text-primary);
  min-width: 100px;
}

.tool-side-effect {
  font-size: 9px;
  padding: 1px 4px;
  border-radius: 3px;
  font-weight: 600;
  text-transform: uppercase;
}

.tool-side-effect.read {
  background: rgba(59, 130, 246, 0.15);
  color: #60a5fa;
}

.tool-side-effect.write {
  background: rgba(245, 158, 11, 0.15);
  color: #fbbf24;
}

.tool-bar-container {
  flex: 1;
  height: 4px;
  background: var(--bg-secondary, rgba(255, 255, 255, 0.04));
  border-radius: 2px;
  overflow: hidden;
}

.tool-bar {
  height: 100%;
  border-radius: 2px;
}

.tool-bar.status-success {
  background: var(--green, #22c55e);
}

.tool-bar.status-error {
  background: var(--red, #ef4444);
}

.tool-bar.status-skipped {
  background: var(--text-secondary, #6b7280);
}

.tool-duration {
  font-size: 11px;
  color: var(--text-secondary);
  min-width: 50px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

/* State Diff */
.trace-state-diff {
  margin-top: 8px;
}

.trace-state-diff-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}

.state-change-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 3px 6px;
  border-radius: 4px;
  font-size: 12px;
  margin-bottom: 2px;
}

.state-change-row.added {
  background: rgba(34, 197, 94, 0.08);
}

.state-change-row.modified {
  background: rgba(245, 158, 11, 0.08);
}

.state-field {
  font-weight: 500;
  color: var(--text-primary);
  min-width: 80px;
}

.state-arrow {
  color: var(--text-secondary);
}

.state-value {
  color: var(--text-secondary);
  font-family: monospace;
  font-size: 11px;
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.state-value.before {
  text-decoration: line-through;
  opacity: 0.6;
}

.state-value.after {
  color: var(--text-primary);
}

/* No tools message */
.trace-no-tools {
  font-size: 12px;
  color: var(--text-secondary);
  padding: 4px 0;
  font-style: italic;
}
```

- [ ] **Step 2: Create `frontend/src/components/TraceViewer.tsx`**

```tsx
import { useState } from 'react'
import { useTrace } from '../hooks/useTrace'
import type { TraceIteration, TraceToolCall, StateChange } from '../types/trace'
import '../styles/trace-viewer.css'

interface TraceViewerProps {
  sessionId: string | null
  refreshTrigger?: number
}

function formatTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

function formatDuration(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.round(ms)}ms`
}

function formatCost(usd: number): string {
  if (usd < 0.001) return '<$0.001'
  return `$${usd.toFixed(3)}`
}

function getProviderClass(provider: string): string {
  const p = provider.toLowerCase()
  if (p.includes('openai') || p === 'openai') return 'provider-openai'
  if (p.includes('anthropic') || p === 'anthropic') return 'provider-anthropic'
  return 'provider-default'
}

function SummaryBar({ summary }: { summary: { total_input_tokens: number; total_output_tokens: number; total_llm_duration_ms: number; total_tool_duration_ms: number; estimated_cost_usd: number; llm_call_count: number; tool_call_count: number } }) {
  return (
    <div className="trace-summary">
      <div className="trace-summary-card">
        <div className="card-value">{formatTokens(summary.total_input_tokens + summary.total_output_tokens)}</div>
        <div className="card-label">Tokens</div>
      </div>
      <div className="trace-summary-card">
        <div className="card-value">{formatCost(summary.estimated_cost_usd)}</div>
        <div className="card-label">Cost</div>
      </div>
      <div className="trace-summary-card">
        <div className="card-value">{formatDuration(summary.total_llm_duration_ms + summary.total_tool_duration_ms)}</div>
        <div className="card-label">Duration</div>
      </div>
      <div className="trace-summary-card">
        <div className="card-value">{summary.llm_call_count}</div>
        <div className="card-label">LLM</div>
      </div>
      <div className="trace-summary-card">
        <div className="card-value">{summary.tool_call_count}</div>
        <div className="card-label">Tools</div>
      </div>
    </div>
  )
}

function ToolCallRow({ tool, maxDuration }: { tool: TraceToolCall; maxDuration: number }) {
  const widthPct = maxDuration > 0 ? (tool.duration_ms / maxDuration) * 100 : 0
  return (
    <div className="trace-tool-row">
      <span className="tool-name">{tool.name}</span>
      <span className={`tool-side-effect ${tool.side_effect}`}>{tool.side_effect}</span>
      <div className="tool-bar-container">
        <div
          className={`tool-bar status-${tool.status}`}
          style={{ width: `${Math.max(widthPct, 2)}%` }}
        />
      </div>
      <span className="tool-duration">{formatDuration(tool.duration_ms)}</span>
    </div>
  )
}

function StateDiffPanel({ changes }: { changes: StateChange[] }) {
  if (changes.length === 0) return null
  return (
    <div className="trace-state-diff">
      <div className="trace-state-diff-title">State Changes</div>
      {changes.map((change, i) => {
        const isNew = change.before === null || change.before === undefined
        return (
          <div key={i} className={`state-change-row ${isNew ? 'added' : 'modified'}`}>
            <span className="state-field">{change.field}</span>
            {!isNew && (
              <>
                <span className="state-value before">{JSON.stringify(change.before)}</span>
                <span className="state-arrow">→</span>
              </>
            )}
            <span className="state-value after">{JSON.stringify(change.after)}</span>
          </div>
        )
      })}
    </div>
  )
}

function IterationRow({ iteration, maxLLMDuration }: { iteration: TraceIteration; maxLLMDuration: number }) {
  const [expanded, setExpanded] = useState(false)
  const llm = iteration.llm_call
  const barPct = llm && maxLLMDuration > 0 ? (llm.duration_ms / maxLLMDuration) * 100 : 0
  const maxToolDuration = Math.max(...iteration.tool_calls.map((t) => t.duration_ms), 1)

  return (
    <div className="trace-iteration">
      <div className="trace-iteration-header" onClick={() => setExpanded(!expanded)}>
        <span className="iter-index">#{iteration.index}</span>
        <span className="iter-phase">P{iteration.phase}</span>
        {llm && (
          <>
            <div className="iter-bar-container">
              <div
                className={`iter-bar ${getProviderClass(llm.provider)}`}
                style={{ width: `${Math.max(barPct, 3)}%` }}
              />
            </div>
            <span className="iter-model">{llm.model}</span>
            <span className="iter-tokens">{formatTokens(llm.input_tokens + llm.output_tokens)}</span>
            <span className="iter-cost">{formatCost(llm.cost_usd)}</span>
          </>
        )}
        <span className={`iter-expand-icon ${expanded ? 'expanded' : ''}`}>▶</span>
      </div>
      {expanded && (
        <div className="trace-iteration-detail">
          {iteration.tool_calls.length > 0 ? (
            <div className="trace-tool-list">
              {iteration.tool_calls.map((tool, i) => (
                <ToolCallRow key={i} tool={tool} maxDuration={maxToolDuration} />
              ))}
            </div>
          ) : (
            <div className="trace-no-tools">No tool calls</div>
          )}
          <StateDiffPanel changes={iteration.state_changes} />
        </div>
      )}
    </div>
  )
}

export default function TraceViewer({ sessionId, refreshTrigger }: TraceViewerProps) {
  const { trace, loading, error } = useTrace(sessionId, refreshTrigger)

  if (loading) {
    return <div className="trace-viewer"><div className="trace-loading">Loading trace…</div></div>
  }

  if (error) {
    return <div className="trace-viewer"><div className="trace-error">{error}</div></div>
  }

  if (!trace || trace.total_iterations === 0) {
    return <div className="trace-viewer"><div className="trace-empty">暂无 trace 数据</div></div>
  }

  const maxLLMDuration = Math.max(
    ...trace.iterations.map((it) => it.llm_call?.duration_ms ?? 0),
    1,
  )

  return (
    <div className="trace-viewer">
      <SummaryBar summary={trace.summary} />
      <div className="trace-iterations">
        {trace.iterations.map((iteration) => (
          <IterationRow
            key={iteration.index}
            iteration={iteration}
            maxLLMDuration={maxLLMDuration}
          />
        ))}
      </div>
    </div>
  )
}
```

- [ ] **Step 3: Run typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/TraceViewer.tsx frontend/src/styles/trace-viewer.css
git commit -m "feat(trace): add TraceViewer component with summary, iterations, and tool waterfall"
```

---

### Task 5: Frontend — Integrate TraceViewer into App.tsx

**Files:**
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Add TraceViewer to App.tsx right panel**

Add import at the top (after other component imports, around line 7):

```typescript
import TraceViewer from './components/TraceViewer'
```

Add a state variable for tracking the active right-panel tab (after line 58, near `bootstrapping` state):

```typescript
const [rightTab, setRightTab] = useState<'plan' | 'trace'>('plan')
const [traceTrigger, setTraceTrigger] = useState(0)
```

Add a `handleDone` callback that increments the trace trigger (after `handlePlanUpdate` around line 115):

```typescript
const handleTraceTrigger = useCallback(() => {
  setTraceTrigger((n) => n + 1)
}, [])
```

In the right panel section (around line 210), wrap existing content in a tab structure. Replace lines 210-250 with:

```tsx
        <div className="right-panel">
          <div className="right-panel-tabs">
            <button
              className={`right-tab ${rightTab === 'plan' ? 'active' : ''}`}
              onClick={() => setRightTab('plan')}
            >
              Plan
            </button>
            <button
              className={`right-tab ${rightTab === 'trace' ? 'active' : ''}`}
              onClick={() => setRightTab('trace')}
            >
              Trace
            </button>
          </div>
          {rightTab === 'plan' && (
            <>
              {plan && plan.destination && (
                <div className="destination-banner">
                  <div className="dest-label">目的地</div>
                  <div className="dest-name">{plan.destination}</div>
                  {plan.dates && (
                    <div className="dest-dates">{plan.dates.start} → {plan.dates.end}</div>
                  )}
                  <div className="dest-meta">
                    {plan.budget && (
                      <div className="dest-chip">
                        预算 ¥{plan.budget.total.toLocaleString()}
                      </div>
                    )}
                    {plan.accommodation && (
                      <div className="dest-chip">
                        住宿 {plan.accommodation.hotel ?? plan.accommodation.area}
                      </div>
                    )}
                  </div>
                </div>
              )}
              {plan && (
                <>
                  {showPhase3Workbench && (
                    <div className="sidebar-section">
                      <Phase3Workbench plan={plan} />
                    </div>
                  )}
                  <div className="sidebar-section">
                    <BudgetChart plan={plan} />
                  </div>
                  <div className="sidebar-section">
                    <MapView dailyPlans={plan.daily_plans} dark={dark} />
                  </div>
                  <div className="sidebar-section">
                    <Timeline dailyPlans={plan.daily_plans} />
                  </div>
                </>
              )}
            </>
          )}
          {rightTab === 'trace' && (
            <TraceViewer sessionId={sessionId} refreshTrigger={traceTrigger} />
          )}
        </div>
```

- [ ] **Step 2: Add tab styles to `frontend/src/styles/trace-viewer.css`**

Append to the end of `trace-viewer.css`:

```css
/* Right Panel Tabs */
.right-panel-tabs {
  display: flex;
  gap: 0;
  padding: 8px 12px 0;
  border-bottom: 1px solid var(--border, rgba(255, 255, 255, 0.06));
}

.right-tab {
  padding: 6px 16px;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease;
}

.right-tab:hover {
  color: var(--text-primary);
}

.right-tab.active {
  color: var(--text-primary);
  border-bottom-color: var(--accent-amber, #f59e0b);
}
```

- [ ] **Step 3: Run typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 4: Run build**

Run: `cd frontend && npm run build`
Expected: Build succeeds

- [ ] **Step 5: Commit**

```bash
git add frontend/src/App.tsx frontend/src/styles/trace-viewer.css
git commit -m "feat(trace): integrate TraceViewer into right panel with Plan/Trace tabs"
```

---

### Task 6: Verification & Cleanup

**Files:**
- None new. Verification only.

- [ ] **Step 1: Run backend trace tests**

Run: `cd backend && python -m pytest tests/test_trace_api.py -v`
Expected: All 5 tests pass

- [ ] **Step 2: Run frontend typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Run frontend build**

Run: `cd frontend && npm run build`
Expected: Build succeeds with no warnings

- [ ] **Step 4: Verify no unintended file changes**

Run: `git diff --stat HEAD~5..HEAD`
Expected: Only the files listed in the spec's File Manifest are changed/created:
- `backend/api/__init__.py` (new)
- `backend/api/trace.py` (new)
- `backend/tests/test_trace_api.py` (new)
- `backend/main.py` (modified)
- `frontend/src/types/trace.ts` (new)
- `frontend/src/hooks/useTrace.ts` (new)
- `frontend/src/components/TraceViewer.tsx` (new)
- `frontend/src/styles/trace-viewer.css` (new)
- `frontend/src/App.tsx` (modified)

- [ ] **Step 5: Update PROJECT_OVERVIEW.md**

Add a section about the Trace Viewer under the frontend components section and backend API section. Mention:
- `GET /api/sessions/{session_id}/trace` endpoint
- TraceViewer component in right panel
- Plan/Trace tab switch

- [ ] **Step 6: Final commit**

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: update PROJECT_OVERVIEW.md with Trace Viewer"
```
