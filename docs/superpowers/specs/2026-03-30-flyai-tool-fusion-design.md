# FlyAI Tool Fusion Design

> Integrate Fliggy FlyAI skill capabilities into the travel agent's existing tool layer via multi-source data fusion, while adding FlyAI-exclusive tools for meta-search and travel services.

## Context

The project is a multi-phase travel planning agent with 11 tools backed by Amadeus, Google Maps, and OpenWeather APIs. Fliggy recently released FlyAI (`alibaba-flyai/flyai-skill`), an open-source travel skill providing real-time bookable inventory across flights, hotels, attractions, visa services, and more via a Node.js CLI (`flyai-cli`).

FlyAI fills critical gaps in our current tool set: real pricing, booking URLs, ticket purchasing, visa/insurance services, and a 33-category POI taxonomy. This design fuses FlyAI data into existing tools and adds two new tools for capabilities we lack entirely.

## Scope

**In scope:**
- FlyAI CLI async client wrapper
- Multi-source fusion for `search_flights`, `search_accommodations`, `get_poi_info`
- Two new tools: `quick_travel_search` (Phase 2-3), `search_travel_services` (Phase 7)
- Data normalizer layer with merge strategies
- Config extension for FlyAI toggle/timeout/api-key
- Prompt tweaks for Phase 2 and Phase 7
- Full test coverage with mocked subprocess

**Out of scope:**
- Phase flow changes (Phase 1-7 logic untouched)
- Frontend changes
- Memory system changes
- LLM provider changes

## Architecture

### Data flow

```
LLM calls tool (e.g. search_flights)
    |
    v
Tool function (search_flights.py)
    |
    +---> asyncio.gather(
    |       original_source(Amadeus/Google),
    |       flyai_client.search_flight(...)
    |     )
    |
    v
normalizers.py
    |-- normalize_amadeus_flight(raw) -> FlightResult
    |-- normalize_flyai_flight(raw) -> FlightResult
    |-- merge_flights(list_a, list_b) -> merged list
    |
    v
ToolResult(data=merged_results)
    |
    v
LLM sees unified results (source + booking_url fields added)
```

### New file structure

```
backend/tools/
    flyai_client.py              # NEW: async CLI wrapper
    normalizers.py               # NEW: dataclasses + normalize + merge
    quick_travel_search.py       # NEW: meta-search tool
    search_travel_services.py    # NEW: travel services tool
    search_flights.py            # MODIFIED: dual-source fusion
    search_accommodations.py     # MODIFIED: dual-source fusion
    get_poi_info.py              # MODIFIED: dual-source fusion
backend/config.py                # MODIFIED: FlyAIConfig added
config.yaml                     # MODIFIED: flyai section added
backend/main.py                  # MODIFIED: FlyAIClient creation + injection
backend/phase/prompts.py         # MODIFIED: Phase 2,7 prompt additions
```

## Module 1: FlyAI Client (`backend/tools/flyai_client.py`)

### Class: `FlyAIClient`

```python
class FlyAIClient:
    def __init__(self, timeout: int = 30, api_key: str | None = None)

    @property
    def available(self) -> bool
        """True if flyai CLI is found on PATH."""

    async def fast_search(self, query: str) -> list[dict]
    async def search_flight(self, origin: str, **kwargs) -> list[dict]
    async def search_hotels(self, dest_name: str, **kwargs) -> list[dict]
    async def search_poi(self, city_name: str, **kwargs) -> list[dict]
```

### Design decisions

1. **Async subprocess**: Uses `asyncio.create_subprocess_exec` to avoid blocking the agent loop event loop. Each call spawns a short-lived `flyai` process, reads stdout JSON, and returns parsed data.

2. **Graceful degradation**: The `available` property checks `shutil.which("flyai")` once at init time. All public methods return empty lists when `available is False` — no exceptions.

3. **Unified error handling**: Parses the `status` field from flyai output. `status != 0`, JSON parse failures, and timeout all return empty lists with a warning logged to stderr. Never raises into the tool layer.

4. **Timeout protection**: Default 30 seconds via `asyncio.wait_for`. Configurable through `FlyAIConfig.cli_timeout`.

