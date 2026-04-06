# FlyAI Tool Fusion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate FlyAI CLI into the existing travel agent tool layer via multi-source data fusion, adding 2 new tools and upgrading 3 existing tools with dual-source querying.

**Architecture:** An async Python wrapper invokes the `flyai` Node.js CLI as a subprocess. Three existing tools (`search_flights`, `search_accommodations`, `get_poi_info`) gain an internal FlyAI branch that runs in parallel with the original API call, then normalizes and merges results. Two new FlyAI-exclusive tools provide meta-search and travel services.

**Tech Stack:** Python 3.11+, asyncio (subprocess), httpx (existing), dataclasses, difflib.SequenceMatcher (fuzzy match), pytest + respx + unittest.mock (testing)

**Spec:** `docs/superpowers/specs/2026-03-30-flyai-tool-fusion-design.md`

---

## File Structure

```
backend/tools/
    flyai_client.py              # NEW — async CLI wrapper (~80 lines)
    normalizers.py               # NEW — dataclasses + normalize + merge (~220 lines)
    quick_travel_search.py       # NEW — meta-search tool (~50 lines)
    search_travel_services.py    # NEW — travel services tool (~60 lines)
    search_flights.py            # MODIFY — add flyai dual-source fusion
    search_accommodations.py     # MODIFY — add flyai dual-source fusion
    get_poi_info.py              # MODIFY — add flyai dual-source fusion
backend/config.py                # MODIFY — add FlyAIConfig dataclass + AppConfig field
config.yaml                      # MODIFY — add flyai section
backend/main.py                  # MODIFY — create FlyAIClient, inject into tool factories
backend/phase/prompts.py         # MODIFY — Phase 2 and Phase 7 prompt additions
backend/tests/
    test_flyai_client.py         # NEW — 5 test cases
    test_normalizers.py          # NEW — 6 test cases
    test_tool_fusion.py          # NEW — 5 test cases
    test_flyai_new_tools.py      # NEW — 3 test cases
```

---

## Task 1: FlyAI Config

Add `FlyAIConfig` dataclass and wire it into `AppConfig` and `config.yaml`.

**Files:**
- Modify: `backend/config.py:17-41` (add FlyAIConfig dataclass, add field to AppConfig)
- Modify: `backend/config.py:102-131` (parse flyai section in load_config)
- Modify: `config.yaml:24-26` (add flyai section)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_flyai_config.py`:

```python
# backend/tests/test_flyai_config.py
import pytest
from config import load_config, FlyAIConfig, AppConfig


def test_flyai_config_defaults():
    """FlyAIConfig should have sensible defaults."""
    cfg = FlyAIConfig()
    assert cfg.enabled is True
    assert cfg.cli_timeout == 30
    assert cfg.api_key is None


def test_app_config_has_flyai_field():
    """AppConfig should include a flyai field."""
    cfg = AppConfig()
    assert isinstance(cfg.flyai, FlyAIConfig)
    assert cfg.flyai.enabled is True


def test_load_config_parses_flyai(tmp_path):
    """load_config should parse the flyai section from YAML."""
    yaml_content = """
flyai:
  enabled: false
  cli_timeout: 15
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)
    cfg = load_config(str(cfg_file))
    assert cfg.flyai.enabled is False
    assert cfg.flyai.cli_timeout == 15
    assert cfg.flyai.api_key is None


def test_load_config_flyai_missing(tmp_path):
    """When flyai section is absent, defaults should apply."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("llm:\n  provider: openai\n")
    cfg = load_config(str(cfg_file))
    assert cfg.flyai.enabled is True
    assert cfg.flyai.cli_timeout == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_flyai_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'FlyAIConfig'`

- [ ] **Step 3: Implement FlyAIConfig**

In `backend/config.py`, after `ApiKeysConfig` (line 31), add:

```python
@dataclass(frozen=True)
class FlyAIConfig:
    enabled: bool = True
    cli_timeout: int = 30
    api_key: str | None = None
```

In `AppConfig` (line 34), add field after `context_compression_threshold`:

```python
@dataclass(frozen=True)
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    llm_overrides: dict[str, LLMConfig] = field(default_factory=dict)
    api_keys: ApiKeysConfig = field(default_factory=ApiKeysConfig)
    data_dir: str = "./data"
    max_retries: int = 3
    context_compression_threshold: float = 0.5
    flyai: FlyAIConfig = field(default_factory=FlyAIConfig)
```

In `load_config()`, after line 122 (`api_keys = ...`), add parsing:

```python
    # Parse flyai config
    flyai_raw = raw.get("flyai", {})
    flyai = FlyAIConfig(
        enabled=flyai_raw.get("enabled", True),
        cli_timeout=int(flyai_raw.get("cli_timeout", 30)),
        api_key=_resolve_env(flyai_raw.get("api_key", "")) or None,
    )
```

In the `return AppConfig(...)` block (line 124-131), add `flyai=flyai`.

Note: The no-YAML path (line 106-109) does NOT need changes — `flyai` has `default_factory` so it gets a default value automatically.

- [ ] **Step 4: Update config.yaml**

Append to `config.yaml` after line 26:

```yaml

flyai:
  enabled: true
  cli_timeout: 30
  # api_key: ${FLYAI_API_KEY}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_flyai_config.py -v`
Expected: 4 PASSED

- [ ] **Step 6: Run full test suite**

Run: `cd backend && python -m pytest`
Expected: All existing tests still pass (no regressions)

- [ ] **Step 7: Commit**

```bash
git add backend/config.py config.yaml backend/tests/test_flyai_config.py
git commit -m "feat: add FlyAIConfig dataclass and config.yaml flyai section"
```

---

## Task 2: FlyAI Client

Create the async subprocess wrapper for `flyai` CLI.

**Files:**
- Create: `backend/tools/flyai_client.py`
- Test: `backend/tests/test_flyai_client.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_flyai_client.py`:

```python
# backend/tests/test_flyai_client.py
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_which_found():
    with patch("shutil.which", return_value="/usr/local/bin/flyai"):
        yield


@pytest.fixture
def mock_which_missing():
    with patch("shutil.which", return_value=None):
        yield


def _make_proc_mock(stdout_data: bytes, stderr_data: bytes = b"", returncode: int = 0):
    """Create a mock subprocess that returns given stdout/stderr."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout_data, stderr_data))
    proc.returncode = returncode
    return proc


@pytest.mark.asyncio
async def test_search_flight_success(mock_which_found):
    from tools.flyai_client import FlyAIClient

    payload = json.dumps({
        "status": 0,
        "message": "success",
        "data": {"itemList": [{"title": "MU5101", "price": "1200"}]}
    }).encode()

    with patch("asyncio.create_subprocess_exec", return_value=_make_proc_mock(payload)):
        client = FlyAIClient(timeout=10)
        assert client.available is True
        result = await client.search_flight(origin="上海", destination="北京", date="2026-05-01")
        assert len(result) == 1
        assert result[0]["title"] == "MU5101"


@pytest.mark.asyncio
async def test_not_installed(mock_which_missing):
    from tools.flyai_client import FlyAIClient

    client = FlyAIClient()
    assert client.available is False
    result = await client.search_flight(origin="上海", destination="北京", date="2026-05-01")
    assert result == []


@pytest.mark.asyncio
async def test_timeout(mock_which_found):
    from tools.flyai_client import FlyAIClient

    async def slow_communicate():
        await asyncio.sleep(100)
        return (b"", b"")

    proc = AsyncMock()
    proc.communicate = slow_communicate
    proc.kill = MagicMock()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        client = FlyAIClient(timeout=0.01)
        result = await client.search_flight(origin="上海", destination="北京", date="2026-05-01")
        assert result == []


@pytest.mark.asyncio
async def test_nonzero_status(mock_which_found):
    from tools.flyai_client import FlyAIClient

    payload = json.dumps({
        "status": 1,
        "message": "error",
        "data": {}
    }).encode()

    with patch("asyncio.create_subprocess_exec", return_value=_make_proc_mock(payload)):
        client = FlyAIClient(timeout=10)
        result = await client.fast_search(query="杭州三日游")
        assert result == []


@pytest.mark.asyncio
async def test_empty_item_list(mock_which_found):
    from tools.flyai_client import FlyAIClient

    payload = json.dumps({
        "status": 0,
        "message": "success",
        "data": {"itemList": []}
    }).encode()

    with patch("asyncio.create_subprocess_exec", return_value=_make_proc_mock(payload)):
        client = FlyAIClient(timeout=10)
        result = await client.search_hotels(dest_name="东京")
        assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_flyai_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.flyai_client'`

- [ ] **Step 3: Implement FlyAIClient**

Create `backend/tools/flyai_client.py`:

```python
# backend/tools/flyai_client.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil

logger = logging.getLogger(__name__)


class FlyAIClient:
    """Async wrapper around the flyai Node.js CLI tool.

    All public methods return list[dict]. They never raise exceptions —
    errors are logged and an empty list is returned (graceful degradation).
    """

    def __init__(self, timeout: int = 30, api_key: str | None = None) -> None:
        self.timeout = timeout
        self._available = shutil.which("flyai") is not None
        self._env: dict[str, str] | None = None
        if api_key:
            self._env = {**os.environ, "FLYAI_API_KEY": api_key}

    @property
    def available(self) -> bool:
        return self._available

    async def fast_search(self, query: str) -> list[dict]:
        return await self._run("fliggy-fast-search", query=query)

    async def search_flight(self, origin: str, **kwargs) -> list[dict]:
        return await self._run("search-flight", origin=origin, **kwargs)

    async def search_hotels(self, dest_name: str, **kwargs) -> list[dict]:
        return await self._run("search-hotels", dest_name=dest_name, **kwargs)

    async def search_poi(self, city_name: str, **kwargs) -> list[dict]:
        return await self._run("search-poi", city_name=city_name, **kwargs)

    async def _run(self, command: str, **kwargs) -> list[dict]:
        if not self._available:
            return []

        cmd = ["flyai", command]
        for key, value in kwargs.items():
            if value is not None:
                cmd.extend([f"--{key.replace('_', '-')}", str(value)])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            logger.warning("FlyAI CLI timed out for command: %s", command)
            try:
                proc.kill()  # type: ignore[union-attr]
            except ProcessLookupError:
                pass
            return []
        except Exception as exc:
            logger.warning("FlyAI CLI subprocess error: %s", exc)
            return []

        try:
            data = json.loads(stdout.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("FlyAI CLI invalid JSON: %s", exc)
            return []

        if data.get("status") != 0:
            logger.warning("FlyAI CLI non-zero status: %s", data.get("message"))
            return []

        return data.get("data", {}).get("itemList", [])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_flyai_client.py -v`
Expected: 5 PASSED

- [ ] **Step 5: Commit**

```bash
git add backend/tools/flyai_client.py backend/tests/test_flyai_client.py
git commit -m "feat: add FlyAIClient async subprocess wrapper for flyai CLI"
```

---

## Task 3: Normalizers

Create the data normalization and merge layer.

**Files:**
- Create: `backend/tools/normalizers.py`
- Test: `backend/tests/test_normalizers.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_normalizers.py`:

```python
# backend/tests/test_normalizers.py
from __future__ import annotations

import pytest


def test_normalize_amadeus_flight():
    from tools.normalizers import normalize_amadeus_flight, FlightResult

    raw = {
        "id": "1",
        "price": {"total": "3500.00", "currency": "CNY"},
        "itineraries": [{
            "duration": "PT3H30M",
            "segments": [{
                "carrierCode": "CA",
                "number": "1234",
                "departure": {"iataCode": "PEK", "at": "2026-07-15T08:00:00"},
                "arrival": {"iataCode": "NRT", "at": "2026-07-15T12:30:00"},
            }]
        }]
    }
    result = normalize_amadeus_flight(raw)
    assert isinstance(result, FlightResult)
    assert result.airline == "CA"
    assert result.flight_no == "CA1234"
    assert result.price == 3500.0
    assert result.source == "amadeus"
    assert result.booking_url is None


def test_normalize_flyai_flight():
    from tools.normalizers import normalize_flyai_flight, FlightResult

    raw = {
        "title": "东方航空 MU5101",
        "journeys": [{
            "segments": [{
                "airlineName": "东方航空",
                "flightNo": "MU5101",
                "depCityName": "上海",
                "arrCityName": "北京",
                "depTime": "2026-07-15 08:00",
                "arrTime": "2026-07-15 10:30",
                "duration": 150,
                "stopCount": 0,
                "cabin": "经济舱",
            }]
        }],
        "price": "800",
        "jumpUrl": "https://www.fliggy.com/flight/123",
    }
    result = normalize_flyai_flight(raw)
    assert isinstance(result, FlightResult)
    assert result.flight_no == "MU5101"
    assert result.price == 800.0
    assert result.source == "flyai"
    assert result.booking_url == "https://www.fliggy.com/flight/123"


def test_merge_flights_dedup():
    from tools.normalizers import FlightResult, merge_flights

    amadeus = FlightResult(
        airline="MU", flight_no="MU5101", origin="上海", destination="北京",
        dep_time="2026-07-15T08:00:00", arr_time="2026-07-15T10:30:00",
        duration_min=150, stops=0, price=900.0, currency="CNY",
        cabin_class="economy", source="amadeus", booking_url=None,
    )
    flyai = FlightResult(
        airline="东方航空", flight_no="MU5101", origin="上海", destination="北京",
        dep_time="2026-07-15 08:00", arr_time="2026-07-15 10:30",
        duration_min=150, stops=0, price=800.0, currency="CNY",
        cabin_class="经济舱", source="flyai",
        booking_url="https://www.fliggy.com/flight/123",
    )
    merged = merge_flights([amadeus], [flyai])
    # Same flight_no + same dep date → deduplicated to one, prefer with booking_url
    assert len(merged) == 1
    assert merged[0].booking_url is not None
    assert merged[0].source == "flyai"


def test_normalize_google_accommodation():
    from tools.normalizers import normalize_google_accommodation, AccommodationResult

    raw = {
        "name": "Park Hyatt Tokyo",
        "formatted_address": "3-7-1-2 Nishi Shinjuku",
        "rating": 4.6,
        "geometry": {"location": {"lat": 35.6894, "lng": 139.6917}},
        "price_level": 4,
    }
    result = normalize_google_accommodation(raw)
    assert isinstance(result, AccommodationResult)
    assert result.name == "Park Hyatt Tokyo"
    assert result.lat == 35.6894
    assert result.source == "google"


def test_flyai_hotel_null_price():
    from tools.normalizers import normalize_flyai_hotel, AccommodationResult

    raw = {
        "title": "全季酒店",
        "address": "杭州市西湖区",
        "score": "4.5",
        "detailUrl": "https://www.fliggy.com/hotel/456",
        "price": None,
    }
    result = normalize_flyai_hotel(raw)
    assert isinstance(result, AccommodationResult)
    assert result.price_per_night is None
    assert result.booking_url == "https://www.fliggy.com/hotel/456"


def test_merge_pois_fuzzy():
    from tools.normalizers import POIResult, merge_pois

    google = POIResult(
        name="Kinkaku-ji Temple", address="1 Kinkakujicho, Kyoto",
        lat=35.0394, lng=135.7292, rating=4.7, category=None,
        is_free=None, ticket_price=None, ticket_url=None,
        source="google", booking_url=None,
    )
    flyai = POIResult(
        name="金阁寺 Kinkaku-ji", address="京都市北区",
        lat=None, lng=None, rating=4.8, category="名胜古迹",
        is_free=False, ticket_price=400.0, ticket_url="https://fliggy.com/poi/789",
        source="flyai", booking_url="https://fliggy.com/poi/789",
    )
    merged = merge_pois([google], [flyai])
    # Fuzzy match > 0.8 → merged record with Google coords + FlyAI ticket info
    matched = [p for p in merged if p.source == "merged"]
    assert len(matched) == 1
    assert matched[0].lat == 35.0394  # from Google
    assert matched[0].ticket_price == 400.0  # from FlyAI
    assert matched[0].category == "名胜古迹"  # from FlyAI
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_normalizers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.normalizers'`

