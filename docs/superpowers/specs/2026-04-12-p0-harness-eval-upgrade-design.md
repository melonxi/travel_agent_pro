# P0 Harness & Eval Upgrade — Design Spec

> **Date**: 2026-04-12
> **Goal**: Execute the P0 improvements from the competitive analysis report v2 to make Travel Agent Pro's quality provable, cost-visible, and infeasibility-aware.

---

## 1. Problem Statement

The competitive analysis report v2 identifies three core gaps preventing the project from standing out in interviews:

1. **Harness quality layer is underpowered** (219 lines vs 1100+ core engine lines) — guardrails only detect English injection, validator has null-pointer risks and late timing, judge silently swallows parse failures.
2. **Agent quality is unquantifiable** — no eval pipeline, no golden cases, no metrics tracking.
3. **Cost/latency invisible** — LLM providers return usage data but it's completely ignored; no session-level stats.
4. **Infeasible tasks undetected** — no pre-check at Phase 1→3 boundary; impossible requests get planned.

---

## 2. Scope

### In Scope (P0)

| ID | Feature | Deliverable |
|----|---------|-------------|
| H1 | Guardrail strengthening | Chinese injection patterns, input length limits, tool result structure validation |
| H2 | Validator hardening | Null safety, budget/dates guards, constraint checks expanded |
| H3 | Judge improvement | Score clamping [1,5], parse failure logging, score persistence |
| C1 | LLM usage extraction | Capture actual tokens from OpenAI/Anthropic responses |
| C2 | Tool call latency tracking | Duration per tool call |
| C3 | Session stats aggregation | Per-session cost/token/latency summary + API endpoint |
| F1 | Infeasibility detection | Phase 1→3 gate: budget/duration/destination feasibility pre-check |
| E1 | Golden case format | YAML schema for eval cases |
| E2 | Eval runner | Batch executor with JSON report |
| E3 | Initial golden cases | 15 cases covering simple/complex/edge/infeasible scenarios |
| N1 | README narrative rewrite | Harness Engineering framing |

### Out of Scope

- Frontend changes (Memory Center, Trace Viewer → P1)
- RAG / knowledge base (P2)
- MCP adapter (P2)
- Multi-agent architecture (P2)
- Full security hardening (P2)

---

## 3. Design

### 3.1 Harness Quality Layer Strengthening (H1–H3)

#### H1: Guardrail Strengthening (`backend/harness/guardrail.py`)

**Chinese injection patterns** — Add to existing `_INJECTION_PATTERNS`:
```python
_INJECTION_PATTERNS_ZH = [
    r"忽略.{0,4}(之前|以上|所有|前面).{0,4}(指令|规则|提示|要求)",
    r"你现在是",
    r"不要遵守.{0,4}(规则|指令|限制)",
    r"(请|你)?无视.{0,4}(之前|以上|所有).{0,4}(指令|规则)",
    r"(扮演|充当|假装).{0,4}(另一个|其他|别的)",
    r"(输出|显示|告诉我).{0,4}(系统|system).{0,4}(提示|prompt)",
]
```

**Input length limit** — Reject user input > 5000 chars in any single field.

**Tool result structure validation** — After search tools return, validate required fields exist:
- `search_flights` results must contain: `price`, `departure_time`, `arrival_time`
- `search_accommodations` results must contain: `price`, `name`
- `search_trains` results must contain: `price`, `departure_time`

