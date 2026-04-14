# Travel Agent Pro

A full-stack AI travel planning system powered by a hand-crafted Agent Loop with a production Phase 1/3/5/7 planning path and a **5-layer harness architecture** ensuring safety, correctness, and quality at every step. Built from scratch without LangChain or other agent frameworks.

**Quality at Scale:** 590+ tests, 15 executable golden eval cases, JSON eval reports, cost/latency tracking per session.

## Architecture

```
User <-> React Frontend <-> FastAPI Gateway <-> Agent Loop <-> LLM (OpenAI / Anthropic)
                                                   |
                                              Tool Engine
                                                   |
                            +-----------+----------+----------+-----------+
                            |           |          |          |           |
                        Search      Flights     POI Info    Weather    Summary
                      Destinations    Hotels      Routes
```

### Production Planning Path

| Phase | Name | Purpose | Tools |
|-------|------|---------|-------|
| 1 | Inspiration & Destination Lock | Narrow vague intent into a destination | `xiaohongshu_search`, `web_search`, `quick_travel_search` |
| 3 | Framework Planning | Build trip brief, candidate pool, skeletons, transport and lodging locks | `search_flights`, `search_trains`, `search_accommodations`, `get_poi_info`, `calculate_route` |
| 5 | Daily Itinerary Assembly | Expand the selected skeleton into day-by-day plans and validate constraints | `assemble_day_plan`, `check_availability`, `check_weather`, `generate_summary` |
| 7 | Pre-Departure Checklist | Final checklist and handoff | `generate_summary` |

The `PhaseRouter` manages transitions automatically based on plan state completeness. Phase 3 has four substeps (`brief`, `candidate`, `skeleton`, `lock`) with progressively opened tools.

### Core Components

- **Agent Loop** (`agent/loop.py`) — The central orchestrator. Runs a think-act-observe cycle: sends messages to LLM, intercepts tool calls, executes tools, feeds results back. Supports SSE streaming.
- **Tool Engine** (`tools/engine.py`) — Registry + dispatcher. Tools are plain async functions decorated with `@tool`. JSON Schema is auto-generated from type hints.
- **State Manager** (`state/manager.py`) — Maintains `TravelPlanState` (destinations, flights, hotels, itinerary, budget) with snapshot history for undo.
- **Context Manager** (`context/manager.py`) — Assembles 4-layer system messages: soul identity, phase prompt, current state summary, user memory.
- **Memory Manager** (`memory/manager.py`) — Persists user preferences and trip history across sessions as JSON.
- **Harness** (`harness/`) — Hard constraint validator + soft quality scoring via LLM judge prompt.

### 5-Layer Harness Architecture

```
Input → Guardrail → Agent Loop → Validator → Judge → Output
  │         │                         │         │
  │    Chinese injection           Hard        Soft quality
  │    detection, length          constraint    scoring
  │    limits, struct             checks        (LLM-based)
  │    validation
  │
  ├── Feasibility Gate (Phase 1→3)
  │     Rule-based budget/duration checks
  │     30+ destination lookup tables
  │
  └── Cost & Latency Tracking
        Per-session token usage, model pricing
        Tool call duration monitoring
```

Each layer operates independently:
- **Guardrail** — Input sanitization: Chinese prompt injection detection (6 patterns), message length limits (5000 chars), required field validation
- **Validator** — Hard constraint enforcement: budget overruns, date conflicts, null safety guards
- **Judge** — LLM-based quality scoring [1-5] with score clamping and parse failure logging
- **Feasibility Gate** — Rule-based infeasibility detection before expensive planning (30+ destination cost/duration tables)
- **Cost Tracker** — Per-session token usage extraction (OpenAI + Anthropic), model pricing estimation, tool call duration monitoring
- **Eval Runner** — YAML golden cases execute through an injectable case executor, then produce state/tool/text assertion results and JSON reports

## Tech Stack

**Backend** — Python 3.12+
- FastAPI + Uvicorn (SSE streaming via `sse-starlette`)
- OpenAI SDK / Anthropic SDK (dual provider support)
- Pydantic v2 for all data models
- OpenTelemetry + Jaeger (local tracing and span event inspection)
- pytest + pytest-asyncio (590+ tests)

**Frontend** — TypeScript + React 19
- Vite 6 dev server with API proxy
- Leaflet / react-leaflet for interactive maps
- Server-Sent Events for real-time streaming
- Dark theme UI

## Quick Start

### Prerequisites

- Python >= 3.12
- Node.js >= 18
- An OpenAI or Anthropic API key

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Create .env with your API keys
cat > .env << 'EOF'
DEFAULT_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o
# Optional: custom base URL
# OPENAI_BASE_URL=https://api.openai.com/v1

# Or use Anthropic:
# DEFAULT_PROVIDER=anthropic
# ANTHROPIC_API_KEY=sk-ant-...
# ANTHROPIC_MODEL=claude-sonnet-4-20250514

# Optional domain tool keys
# OPENWEATHER_API_KEY=...
# TAVILY_API_KEY=...
EOF