- [ ] **Step 3: Implement normalizers**

Create `backend/tools/normalizers.py`:

```python
# backend/tools/normalizers.py
from __future__ import annotations

from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from typing import Any


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FlightResult:
    airline: str
    flight_no: str
    origin: str
    destination: str
    dep_time: str
    arr_time: str
    duration_min: int
    stops: int
    price: float | None
    currency: str
    cabin_class: str
    source: str           # "amadeus" | "flyai"
    booking_url: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AccommodationResult:
    name: str
    address: str
    lat: float | None
    lng: float | None
    rating: float | None
    price_per_night: float | None
    currency: str
    star_rating: str | None
    bed_type: str | None
    source: str           # "google" | "flyai" | "merged"
    booking_url: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class POIResult:
    name: str
    address: str
    lat: float | None
    lng: float | None
    rating: float | None
    category: str | None
    is_free: bool | None
    ticket_price: float | None
    ticket_url: str | None
    source: str           # "google" | "flyai" | "merged"
    booking_url: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Normalize: Amadeus / Google → unified dataclass
# ---------------------------------------------------------------------------

def normalize_amadeus_flight(raw: dict) -> FlightResult:
    price_obj = raw.get("price", {})
    itineraries = raw.get("itineraries", [{}])
    first_itin = itineraries[0] if itineraries else {}
    segments = first_itin.get("segments", [{}])
    first_seg = segments[0] if segments else {}

    carrier = first_seg.get("carrierCode", "")
    number = first_seg.get("number", "")
    dep = first_seg.get("departure", {})
    arr = first_seg.get("arrival", {})

    # Parse duration like "PT3H30M" → minutes
    duration_str = first_itin.get("duration", "")
    duration_min = _parse_iso_duration(duration_str)

    price_str = price_obj.get("total")
    price = float(price_str) if price_str else None

    return FlightResult(
        airline=carrier,
        flight_no=f"{carrier}{number}",
        origin=dep.get("iataCode", ""),
        destination=arr.get("iataCode", ""),
        dep_time=dep.get("at", ""),
        arr_time=arr.get("at", ""),
        duration_min=duration_min,
        stops=max(0, len(segments) - 1),
        price=price,
        currency=price_obj.get("currency", "USD"),
        cabin_class="economy",
        source="amadeus",
        booking_url=None,
    )


def normalize_google_accommodation(raw: dict) -> AccommodationResult:
    loc = raw.get("geometry", {}).get("location", {})
    return AccommodationResult(
        name=raw.get("name", ""),
        address=raw.get("formatted_address", ""),
        lat=loc.get("lat"),
        lng=loc.get("lng"),
        rating=raw.get("rating"),
        price_per_night=None,  # Google Places doesn't provide actual prices
        currency="USD",
        star_rating=str(raw["price_level"]) if raw.get("price_level") else None,
        bed_type=None,
        source="google",
        booking_url=None,
    )


def normalize_google_poi(raw: dict) -> POIResult:
    loc = raw.get("geometry", {}).get("location", {})
    return POIResult(
        name=raw.get("name", ""),
        address=raw.get("formatted_address", ""),
        lat=loc.get("lat"),
        lng=loc.get("lng"),
        rating=raw.get("rating"),
        category=", ".join(raw.get("types", [])) or None,
        is_free=None,
        ticket_price=None,
        ticket_url=None,
        source="google",
        booking_url=None,
    )


# ---------------------------------------------------------------------------
# Normalize: FlyAI → unified dataclass
# ---------------------------------------------------------------------------

def normalize_flyai_flight(raw: dict) -> FlightResult:
    journeys = raw.get("journeys", [{}])
    first_journey = journeys[0] if journeys else {}
    segments = first_journey.get("segments", [{}])
    first_seg = segments[0] if segments else {}

    price_str = raw.get("price")
    price = float(price_str) if price_str else None

    return FlightResult(
        airline=first_seg.get("airlineName", ""),
        flight_no=first_seg.get("flightNo", ""),
        origin=first_seg.get("depCityName", ""),
        destination=first_seg.get("arrCityName", ""),
        dep_time=first_seg.get("depTime", ""),
        arr_time=first_seg.get("arrTime", ""),
        duration_min=int(first_seg.get("duration", 0)),
        stops=int(first_seg.get("stopCount", 0)),
        price=price,
        currency="CNY",
        cabin_class=first_seg.get("cabin", ""),
        source="flyai",
        booking_url=raw.get("jumpUrl"),
    )


def normalize_flyai_hotel(raw: dict) -> AccommodationResult:
    price_val = raw.get("price")
    price = float(price_val) if price_val is not None else None

    score_val = raw.get("score")
    rating = float(score_val) if score_val is not None else None

    return AccommodationResult(
        name=raw.get("title", ""),
        address=raw.get("address", ""),
        lat=None,
        lng=None,
        rating=rating,
        price_per_night=price,
        currency="CNY",
        star_rating=raw.get("starRating"),
        bed_type=raw.get("bedType"),
        source="flyai",
        booking_url=raw.get("detailUrl"),
    )


def normalize_flyai_poi(raw: dict) -> POIResult:
    ticket_info = raw.get("ticketInfo", {}) or {}
    ticket_price_val = ticket_info.get("price")
    ticket_price = float(ticket_price_val) if ticket_price_val is not None else None

    return POIResult(
        name=raw.get("title", raw.get("name", "")),
        address=raw.get("address", ""),
        lat=None,
        lng=None,
        rating=float(raw["score"]) if raw.get("score") else None,
        category=raw.get("category"),
        is_free=raw.get("freePoiStatus", None),
        ticket_price=ticket_price,
        ticket_url=raw.get("jumpUrl"),
        source="flyai",
        booking_url=raw.get("jumpUrl"),
    )


# ---------------------------------------------------------------------------
# Merge strategies
# ---------------------------------------------------------------------------

def merge_flights(
    amadeus: list[FlightResult], flyai: list[FlightResult]
) -> list[FlightResult]:
    """Deduplicate by (flight_no, dep_date). Prefer record with booking_url."""
    seen: dict[tuple[str, str], FlightResult] = {}

    for f in amadeus:
        dep_date = f.dep_time[:10]
        key = (f.flight_no.upper(), dep_date)
        seen[key] = f

    for f in flyai:
        dep_date = f.dep_time[:10]
        key = (f.flight_no.upper(), dep_date)
        if key in seen:
            # Prefer the one with booking_url
            if f.booking_url and not seen[key].booking_url:
                seen[key] = f
        else:
            seen[key] = f

    return list(seen.values())


def merge_accommodations(
    google: list[AccommodationResult], flyai: list[AccommodationResult]
) -> list[AccommodationResult]:
    """Fuzzy match by name (ratio > 0.8). Merge Google coords + FlyAI price/booking."""
    result: list[AccommodationResult] = []
    flyai_matched: set[int] = set()

    for g in google:
        best_idx, best_ratio = -1, 0.0
        for i, f in enumerate(flyai):
            if i in flyai_matched:
                continue
            ratio = SequenceMatcher(None, g.name.lower(), f.name.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = i

        if best_ratio > 0.8 and best_idx >= 0:
            flyai_matched.add(best_idx)
            f = flyai[best_idx]
            result.append(AccommodationResult(
                name=g.name,
                address=g.address or f.address,
                lat=g.lat,
                lng=g.lng,
                rating=g.rating or f.rating,
                price_per_night=f.price_per_night or g.price_per_night,
                currency=f.currency if f.price_per_night else g.currency,
                star_rating=f.star_rating or g.star_rating,
                bed_type=f.bed_type,
                source="merged",
                booking_url=f.booking_url,
            ))
        else:
            result.append(g)

    for i, f in enumerate(flyai):
        if i not in flyai_matched:
            result.append(f)

    return result


def merge_pois(
    google: list[POIResult], flyai: list[POIResult]
) -> list[POIResult]:
    """Same fuzzy-match strategy as accommodations."""
    result: list[POIResult] = []
    flyai_matched: set[int] = set()

    for g in google:
        best_idx, best_ratio = -1, 0.0
        for i, f in enumerate(flyai):
            if i in flyai_matched:
                continue
            ratio = SequenceMatcher(None, g.name.lower(), f.name.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = i

        if best_ratio > 0.8 and best_idx >= 0:
            flyai_matched.add(best_idx)
            f = flyai[best_idx]
            result.append(POIResult(
                name=g.name,
                address=g.address or f.address,
                lat=g.lat,
                lng=g.lng,
                rating=g.rating or f.rating,
                category=f.category or g.category,
                is_free=f.is_free if f.is_free is not None else g.is_free,
                ticket_price=f.ticket_price,
                ticket_url=f.ticket_url,
                source="merged",
                booking_url=f.booking_url or g.booking_url,
            ))
        else:
            result.append(g)

    for i, f in enumerate(flyai):
        if i not in flyai_matched:
            result.append(f)

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso_duration(s: str) -> int:
    """Parse ISO 8601 duration like 'PT3H30M' to minutes."""
    if not s or not s.startswith("PT"):
        return 0
    s = s[2:]  # strip "PT"
    hours = 0
    minutes = 0
    if "H" in s:
        parts = s.split("H")
        hours = int(parts[0])
        s = parts[1]
    if "M" in s:
        minutes = int(s.replace("M", ""))
    return hours * 60 + minutes
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_normalizers.py -v`
Expected: 6 PASSED

