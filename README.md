# Travel Agent Pro

A full-stack AI travel planning system powered by a hand-crafted Agent Loop with 7-phase cognitive decision flow. Built from scratch without LangChain or other agent frameworks.

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

### 7-Phase Cognitive Decision Flow

| Phase | Name | Purpose | Tools |
|-------|------|---------|-------|
| 1 | Requirements Gathering | Understand travel preferences | - |
| 2 | Destination Research | Search and evaluate destinations | - |
| 3 | Option Evaluation | Compare flights, hotels, activities | `search_flights`, `search_accommodations`, `get_poi_info` |
| 4 | Itinerary Planning | Build day-by-day schedule | `calculate_route`, `assemble_day_plan`, `check_availability` |
| 5 | Validation & Refinement | Verify constraints and quality | `check_weather`, `generate_summary` |
| 6 | ~~Booking~~ | *(skipped — no real transactions)* | - |
| 7 | Presentation | Deliver the final plan | - |

The `PhaseRouter` manages transitions automatically based on plan state completeness. Each phase has its own system prompt, allowed tools, and control mode (auto / confirm).

### Core Components

- **Agent Loop** (`agent/loop.py`) — The central orchestrator. Runs a think-act-observe cycle: sends messages to LLM, intercepts tool calls, executes tools, feeds results back. Supports SSE streaming.
- **Tool Engine** (`tools/engine.py`) — Registry + dispatcher. Tools are plain async functions decorated with `@tool`. JSON Schema is auto-generated from type hints.
- **State Manager** (`state/manager.py`) — Maintains `TravelPlanState` (destinations, flights, hotels, itinerary, budget) with snapshot history for undo.
- **Context Manager** (`context/manager.py`) — Assembles 4-layer system messages: soul identity, phase prompt, current state summary, user memory.
- **Memory Manager** (`memory/manager.py`) — Persists user preferences and trip history across sessions as JSON.
- **Harness** (`harness/`) — Hard constraint validator + soft quality scoring via LLM judge prompt.

## Tech Stack

**Backend** — Python 3.12+
- FastAPI + Uvicorn (SSE streaming via `sse-starlette`)
- OpenAI SDK / Anthropic SDK (dual provider support)
- Pydantic v2 for all data models
- OpenTelemetry + Jaeger (local tracing and span event inspection)
- pytest + pytest-asyncio (164 tests)

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
pytest                # 164 tests
pytest --cov          # with coverage
```

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
│   ├── tools/               # 12 domain tools with @tool decorator
│   ├── phase/               # Phase prompts + PhaseRouter
│   ├── context/             # 4-layer system message assembly + soul.md
│   ├── memory/              # User preference & trip history persistence
│   ├── harness/             # Constraint validator + quality judge
│   ├── telemetry/           # OTel attributes, decorators, setup
│   └── tests/               # 164 tests (pytest-asyncio)
├── frontend/
│   ├── src/
│   │   ├── App.tsx          # Main layout (chat + info panel)
│   │   ├── hooks/useSSE.ts  # SSE streaming hook
│   │   └── components/      # ChatPanel, MapView, Timeline, BudgetChart, etc.
│   └── vite.config.ts       # Dev proxy to backend
├── docker-compose.observability.yml # Local Jaeger all-in-one
└── config.yaml              # Optional YAML config (env vars take precedence)
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/api/sessions` | Create a new session |
| GET | `/api/sessions/{id}/plan` | Get current travel plan state |
| POST | `/api/sessions/{id}/chat` | SSE streaming chat |

## License

MIT