5. **API key forwarding**: If configured, sets `FLYAI_API_KEY` environment variable in the subprocess env.

### Internal method: `_run`

```python
async def _run(self, command: str, **kwargs) -> list[dict]:
    """Execute flyai CLI command, return itemList or empty list."""
    cmd = ["flyai", command]
    for key, value in kwargs.items():
        if value is not None:
            cmd.extend([f"--{key.replace('_', '-')}", str(value)])

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=PIPE, stderr=PIPE, env=self._env
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=self.timeout
    )

    data = json.loads(stdout.decode())
    if data.get("status") != 0:
        return []
    return data.get("data", {}).get("itemList", [])
```

## Module 2: Normalizers (`backend/tools/normalizers.py`)

### Data structures

```python
@dataclass
class FlightResult:
    airline: str
    flight_no: str
    origin: str                    # city name
    destination: str               # city name
    dep_time: str                  # ISO datetime
    arr_time: str                  # ISO datetime
    duration_min: int
    stops: int                     # 0 = direct
    price: float | None
    currency: str                  # "CNY", "USD", etc.
    cabin_class: str
    source: str                    # "amadeus" | "flyai"
    booking_url: str | None

@dataclass
class AccommodationResult:
    name: str
    address: str
    lat: float | None
    lng: float | None
    rating: float | None           # 0-5
    price_per_night: float | None
    currency: str
    star_rating: str | None        # e.g. "豪华型", "4"
    bed_type: str | None
    source: str                    # "google" | "flyai" | "merged"
    booking_url: str | None

@dataclass
class POIResult:
    name: str
    address: str
    lat: float | None
    lng: float | None
    rating: float | None
    category: str | None           # FlyAI 33-category taxonomy
    is_free: bool | None
    ticket_price: float | None
    ticket_url: str | None
    source: str                    # "google" | "flyai" | "merged"
    booking_url: str | None
```

### Normalizer functions

Each source gets its own normalizer that maps raw API response fields to the unified dataclass:

- `normalize_amadeus_flight(raw: dict) -> FlightResult` — maps Amadeus segment data
- `normalize_flyai_flight(raw: dict) -> FlightResult` — maps FlyAI `journeys[0].segments[0]` structure, extracts `jumpUrl` as `booking_url`
- `normalize_google_accommodation(raw: dict) -> AccommodationResult` — maps Google Places fields
- `normalize_flyai_hotel(raw: dict) -> AccommodationResult` — maps FlyAI fields (`mainPic` → ignored, `detailUrl` → `booking_url`, `score` → `rating`)
- `normalize_google_poi(raw: dict) -> POIResult` — maps Google Places detail
- `normalize_flyai_poi(raw: dict) -> POIResult` — maps FlyAI POI fields (`freePoiStatus` → `is_free`, `ticketInfo.price` → `ticket_price`, `jumpUrl` → `ticket_url`)

### Merge functions

```python
def merge_flights(amadeus: list[FlightResult], flyai: list[FlightResult]) -> list[FlightResult]:
    """Deduplicate by (flight_no, dep_date). Prefer record with booking_url."""

def merge_accommodations(google: list[AccommodationResult], flyai: list[AccommodationResult]) -> list[AccommodationResult]:
    """Fuzzy match by name (SequenceMatcher ratio > 0.8).
    Matched pairs: take Google coords + FlyAI price/booking, source='merged'.
    Unmatched: keep as-is from each source."""

def merge_pois(google: list[POIResult], flyai: list[POIResult]) -> list[POIResult]:
    """Same fuzzy-match strategy as accommodations.
    Matched: Google coords/rating + FlyAI ticket/category, source='merged'."""
```

### Serialization

All three dataclasses implement `to_dict() -> dict` for JSON serialization in ToolResult. The `to_dict` output is what the LLM sees.

## Module 3: Tool Fusion (modifications to 3 existing tools)

### 3.1 `search_flights.py`

**Current signature**: `make_search_flights_tool(plan, api_keys) -> ToolDef`

