# Memory System Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade Travel Agent Pro memory from a lightweight `UserMemory` JSON profile into structured, scoped, reviewable travel memory with extraction candidates, policy-based merge, relevant context injection, events, episodes, and management APIs.

**Architecture:** Keep the first implementation file-backed and compatible with the existing `data/users/{user_id}/memory.json` layout. Add focused modules under `backend/memory/` for models, store, policy, retrieval, and formatting; integrate them through `MemoryManager` so the rest of the app has one facade. Replace the Phase 1 -> 3-only extraction path with per-turn background candidate extraction and rule-based policy, while keeping legacy parsing functions until existing tests are migrated.

**Tech Stack:** Python 3.12 dataclasses, FastAPI, pytest, pytest-asyncio, JSON/JSONL file storage, existing LLM provider abstraction

---

## File Structure

### Create

| File | Responsibility |
|------|----------------|
| `backend/memory/store.py` | File-backed v2 memory store, v1 migration, per-user async locks, atomic writes, JSONL events and episodes |
| `backend/memory/policy.py` | Deterministic risk classification, PII redaction, candidate-to-item conversion, merge semantics |
| `backend/memory/retriever.py` | Rule-based retrieval for core, trip, and phase-relevant memory |
| `backend/memory/formatter.py` | Compact prompt text formatter for retrieved memory |
| `backend/tests/test_memory_models.py` | Model serialization and stable id tests |
| `backend/tests/test_memory_store.py` | Store migration, write, lock, JSONL tests |
| `backend/tests/test_memory_policy.py` | Risk, redaction, merge, conflict tests |
| `backend/tests/test_memory_retriever.py` | Scope and phase filtering tests |
| `backend/tests/test_memory_formatter.py` | Prompt text formatting tests |
| `backend/tests/test_memory_integration.py` | FastAPI integration tests for API, extraction scheduling, prompt injection |

### Modify

| File | Changes |
|------|---------|
| `backend/memory/models.py` | Add `MemoryItem`, `MemorySource`, `MemoryCandidate`, `MemoryEvent`, `TripEpisode`, v2 envelope helpers; keep legacy `UserMemory` |
| `backend/memory/manager.py` | Convert to facade over store, retriever, formatter; preserve `load`, `save`, `generate_summary` compatibility |
| `backend/memory/extraction.py` | Add candidate prompt/parser/extractor while keeping legacy functions |
| `backend/config.py` | Add `MemoryConfig` tree and compatibility mapping from existing `memory_extraction` |
| `config.yaml` | Add new `memory:` config block while retaining old `memory_extraction` until all call sites migrate |
| `backend/context/manager.py` | Rename `user_summary` parameter to `memory_context` and output `## 相关用户记忆` |
| `backend/main.py` | Wire store/manager, per-turn extraction, memory APIs, SSE `memory_pending`, event recording, episode generation |
| `backend/state/models.py` | Add optional `trip_id` to `TravelPlanState` serialization |
| `PROJECT_OVERVIEW.md` | Update memory architecture description after implementation commit |

---

## Task 1: Structured Memory Models

**Files:**
- Modify: `backend/memory/models.py`
- Test: `backend/tests/test_memory_models.py`

- [ ] **Step 1: Write model tests**

Create `backend/tests/test_memory_models.py`:

```python
from memory.models import (
    MemoryCandidate,
    MemoryItem,
    MemorySource,
    TripEpisode,
    generate_memory_id,
)


def test_preference_id_is_stable_for_same_key():
    first = generate_memory_id(
        user_id="u1",
        type="preference",
        domain="pace",
        key="preferred_pace",
        scope="global",
    )
    second = generate_memory_id(
        user_id="u1",
        type="preference",
        domain="pace",
        key="preferred_pace",
        scope="global",
    )
    assert first == second
    assert len(first) == 16


def test_rejection_id_includes_value():
    red_eye = generate_memory_id(
        user_id="u1",
        type="rejection",
        domain="flight",
        key="avoid",
        scope="global",
        value="red_eye",
    )
    layover = generate_memory_id(
        user_id="u1",
        type="rejection",
        domain="flight",
        key="avoid",
        scope="global",
        value="long_layover",
    )
    assert red_eye != layover


def test_memory_item_round_trip():
    item = MemoryItem(
        id="mem123",
        user_id="u1",
        type="preference",
        domain="food",
        key="dietary_restrictions",
        value=["no_spicy"],
        scope="global",
        polarity="avoid",
        confidence=0.9,
        status="active",
        source=MemorySource(kind="message", session_id="s1", quote="我不吃辣"),
        created_at="2026-04-11T00:00:00",
        updated_at="2026-04-11T00:00:00",
    )
    loaded = MemoryItem.from_dict(item.to_dict())
    assert loaded == item


def test_candidate_to_dict_preserves_risk_and_evidence():
    candidate = MemoryCandidate(
        type="preference",
        domain="flight",
        key="avoid_red_eye",
        value=True,
        scope="global",
        polarity="avoid",
        confidence=0.95,
        risk="medium",
        evidence="以后我都不坐红眼航班",
        reason="用户明确表达长期偏好",
    )
    data = candidate.to_dict()
    assert data["risk"] == "medium"
    assert data["evidence"] == "以后我都不坐红眼航班"


def test_trip_episode_round_trip():
    episode = TripEpisode(
        id="ep1",
        user_id="u1",
        session_id="s1",
        trip_id="trip1",
        destination="东京",
        dates="2026-05",
        travelers={"adults": 2},
        budget={"total": 30000, "currency": "CNY"},
        selected_skeleton={"id": "sk1"},
        final_plan_summary="东京轻松五日游",
        accepted_items=[{"type": "skeleton", "id": "sk1"}],
        rejected_items=[{"type": "poi", "name": "迪士尼"}],
        lessons=["用户不喜欢排队时间长的热门景点"],
        satisfaction=None,
        created_at="2026-04-11T00:00:00",
    )
    assert TripEpisode.from_dict(episode.to_dict()) == episode
```

- [ ] **Step 2: Run model tests and verify failure**

Run: `cd backend && python -m pytest tests/test_memory_models.py -v`

Expected: FAIL with import errors for `MemoryItem`, `MemorySource`, `MemoryCandidate`, `TripEpisode`, or `generate_memory_id`.

- [ ] **Step 3: Implement model classes**

Modify `backend/memory/models.py` by keeping existing `Rejection`, `TripSummary`, and `UserMemory`, then append these definitions:

```python
import hashlib


def _normalize_memory_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value).strip().lower()
    if isinstance(value, list):
        return "|".join(sorted(_normalize_memory_value(item) for item in value))
    if isinstance(value, dict):
        parts = [
            f"{key}:{_normalize_memory_value(value[key])}"
            for key in sorted(value)
        ]
        return "|".join(parts)
    return str(value).strip().lower()


def generate_memory_id(
    *,
    user_id: str,
    type: str,
    domain: str,
    key: str,
    scope: str,
    trip_id: str | None = None,
    value: Any = None,
) -> str:
    if type == "rejection":
        raw = f"{user_id}:{type}:{domain}:{key}:{_normalize_memory_value(value)}:{scope}:{trip_id or ''}"
    else:
        raw = f"{user_id}:{type}:{domain}:{key}:{scope}:{trip_id or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class MemorySource:
    kind: str
    session_id: str
    message_id: str | None = None
    tool_call_id: str | None = None
    quote: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "tool_call_id": self.tool_call_id,
            "quote": self.quote,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemorySource":
        return cls(
            kind=str(data.get("kind", "")),
            session_id=str(data.get("session_id", "")),
            message_id=data.get("message_id"),
            tool_call_id=data.get("tool_call_id"),
            quote=data.get("quote"),
        )


@dataclass
class MemoryItem:
    id: str
    user_id: str
    type: str
    domain: str
    key: str
    value: Any
    scope: str
    polarity: str
    confidence: float
    status: str
    source: MemorySource
    created_at: str
    updated_at: str
    expires_at: str | None = None
    destination: str | None = None
    session_id: str | None = None
    trip_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "type": self.type,
            "domain": self.domain,
            "key": self.key,
            "value": self.value,
            "scope": self.scope,
            "polarity": self.polarity,
            "confidence": self.confidence,
            "status": self.status,
            "source": self.source.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "destination": self.destination,
            "session_id": self.session_id,
            "trip_id": self.trip_id,
            "attributes": self.attributes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryItem":
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            type=str(data["type"]),
            domain=str(data["domain"]),
            key=str(data["key"]),
            value=data.get("value"),
            scope=str(data["scope"]),
            polarity=str(data.get("polarity", "neutral")),
            confidence=float(data.get("confidence", 0.0)),
            status=str(data.get("status", "active")),
            source=MemorySource.from_dict(data.get("source", {})),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            expires_at=data.get("expires_at"),
            destination=data.get("destination"),
            session_id=data.get("session_id"),
            trip_id=data.get("trip_id"),
            attributes=dict(data.get("attributes", {})),
        )


@dataclass
class MemoryCandidate:
    type: str
    domain: str
    key: str
    value: Any
    scope: str
    polarity: str
    confidence: float
    risk: str
    evidence: str
    reason: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "domain": self.domain,
            "key": self.key,
            "value": self.value,
            "scope": self.scope,
            "polarity": self.polarity,
            "confidence": self.confidence,
            "risk": self.risk,
            "evidence": self.evidence,
            "reason": self.reason,
            "attributes": self.attributes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryCandidate":
        return cls(
            type=str(data.get("type", "preference")),
            domain=str(data.get("domain", "general")),
            key=str(data.get("key", "general")),
            value=data.get("value"),
            scope=str(data.get("scope", "trip")),
            polarity=str(data.get("polarity", "neutral")),
            confidence=float(data.get("confidence", 0.0)),
            risk=str(data.get("risk", "low")),
            evidence=str(data.get("evidence", "")),
            reason=str(data.get("reason", "")),
            attributes=dict(data.get("attributes", {})),
        )


@dataclass
class MemoryEvent:
    id: str
    user_id: str
    session_id: str
    event_type: str
    object_type: str
    object_payload: dict[str, Any]
    reason_text: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "event_type": self.event_type,
            "object_type": self.object_type,
            "object_payload": self.object_payload,
            "reason_text": self.reason_text,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryEvent":
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            session_id=str(data["session_id"]),
            event_type=str(data["event_type"]),
            object_type=str(data["object_type"]),
            object_payload=dict(data.get("object_payload", {})),
            reason_text=data.get("reason_text"),
            created_at=str(data["created_at"]),
        )


@dataclass
class TripEpisode:
    id: str
    user_id: str
    session_id: str
    trip_id: str | None
    destination: str | None
    dates: str | None
    travelers: dict[str, Any] | None
    budget: dict[str, Any] | None
    selected_skeleton: dict[str, Any] | None
    final_plan_summary: str
    accepted_items: list[dict[str, Any]]
    rejected_items: list[dict[str, Any]]
    lessons: list[str]
    satisfaction: int | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "trip_id": self.trip_id,
            "destination": self.destination,
            "dates": self.dates,
            "travelers": self.travelers,
            "budget": self.budget,
            "selected_skeleton": self.selected_skeleton,
            "final_plan_summary": self.final_plan_summary,
            "accepted_items": self.accepted_items,
            "rejected_items": self.rejected_items,
            "lessons": self.lessons,
            "satisfaction": self.satisfaction,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TripEpisode":
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            session_id=str(data["session_id"]),
            trip_id=data.get("trip_id"),
            destination=data.get("destination"),
            dates=data.get("dates"),
            travelers=data.get("travelers"),
            budget=data.get("budget"),
            selected_skeleton=data.get("selected_skeleton"),
            final_plan_summary=str(data.get("final_plan_summary", "")),
            accepted_items=list(data.get("accepted_items", [])),
            rejected_items=list(data.get("rejected_items", [])),
            lessons=list(data.get("lessons", [])),
            satisfaction=data.get("satisfaction"),
            created_at=str(data.get("created_at", "")),
        )
```

- [ ] **Step 4: Run model tests**

Run: `cd backend && python -m pytest tests/test_memory_models.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/memory/models.py backend/tests/test_memory_models.py
git commit -m "feat: add structured memory models"
```

---

## Task 2: File-Backed MemoryStore and Migration

**Files:**
- Create: `backend/memory/store.py`
- Modify: `backend/memory/manager.py`
- Test: `backend/tests/test_memory_store.py`

- [ ] **Step 1: Write store tests**

Create `backend/tests/test_memory_store.py`:

```python
import asyncio
import json
from pathlib import Path

import pytest

from memory.models import MemoryEvent, MemoryItem, MemorySource, Rejection, UserMemory
from memory.store import FileMemoryStore


@pytest.mark.asyncio
async def test_load_empty_returns_schema_v2(tmp_path: Path):
    store = FileMemoryStore(tmp_path)
    envelope = await store.load_envelope("u1")
    assert envelope["schema_version"] == 2
    assert envelope["user_id"] == "u1"
    assert envelope["items"] == []


@pytest.mark.asyncio
async def test_migrates_legacy_user_memory(tmp_path: Path):
    user_dir = tmp_path / "users" / "u1"
    user_dir.mkdir(parents=True)
    legacy = UserMemory(
        user_id="u1",
        explicit_preferences={"节奏": "轻松"},
        rejections=[Rejection(item="红眼航班", reason="休息不好", permanent=True)],
    )
    (user_dir / "memory.json").write_text(
        json.dumps(legacy.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )
    store = FileMemoryStore(tmp_path)
    items = await store.list_items("u1")
    assert {item.key for item in items} == {"节奏", "avoid"}
    assert all(item.source.kind == "migration" for item in items)


@pytest.mark.asyncio
async def test_upsert_item_writes_schema_v2(tmp_path: Path):
    store = FileMemoryStore(tmp_path)
    item = MemoryItem(
        id="mem1",
        user_id="u1",
        type="preference",
        domain="pace",
        key="preferred_pace",
        value="轻松",
        scope="global",
        polarity="like",
        confidence=0.9,
        status="active",
        source=MemorySource(kind="message", session_id="s1"),
        created_at="2026-04-11T00:00:00",
        updated_at="2026-04-11T00:00:00",
    )
    await store.upsert_item(item)
    loaded = await store.list_items("u1")
    assert loaded == [item]
    raw = json.loads((tmp_path / "users" / "u1" / "memory.json").read_text())
    assert raw["schema_version"] == 2


@pytest.mark.asyncio
async def test_update_status_marks_item(tmp_path: Path):
    store = FileMemoryStore(tmp_path)
    item = MemoryItem(
        id="mem1",
        user_id="u1",
        type="preference",
        domain="pace",
        key="preferred_pace",
        value="轻松",
        scope="global",
        polarity="like",
        confidence=0.9,
        status="pending",
        source=MemorySource(kind="message", session_id="s1"),
        created_at="2026-04-11T00:00:00",
        updated_at="2026-04-11T00:00:00",
    )
    await store.upsert_item(item)
    await store.update_status("u1", "mem1", "active")
    assert (await store.list_items("u1"))[0].status == "active"


@pytest.mark.asyncio
async def test_append_event_writes_jsonl(tmp_path: Path):
    store = FileMemoryStore(tmp_path)
    event = MemoryEvent(
        id="evt1",
        user_id="u1",
        session_id="s1",
        event_type="accept",
        object_type="skeleton",
        object_payload={"id": "sk1"},
        reason_text=None,
        created_at="2026-04-11T00:00:00",
    )
    await store.append_event(event)
    path = tmp_path / "users" / "u1" / "memory_events.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["id"] == "evt1"


@pytest.mark.asyncio
async def test_concurrent_upserts_do_not_drop_items(tmp_path: Path):
    store = FileMemoryStore(tmp_path)

    async def write_item(index: int):
        await store.upsert_item(
            MemoryItem(
                id=f"mem{index}",
                user_id="u1",
                type="preference",
                domain="general",
                key=f"k{index}",
                value=f"v{index}",
                scope="global",
                polarity="neutral",
                confidence=0.8,
                status="active",
                source=MemorySource(kind="message", session_id="s1"),
                created_at="2026-04-11T00:00:00",
                updated_at="2026-04-11T00:00:00",
            )
        )

    await asyncio.gather(*(write_item(i) for i in range(10)))
    loaded = await store.list_items("u1")
    assert len(loaded) == 10
```

- [ ] **Step 2: Run store tests and verify failure**

Run: `cd backend && python -m pytest tests/test_memory_store.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'memory.store'`.

- [ ] **Step 3: Implement `FileMemoryStore`**

Create `backend/memory/store.py`:

```python
from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from memory.models import (
    MemoryEvent,
    MemoryItem,
    MemorySource,
    Rejection,
    TripEpisode,
    TripSummary,
    UserMemory,
    generate_memory_id,
)


class FileMemoryStore:
    def __init__(self, data_dir: str | Path = "./data"):
        self.data_dir = Path(data_dir)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _user_dir(self, user_id: str) -> Path:
        return self.data_dir / "users" / user_id

    def _memory_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "memory.json"

    async def load_envelope(self, user_id: str) -> dict[str, Any]:
        async with self._locks[user_id]:
            return self._load_envelope_unlocked(user_id)

    def _load_envelope_unlocked(self, user_id: str) -> dict[str, Any]:
        path = self._memory_path(user_id)
        if not path.exists():
            return {"schema_version": 2, "user_id": user_id, "items": [], "legacy": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") == 2:
            data.setdefault("items", [])
            data.setdefault("legacy", {})
            return data
        legacy = UserMemory.from_dict(data)
        items = self._migrate_legacy(legacy)
        return {
            "schema_version": 2,
            "user_id": user_id,
            "items": [item.to_dict() for item in items],
            "legacy": data,
        }

    def _migrate_legacy(self, memory: UserMemory) -> list[MemoryItem]:
        items: list[MemoryItem] = []
        timestamp = "1970-01-01T00:00:00"
        for key, value in memory.explicit_preferences.items():
            items.append(
                MemoryItem(
                    id=generate_memory_id(
                        user_id=memory.user_id,
                        type="preference",
                        domain="general",
                        key=str(key),
                        scope="global",
                    ),
                    user_id=memory.user_id,
                    type="preference",
                    domain="general",
                    key=str(key),
                    value=value,
                    scope="global",
                    polarity="neutral",
                    confidence=0.8,
                    status="active",
                    source=MemorySource(kind="migration", session_id=""),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        for key, value in memory.implicit_preferences.items():
            items.append(
                MemoryItem(
                    id=generate_memory_id(
                        user_id=memory.user_id,
                        type="preference",
                        domain="general",
                        key=str(key),
                        scope="global",
                    ),
                    user_id=memory.user_id,
                    type="preference",
                    domain="general",
                    key=str(key),
                    value=value,
                    scope="global",
                    polarity="neutral",
                    confidence=0.6,
                    status="active",
                    source=MemorySource(kind="migration", session_id=""),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        for rejection in memory.rejections:
            items.append(
                MemoryItem(
                    id=generate_memory_id(
                        user_id=memory.user_id,
                        type="rejection",
                        domain="general",
                        key="avoid",
                        scope="global",
                        value=rejection.item,
                    ),
                    user_id=memory.user_id,
                    type="rejection",
                    domain="general",
                    key="avoid",
                    value=rejection.item,
                    scope="global",
                    polarity="avoid",
                    confidence=0.9 if rejection.permanent else 0.6,
                    status="active" if rejection.permanent else "pending",
                    source=MemorySource(kind="migration", session_id=""),
                    created_at=timestamp,
                    updated_at=timestamp,
                    attributes={"reason": rejection.reason, "context": rejection.context},
                )
            )
        return items

    def _write_envelope_unlocked(self, user_id: str, envelope: dict[str, Any]) -> None:
        user_dir = self._user_dir(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        path = self._memory_path(user_id)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)

    async def list_items(self, user_id: str, *, status: str | None = None) -> list[MemoryItem]:
        async with self._locks[user_id]:
            envelope = self._load_envelope_unlocked(user_id)
            items = [MemoryItem.from_dict(row) for row in envelope.get("items", [])]
            if status is not None:
                items = [item for item in items if item.status == status]
            return items

    async def upsert_item(self, item: MemoryItem) -> None:
        async with self._locks[item.user_id]:
            envelope = self._load_envelope_unlocked(item.user_id)
            rows = [MemoryItem.from_dict(row) for row in envelope.get("items", [])]
            replaced = False
            for index, existing in enumerate(rows):
                if existing.id == item.id:
                    rows[index] = item
                    replaced = True
                    break
            if not replaced:
                rows.append(item)
            envelope["items"] = [row.to_dict() for row in rows]
            envelope["schema_version"] = 2
            envelope["user_id"] = item.user_id
            self._write_envelope_unlocked(item.user_id, envelope)

    async def update_status(self, user_id: str, item_id: str, status: str) -> None:
        async with self._locks[user_id]:
            envelope = self._load_envelope_unlocked(user_id)
            rows = [MemoryItem.from_dict(row) for row in envelope.get("items", [])]
            for item in rows:
                if item.id == item_id:
                    item.status = status
            envelope["items"] = [row.to_dict() for row in rows]
            self._write_envelope_unlocked(user_id, envelope)

    async def append_event(self, event: MemoryEvent) -> None:
        async with self._locks[event.user_id]:
            user_dir = self._user_dir(event.user_id)
            user_dir.mkdir(parents=True, exist_ok=True)
            with (user_dir / "memory_events.jsonl").open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    async def append_episode(self, episode: TripEpisode) -> None:
        async with self._locks[episode.user_id]:
            user_dir = self._user_dir(episode.user_id)
            user_dir.mkdir(parents=True, exist_ok=True)
            with (user_dir / "trip_episodes.jsonl").open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(episode.to_dict(), ensure_ascii=False) + "\n")

    async def list_episodes(
        self, user_id: str, *, destination: str | None = None
    ) -> list[TripEpisode]:
        async with self._locks[user_id]:
            path = self._user_dir(user_id) / "trip_episodes.jsonl"
            if not path.exists():
                return []
            episodes = [
                TripEpisode.from_dict(json.loads(line))
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if destination:
                episodes = [episode for episode in episodes if episode.destination == destination]
            return episodes
```

- [ ] **Step 4: Run store tests**

Run: `cd backend && python -m pytest tests/test_memory_store.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/memory/store.py backend/tests/test_memory_store.py
git commit -m "feat: add file-backed memory store"
```

---

## Task 3: Memory Policy, Redaction, and Merge

**Files:**
- Create: `backend/memory/policy.py`
- Test: `backend/tests/test_memory_policy.py`

- [ ] **Step 1: Write policy tests**

Create `backend/tests/test_memory_policy.py`:

```python
from memory.models import MemoryCandidate, MemoryItem, MemorySource
from memory.policy import MemoryMerger, MemoryPolicy


def candidate(**overrides):
    data = {
        "type": "preference",
        "domain": "pace",
        "key": "preferred_pace",
        "value": "轻松",
        "scope": "global",
        "polarity": "like",
        "confidence": 0.9,
        "risk": "low",
        "evidence": "我一直喜欢轻松一点",
        "reason": "长期偏好",
    }
    data.update(overrides)
    return MemoryCandidate(**data)


def test_low_risk_high_confidence_auto_saves():
    policy = MemoryPolicy()
    action = policy.classify(candidate())
    assert action == "auto_save"


def test_medium_risk_defaults_to_pending():
    policy = MemoryPolicy()
    action = policy.classify(
        candidate(domain="flight", key="avoid_red_eye", value=True, risk="medium")
    )
    assert action == "pending"


def test_high_risk_is_pending():
    policy = MemoryPolicy()
    action = policy.classify(
        candidate(domain="food", key="allergies", value=["peanut"], risk="high")
    )
    assert action == "pending"


def test_payment_candidate_is_dropped():
    policy = MemoryPolicy()
    action = policy.classify(
        candidate(domain="payment", key="card", value="4111111111111111", risk="high")
    )
    assert action == "drop"


def test_redacts_passport_number_candidate():
    policy = MemoryPolicy()
    action = policy.classify(
        candidate(
            domain="documents",
            key="passport_validity",
            value={"number": "E12345678", "country": "CN"},
            risk="high",
        )
    )
    assert action == "drop"


def test_candidate_to_item_sets_pending_status_for_medium():
    policy = MemoryPolicy()
    item = policy.to_item(
        candidate(domain="flight", key="avoid_red_eye", value=True, risk="medium"),
        user_id="u1",
        session_id="s1",
        now="2026-04-11T00:00:00",
    )
    assert item.status == "pending"
    assert item.source.quote == "我一直喜欢轻松一点"


def test_merge_same_scalar_conflict_creates_pending_conflict():
    existing = MemoryItem(
        id="mem1",
        user_id="u1",
        type="preference",
        domain="pace",
        key="preferred_pace",
        value="轻松",
        scope="global",
        polarity="like",
        confidence=0.9,
        status="active",
        source=MemorySource(kind="message", session_id="s1"),
        created_at="2026-04-10T00:00:00",
        updated_at="2026-04-10T00:00:00",
    )
    incoming = MemoryItem(
        id="mem1",
        user_id="u1",
        type="preference",
        domain="pace",
        key="preferred_pace",
        value="紧凑",
        scope="global",
        polarity="like",
        confidence=0.9,
        status="active",
        source=MemorySource(kind="message", session_id="s2"),
        created_at="2026-04-11T00:00:00",
        updated_at="2026-04-11T00:00:00",
    )
    merged = MemoryMerger().merge([existing], incoming)
    assert merged[0].status == "obsolete"
    assert merged[1].status == "pending_conflict"


def test_merge_list_values_unions():
    existing = MemoryItem(
        id="mem1",
        user_id="u1",
        type="preference",
        domain="food",
        key="cuisine_likes",
        value=["寿司"],
        scope="global",
        polarity="like",
        confidence=0.7,
        status="active",
        source=MemorySource(kind="message", session_id="s1"),
        created_at="2026-04-10T00:00:00",
        updated_at="2026-04-10T00:00:00",
    )
    incoming = MemoryItem(
        id="mem1",
        user_id="u1",
        type="preference",
        domain="food",
        key="cuisine_likes",
        value=["拉面"],
        scope="global",
        polarity="like",
        confidence=0.8,
        status="active",
        source=MemorySource(kind="message", session_id="s2"),
        created_at="2026-04-11T00:00:00",
        updated_at="2026-04-11T00:00:00",
    )
    merged = MemoryMerger().merge([existing], incoming)
    assert merged[0].value == ["寿司", "拉面"]
    assert merged[0].confidence == 0.8
```