- [ ] **Step 5: Commit**

```bash
git add backend/tools/normalizers.py backend/tests/test_normalizers.py
git commit -m "feat: add normalizer dataclasses and merge strategies for multi-source fusion"
```

---

## Task 4: Tool Fusion — Modify 3 Existing Tools

Add dual-source fusion to `search_flights`, `search_accommodations`, `get_poi_info`.

**Files:**
- Modify: `backend/tools/search_flights.py` (entire file)
- Modify: `backend/tools/search_accommodations.py` (entire file)
- Modify: `backend/tools/get_poi_info.py` (entire file)
- Test: `backend/tests/test_tool_fusion.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_tool_fusion.py`:

```python
# backend/tests/test_tool_fusion.py
from __future__ import annotations

import pytest
import respx
from httpx import Response
from unittest.mock import AsyncMock, patch

from config import ApiKeysConfig


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def api_keys():
    return ApiKeysConfig(
        amadeus_key="test_key",
        google_maps="test_maps_key",
    )


@pytest.fixture
def mock_flyai_client():
    client = AsyncMock()
    client.available = True
    return client


@pytest.fixture
def mock_flyai_unavailable():
    client = AsyncMock()
    client.available = False
    return client


# ── search_flights fusion ─────────────────────────────────────────

AMADEUS_RESPONSE = {
    "data": [{
        "id": "1",
        "price": {"total": "3500.00", "currency": "CNY"},
        "itineraries": [{
            "duration": "PT3H30M",
            "segments": [{
                "carrierCode": "CA",
                "number": "1234",
                "departure": {"iataCode": "PEK", "at": "2026-07-15T08:00:00"},
                "arrival": {"iataCode": "NRT", "at": "2026-07-15T12:30:00"},
            }]
        }]
    }]
}


@respx.mock
@pytest.mark.asyncio
async def test_flights_both_succeed(api_keys, mock_flyai_client):
    from tools.search_flights import make_search_flights_tool

    respx.post("https://test.api.amadeus.com/v2/shopping/flight-offers").mock(
        return_value=Response(200, json=AMADEUS_RESPONSE)
    )
    mock_flyai_client.search_flight.return_value = [{
        "title": "MU5101",
        "journeys": [{"segments": [{
            "airlineName": "东方航空", "flightNo": "MU5101",
            "depCityName": "上海", "arrCityName": "东京",
            "depTime": "2026-07-15 10:00", "arrTime": "2026-07-15 14:00",
            "duration": 240, "stopCount": 0, "cabin": "经济舱",
        }]}],
        "price": "2800",
        "jumpUrl": "https://fliggy.com/f/123",
    }]

    tool_fn = make_search_flights_tool(api_keys, mock_flyai_client)
    result = await tool_fn(origin="PEK", destination="NRT", date="2026-07-15")
    # Should have results from both sources (different flight_no)
    assert len(result["flights"]) == 2


@respx.mock
@pytest.mark.asyncio
async def test_flights_flyai_fails(api_keys, mock_flyai_client):
    from tools.search_flights import make_search_flights_tool

    respx.post("https://test.api.amadeus.com/v2/shopping/flight-offers").mock(
        return_value=Response(200, json=AMADEUS_RESPONSE)
    )
    mock_flyai_client.search_flight.side_effect = Exception("network error")

    tool_fn = make_search_flights_tool(api_keys, mock_flyai_client)
    result = await tool_fn(origin="PEK", destination="NRT", date="2026-07-15")
    # Amadeus results only
    assert len(result["flights"]) >= 1
    assert any(f["source"] == "amadeus" for f in result["flights"])


@respx.mock
@pytest.mark.asyncio
async def test_flights_flyai_disabled(api_keys, mock_flyai_unavailable):
    from tools.search_flights import make_search_flights_tool

    respx.post("https://test.api.amadeus.com/v2/shopping/flight-offers").mock(
        return_value=Response(200, json=AMADEUS_RESPONSE)
    )

    tool_fn = make_search_flights_tool(api_keys, mock_flyai_unavailable)
    result = await tool_fn(origin="PEK", destination="NRT", date="2026-07-15")
    assert len(result["flights"]) >= 1
    # FlyAI should not have been called
    mock_flyai_unavailable.search_flight.assert_not_called()


# ── search_accommodations fusion ──────────────────────────────────

GOOGLE_PLACES_RESPONSE = {
    "results": [{
        "name": "Park Hyatt Tokyo",
        "formatted_address": "3-7-1 Nishi Shinjuku",
        "rating": 4.6,
        "geometry": {"location": {"lat": 35.69, "lng": 139.69}},
        "price_level": 4,
    }]
}


@respx.mock
@pytest.mark.asyncio
async def test_accommodations_both_succeed(api_keys, mock_flyai_client):
    from tools.search_accommodations import make_search_accommodations_tool

    respx.get("https://maps.googleapis.com/maps/api/place/textsearch/json").mock(
        return_value=Response(200, json=GOOGLE_PLACES_RESPONSE)
    )
    mock_flyai_client.search_hotels.return_value = [{
        "title": "全季酒店",
        "address": "东京新宿",
        "score": "4.2",
        "detailUrl": "https://fliggy.com/h/456",
        "price": "500",
    }]

    tool_fn = make_search_accommodations_tool(api_keys, mock_flyai_client)
    result = await tool_fn(destination="东京", check_in="2026-07-15", check_out="2026-07-20")
    assert len(result["accommodations"]) == 2  # different names → not merged


# ── get_poi_info fusion ───────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_poi_both_succeed(api_keys, mock_flyai_client):
    from tools.get_poi_info import make_get_poi_info_tool

    respx.get("https://maps.googleapis.com/maps/api/place/textsearch/json").mock(
        return_value=Response(200, json={
            "results": [{
                "name": "Fushimi Inari Shrine",
                "formatted_address": "68 Fukakusa, Kyoto",
                "rating": 4.6,
                "geometry": {"location": {"lat": 34.97, "lng": 135.77}},
                "types": ["place_of_worship"],
            }]
        })
    )
    mock_flyai_client.search_poi.return_value = [{
        "title": "伏见稻荷大社",
        "address": "京都市伏見区",
        "score": "4.8",
        "category": "神社寺院",
        "freePoiStatus": True,
        "ticketInfo": {"price": None},
        "jumpUrl": "https://fliggy.com/p/789",
    }]

    tool_fn = make_get_poi_info_tool(api_keys, mock_flyai_client)
    result = await tool_fn(query="伏见稻荷", location="京都")
    assert len(result["pois"]) >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_tool_fusion.py -v`