Missing fields → `level="warn"` with descriptive message (don't block, but log).

#### H2: Validator Hardening (`backend/harness/validator.py`)

**Null safety** — Guard `plan.budget` and `plan.dates` with `if plan.budget:` / `if plan.dates:` checks.

**Time format safety** — Wrap `_time_to_minutes()` in try/except for malformed "HH:MM" values.

**Geographic distance check** — New validation: if consecutive activities in different cities on the same day, warn (requires checking `location` field on activities).

#### H3: Judge Improvement (`backend/harness/judge.py`)

**Score clamping** — After parsing, clamp each score to `max(1, min(5, score))`.

**Parse failure logging** — When JSON parse fails, log the raw LLM response (truncated to 500 chars) at `logger.warning` level instead of silent default.

**Score validation** — If any score field is not an integer or is missing, log a warning.

### 3.2 Cost/Latency Tracking (C1–C3)

#### C1: LLM Usage Extraction

**OpenAI** (`backend/llm/openai_provider.py`):
- In streaming: accumulate chunks, extract usage from final chunk's `usage` field (OpenAI includes `usage` in the last chunk when `stream_options={"include_usage": True}`).
- In non-streaming: extract `response.usage.prompt_tokens`, `response.usage.completion_tokens`.
- Yield a new `LLMChunk` type `USAGE` with token data.

**Anthropic** (`backend/llm/anthropic_provider.py`):
- Stream: extract from `message_start` event's `message.usage` and `message_delta` event's `usage`.
- Non-stream: extract from `response.usage.input_tokens`, `response.usage.output_tokens`.
- Yield same `USAGE` chunk type.

#### C2: Tool Call Latency

**In `backend/tools/engine.py`**:
- Record `start_time = time.monotonic()` before each tool execution.
- Record `duration_ms = (time.monotonic() - start_time) * 1000` after.
- Attach to `ToolResult` as new field `duration_ms: float | None = None`.

#### C3: Session Stats

**New data model** (`backend/telemetry/stats.py`):
```python
@dataclass
class LLMCallRecord:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: float
    phase: int
    iteration: int
    timestamp: float

@dataclass
class ToolCallRecord:
    tool_name: str
    duration_ms: float
    status: str
    error_code: str | None
    phase: int
    timestamp: float

@dataclass
class SessionStats:
    llm_calls: list[LLMCallRecord]
    tool_calls: list[ToolCallRecord]

    @property
    def total_input_tokens(self) -> int: ...
    @property
    def total_output_tokens(self) -> int: ...
    @property
    def total_llm_duration_ms(self) -> float: ...
    @property
    def total_tool_duration_ms(self) -> float: ...
    @property
    def estimated_cost_usd(self) -> float: ...
    def to_dict(self) -> dict: ...
```

**Pricing table** (hardcoded, easily updatable):
```python
PRICING = {
    "gpt-4o": {"input": 2.50, "output": 10.00},       # per 1M tokens
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
}
```

**API endpoint** (`GET /api/sessions/{session_id}/stats`):
- Returns `SessionStats.to_dict()` with breakdowns by phase, model, tool.

**Integration**:
- `SessionStats` instance stored in `sessions[session_id]["stats"]`.
- Agent loop records LLM calls after each `chat()` call.
- ToolEngine records tool calls after each `execute()` call.

### 3.3 Infeasibility Detection (F1)

**New module** (`backend/harness/feasibility.py`):

```python
@dataclass
class FeasibilityResult:
    feasible: bool
    warnings: list[str]
    blockers: list[str]

def check_feasibility(plan: TravelPlanState) -> FeasibilityResult:
    """Rule-based feasibility pre-check at Phase 1→3 boundary."""
```

**Rules**:

1. **Budget floor check**: Known minimum daily costs per destination region.
   - Lookup table: `{"日本": 500, "马尔代夫": 1500, "泰国": 300, ...}` (CNY/day/person)
   - If `budget.total / travelers.total / dates.total_days < min_daily_cost` → blocker
   - If not enough data (no budget yet), skip check

2. **Duration minimum check**: Known minimum stay per destination.
   - `{"日本": 3, "马尔代夫": 4, "欧洲多国": 7, ...}` (days)
   - If `dates.total_days < min_days` → warning

3. **Impossible combination check**:
   - Budget < 1000 CNY total for international travel → blocker
   - 1-day international trip → blocker

**Integration**:
- Register as a `before_phase_transition` gate in `main.py`.
- Only fires when `from_phase=1, to_phase=3`.
- If blockers exist: return `GateResult(allowed=False, feedback=formatted_message)`.
- If only warnings: return `GateResult(allowed=True, feedback=formatted_warnings)`.
- Agent loop will inject the feedback into conversation, allowing the agent to communicate infeasibility to user.

### 3.4 Eval Pipeline (E1–E3)

#### E1: Golden Case YAML Format

**Directory**: `evals/golden_cases/`

```yaml
# evals/golden_cases/simple_domestic.yaml
id: simple-domestic-3day-beijing
name: "3天北京自由行"
description: "Simple domestic trip, straightforward constraints"
difficulty: easy

inputs:
  - role: user
    content: "我想去北京玩3天，预算5000元，两个大人"
  - role: user
    content: "确定去北京"

expected:
  final_phase: 3
  state_fields:
    destination: "北京"
    travelers:
      adults: 2
    budget:
      total: 5000

  required_tools: ["web_search"]
  forbidden_tools: ["search_flights"]  # domestic, no flights needed initially

  hard_constraints:
    - budget_not_exceeded: true

assertions:
  - type: state_field_set
    field: destination
    value_contains: "北京"
  - type: phase_reached
    phase: 3
  - type: tool_called
    tool: update_plan_state
    min_calls: 1
```

#### E2: Eval Runner (`evals/runner.py`)

```python
class EvalRunner:
    """Execute golden cases against the agent and generate reports."""

    async def run_case(self, case: GoldenCase) -> CaseResult:
        """Run a single golden case through the agent loop."""

    async def run_suite(self, cases: list[GoldenCase]) -> SuiteResult:
        """Run all cases and aggregate metrics."""

    def generate_report(self, result: SuiteResult) -> dict:
        """Generate JSON report with metrics."""
```

**Metrics collected per case**:
- `task_completion`: Did it reach expected phase?
- `state_accuracy`: Are expected state fields correctly set?
- `tool_selection_accuracy`: Were required tools called? Were forbidden tools avoided?
- `hard_constraint_pass`: Did all hard constraints pass?
- `step_count`: Total iterations
- `total_tokens`: Input + output tokens
- `total_duration_ms`: Wall clock time
- `estimated_cost_usd`: Based on pricing table

**Aggregate metrics in report**:
- `pass_rate`: % of cases fully passing
- `hard_constraint_rate`: % of cases passing hard constraints
- `avg_tokens`: Average token usage
- `avg_cost`: Average cost per case
- `avg_duration`: Average latency

**Report format**: JSON file at `evals/reports/eval-{timestamp}.json`

#### E3: Initial Golden Cases (15 cases)

| ID | Scenario | Difficulty | Key Test |
|----|----------|-----------|----------|
| simple-domestic-3day | 3天北京自由行 | easy | Basic Phase 1 completion |
| simple-domestic-budget | 2000元杭州周末游 | easy | Tight budget handling |
| international-japan-5day | 5天日本自由行 | medium | International + flights |
| international-family | 家庭日本7天亲子游 | medium | Travelers with children |
| budget-tight-japan | 3000元5天日本 | hard | Very tight budget |
| elderly-altitude | 带80岁老人去九寨沟 | hard | Special needs awareness |
| infeasible-budget | 500元马尔代夫5星7天 | infeasible | Must detect impossibility |
| infeasible-duration | 1天欧洲5国游 | infeasible | Must detect impossibility |
| multi-turn-change | 东京改大阪 | medium | Backtrack handling |
| dietary-constraint | 3人三亚含素食者 | medium | Dietary constraint tracking |
| multi-city-domestic | 北京+上海5天 | medium | Multi-destination |
| vague-intent | "想出去玩" | easy | Handles vague input |
| peak-season | 春节三亚 | medium | Peak season pricing |
| accessibility | 轮椅用户京都游 | hard | Accessibility needs |
| long-trip | 15天欧洲多国 | hard | Complex multi-destination |

### 3.5 README Narrative Rewrite (N1)

Rewrite the README opening section to use the **Harness Engineering** framing from the competitive report. Key structural changes:

1. **Lead with Harness Engineering** concept, not feature list
2. **Architecture diagram** showing 5-layer harness structure
3. **Key engineering decisions** section explaining why (not just what)
4. **Eval & quality** section with metrics (once eval pipeline exists)
5. Keep existing setup/usage sections, update test count to 543

---

## 4. Architecture Impact

```
backend/
├── harness/
│   ├── guardrail.py        # MODIFY: +Chinese patterns, +length limits, +struct validation
│   ├── validator.py         # MODIFY: +null safety, +time format safety
│   ├── judge.py             # MODIFY: +score clamping, +parse logging
│   └── feasibility.py       # NEW: Phase 1→3 feasibility pre-check
├── llm/
│   ├── openai_provider.py   # MODIFY: +usage extraction
│   ├── anthropic_provider.py # MODIFY: +usage extraction
│   └── types.py             # MODIFY: +USAGE chunk type
├── tools/
│   └── engine.py            # MODIFY: +duration_ms tracking
├── agent/
│   └── types.py             # MODIFY: +duration_ms on ToolResult
├── telemetry/
│   └── stats.py             # NEW: SessionStats, LLMCallRecord, ToolCallRecord
├── main.py                  # MODIFY: +stats endpoint, +feasibility gate, +usage recording
└── tests/
    ├── test_guardrail.py    # MODIFY: +Chinese injection tests
    ├── test_validator.py    # MODIFY: +null safety tests
    ├── test_judge.py        # MODIFY: +clamping tests
    ├── test_feasibility.py  # NEW: feasibility check tests
    └── test_stats.py        # NEW: stats tracking tests

evals/
├── golden_cases/            # NEW: 15 YAML case files
├── runner.py                # NEW: eval execution engine
├── models.py                # NEW: GoldenCase, CaseResult, SuiteResult
├── reports/                 # NEW: generated eval reports
└── README.md                # NEW: how to run evals

README.md                    # MODIFY: Harness Engineering narrative
```

---

## 5. Testing Strategy

Each module gets targeted unit tests:

| Module | New Tests | Key Scenarios |
|--------|-----------|---------------|
| guardrail.py | 8+ | Chinese injection detection, length limits, struct validation |
| validator.py | 5+ | Null budget/dates, malformed time, geographic distance |
| judge.py | 4+ | Score clamping, parse failure logging, edge values |
| feasibility.py | 8+ | Budget floor, duration minimum, impossible combos, partial data |
| stats.py | 6+ | Token recording, cost calculation, summary aggregation |
| openai_provider | 2+ | Usage extraction from stream/non-stream |
| anthropic_provider | 2+ | Usage extraction from stream/non-stream |
| engine.py | 2+ | Duration tracking |

All tests use existing pytest + pytest-asyncio framework. No new test dependencies needed.

---

## 6. Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Feasibility lookup table incomplete | Start with top 20 destinations; use conservative thresholds; skip check if destination unknown |
| Eval cases depend on LLM output (non-deterministic) | Use temperature=0 for eval runs; assertions check structure not exact content |
| Cost pricing outdated | Pricing table is a simple dict, trivially updatable |
| Chinese injection patterns too broad | Test with benign Chinese sentences to avoid false positives |

---

## 7. Implementation Order

1. **Harness hardening** (H1-H3) — foundational, no dependencies
2. **Cost/latency tracking** (C1-C3) — independent, enables eval metrics
3. **Infeasibility detection** (F1) — uses hook system, independent
4. **Eval pipeline** (E1-E3) — depends on stats being available for cost metrics
5. **README narrative** (N1) — last, references completed features