**New signature**: `make_search_flights_tool(plan, api_keys, flyai_client: FlyAIClient | None) -> ToolDef`

**Internal flow**:
1. Build Amadeus query from existing parameters (origin_iata, dest_iata, dep_date)
2. Build FlyAI query: map IATA to city name (small lookup dict or pass through), forward date/budget params
3. `asyncio.gather(amadeus_search(...), flyai_search(...), return_exceptions=True)`
4. Normalize both result sets
5. `merge_flights(amadeus_results, flyai_results)`
6. Return merged list as ToolResult

**Parameter changes to LLM schema**: None. Existing parameters unchanged. The `booking_url` field appears naturally in results.

**Degradation**: If `flyai_client is None` or `flyai_client.available is False`, skip the FlyAI branch entirely. If FlyAI call raises/times out, treat as empty FlyAI results.

### 3.2 `search_accommodations.py`

**New signature**: `make_search_accommodations_tool(plan, api_keys, flyai_client: FlyAIClient | None) -> ToolDef`

**Internal flow**:
1. Google Places search (existing logic)
2. FlyAI `search_hotels(dest_name=destination, check_in_date=..., check_out_date=..., max_price=budget)`
3. Normalize + `merge_accommodations`
4. Return merged list

**Key mapping**: `destination` param → FlyAI `dest_name` (direct pass-through, FlyAI accepts Chinese city names well).

### 3.3 `get_poi_info.py`

**New signature**: `make_get_poi_info_tool(plan, api_keys, flyai_client: FlyAIClient | None) -> ToolDef`

**Internal flow**:
1. Google Places detail (existing logic for coords, rating, hours)
2. FlyAI `search_poi(city_name=city, keyword=poi_name)`
3. Normalize + `merge_pois`
4. Return merged result

## Module 4: New Tools

### 4.1 `quick_travel_search.py`

```python
@tool(
    name="quick_travel_search",
    description="Search across all travel categories (flights, hotels, tickets, tours, visa, etc.) using natural language. Returns a broad overview of available products and prices for trip inspiration and budget estimation.",
    phases=[2, 3],
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language travel query, e.g. 'Hangzhou 3-day trip', 'France visa', 'Shanghai cruise'"
            }
        },
        "required": ["query"]
    }
)
```

**Internal**: Calls `flyai_client.fast_search(query)`. Maps results into a simplified list with inferred `category` field based on title/tags keywords.

**Degradation**: Returns `ToolResult(status="error", suggestion="FlyAI service unavailable. Use search_destinations instead.")` when FlyAI is not available.

### 4.2 `search_travel_services.py`

```python
@tool(
    name="search_travel_services",
    description="Search for travel auxiliary services: visa processing, travel insurance, SIM cards, car rental, airport transfer. Use after itinerary is finalized to recommend practical services.",
    phases=[7],
    parameters={
        "type": "object",
        "properties": {
            "destination": {
                "type": "string",
                "description": "Travel destination"
            },
            "service_type": {
                "type": "string",
                "enum": ["visa", "insurance", "sim_card", "car_rental", "transfer"],
                "description": "Type of service to search"
            }
        },
        "required": ["destination", "service_type"]
    }
)
```

**Internal**: Constructs a query string like `"{destination} 签证办理"` / `"{destination} 旅行保险"` based on `service_type`, then calls `flyai_client.fast_search(query)`. Filters results by relevance.

**Degradation**: Returns `ToolResult(status="error", suggestion="Auxiliary service search unavailable. Suggest user search for services independently.")`.

## Module 5: Configuration

### `FlyAIConfig` dataclass in `backend/config.py`

```python
@dataclass(frozen=True)
class FlyAIConfig:
    enabled: bool = True
    cli_timeout: int = 30
    api_key: str | None = None
```

Added as field on `AppConfig`:

```python
@dataclass(frozen=True)
class AppConfig:
    # ... existing fields ...
    flyai: FlyAIConfig = field(default_factory=FlyAIConfig)
```

### `config.yaml` addition

```yaml
flyai:
  enabled: true
  cli_timeout: 30
  api_key: ${FLYAI_API_KEY}
```

### Degradation chain