Expected: FAIL — `make_search_flights_tool() takes 1 positional argument but 2 were given`

- [ ] **Step 3: Modify search_flights.py**

Rewrite `backend/tools/search_flights.py` to accept `flyai_client` and perform dual-source fusion:

```python
# backend/tools/search_flights.py
from __future__ import annotations

import asyncio
import logging

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool
from tools.normalizers import normalize_amadeus_flight, normalize_flyai_flight, merge_flights

logger = logging.getLogger(__name__)

_PARAMETERS = {
    "type": "object",
    "properties": {
        "origin": {
            "type": "string",
            "description": "出发城市 IATA 代码，如 'PEK' 'SHA'",
        },
        "destination": {
            "type": "string",
            "description": "目的地城市 IATA 代码，如 'NRT' 'DPS'",
        },
        "date": {"type": "string", "description": "出发日期，如 '2024-07-15'"},
        "max_results": {"type": "integer", "description": "最大返回数量", "default": 5},
    },
    "required": ["origin", "destination", "date"],
}

# IATA code → Chinese city name (common destinations)
_IATA_TO_CITY: dict[str, str] = {
    "PEK": "北京", "PKX": "北京", "SHA": "上海", "PVG": "上海",
    "CAN": "广州", "SZX": "深圳", "CTU": "成都", "HGH": "杭州",
    "NKG": "南京", "WUH": "武汉", "CKG": "重庆", "XIY": "西安",
    "KMG": "昆明", "XMN": "厦门", "NRT": "东京", "HND": "东京",
    "KIX": "大阪", "ICN": "首尔", "BKK": "曼谷", "SIN": "新加坡",
    "DPS": "巴厘岛", "HKG": "香港", "TPE": "台北",
}


def make_search_flights_tool(api_keys: ApiKeysConfig, flyai_client=None):
    @tool(
        name="search_flights",
        description="""搜索航班信息。
Use when: 用户在阶段 3-4，需要查询航班选项。
Don't use when: 航班已预订或不需要飞行。
返回航班列表，含价格、时间、航空公司信息和预订链接。""",
        phases=[3, 4],
        parameters=_PARAMETERS,
    )
    async def search_flights(
        origin: str, destination: str, date: str, max_results: int = 5
    ) -> dict:
        tasks = []

        # Branch 1: Amadeus
        async def _amadeus():
            if not api_keys.amadeus_key:
                return []
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://test.api.amadeus.com/v2/shopping/flight-offers",
                    json={
                        "originLocationCode": origin,
                        "destinationLocationCode": destination,
                        "departureDate": date,
                        "adults": 1,
                        "max": max_results,
                    },
                    headers={"Authorization": f"Bearer {api_keys.amadeus_key}"},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
            return [normalize_amadeus_flight(o) for o in data.get("data", [])[:max_results]]

        tasks.append(_amadeus())

        # Branch 2: FlyAI
        async def _flyai():
            if not flyai_client or not flyai_client.available:
                return []
            origin_city = _IATA_TO_CITY.get(origin.upper(), origin)
            dest_city = _IATA_TO_CITY.get(destination.upper(), destination)
            raw_list = await flyai_client.search_flight(
                origin=origin_city, destination=dest_city, date=date,
            )
            return [normalize_flyai_flight(r) for r in raw_list]

        tasks.append(_flyai())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        amadeus_results = results[0] if not isinstance(results[0], BaseException) else []
        flyai_results = results[1] if not isinstance(results[1], BaseException) else []

        if isinstance(results[0], BaseException):
            logger.warning("Amadeus search failed: %s", results[0])
        if isinstance(results[1], BaseException):
            logger.warning("FlyAI flight search failed: %s", results[1])

        if not amadeus_results and not flyai_results:
            if not api_keys.amadeus_key:
                raise ToolError(
                    "Amadeus API key not configured",
                    error_code="NO_API_KEY",
                    suggestion="Set AMADEUS_KEY",
                )
            raise ToolError(
                "No flight results from any source",
                error_code="NO_RESULTS",
                suggestion="Try different dates or airports",
            )

        merged = merge_flights(amadeus_results, flyai_results)

        return {
            "flights": [f.to_dict() for f in merged],
            "origin": origin,
            "destination": destination,
        }

    return search_flights
```