- [ ] **Step 2: Run policy tests and verify failure**

Run: `cd backend && python -m pytest tests/test_memory_policy.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'memory.policy'`.

- [ ] **Step 3: Implement policy module**

Create `backend/memory/policy.py`:

```python
from __future__ import annotations

import re
from typing import Any

from memory.models import (
    MemoryCandidate,
    MemoryItem,
    MemorySource,
    generate_memory_id,
)

_DENIED_DOMAINS = {"payment", "membership"}
_SENSITIVE_NUMBER_RE = re.compile(r"\d{9,18}")


class MemoryPolicy:
    def __init__(
        self,
        *,
        auto_save_low_risk: bool = True,
        auto_save_medium_risk: bool = False,
    ):
        self.auto_save_low_risk = auto_save_low_risk
        self.auto_save_medium_risk = auto_save_medium_risk

    def classify(self, candidate: MemoryCandidate) -> str:
        if candidate.domain in _DENIED_DOMAINS:
            return "drop"
        if self._contains_forbidden_pii(candidate.value):
            return "drop"
        if candidate.risk == "high":
            return "pending"
        if candidate.risk == "medium":
            if candidate.confidence < 0.8:
                return "pending"
            return "auto_save" if self.auto_save_medium_risk else "pending"
        if candidate.confidence < 0.7:
            return "pending"
        return "auto_save" if self.auto_save_low_risk else "pending"

    def _contains_forbidden_pii(self, value: Any) -> bool:
        if isinstance(value, str):
            return bool(_SENSITIVE_NUMBER_RE.search(value))
        if isinstance(value, list):
            return any(self._contains_forbidden_pii(item) for item in value)
        if isinstance(value, dict):
            if "number" in value:
                return True
            return any(self._contains_forbidden_pii(item) for item in value.values())
        return False

    def to_item(
        self,
        candidate: MemoryCandidate,
        *,
        user_id: str,
        session_id: str,
        now: str,
        trip_id: str | None = None,
    ) -> MemoryItem:
        action = self.classify(candidate)
        status = "pending" if action == "pending" else "active"
        return MemoryItem(
            id=generate_memory_id(
                user_id=user_id,
                type=candidate.type,
                domain=candidate.domain,
                key=candidate.key,
                scope=candidate.scope,
                trip_id=trip_id if candidate.scope == "trip" else None,
                value=candidate.value,
            ),
            user_id=user_id,
            type=candidate.type,
            domain=candidate.domain,
            key=candidate.key,
            value=candidate.value,
            scope=candidate.scope,
            polarity=candidate.polarity,
            confidence=candidate.confidence,
            status=status,
            source=MemorySource(
                kind="message",
                session_id=session_id,
                quote=candidate.evidence[:120],
            ),
            created_at=now,
            updated_at=now,
            session_id=session_id,
            trip_id=trip_id if candidate.scope == "trip" else None,
            attributes=candidate.attributes,
        )


class MemoryMerger:
    def merge(self, existing_items: list[MemoryItem], incoming: MemoryItem) -> list[MemoryItem]:
        merged = list(existing_items)
        for index, existing in enumerate(merged):
            if existing.id != incoming.id:
                continue
            if existing.value == incoming.value:
                existing.updated_at = incoming.updated_at
                existing.confidence = max(existing.confidence, incoming.confidence)
                return merged
            if isinstance(existing.value, list) and isinstance(incoming.value, list):
                values = list(dict.fromkeys([*existing.value, *incoming.value]))
                existing.value = values
                existing.updated_at = incoming.updated_at
                existing.confidence = max(existing.confidence, incoming.confidence)
                return merged
            existing.status = "obsolete"
            incoming.status = "pending_conflict"
            merged[index] = existing
            merged.append(incoming)
            return merged
        merged.append(incoming)
        return merged
```

- [ ] **Step 4: Run policy tests**

Run: `cd backend && python -m pytest tests/test_memory_policy.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/memory/policy.py backend/tests/test_memory_policy.py
git commit -m "feat: add memory policy and merge rules"
```

---

## Task 4: Candidate Extraction Parser and Prompt

**Files:**
- Modify: `backend/memory/extraction.py`
- Test: `backend/tests/test_memory_extraction.py`

- [ ] **Step 1: Extend extraction tests**

Append to `backend/tests/test_memory_extraction.py`:

```python
from memory.extraction import (
    build_candidate_extraction_prompt,
    parse_candidate_extraction_response,
)


class TestCandidateExtraction:
    def test_candidate_prompt_includes_watermarked_messages(self):
        prompt = build_candidate_extraction_prompt(
            user_messages=["以后我都不坐红眼航班"],
            existing_items=[],
            plan_facts={"budget": {"total": 30000}},
        )
        assert "以后我都不坐红眼航班" in prompt
        assert "本次目的地、日期、预算默认不是 global memory" in prompt

    def test_parse_candidate_response(self):
        response = """
        {
          "candidates": [
            {
              "type": "preference",
              "domain": "flight",
              "key": "avoid_red_eye",
              "value": true,
              "scope": "global",
              "polarity": "avoid",
              "confidence": 0.95,
              "risk": "medium",
              "evidence": "以后我都不坐红眼航班",
              "reason": "用户明确表达长期偏好"
            }
          ]
        }
        """
        candidates = parse_candidate_extraction_response(response)
        assert len(candidates) == 1
        assert candidates[0].domain == "flight"
        assert candidates[0].key == "avoid_red_eye"

    def test_parse_candidate_response_drops_runtime_domain_drift():
        response = '{"candidates": [{"type": "preference", "domain": "made_up", "key": "x", "value": "y", "scope": "global", "polarity": "like", "confidence": 0.8, "risk": "low", "evidence": "x", "reason": "x"}]}'
        candidates = parse_candidate_extraction_response(response)
        assert candidates[0].domain == "general"
        assert candidates[0].attributes["raw_domain"] == "made_up"

    def test_parse_bad_candidate_json_returns_empty_list(self):
        assert parse_candidate_extraction_response("not json") == []
```

- [ ] **Step 2: Run extraction tests and verify failure**

Run: `cd backend && python -m pytest tests/test_memory_extraction.py::TestCandidateExtraction -v`

Expected: FAIL with import errors for new functions.

- [ ] **Step 3: Implement candidate extraction helpers**

Modify `backend/memory/extraction.py` by adding imports and functions while keeping existing legacy functions:

```python
from memory.models import MemoryCandidate, MemoryItem

_ALLOWED_DOMAINS = {
    "pace",
    "food",
    "hotel",
    "flight",
    "train",
    "budget",
    "family",
    "accessibility",
    "planning_style",
    "destination",
    "documents",
    "general",
}


def build_candidate_extraction_prompt(
    *,
    user_messages: list[str],
    existing_items: list[MemoryItem],
    plan_facts: dict[str, Any],
) -> str:
    messages_text = "\n".join(f"- {message}" for message in user_messages)
    items_text = json.dumps(
        [item.to_dict() for item in existing_items],
        ensure_ascii=False,
        indent=2,
    )
    facts_text = json.dumps(plan_facts, ensure_ascii=False, indent=2)
    return f"""从用户消息中提取旅行 Agent 可长期或本次使用的 memory candidates。

用户消息：
{messages_text}

当前已解析的本次旅行事实：
{facts_text}

已有 memory items：
{items_text}

规则：
- 只输出用户明确表达或行为强烈暗示的偏好、约束、排除、画像。
- 本次目的地、日期、预算默认不是 global memory；如有"以后/每次/一直/都"等长期信号才可设置 scope=global。
- "这次/本次/这趟"默认 scope=trip。
- 允许 domain：pace, food, hotel, flight, train, budget, family, accessibility, planning_style, destination, documents, general。
- 不允许存支付、会员、证件号、身份证号、完整健康病史、家庭成员姓名。
- 健康、过敏、证件、家庭成员、永久排除必须 risk=high 或 medium，并进入待确认。

严格输出 JSON：
{{"candidates": [{{"type": "preference", "domain": "flight", "key": "avoid_red_eye", "value": true, "scope": "global", "polarity": "avoid", "confidence": 0.95, "risk": "medium", "evidence": "用户原文短证据", "reason": "为什么这是记忆"}}]}}
如果没有可提取内容，输出 {{"candidates": []}}"""


def parse_candidate_extraction_response(response: str) -> list[MemoryCandidate]:
    text = response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    raw_candidates = data.get("candidates", [])
    if not isinstance(raw_candidates, list):
        return []
    parsed: list[MemoryCandidate] = []
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        candidate = MemoryCandidate.from_dict(raw)
        if candidate.domain not in _ALLOWED_DOMAINS:
            candidate.attributes["raw_domain"] = candidate.domain
            candidate.domain = "general"
        parsed.append(candidate)
    return parsed
```