uvicorn main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
# Opens at http://localhost:5173, proxies /api to backend
```

### Run Tests

```bash
cd backend
pytest                # 590+ tests
pytest --cov          # with coverage
```

## Failure Analysis

The repo includes a reproducible failure-analysis workflow for the P0 gap scenarios:

```bash
python scripts/failure-analysis/run_and_analyze.py
npx playwright test scripts/failure-analysis/capture_screenshots.ts --config=playwright.config.ts
```

Committed analysis lives in `docs/learning/2026-04-13-失败案例分析.md`. Local run artifacts land in:

- `docs/learning/2026-04-13-失败案例分析.md` — scenario-by-scenario taxonomy, root cause analysis, and remediation status
- `scripts/failure-analysis/results/failure-results.json` — raw execution results, generated locally and ignored by git
- `screenshots/failure-analysis/` — one screenshot per failure scenario

## Demo Recording

The demo workflow is **deterministic scripted playback**, not a live LLM-dependent run. It needs the frontend dev server, then replays the visible Phase 1 → Phase 3 → Phase 5 → backtrack story from a fixed fixture so recording output stays stable.

```bash
scripts/demo/run-all-demos.sh
```

Artifacts land in `screenshots/demos/`:

- `phase1-recommendations.png`
- `phase3-planning.png`
- `phase5-backtrack-change-preference.png`
- `demo-full-flow.webm`

## Observability

The backend ships with OpenTelemetry tracing enabled by default. Local traces can
be viewed in Jaeger, including Phase B span events for tool inputs/outputs, LLM
request summaries, phase snapshots, and context compression decisions.

### Start Jaeger Locally

```bash
docker compose -f docker-compose.observability.yml up -d
```

Jaeger UI: `http://localhost:16686`

### Telemetry Config

`backend/config.py` loads these defaults when `config.yaml` does not override
them:

| Field | Default | Description |
|-------|---------|-------------|
| `telemetry.enabled` | `true` | Enable OpenTelemetry instrumentation |
| `telemetry.endpoint` | `http://localhost:4317` | OTLP gRPC endpoint |
| `telemetry.service_name` | `travel-agent-pro` | Service name shown in Jaeger |

Example `config.yaml` override:

```yaml
telemetry:
  enabled: true
  endpoint: http://localhost:4317
  service_name: travel-agent-pro
```

### Inspect Phase B Span Events

1. Start Jaeger with `docker compose -f docker-compose.observability.yml up -d`
2. Start the backend with `cd backend && uvicorn main:app --reload --port 8000`
3. Trigger one chat request from the frontend or API
4. Open Jaeger and search for service `travel-agent-pro`
5. Open a trace and inspect the span `Logs` / `Events` section

You should see event payloads like:

- `tool.execute`: `tool.input`, `tool.output`
- `llm.chat`: `llm.request`, `llm.response`
- `phase.transition`: `phase.plan_snapshot`
- `context.should_compress`: `context.compression`

Large string payloads are truncated before export to avoid oversized event data.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DEFAULT_PROVIDER` | Yes | `openai` or `anthropic` |
| `OPENAI_API_KEY` | If using OpenAI | API key |
| `OPENAI_MODEL` | No | Model name (default: `gpt-4o`) |
| `OPENAI_BASE_URL` | No | Custom API endpoint |
| `ANTHROPIC_API_KEY` | If using Anthropic | API key |
| `ANTHROPIC_MODEL` | No | Model name (default: `claude-sonnet-4-20250514`) |
| `ANTHROPIC_BASE_URL` | No | Custom API endpoint |
| `OPENWEATHER_API_KEY` | No | For weather/feasibility tools |
| `TAVILY_API_KEY` | No | For web search fallback |

## Project Structure

```
travel_agent_pro/
├── backend/
│   ├── main.py              # FastAPI app (health, sessions, plan, chat SSE)
│   ├── config.py            # Config loading (.env + YAML + env var overrides)
│   ├── agent/               # Agent loop, message types, hook system
│   ├── llm/                 # LLM providers (OpenAI, Anthropic) + factory
│   ├── state/               # TravelPlanState model + state manager
│   ├── tools/               # 24+ domain tools with @tool decorator
│   ├── phase/               # Phase prompts + PhaseRouter
│   ├── context/             # 4-layer system message assembly + soul.md
│   ├── memory/              # User preference & trip history persistence
│   ├── harness/             # 5-layer harness: guardrail, validator, judge, feasibility
│   ├── telemetry/           # OTel tracing + SessionStats cost/latency tracking
│   ├── evals/               # Executable eval pipeline (15 golden cases, JSON reports)
│   └── tests/               # 590+ tests (pytest-asyncio)
├── frontend/
│   ├── src/
│   │   ├── App.tsx          # Main layout (chat + info panel)
│   │   ├── hooks/useSSE.ts  # SSE streaming hook
│   │   └── components/      # ChatPanel, MapView, Timeline, BudgetChart, etc.
│   └── vite.config.ts       # Dev proxy to backend
├── docs/
│   └── learning/
│       └── 2026-04-13-失败案例分析.md  # Failure taxonomy and root-cause report for P0 gap scenarios
├── scripts/
│   ├── failure-analysis/    # Live scenario runner + screenshot capture harness
│   └── demo/                # Demo seed helper, scripted fixture, Playwright recording spec
├── screenshots/
│   ├── failure-analysis/    # Failure scenario screenshots
│   └── demos/               # Demo PNGs + final .webm recording
├── docker-compose.observability.yml # Local Jaeger all-in-one
└── config.yaml              # Optional YAML config (env vars take precedence)
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/api/sessions` | Create a new session |
| GET | `/api/sessions/{id}/plan` | Get current travel plan state |
| GET | `/api/sessions/{id}/stats` | Session cost/token/latency statistics |
| POST | `/api/sessions/{id}/chat` | SSE streaming chat |

## License

MIT