- [ ] **Step 4: Modify search_accommodations.py**

Rewrite `backend/tools/search_accommodations.py`:

```python
# backend/tools/search_accommodations.py
from __future__ import annotations

import asyncio
import logging

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool
from tools.normalizers import (
    normalize_google_accommodation, normalize_flyai_hotel, merge_accommodations,
)

logger = logging.getLogger(__name__)

_PARAMETERS = {
    "type": "object",
    "properties": {
        "destination": {
            "type": "string",
            "description": "目的地名称，如 '东京' '巴黎'",
        },
        "check_in": {"type": "string", "description": "入住日期，如 '2024-07-15'"},
        "check_out": {"type": "string", "description": "退房日期，如 '2024-07-20'"},
        "budget_per_night": {"type": "number", "description": "每晚预算（美元）"},
        "area": {"type": "string", "description": "偏好区域，如 '市中心' '海滨'"},
        "requirements": {
            "type": "array",
            "items": {"type": "string"},
            "description": "特殊要求，如 ['含早餐', '有泳池', '可停车']",
        },
    },
    "required": ["destination", "check_in", "check_out"],
}


def make_search_accommodations_tool(api_keys: ApiKeysConfig, flyai_client=None):
    @tool(
        name="search_accommodations",
        description="""搜索住宿信息。
Use when: 用户在阶段 3-4，需要查询住宿选项。
Don't use when: 住宿已确定。
返回住宿列表，含评分、价格、位置信息和预订链接。""",
        phases=[3, 4],
        parameters=_PARAMETERS,
    )
    async def search_accommodations(
        destination: str,
        check_in: str,
        check_out: str,
        budget_per_night: float | None = None,
        area: str | None = None,
        requirements: list[str] | None = None,
    ) -> dict:
        tasks = []

        # Branch 1: Google Places
        async def _google():
            if not api_keys.google_maps:
                return []
            query = f"hotel lodging in {destination}"
            if area:
                query += f" {area}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://maps.googleapis.com/maps/api/place/textsearch/json",
                    params={
                        "query": query,
                        "key": api_keys.google_maps,
                        "type": "lodging",
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
            return [normalize_google_accommodation(p) for p in data.get("results", [])[:5]]

        tasks.append(_google())

        # Branch 2: FlyAI
        async def _flyai():
            if not flyai_client or not flyai_client.available:
                return []
            kwargs = {"check_in_date": check_in, "check_out_date": check_out}
            if budget_per_night:
                kwargs["max_price"] = str(int(budget_per_night))
            raw_list = await flyai_client.search_hotels(
                dest_name=destination, **kwargs,
            )
            return [normalize_flyai_hotel(r) for r in raw_list]

        tasks.append(_flyai())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        google_results = results[0] if not isinstance(results[0], BaseException) else []
        flyai_results = results[1] if not isinstance(results[1], BaseException) else []

        if isinstance(results[0], BaseException):
            logger.warning("Google accommodation search failed: %s", results[0])
        if isinstance(results[1], BaseException):
            logger.warning("FlyAI hotel search failed: %s", results[1])

        if not google_results and not flyai_results:
            if not api_keys.google_maps:
                raise ToolError(
                    "Google Maps API key not configured",
                    error_code="NO_API_KEY",
                    suggestion="Set GOOGLE_MAPS_API_KEY",
                )
            raise ToolError(
                "No accommodation results from any source",
                error_code="NO_RESULTS",
                suggestion="Try different dates or destination",
            )

        merged = merge_accommodations(google_results, flyai_results)

        return {
            "accommodations": [a.to_dict() for a in merged],
            "destination": destination,
            "check_in": check_in,
            "check_out": check_out,
        }

    return search_accommodations
```

- [ ] **Step 5: Modify get_poi_info.py**

Rewrite `backend/tools/get_poi_info.py`:

```python
# backend/tools/get_poi_info.py
from __future__ import annotations

import asyncio
import logging

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool
from tools.normalizers import normalize_google_poi, normalize_flyai_poi, merge_pois

logger = logging.getLogger(__name__)

_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "景点/POI 搜索关键词，如 '金阁寺' '卢浮宫'",
        },
        "location": {
            "type": "string",
            "description": "限定搜索范围的城市或地区，如 '京都' '巴黎'",
        },
    },
    "required": ["query"],
}


def make_get_poi_info_tool(api_keys: ApiKeysConfig, flyai_client=None):
    @tool(
        name="get_poi_info",
        description="""获取景点/兴趣点详细信息。
Use when: 用户在阶段 3-5，需要了解某个景点的详情。
Don't use when: 已有该景点的完整信息。
返回景点列表，含名称、地址、评分、门票价格和位置。""",
        phases=[3, 4, 5],
        parameters=_PARAMETERS,
    )
    async def get_poi_info(query: str, location: str | None = None) -> dict:
        tasks = []

        # Branch 1: Google Places
        async def _google():
            if not api_keys.google_maps:
                return []
            search_query = query
            if location:
                search_query += f" in {location}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://maps.googleapis.com/maps/api/place/textsearch/json",
                    params={
                        "query": search_query,
                        "key": api_keys.google_maps,
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
            return [normalize_google_poi(p) for p in data.get("results", [])[:5]]

        tasks.append(_google())

        # Branch 2: FlyAI
        async def _flyai():
            if not flyai_client or not flyai_client.available:
                return []
            city = location or ""
            raw_list = await flyai_client.search_poi(
                city_name=city, keyword=query,
            )
            return [normalize_flyai_poi(r) for r in raw_list]

        tasks.append(_flyai())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        google_results = results[0] if not isinstance(results[0], BaseException) else []
        flyai_results = results[1] if not isinstance(results[1], BaseException) else []

        if isinstance(results[0], BaseException):
            logger.warning("Google POI search failed: %s", results[0])
        if isinstance(results[1], BaseException):
            logger.warning("FlyAI POI search failed: %s", results[1])

        if not google_results and not flyai_results:
            if not api_keys.google_maps:
                raise ToolError(
                    "Google Maps API key not configured",
                    error_code="NO_API_KEY",
                    suggestion="Set GOOGLE_MAPS_API_KEY",
                )
            raise ToolError(
                "No POI results from any source",
                error_code="NO_RESULTS",
                suggestion="Try a different search query",
            )

        merged = merge_pois(google_results, flyai_results)

        return {"pois": [p.to_dict() for p in merged], "query": query}

    return get_poi_info
```

- [ ] **Step 6: Run fusion tests**

Run: `cd backend && python -m pytest tests/test_tool_fusion.py -v`
Expected: 5 PASSED

- [ ] **Step 7: Fix existing tests for new signatures and return formats**