- [ ] **Step 4: Run full memory extraction tests**

Run: `cd backend && python -m pytest tests/test_memory_extraction.py -v`

Expected: PASS, including legacy tests.

- [ ] **Step 5: Commit**

```bash
git add backend/memory/extraction.py backend/tests/test_memory_extraction.py
git commit -m "feat: add memory candidate extraction parser"
```

---

## Task 5: Retrieval and Prompt Formatting

**Files:**
- Create: `backend/memory/retriever.py`
- Create: `backend/memory/formatter.py`
- Test: `backend/tests/test_memory_retriever.py`
- Test: `backend/tests/test_memory_formatter.py`

- [ ] **Step 1: Write retriever tests**

Create `backend/tests/test_memory_retriever.py`:

```python
from memory.models import MemoryItem, MemorySource, TripEpisode
from memory.retriever import MemoryRetriever
from state.models import TravelPlanState


def item(id, domain, key, scope, status="active", trip_id=None):
    return MemoryItem(
        id=id,
        user_id="u1",
        type="preference",
        domain=domain,
        key=key,
        value="value",
        scope=scope,
        polarity="like",
        confidence=0.9,
        status=status,
        source=MemorySource(kind="message", session_id="s1"),
        created_at="2026-04-11T00:00:00",
        updated_at="2026-04-11T00:00:00",
        trip_id=trip_id,
    )


def test_core_profile_uses_active_global_only():
    retriever = MemoryRetriever()
    result = retriever.retrieve_core_profile(
        [
            item("a", "pace", "preferred_pace", "global"),
            item("b", "food", "allergies", "trip", trip_id="trip1"),
            item("c", "hotel", "hotel_style", "global", status="pending"),
        ],
        limit=10,
    )
    assert [memory.id for memory in result] == ["a"]


def test_trip_memory_filters_by_trip_id():
    retriever = MemoryRetriever()
    plan = TravelPlanState(session_id="s1")
    plan.trip_id = "trip1"
    result = retriever.retrieve_trip_memory(
        [
            item("a", "hotel", "location_preference", "trip", trip_id="trip1"),
            item("b", "hotel", "location_preference", "trip", trip_id="trip2"),
        ],
        plan,
    )
    assert [memory.id for memory in result] == ["a"]


def test_phase_relevant_for_phase5_focuses_daily_planning_domains():
    retriever = MemoryRetriever()
    plan = TravelPlanState(session_id="s1", phase=5)
    result = retriever.retrieve_phase_relevant(
        [
            item("pace", "pace", "preferred_pace", "global"),
            item("food", "food", "dietary_restrictions", "global"),
            item("flight", "flight", "avoid_red_eye", "global"),
        ],
        plan,
        phase=5,
        limit=10,
    )
    assert [memory.id for memory in result] == ["pace", "food"]
```

- [ ] **Step 2: Write formatter tests**

Create `backend/tests/test_memory_formatter.py`:

```python
from memory.formatter import RetrievedMemory, format_memory_context
from memory.models import MemoryItem, MemorySource


def make_item(id, domain, key, value, scope):
    return MemoryItem(
        id=id,
        user_id="u1",
        type="preference",
        domain=domain,
        key=key,
        value=value,
        scope=scope,
        polarity="like",
        confidence=0.9,
        status="active",
        source=MemorySource(kind="message", session_id="s1"),
        created_at="2026-04-11T00:00:00",
        updated_at="2026-04-11T00:00:00",
    )


def test_format_empty_memory_context():
    context = format_memory_context(RetrievedMemory(core=[], trip=[], phase=[]))
    assert context == "暂无相关用户记忆"


def test_format_memory_context_has_three_sections():
    context = format_memory_context(
        RetrievedMemory(
            core=[make_item("a", "pace", "preferred_pace", "轻松", "global")],
            trip=[make_item("b", "hotel", "location_preference", "新宿", "trip")],
            phase=[make_item("c", "food", "dietary_restrictions", "不吃辣", "global")],
        )
    )
    assert "## 核心用户画像" in context
    assert "## 本次旅行记忆" in context
    assert "## 当前阶段相关历史" in context
    assert "[pace] preferred_pace: 轻松" in context
```

- [ ] **Step 3: Run retriever and formatter tests and verify failure**

Run: `cd backend && python -m pytest tests/test_memory_retriever.py tests/test_memory_formatter.py -v`

Expected: FAIL with import errors for `memory.retriever` and `memory.formatter`.

- [ ] **Step 4: Implement retriever**

Create `backend/memory/retriever.py`:

```python
from __future__ import annotations

from memory.models import MemoryItem
from state.models import TravelPlanState

_PHASE_DOMAINS = {
    1: {"destination", "pace", "budget", "family", "planning_style"},
    3: {"destination", "pace", "budget", "family", "hotel", "flight", "train", "accessibility"},
    5: {"pace", "food", "accessibility", "family", "budget"},
    7: {"documents", "flight", "train", "food", "accessibility"},
}


class MemoryRetriever:
    def retrieve_core_profile(
        self, items: list[MemoryItem], *, limit: int = 10
    ) -> list[MemoryItem]:
        candidates = [
            item
            for item in items
            if item.status == "active" and item.scope == "global"
        ]
        return self._rank(candidates)[:limit]

    def retrieve_trip_memory(
        self, items: list[MemoryItem], plan: TravelPlanState
    ) -> list[MemoryItem]:
        trip_id = getattr(plan, "trip_id", None)
        if not trip_id:
            return []
        return [
            item
            for item in items
            if item.status == "active"
            and item.scope == "trip"
            and item.trip_id == trip_id
        ]

    def retrieve_phase_relevant(
        self,
        items: list[MemoryItem],
        plan: TravelPlanState,
        *,
        phase: int,
        limit: int = 8,
    ) -> list[MemoryItem]:
        domains = _PHASE_DOMAINS.get(phase, {"general"})
        candidates = [
            item
            for item in items
            if item.status == "active"
            and item.domain in domains
            and item.scope in {"global", "trip"}
        ]
        trip_id = getattr(plan, "trip_id", None)
        candidates = [
            item
            for item in candidates
            if item.scope == "global" or (trip_id and item.trip_id == trip_id)
        ]
        return self._rank(candidates)[:limit]

    def _rank(self, items: list[MemoryItem]) -> list[MemoryItem]:
        return sorted(
            items,
            key=lambda item: (
                item.status == "active",
                item.type in {"constraint", "rejection"},
                item.confidence,
                item.updated_at,
            ),
            reverse=True,
        )
```

- [ ] **Step 5: Implement formatter**

Create `backend/memory/formatter.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from memory.models import MemoryItem


@dataclass
class RetrievedMemory:
    core: list[MemoryItem]
    trip: list[MemoryItem]
    phase: list[MemoryItem]


def _format_value(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(str(item) for item in value)
    if isinstance(value, dict):
        return "；".join(f"{key}={value[key]}" for key in sorted(value))
    return str(value)


def _format_items(items: list[MemoryItem]) -> list[str]:
    return [
        f"- [{item.domain}] {item.key}: {_format_value(item.value)}"
        for item in items
    ]


def format_memory_context(retrieved: RetrievedMemory) -> str:
    if not retrieved.core and not retrieved.trip and not retrieved.phase:
        return "暂无相关用户记忆"
    parts: list[str] = []
    if retrieved.core:
        parts.append("## 核心用户画像")
        parts.extend(_format_items(retrieved.core))
    if retrieved.trip:
        parts.append("## 本次旅行记忆")
        parts.extend(_format_items(retrieved.trip))
    if retrieved.phase:
        parts.append("## 当前阶段相关历史")
        parts.extend(_format_items(retrieved.phase))
    return "\n".join(parts)
```

- [ ] **Step 6: Run retriever and formatter tests**