1. `flyai.enabled = false` in config → FlyAI never invoked, tools use original sources only
2. `flyai-cli` not installed on system → `FlyAIClient.available = False`, same effect
3. Single FlyAI call timeout/error → that call returns empty, other source unaffected

## Module 6: Prompt Changes

### Phase 2 (`prompts.py`)

Append to existing Phase 2 system prompt:

> You have access to `quick_travel_search` — use it to give the user a quick overview of available products, prices, and travel options for candidate destinations. This helps with budget estimation and destination comparison.

### Phase 7 (`prompts.py`)

Append to existing Phase 7 system prompt:

> You have access to `search_travel_services` — use it to recommend practical services like visa processing, travel insurance, SIM cards, or airport transfers. Include relevant booking links in the final summary.

### Other phases

No prompt changes. Fusion happens transparently inside existing tools.

## Module 7: Tool Registration

In `backend/main.py` `_build_agent()`:

```python
# Create FlyAI client
flyai_client = None
if config.flyai.enabled:
    from tools.flyai_client import FlyAIClient
    flyai_client = FlyAIClient(
        timeout=config.flyai.cli_timeout,
        api_key=config.flyai.api_key
    )

# Register tools (modified signatures for 3 fused tools)
tools = [
    make_search_flights_tool(plan, config.api_keys, flyai_client),
    make_search_accommodations_tool(plan, config.api_keys, flyai_client),
    make_get_poi_info_tool(plan, config.api_keys, flyai_client),
    make_quick_travel_search_tool(flyai_client),          # NEW
    make_search_travel_services_tool(flyai_client),       # NEW
    # ... remaining 8 tools unchanged ...
]
```

### Phase mapping (final)

| Tool | Phases | Change |
|------|--------|--------|
| `search_flights` | [3, 4] | unchanged |
| `search_accommodations` | [3, 4] | unchanged |
| `get_poi_info` | [3, 4, 5] | unchanged |
| `quick_travel_search` | [2, 3] | **new** |
| `search_travel_services` | [7] | **new** |
| all other 8 tools | unchanged | unchanged |

## Module 8: Testing

### Test files

| File | Coverage |
|------|----------|
| `test_flyai_client.py` | CLI wrapper: success, not-installed, timeout, error, empty |
| `test_normalizers.py` | Normalize + merge: field mapping, dedup, fuzzy match, null handling |
| `test_tool_fusion.py` | Fused tools: both succeed, one fails, both fail, FlyAI disabled |
| `test_flyai_new_tools.py` | New tools: normal call, service types, degradation |

### Mock strategy

All tests mock `asyncio.create_subprocess_exec` to simulate flyai CLI output. No real API calls. Consistent with existing test patterns for Amadeus/Google mocks.

### Key test cases (19 total)

**FlyAI Client (5)**:
- CLI returns valid JSON with status=0
- CLI not installed (shutil.which returns None)
- CLI times out (asyncio.TimeoutError)
- CLI returns non-zero exit code
- CLI returns empty itemList

**Normalizers (6)**:
- Amadeus flight → FlightResult mapping
- FlyAI flight → FlightResult mapping (with booking_url)
- Flight dedup: same flight_no keeps booking_url version
- Google + FlyAI hotel merge (name ratio > 0.8)
- FlyAI hotel with null price → price_per_night is None
- Google + FlyAI POI merge (coords from Google, ticket from FlyAI)

**Tool Fusion (5)**:
- Both sources succeed → merged results
- FlyAI fails, original succeeds → original-only results
- Original fails, FlyAI succeeds → FlyAI-only results
- Both fail → ToolResult with error status
- FlyAI disabled in config → original-only, no FlyAI call made

**New Tools (3)**:
- quick_travel_search normal call → categorized results
- search_travel_services per service_type → filtered results
- FlyAI unavailable → error ToolResult with suggestion

## Change Summary

| Category | Files | Est. Lines |
|----------|-------|-----------|
| New source files | 4 | ~500 |
| Modified source files | 7 | ~200 |
| New test files | 4 | ~350 |
| **Total** | **15** | **~1050** |