The existing tests need two kinds of updates:
1. **Signature**: Add `flyai_client=None` to all `make_*_tool()` calls
2. **Assertions**: Return format changes — results are now normalized dataclass dicts (e.g. `price` becomes `float` not `str`, top-level `source` field is removed)

**`backend/tests/test_search_flights.py`:**

Line 13 — add flyai_client param:
```python
# Old:
    return make_search_flights_tool(keys)
# New:
    return make_search_flights_tool(keys, flyai_client=None)
```

Line 34-37 — fix assertions (price is now float, source is per-flight not top-level):
```python
# Old:
    assert len(result["flights"]) == 1
    assert result["flights"][0]["price"] == "3500.00"
    assert result["source"] == "amadeus"
    assert result["origin"] == "PEK"
# New:
    assert len(result["flights"]) == 1
    assert result["flights"][0]["price"] == 3500.0
    assert result["flights"][0]["source"] == "amadeus"
    assert result["origin"] == "PEK"
```

Line 43 — add flyai_client param:
```python
# Old:
    fn = make_search_flights_tool(keys)
# New:
    fn = make_search_flights_tool(keys, flyai_client=None)
```

**`backend/tests/test_search_accommodations.py`:**

Line 13 — add flyai_client param:
```python
# Old:
    return make_search_accommodations_tool(keys)
# New:
    return make_search_accommodations_tool(keys, flyai_client=None)
```

Line 38-40 — fix assertions (source moves into each accommodation dict):
```python
# Old:
    assert len(result["accommodations"]) == 1
    assert result["accommodations"][0]["name"] == "Tokyo Hotel"
    assert result["source"] == "google_places"
# New:
    assert len(result["accommodations"]) == 1
    assert result["accommodations"][0]["name"] == "Tokyo Hotel"
    assert result["accommodations"][0]["source"] == "google"
```

Line 46 — add flyai_client param:
```python
# Old:
    fn = make_search_accommodations_tool(keys)
# New:
    fn = make_search_accommodations_tool(keys, flyai_client=None)
```

**`backend/tests/test_get_poi_info.py`:**

Line 13 — add flyai_client param:
```python
# Old:
    return make_get_poi_info_tool(keys)
# New:
    return make_get_poi_info_tool(keys, flyai_client=None)
```

Line 36-38 — fix assertions (source moves into each poi dict):
```python
# Old:
    assert len(result["pois"]) == 1
    assert result["pois"][0]["name"] == "Kinkaku-ji"
    assert result["source"] == "google_places"
# New:
    assert len(result["pois"]) == 1
    assert result["pois"][0]["name"] == "Kinkaku-ji"
    assert result["pois"][0]["source"] == "google"
```

Line 44 — add flyai_client param:
```python
# Old:
    fn = make_get_poi_info_tool(keys)
# New:
    fn = make_get_poi_info_tool(keys, flyai_client=None)
```

- [ ] **Step 8: Run full test suite**

Run: `cd backend && python -m pytest`
Expected: All tests pass

- [ ] **Step 9: Commit**

```bash
git add backend/tools/search_flights.py backend/tools/search_accommodations.py \
       backend/tools/get_poi_info.py backend/tests/test_tool_fusion.py \
       backend/tests/test_search_flights.py backend/tests/test_search_accommodations.py \
       backend/tests/test_get_poi_info.py
git commit -m "feat: add dual-source fusion to search_flights, search_accommodations, get_poi_info"
```

---

## Task 5: New Tools — quick_travel_search and search_travel_services

Create the two FlyAI-exclusive tools.

**Files:**
- Create: `backend/tools/quick_travel_search.py`
- Create: `backend/tools/search_travel_services.py`
- Test: `backend/tests/test_flyai_new_tools.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_flyai_new_tools.py`:

```python
# backend/tests/test_flyai_new_tools.py
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from tools.base import ToolError


@pytest.fixture
def mock_flyai_client():
    client = AsyncMock()
    client.available = True
    return client


@pytest.fixture
def mock_flyai_unavailable():
    client = AsyncMock()
    client.available = False
    return client


@pytest.mark.asyncio
async def test_quick_travel_search_normal(mock_flyai_client):
    from tools.quick_travel_search import make_quick_travel_search_tool

    mock_flyai_client.fast_search.return_value = [
        {"title": "杭州3日游", "price": "1500", "jumpUrl": "https://fliggy.com/1"},
        {"title": "西湖门票", "price": "0", "jumpUrl": "https://fliggy.com/2"},
    ]

    tool_fn = make_quick_travel_search_tool(mock_flyai_client)
    result = await tool_fn(query="杭州三日游")
    assert len(result["results"]) == 2
    assert result["results"][0]["title"] == "杭州3日游"


@pytest.mark.asyncio
async def test_search_travel_services_visa(mock_flyai_client):
    from tools.search_travel_services import make_search_travel_services_tool

    mock_flyai_client.fast_search.return_value = [
        {"title": "日本签证代办", "price": "299", "jumpUrl": "https://fliggy.com/visa/1"},
    ]

    tool_fn = make_search_travel_services_tool(mock_flyai_client)
    result = await tool_fn(destination="日本", service_type="visa")
    assert len(result["services"]) == 1
    mock_flyai_client.fast_search.assert_called_once()
    # Verify the query contains visa-related keyword
    call_args = mock_flyai_client.fast_search.call_args
    assert "签证" in call_args.kwargs.get("query", call_args.args[0] if call_args.args else "")


@pytest.mark.asyncio
async def test_quick_travel_search_unavailable(mock_flyai_unavailable):
    from tools.quick_travel_search import make_quick_travel_search_tool

    tool_fn = make_quick_travel_search_tool(mock_flyai_unavailable)
    with pytest.raises(ToolError, match="unavailable"):
        await tool_fn(query="杭州三日游")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_flyai_new_tools.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement quick_travel_search.py**

Create `backend/tools/quick_travel_search.py`:

```python
# backend/tools/quick_travel_search.py
from __future__ import annotations

from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "自然语言旅行搜索，如 '杭州三日游' '法国签证' '上海邮轮'",
        },
    },
    "required": ["query"],
}


def make_quick_travel_search_tool(flyai_client):
    @tool(
        name="quick_travel_search",
        description="""跨品类快速搜索旅行产品（机票、酒店、门票、跟团游、签证等）。
Use when: 用户在阶段 2-3，需要快速了解某个目的地的旅行产品概览和价格范围。
Don't use when: 已确定具体出行方案，应使用专项搜索工具。
返回多品类产品列表，含标题、价格和预订链接。""",
        phases=[2, 3],
        parameters=_PARAMETERS,
    )
    async def quick_travel_search(query: str) -> dict:
        if not flyai_client or not flyai_client.available:
            raise ToolError(
                "FlyAI service unavailable",
                error_code="SERVICE_UNAVAILABLE",
                suggestion="Use search_destinations for destination research instead.",
            )

        raw_list = await flyai_client.fast_search(query=query)

        results = []
        for item in raw_list:
            results.append({
                "title": item.get("title", ""),
                "price": item.get("price"),
                "booking_url": item.get("jumpUrl") or item.get("detailUrl"),
                "image_url": item.get("picUrl") or item.get("mainPic"),
            })

        return {"results": results, "query": query, "source": "flyai"}

    return quick_travel_search