Run: `cd backend && python -m pytest tests/test_memory_retriever.py tests/test_memory_formatter.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/memory/retriever.py backend/memory/formatter.py backend/tests/test_memory_retriever.py backend/tests/test_memory_formatter.py
git commit -m "feat: add memory retrieval and formatting"
```

---

## Task 6: Config, Manager Facade, Context Injection, and Trip Id

**Files:**
- Modify: `backend/config.py`
- Modify: `config.yaml`
- Modify: `backend/memory/manager.py`
- Modify: `backend/context/manager.py`
- Modify: `backend/agent/loop.py`
- Modify: `backend/state/models.py`
- Test: `backend/tests/test_memory_manager.py`
- Test: existing context and telemetry tests

- [ ] **Step 1: Write manager facade tests**

Create `backend/tests/test_memory_manager.py`:

```python
import pytest

from memory.manager import MemoryManager
from memory.models import MemoryItem, MemorySource
from state.models import TravelPlanState


@pytest.mark.asyncio
async def test_generate_context_uses_retrieved_memory(tmp_path):
    manager = MemoryManager(data_dir=str(tmp_path))
    item = MemoryItem(
        id="mem1",
        user_id="u1",
        type="preference",
        domain="pace",
        key="preferred_pace",
        value="轻松",
        scope="global",
        polarity="like",
        confidence=0.9,
        status="active",
        source=MemorySource(kind="message", session_id="s1"),
        created_at="2026-04-11T00:00:00",
        updated_at="2026-04-11T00:00:00",
    )
    await manager.store.upsert_item(item)
    context = await manager.generate_context("u1", TravelPlanState(session_id="s1", phase=5))
    assert "## 核心用户画像" in context
    assert "preferred_pace: 轻松" in context


@pytest.mark.asyncio
async def test_legacy_load_still_returns_user_memory(tmp_path):
    manager = MemoryManager(data_dir=str(tmp_path))
    memory = await manager.load("u1")
    assert memory.user_id == "u1"
```

- [ ] **Step 2: Run manager tests and verify failure**

Run: `cd backend && python -m pytest tests/test_memory_manager.py -v`

Expected: FAIL because `MemoryManager` has no `store` or `generate_context`.

- [ ] **Step 3: Add `trip_id` to `TravelPlanState`**

Modify `backend/state/models.py`:

```python
@dataclass
class TravelPlanState:
    session_id: str
    phase: int = 1
    trip_id: str | None = None
    destination: str | None = None
```

Update `to_dict()` to include:

```python
"trip_id": self.trip_id,
```

Update `from_dict()` constructor arguments to include:

```python
trip_id=d.get("trip_id"),
```

- [ ] **Step 4: Update `MemoryManager` facade**

Replace `backend/memory/manager.py` with a facade that preserves legacy methods:

```python
from __future__ import annotations

import json
from pathlib import Path

from memory.formatter import RetrievedMemory, format_memory_context
from memory.models import UserMemory
from memory.retriever import MemoryRetriever
from memory.store import FileMemoryStore
from state.models import TravelPlanState


class MemoryManager:
    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)
        self.store = FileMemoryStore(self.data_dir)
        self.retriever = MemoryRetriever()

    def _user_dir(self, user_id: str) -> Path:
        return self.data_dir / "users" / user_id

    async def save(self, memory: UserMemory) -> None:
        user_dir = self._user_dir(memory.user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        path = user_dir / "memory.json"
        path.write_text(json.dumps(memory.to_dict(), ensure_ascii=False, indent=2))

    async def load(self, user_id: str) -> UserMemory:
        path = self._user_dir(user_id) / "memory.json"
        if not path.exists():
            return UserMemory(user_id=user_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") == 2:
            legacy = data.get("legacy", {})
            if legacy:
                return UserMemory.from_dict(legacy)
            return UserMemory(user_id=user_id)
        return UserMemory.from_dict(data)

    def generate_summary(self, memory: UserMemory) -> str:
        parts: list[str] = []
        if memory.explicit_preferences:
            prefs = ", ".join(
                f"{k}: {v}" for k, v in memory.explicit_preferences.items()
            )
            parts.append(f"偏好：{prefs}")
        if memory.trip_history:
            trips = "; ".join(
                f"{t.destination}({t.dates}, 满意度{t.satisfaction}/5)"
                if t.satisfaction
                else f"{t.destination}({t.dates})"
                for t in memory.trip_history
            )
            parts.append(f"出行历史：{trips}")
        permanent_rejections = [r for r in memory.rejections if r.permanent]
        if permanent_rejections:
            rejects = ", ".join(f"{r.item}({r.reason})" for r in permanent_rejections)
            parts.append(f"永久排除：{rejects}")
        return "\n".join(parts) if parts else "暂无用户画像"

    async def generate_context(self, user_id: str, plan: TravelPlanState) -> str:
        items = await self.store.list_items(user_id)
        retrieved = RetrievedMemory(
            core=self.retriever.retrieve_core_profile(items),
            trip=self.retriever.retrieve_trip_memory(items, plan),
            phase=self.retriever.retrieve_phase_relevant(
                items,
                plan,
                phase=plan.phase,
            ),
        )
        return format_memory_context(retrieved)
```

- [ ] **Step 5: Update `ContextManager` parameter name and heading**

Modify `backend/context/manager.py`:

```python
def build_system_message(
    self,
    plan: TravelPlanState,
    phase_prompt: str,
    memory_context: str = "",
    available_tools: list[str] | None = None,
) -> Message:
```

Replace the memory section:

```python
if memory_context:
    parts.extend(["", "---", "", f"## 相关用户记忆\n\n{memory_context}"])
```

Update `backend/agent/loop.py` phase rebuild call:

```python
memory_context = await self.memory_mgr.generate_context(self.user_id, self.plan)
rebuilt = [
    self.context_manager.build_system_message(
        self.plan,
        phase_prompt,
        memory_context,
        available_tools=self._current_tool_names(to_phase),
    )
]
```

- [ ] **Step 6: Update config**

Modify `backend/config.py` to add:

```python
@dataclass(frozen=True)
class MemoryExtractionV2Config:
    enabled: bool = True
    model: str = "gpt-4o-mini"
    trigger: str = "each_turn"
    max_user_messages: int = 8


@dataclass(frozen=True)
class MemoryPolicyConfig:
    auto_save_low_risk: bool = True
    auto_save_medium_risk: bool = False
    require_confirmation_for_high_risk: bool = True


@dataclass(frozen=True)
class MemoryRetrievalConfig:
    core_limit: int = 10
    phase_limit: int = 8
    include_pending: bool = False


@dataclass(frozen=True)
class MemoryStorageConfig:
    backend: str = "json"


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = True
    extraction: MemoryExtractionV2Config = field(default_factory=MemoryExtractionV2Config)
    policy: MemoryPolicyConfig = field(default_factory=MemoryPolicyConfig)
    retrieval: MemoryRetrievalConfig = field(default_factory=MemoryRetrievalConfig)
    storage: MemoryStorageConfig = field(default_factory=MemoryStorageConfig)
```

Add `memory: MemoryConfig = field(default_factory=MemoryConfig)` to `AppConfig`.

Add builder functions:

```python
def _build_memory_config(raw: dict, legacy: MemoryExtractionConfig) -> MemoryConfig:
    extraction_raw = raw.get("extraction", {})
    policy_raw = raw.get("policy", {})
    retrieval_raw = raw.get("retrieval", {})
    storage_raw = raw.get("storage", {})
    return MemoryConfig(
        enabled=bool(raw.get("enabled", True)),
        extraction=MemoryExtractionV2Config(
            enabled=bool(extraction_raw.get("enabled", legacy.enabled)),
            model=str(extraction_raw.get("model", legacy.model)),
            trigger=str(extraction_raw.get("trigger", "each_turn")),
            max_user_messages=int(extraction_raw.get("max_user_messages", 8)),
        ),
        policy=MemoryPolicyConfig(
            auto_save_low_risk=bool(policy_raw.get("auto_save_low_risk", True)),
            auto_save_medium_risk=bool(policy_raw.get("auto_save_medium_risk", False)),
            require_confirmation_for_high_risk=bool(
                policy_raw.get("require_confirmation_for_high_risk", True)
            ),
        ),
        retrieval=MemoryRetrievalConfig(
            core_limit=int(retrieval_raw.get("core_limit", 10)),
            phase_limit=int(retrieval_raw.get("phase_limit", 8)),
            include_pending=bool(retrieval_raw.get("include_pending", False)),
        ),
        storage=MemoryStorageConfig(
            backend=str(storage_raw.get("backend", "json")),
        ),
    )
```

In `load_config`, build legacy `memory_extraction` first, then set `memory=_build_memory_config(raw.get("memory", {}), memory_extraction)`.

Modify `config.yaml`:

```yaml
memory:
  enabled: true
  extraction:
    enabled: true
    model: "astron-code-latest"
    trigger: "each_turn"
    max_user_messages: 8
  policy:
    auto_save_low_risk: true
    auto_save_medium_risk: false
    require_confirmation_for_high_risk: true
  retrieval:
    core_limit: 10
    phase_limit: 8
    include_pending: false
  storage:
    backend: "json"
```

- [ ] **Step 7: Run targeted tests**

Run: `cd backend && python -m pytest tests/test_memory_manager.py tests/test_state_models.py tests/test_telemetry_agent_loop.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/config.py config.yaml backend/memory/manager.py backend/context/manager.py backend/agent/loop.py backend/state/models.py backend/tests/test_memory_manager.py
git commit -m "feat: wire structured memory context"
```

---

## Task 7: FastAPI Integration, Per-Turn Extraction, APIs, Events, and Episodes

**Files:**
- Modify: `backend/main.py`
- Test: `backend/tests/test_memory_integration.py`

- [ ] **Step 1: Write integration tests**

Create `backend/tests/test_memory_integration.py`:

```python
import json

import pytest
from fastapi.testclient import TestClient

from main import create_app
from memory.models import MemoryCandidate


def test_memory_api_confirm_and_reject(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  provider: openai
  model: test
  api_key: test
  base_url: https://example.com
data_dir: "{data_dir}"
memory:
  enabled: true
  extraction:
    enabled: false
""".format(data_dir=str(tmp_path / "data")),
        encoding="utf-8",
    )
    app = create_app(str(config_path))
    client = TestClient(app)
    response = client.get("/api/memory/u1")
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_memory_episodes_api_returns_list(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  provider: openai
  model: test
  api_key: test
  base_url: https://example.com
data_dir: "{data_dir}"
memory:
  enabled: true
  extraction:
    enabled: false
""".format(data_dir=str(tmp_path / "data")),
        encoding="utf-8",
    )
    app = create_app(str(config_path))
    client = TestClient(app)
    response = client.get("/api/memory/u1/episodes")
    assert response.status_code == 200
    assert response.json()["episodes"] == []


@pytest.mark.asyncio
async def test_memory_pending_event_payload_shape():
    candidate = MemoryCandidate(
        type="preference",
        domain="flight",
        key="avoid_red_eye",
        value=True,
        scope="global",
        polarity="avoid",
        confidence=0.95,
        risk="medium",
        evidence="以后我都不坐红眼航班",
        reason="长期偏好",
    )
    from main import _memory_pending_event

    event = _memory_pending_event([candidate], ["mem1"])
    data = json.loads(event)
    assert data["type"] == "memory_pending"
    assert data["items"][0]["id"] == "mem1"
    assert data["items"][0]["summary"] == "你以后不坐红眼航班"


@pytest.mark.asyncio
async def test_memory_pending_event_from_items_payload_shape():
    from main import _memory_pending_event_from_items
    from memory.models import MemoryItem, MemorySource

    event = _memory_pending_event_from_items(
        [
            MemoryItem(
                id="mem1",
                user_id="u1",
                type="preference",
                domain="flight",
                key="avoid_red_eye",
                value=True,
                scope="global",
                polarity="avoid",
                confidence=0.9,
                status="pending",
                source=MemorySource(kind="message", session_id="s1", quote="以后我都不坐红眼航班"),
                created_at="2026-04-11T00:00:00",
                updated_at="2026-04-11T00:00:00",
            )
        ]
    )
    data = json.loads(event)
    assert data["type"] == "memory_pending"
    assert data["items"][0]["id"] == "mem1"
    assert data["items"][0]["summary"] == "你以后不坐红眼航班"
```

- [ ] **Step 2: Run integration tests and verify failure**

Run: `cd backend && python -m pytest tests/test_memory_integration.py -v`

Expected: FAIL because `/api/memory/{user_id}` and `_memory_pending_event` do not exist.

- [ ] **Step 3: Add helper functions to `backend/main.py`**

Add near existing helper functions:

```python
from datetime import datetime
from uuid import uuid4

from memory.extraction import (
    build_candidate_extraction_prompt,
    parse_candidate_extraction_response,
)
from memory.models import MemoryCandidate, MemoryEvent, MemoryItem, TripEpisode
from memory.policy import MemoryMerger as V2MemoryMerger, MemoryPolicy


def _now_iso() -> str:
    return datetime.now().isoformat()


def _memory_summary(candidate: MemoryCandidate) -> str:
    if candidate.domain == "flight" and candidate.key == "avoid_red_eye":
        return "你以后不坐红眼航班"
    return f"{candidate.domain}.{candidate.key}: {candidate.value}"


def _memory_pending_event(candidates: list[MemoryCandidate], item_ids: list[str]) -> str:
    return json.dumps(
        {
            "type": "memory_pending",
            "items": [
                {
                    "id": item_id,
                    "type": candidate.type,
                    "domain": candidate.domain,
                    "key": candidate.key,
                    "value": candidate.value,
                    "scope": candidate.scope,
                    "polarity": candidate.polarity,
                    "risk": candidate.risk,
                    "evidence": candidate.evidence,
                    "summary": _memory_summary(candidate),
                }
                for candidate, item_id in zip(candidates, item_ids)
            ],
        },
        ensure_ascii=False,
    )


def _memory_pending_event_from_items(items: list[MemoryItem]) -> str:
    candidates = [
        MemoryCandidate(
            type=item.type,
            domain=item.domain,
            key=item.key,
            value=item.value,
            scope=item.scope,
            polarity=item.polarity,
            confidence=item.confidence,
            risk=str(item.attributes.get("risk", "medium")),
            evidence=item.source.quote or "",
            reason=str(item.attributes.get("reason", "")),
        )
        for item in items
    ]
    return _memory_pending_event(candidates, [item.id for item in items])
```

- [ ] **Step 4: Replace system message memory loading**

In `chat`, replace:

```python
memory = await memory_mgr.load(req.user_id)
user_summary = memory_mgr.generate_summary(memory)
sys_msg = context_mgr.build_system_message(
    plan,
    phase_prompt,
    user_summary,
    available_tools=available_tools,
)
```

with:

```python
memory_context = await memory_mgr.generate_context(req.user_id, plan)
sys_msg = context_mgr.build_system_message(
    plan,
    phase_prompt,
    memory_context,
    available_tools=available_tools,
)
```

- [ ] **Step 4.5: Emit previously pending memory at SSE start**

At the beginning of `event_stream`, before iterating `agent.run`, add:

```python
            pending_items = [
                item
                for item in await memory_mgr.store.list_items(session["user_id"])
                if item.status in {"pending", "pending_conflict"}
            ]
            if pending_items:
                yield _memory_pending_event_from_items(pending_items)
```

This implements the spec rule that fire-and-forget extraction completed after the prior SSE stream is surfaced at the beginning of the next chat round. Do not inject these pending items into the system prompt.

- [ ] **Step 5: Add per-turn extraction scheduler**

Replace `_extract_memory_preferences` and `_schedule_memory_extraction` with:

```python
    extraction_tasks_by_session: dict[str, asyncio.Task] = {}

    async def _extract_memory_candidates(
        *,
        user_id: str,
        session_id: str,
        user_messages: list[str],
        plan: TravelPlanState,
    ) -> list[str]:
        if not config.memory.enabled or not config.memory.extraction.enabled:
            return []
        if config.memory.extraction.trigger == "disabled":
            return []
        if not user_messages:
            return []
        try:
            items = await memory_mgr.store.list_items(user_id)
            prompt = build_candidate_extraction_prompt(
                user_messages=user_messages[-config.memory.extraction.max_user_messages:],
                existing_items=items,
                plan_facts=plan.to_dict(),
            )
            extraction_llm = create_llm_provider(
                replace(config.llm, model=config.memory.extraction.model)
            )
            response_parts: list[str] = []
            async for chunk in extraction_llm.chat(
                [Message(role=Role.USER, content=prompt)],
                tools=[],
                stream=False,
            ):
                if chunk.content:
                    response_parts.append(chunk.content)
            candidates = parse_candidate_extraction_response("".join(response_parts))
            policy = MemoryPolicy(
                auto_save_low_risk=config.memory.policy.auto_save_low_risk,
                auto_save_medium_risk=config.memory.policy.auto_save_medium_risk,
            )
            pending_ids: list[str] = []
            for candidate in candidates:
                action = policy.classify(candidate)
                if action == "drop":
                    continue
                item = policy.to_item(
                    candidate,
                    user_id=user_id,
                    session_id=session_id,
                    now=_now_iso(),
                    trip_id=getattr(plan, "trip_id", None),
                )
                existing = await memory_mgr.store.list_items(user_id)
                merged = V2MemoryMerger().merge(existing, item)
                for merged_item in merged:
                    await memory_mgr.store.upsert_item(merged_item)
                if item.status in {"pending", "pending_conflict"}:
                    pending_ids.append(item.id)
            return pending_ids
        except Exception:
            return []

    def _schedule_memory_extraction(
        *,
        user_id: str,
        session_id: str,
        user_messages: list[str],
        plan: TravelPlanState,
    ) -> None:
        if session_id in extraction_tasks_by_session and not extraction_tasks_by_session[session_id].done():
            return
        task = asyncio.create_task(
            _extract_memory_candidates(
                user_id=user_id,
                session_id=session_id,
                user_messages=user_messages,
                plan=plan,
            )
        )
        extraction_tasks_by_session[session_id] = task
        memory_extraction_tasks.add(task)
        task.add_done_callback(memory_extraction_tasks.discard)
```