```

- [ ] **Step 4: Implement search_travel_services.py**

Create `backend/tools/search_travel_services.py`:

```python
# backend/tools/search_travel_services.py
from __future__ import annotations

from tools.base import ToolError, tool

_SERVICE_KEYWORDS: dict[str, str] = {
    "visa": "签证办理",
    "insurance": "旅行保险",
    "sim_card": "境外电话卡",
    "car_rental": "租车自驾",
    "transfer": "接送机",
}

_PARAMETERS = {
    "type": "object",
    "properties": {
        "destination": {
            "type": "string",
            "description": "旅行目的地",
        },
        "service_type": {
            "type": "string",
            "enum": list(_SERVICE_KEYWORDS.keys()),
            "description": "服务类型：visa（签证）、insurance（保险）、sim_card（电话卡）、car_rental（租车）、transfer（接送机）",
        },
    },
    "required": ["destination", "service_type"],
}


def make_search_travel_services_tool(flyai_client):
    @tool(
        name="search_travel_services",
        description="""搜索旅行辅助服务：签证办理、旅行保险、电话卡、租车、接送机。
Use when: 用户在阶段 7，行程已确认，需要推荐实用出行服务。
Don't use when: 行程尚未确定。
返回服务列表，含标题、价格和预订链接。""",
        phases=[7],
        parameters=_PARAMETERS,
    )
    async def search_travel_services(
        destination: str, service_type: str
    ) -> dict:
        if not flyai_client or not flyai_client.available:
            raise ToolError(
                "FlyAI service unavailable",
                error_code="SERVICE_UNAVAILABLE",
                suggestion="Suggest user search for travel services independently.",
            )

        keyword = _SERVICE_KEYWORDS.get(service_type, service_type)
        query = f"{destination} {keyword}"
        raw_list = await flyai_client.fast_search(query=query)

        services = []
        for item in raw_list:
            services.append({
                "title": item.get("title", ""),
                "price": item.get("price"),
                "booking_url": item.get("jumpUrl") or item.get("detailUrl"),
                "image_url": item.get("picUrl") or item.get("mainPic"),
            })

        return {
            "services": services,
            "destination": destination,
            "service_type": service_type,
            "source": "flyai",
        }

    return search_travel_services
```

- [ ] **Step 5: Run tests**

Run: `cd backend && python -m pytest tests/test_flyai_new_tools.py -v`
Expected: 3 PASSED

- [ ] **Step 6: Commit**

```bash
git add backend/tools/quick_travel_search.py backend/tools/search_travel_services.py \
       backend/tests/test_flyai_new_tools.py
git commit -m "feat: add quick_travel_search and search_travel_services FlyAI-exclusive tools"
```

---

## Task 6: Wire Everything Together — main.py, prompts.py

Register new tools, inject FlyAIClient, update phase prompts.

**Files:**
- Modify: `backend/main.py:26-36,67-81` (imports + _build_agent)
- Modify: `backend/phase/prompts.py:8-11,24-27` (Phase 2, Phase 7 prompts)

- [ ] **Step 1: Update main.py imports**

Add to imports section (after line 36):

```python
from tools.quick_travel_search import make_quick_travel_search_tool
from tools.search_travel_services import make_search_travel_services_tool
```

- [ ] **Step 2: Update _build_agent in main.py**

In `_build_agent` (line 67), add FlyAI client creation before tool registration:

```python
    def _build_agent(plan):
        llm = create_llm_provider(config.llm)
        tool_engine = ToolEngine()

        # Create FlyAI client if enabled
        flyai_client = None
        if config.flyai.enabled:
            from tools.flyai_client import FlyAIClient
            flyai_client = FlyAIClient(
                timeout=config.flyai.cli_timeout,
                api_key=config.flyai.api_key,
            )

        tool_engine.register(make_update_plan_state_tool(plan))
        tool_engine.register(make_search_destinations_tool(config.api_keys))
        tool_engine.register(make_search_flights_tool(config.api_keys, flyai_client))
        tool_engine.register(make_search_accommodations_tool(config.api_keys, flyai_client))
        tool_engine.register(make_get_poi_info_tool(config.api_keys, flyai_client))
        tool_engine.register(make_calculate_route_tool(config.api_keys))
        tool_engine.register(make_assemble_day_plan_tool())
        tool_engine.register(make_check_availability_tool(config.api_keys))
        tool_engine.register(make_check_weather_tool(config.api_keys))
        tool_engine.register(make_generate_summary_tool())
        tool_engine.register(make_quick_travel_search_tool(flyai_client))
        tool_engine.register(make_search_travel_services_tool(flyai_client))

        hooks = HookManager()
        # ... rest unchanged ...
```

- [ ] **Step 3: Update Phase 2 prompt**

In `backend/phase/prompts.py`, append to Phase 2 prompt (line 8-11):

```python
    2: """你现在是目的地推荐专家。基于用户的意愿，推荐 2-3 个目的地候选。
每个候选必须附带：季节适宜度、预算估算、签证要求、与用户偏好的匹配度。
最终目的地由用户拍板，你只提供信息和建议，不替用户做决定。
如果用户已经明确了目的地，确认后直接进入下一步。
你可以使用 quick_travel_search 快速了解候选目的地的产品概览和价格区间，帮助用户做预算估算和目的地对比。""",
```

- [ ] **Step 4: Update Phase 7 prompt**

In `backend/phase/prompts.py`, append to Phase 7 prompt (line 24-27):

```python
    7: """你现在是出发前查漏清单生成器。针对已确认的行程，生成完整的出行检查清单。
包含：证件准备、货币兑换、天气对应衣物、已规划项目的注意事项、紧急联系方式、目的地实用贴士。
使用 check_weather 获取最新天气，使用 generate_summary 生成出行摘要。
你可以使用 search_travel_services 搜索签证办理、旅行保险、电话卡、租车、接送机等实用服务，在最终摘要中附上预订链接。
逐项检查，确保没有遗漏。""",
```

- [ ] **Step 5: Run full test suite**

Run: `cd backend && python -m pytest`
Expected: All tests pass (no regressions, existing API tests + new tests all green)

- [ ] **Step 6: Commit**

```bash
git add backend/main.py backend/phase/prompts.py
git commit -m "feat: wire FlyAIClient into tool registration and update phase prompts"
```

---

## Task 7: Final Verification

Run the complete test suite and verify everything works together.

- [ ] **Step 1: Run all tests with verbose output**

Run: `cd backend && python -m pytest -v`
Expected: All tests pass. New test count should be ~148 (129 existing + 4 config + 5 client + 6 normalizer + 5 fusion + 3 new tools = ~152, minus any overlap)

- [ ] **Step 2: Check for import errors**

Run: `cd backend && python -c "from tools.flyai_client import FlyAIClient; from tools.normalizers import FlightResult; from tools.quick_travel_search import make_quick_travel_search_tool; from tools.search_travel_services import make_search_travel_services_tool; print('All imports OK')"`
Expected: `All imports OK`

- [ ] **Step 3: Final commit (if any fixups needed)**

Only if previous steps required adjustments. Otherwise skip.

```bash
git add -A
git commit -m "fix: address test/import issues from integration"
```