Update the call site at the end of `event_stream`:

```python
            _schedule_memory_extraction(
                user_id=session["user_id"],
                session_id=plan.session_id,
                user_messages=[req.message],
                plan=plan,
            )
```

- [ ] **Step 6: Add memory APIs**

Add endpoints inside `create_app`:

```python
    class MemoryStatusRequest(BaseModel):
        item_id: str

    class MemoryEventRequest(BaseModel):
        event_type: str
        object_type: str
        object_payload: dict
        reason_text: str | None = None

    @app.get("/api/memory/{user_id}")
    async def list_memory(user_id: str):
        items = await memory_mgr.store.list_items(user_id)
        return {"items": [item.to_dict() for item in items]}

    @app.post("/api/memory/{user_id}/confirm")
    async def confirm_memory(user_id: str, req: MemoryStatusRequest):
        await memory_mgr.store.update_status(user_id, req.item_id, "active")
        return {"status": "active", "item_id": req.item_id}

    @app.post("/api/memory/{user_id}/reject")
    async def reject_memory(user_id: str, req: MemoryStatusRequest):
        await memory_mgr.store.update_status(user_id, req.item_id, "rejected")
        return {"status": "rejected", "item_id": req.item_id}

    @app.delete("/api/memory/{user_id}/{item_id}")
    async def delete_memory(user_id: str, item_id: str):
        await memory_mgr.store.update_status(user_id, item_id, "obsolete")
        return {"status": "obsolete", "item_id": item_id}

    @app.post("/api/memory/{user_id}/events")
    async def create_memory_event(user_id: str, req: MemoryEventRequest):
        await memory_mgr.store.append_event(
            MemoryEvent(
                id=f"evt_{uuid4().hex[:16]}",
                user_id=user_id,
                session_id="api",
                event_type=req.event_type,
                object_type=req.object_type,
                object_payload=req.object_payload,
                reason_text=req.reason_text,
                created_at=_now_iso(),
            )
        )
        return {"status": "created"}

    @app.get("/api/memory/{user_id}/episodes")
    async def list_memory_episodes(user_id: str):
        episodes = await memory_mgr.store.list_episodes(user_id)
        return {"episodes": [episode.to_dict() for episode in episodes]}
```

- [ ] **Step 7: Record minimal memory events**

In the `tool_result` success block where `update_plan_state` yields state updates, inspect `chunk.tool_result.data` and `tool_call_names`. Add helper:

```python
    async def _record_memory_event(
        *,
        user_id: str,
        session_id: str,
        event_type: str,
        object_type: str,
        object_payload: dict,
        reason_text: str | None = None,
    ) -> None:
        await memory_mgr.store.append_event(
            MemoryEvent(
                id=f"evt_{uuid4().hex[:16]}",
                user_id=user_id,
                session_id=session_id,
                event_type=event_type,
                object_type=object_type,
                object_payload=object_payload,
                reason_text=reason_text,
                created_at=_now_iso(),
            )
        )
```

When `update_plan_state` succeeds and `chunk.tool_result.data["updated_field"]` is `selected_skeleton_id`, `selected_transport`, or `accommodation`, call `_record_memory_event` with `event_type="accept"` and object types `skeleton`, `transport`, `hotel`.

In fallback backtrack block, call `_record_memory_event` with `event_type="reject"` and `object_type="phase_output"`.

- [ ] **Step 8: Generate TripEpisode on archive**

When `plan.phase == 7` and archive is saved, append:

```python
                await memory_mgr.store.append_episode(
                    TripEpisode(
                        id=f"ep_{uuid4().hex[:16]}",
                        user_id=session["user_id"],
                        session_id=plan.session_id,
                        trip_id=getattr(plan, "trip_id", None),
                        destination=plan.destination,
                        dates=f"{plan.dates.start} to {plan.dates.end}" if plan.dates else None,
                        travelers=plan.travelers.to_dict() if plan.travelers else None,
                        budget=plan.budget.to_dict() if plan.budget else None,
                        selected_skeleton=next(
                            (
                                skeleton
                                for skeleton in plan.skeleton_plans
                                if skeleton.get("id") == plan.selected_skeleton_id
                            ),
                            None,
                        ),
                        final_plan_summary=_generate_title(plan),
                        accepted_items=[],
                        rejected_items=[],
                        lessons=[],
                        satisfaction=None,
                        created_at=_now_iso(),
                    )
                )
```

- [ ] **Step 9: Run integration tests**

Run: `cd backend && python -m pytest tests/test_memory_integration.py -v`

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add backend/main.py backend/tests/test_memory_integration.py
git commit -m "feat: integrate structured memory into chat flow"
```

---

## Task 8: Verification, Documentation, and Overview Update

**Files:**
- Modify: `PROJECT_OVERVIEW.md`
- Modify: `docs/superpowers/specs/2026-04-11-memory-system-upgrade-design.md` if implementation discovers a design correction

- [ ] **Step 1: Run full backend memory and context tests**

Run:

```bash
cd backend && python -m pytest \
  tests/test_memory_models.py \
  tests/test_memory_store.py \
  tests/test_memory_policy.py \
  tests/test_memory_extraction.py \
  tests/test_memory_retriever.py \
  tests/test_memory_formatter.py \
  tests/test_memory_manager.py \
  tests/test_memory_integration.py \
  tests/test_state_models.py \
  tests/test_telemetry_agent_loop.py \
  -v
```

Expected: PASS.

- [ ] **Step 2: Run wider backend suite**

Run: `cd backend && python -m pytest -q`

Expected: PASS. If unrelated tests fail because they assert the old `## 用户画像` heading, update those tests to expect `## 相关用户记忆`.

- [ ] **Step 3: Update PROJECT_OVERVIEW**

In `PROJECT_OVERVIEW.md`, update the `backend/memory/` section to mention:

```text
│   ├── memory/                 # 结构化用户记忆
│   │   ├── models.py           # MemoryItem/MemoryEvent/TripEpisode + legacy UserMemory
│   │   ├── store.py            # FileMemoryStore: schema v2, JSON/JSONL, migration, locks
│   │   ├── manager.py          # MemoryManager facade: load/save compatibility + context assembly
│   │   ├── extraction.py       # Candidate extraction prompt/parser
│   │   ├── policy.py           # Risk classification, redaction, merge
│   │   ├── retriever.py        # Phase-aware rule retrieval
│   │   └── formatter.py        # Compact memory prompt formatting
```

Update the Agent 智能层 table row:

```text
| Memory System | 结构化长期/本次/episode 记忆，后台候选提取，policy 合并，阶段相关注入 | 每轮 chat 后后台提取；每次 system prompt 构建前检索 |
```

- [ ] **Step 4: Run diff checks**

Run:

```bash
git diff --check
git status --short
```

Expected: `git diff --check` prints no output. `git status --short` shows only intended memory, config, main, context, tests, docs files.

- [ ] **Step 5: Commit documentation and final verification**

```bash
git add PROJECT_OVERVIEW.md docs/superpowers/specs/2026-04-11-memory-system-upgrade-design.md
git commit -m "docs: update overview for structured memory system"
```

- [ ] **Step 6: Final smoke command**

Run: `cd backend && python -m pytest tests/test_memory_models.py tests/test_memory_store.py tests/test_memory_policy.py tests/test_memory_retriever.py tests/test_memory_formatter.py -q`

Expected: PASS.

---

## Implementation Notes

- Keep legacy `UserMemory`, `build_extraction_prompt`, `parse_extraction_response`, and legacy `MemoryMerger` until all existing tests and call sites are migrated.
- Avoid adding vector search, SQLite memory storage, or a full frontend management page in this implementation.
- Do not store payment data, membership identifiers, passport numbers, ID numbers, or detailed health narratives.
- `pending` and `pending_conflict` memory must not be injected into `ContextManager`.
- `TravelPlanState` remains the authority for current trip facts; trip scope memory is only a summarized support layer.
