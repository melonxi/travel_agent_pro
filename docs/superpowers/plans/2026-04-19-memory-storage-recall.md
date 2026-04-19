# Memory Storage v3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the v2 `MemoryItem(scope=global|trip)` runtime path with v3 profile / working memory / episode slice storage and query-aware symbolic recall.

**Architecture:** Add v3 memory entities and file store under `data/users/{user_id}/memory/`, migrate old v2 files with an explicit script, then replace context assembly with fixed profile recall plus conservative query-aware symbolic recall. `TravelPlanState` remains the only source for current trip facts; complete episodes stay archived and only `EpisodeSlice` objects enter prompts.

**Tech Stack:** Python 3.12 dataclasses, FastAPI, JSON/JSONL file storage, pytest/pytest-asyncio, React 19 + TypeScript for Memory Center/API type updates.

---

## File Structure

Create:

- `backend/memory/v3_models.py` - v3 dataclasses, serializers, id helpers.
- `backend/memory/v3_store.py` - profile, working memory, events, episodes, and episode slices file store.
- `backend/memory/episode_slices.py` - deterministic `TripEpisode` to `EpisodeSlice` generator.
- `backend/memory/symbolic_recall.py` - rule trigger, query parser, and symbolic retriever.
- `scripts/migrate_memory_v2_to_v3.py` - explicit v2 to v3 migration script with dry-run.
- `backend/tests/test_memory_v3_models.py`
- `backend/tests/test_memory_v3_store.py`
- `backend/tests/test_memory_v3_migration.py`
- `backend/tests/test_episode_slices.py`
- `backend/tests/test_symbolic_recall.py`

Modify:

- `backend/memory/manager.py` - replace `generate_context()` internals with v3 context assembly.
- `backend/memory/formatter.py` - add v3 prompt sections and keep legacy helpers only for deprecated tests.
- `backend/memory/extraction.py` - add v3 extraction prompt and parser for split outputs.
- `backend/memory/policy.py` - add v3 profile/working-memory routing and PII checks for split outputs.
- `backend/main.py` - use v3 APIs, emit enriched `memory_recall`, write v3 events/episodes/slices.
- `backend/context/manager.py` - no structural rewrite; verify v3 memory context is still treated as non-instructional context.
- `backend/tests/test_memory_manager.py`, `backend/tests/test_memory_formatter.py`, `backend/tests/test_memory_policy.py`, `backend/tests/test_memory_extraction.py`, `backend/tests/test_memory_integration.py`, `backend/tests/test_context_manager.py` - update expectations from v2 to v3.
- `frontend/src/types/memory.ts` - v3 profile, episode slice, working memory types.
- `frontend/src/hooks/useMemory.ts` - call v3 profile endpoints.
- `frontend/src/components/MemoryCenter.tsx` - show long-term profile, hypotheses, episodes, working memory.
- `frontend/src/components/ChatPanel.tsx` - accept enriched `memory_recall` payload while tolerating old `item_ids` during development.
- `frontend/src/components/TraceViewer.tsx`, `frontend/src/types/trace.ts` - rename memory hit buckets from core/trip/phase to profile/working/query/slice.
- `PROJECT_OVERVIEW.md` - update when implementation changes current architecture.

---

### Task 1: Add v3 Models

**Files:**
- Create: `backend/memory/v3_models.py`
- Test: `backend/tests/test_memory_v3_models.py`

- [ ] **Step 1: Write failing model tests**

Create `backend/tests/test_memory_v3_models.py`:

```python
from memory.v3_models import (
    EpisodeSlice,
    MemoryProfileItem,
    SessionWorkingMemory,
    WorkingMemoryItem,
    generate_profile_item_id,
)


def test_profile_item_round_trips_with_recall_hints():
    item = MemoryProfileItem(
        id="",
        domain="flight",
        key="avoid_red_eye",
        value=True,
        polarity="avoid",
        stability="explicit_declared",
        confidence=0.95,
        status="active",
        context={},
        applicability="适用于所有旅行，除非用户明确临时允许。",
        recall_hints={"domains": ["flight"], "keywords": ["红眼航班"], "priority": "high"},
        source_refs=[{"kind": "message", "session_id": "s1", "quote": "以后不坐红眼航班"}],
        created_at="2026-04-19T00:00:00",
        updated_at="2026-04-19T00:00:00",
    )
    item.id = generate_profile_item_id("constraints", item)

    restored = MemoryProfileItem.from_dict(item.to_dict())

    assert restored.id == "constraints:flight:avoid_red_eye"
    assert restored.recall_hints["keywords"] == ["红眼航班"]
    assert restored.stability == "explicit_declared"


def test_rejection_id_includes_value():
    first = MemoryProfileItem(
        id="",
        domain="hotel",
        key="avoid",
        value="青旅",
        polarity="avoid",
        stability="explicit_declared",
        confidence=0.9,
        status="active",
        context={},
        applicability="适用于所有旅行。",
        recall_hints={},
        source_refs=[],
        created_at="2026-04-19T00:00:00",
        updated_at="2026-04-19T00:00:00",
    )
    second = MemoryProfileItem.from_dict({**first.to_dict(), "value": "红眼航班"})

    assert generate_profile_item_id("rejections", first) == "rejections:hotel:avoid:青旅"
    assert generate_profile_item_id("rejections", second) == "rejections:hotel:avoid:红眼航班"


def test_working_memory_round_trip():
    item = WorkingMemoryItem(
        id="wm_001",
        phase=3,
        kind="temporary_rejection",
        domains=["attraction"],
        content="用户说先别考虑迪士尼。",
        reason="当前候选筛选阶段避免重复推荐。",
        status="active",
        expires={"on_session_end": True, "on_trip_change": True, "on_phase_exit": False},
        created_at="2026-04-19T00:00:00",
    )
    memory = SessionWorkingMemory(
        schema_version=1,
        user_id="default_user",
        session_id="s1",
        trip_id="trip_123",
        items=[item],
    )

    restored = SessionWorkingMemory.from_dict(memory.to_dict())

    assert restored.items[0].kind == "temporary_rejection"
    assert restored.items[0].expires["on_trip_change"] is True


def test_episode_slice_round_trip():
    slice_ = EpisodeSlice(
        id="slice_001",
        user_id="default_user",
        source_episode_id="ep_kyoto",
        source_trip_id="trip_123",
        slice_type="accommodation_decision",
        domains=["hotel", "accommodation"],
        entities={"destination": "京都"},
        keywords=["住宿", "酒店"],
        content="上次京都选择町屋。",
        applicability="仅供住宿偏好参考。",
        created_at="2026-04-19T00:00:00",
    )

    restored = EpisodeSlice.from_dict(slice_.to_dict())

    assert restored.source_episode_id == "ep_kyoto"
    assert restored.keywords == ["住宿", "酒店"]
```

- [ ] **Step 2: Run tests and verify failure**

Run: `PYTHONPATH=backend pytest backend/tests/test_memory_v3_models.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'memory.v3_models'`.

- [ ] **Step 3: Implement v3 models**

Create `backend/memory/v3_models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _normalize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "|".join(_normalize_value(item) for item in value)
    if isinstance(value, dict):
        return "|".join(f"{key}:{_normalize_value(value[key])}" for key in sorted(value))
    return str(value).strip()


@dataclass
class MemoryProfileItem:
    id: str
    domain: str
    key: str
    value: Any
    polarity: str
    stability: str
    confidence: float
    status: str
    context: dict[str, Any] = field(default_factory=dict)
    applicability: str = ""
    recall_hints: dict[str, Any] = field(default_factory=dict)
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "domain": self.domain,
            "key": self.key,
            "value": self.value,
            "polarity": self.polarity,
            "stability": self.stability,
            "confidence": self.confidence,
            "status": self.status,
            "context": self.context,
            "applicability": self.applicability,
            "recall_hints": self.recall_hints,
            "source_refs": self.source_refs,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryProfileItem":
        return cls(
            id=str(data.get("id", "")),
            domain=str(data.get("domain", "general")),
            key=str(data.get("key", "general")),
            value=data.get("value"),
            polarity=str(data.get("polarity", "neutral")),
            stability=str(data.get("stability", "single_observation")),
            confidence=float(data.get("confidence", 0.0)),
            status=str(data.get("status", "active")),
            context=dict(data.get("context", {})),
            applicability=str(data.get("applicability", "")),
            recall_hints=dict(data.get("recall_hints", {})),
            source_refs=list(data.get("source_refs", [])),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )


def generate_profile_item_id(bucket: str, item: MemoryProfileItem) -> str:
    if bucket == "rejections":
        return f"{bucket}:{item.domain}:{item.key}:{_normalize_value(item.value)}"
    if bucket == "preference_hypotheses":
        context = _normalize_value(item.context)
        return f"{bucket}:{item.domain}:{item.key}:{context}"
    return f"{bucket}:{item.domain}:{item.key}"


@dataclass
class UserMemoryProfile:
    schema_version: int
    user_id: str
    constraints: list[MemoryProfileItem] = field(default_factory=list)
    rejections: list[MemoryProfileItem] = field(default_factory=list)
    stable_preferences: list[MemoryProfileItem] = field(default_factory=list)
    preference_hypotheses: list[MemoryProfileItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "user_id": self.user_id,
            "constraints": [item.to_dict() for item in self.constraints],
            "rejections": [item.to_dict() for item in self.rejections],
            "stable_preferences": [item.to_dict() for item in self.stable_preferences],
            "preference_hypotheses": [item.to_dict() for item in self.preference_hypotheses],
        }

    @classmethod
    def empty(cls, user_id: str) -> "UserMemoryProfile":
        return cls(schema_version=3, user_id=user_id)

    @classmethod
    def from_dict(cls, data: dict[str, Any], user_id: str | None = None) -> "UserMemoryProfile":
        return cls(
            schema_version=int(data.get("schema_version", 3)),
            user_id=str(data.get("user_id", user_id or "")),
            constraints=[MemoryProfileItem.from_dict(item) for item in data.get("constraints", [])],
            rejections=[MemoryProfileItem.from_dict(item) for item in data.get("rejections", [])],
            stable_preferences=[MemoryProfileItem.from_dict(item) for item in data.get("stable_preferences", [])],
            preference_hypotheses=[MemoryProfileItem.from_dict(item) for item in data.get("preference_hypotheses", [])],
        )


@dataclass
class WorkingMemoryItem:
    id: str
    phase: int
    kind: str
    domains: list[str]
    content: str
    reason: str
    status: str
    expires: dict[str, bool]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkingMemoryItem":
        return cls(
            id=str(data["id"]),
            phase=int(data.get("phase", 0)),
            kind=str(data.get("kind", "note")),
            domains=list(data.get("domains", [])),
            content=str(data.get("content", "")),
            reason=str(data.get("reason", "")),
            status=str(data.get("status", "active")),
            expires=dict(data.get("expires", {})),
            created_at=str(data.get("created_at", "")),
        )


@dataclass
class SessionWorkingMemory:
    schema_version: int
    user_id: str
    session_id: str
    trip_id: str | None
    items: list[WorkingMemoryItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "trip_id": self.trip_id,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def empty(cls, user_id: str, session_id: str, trip_id: str | None) -> "SessionWorkingMemory":
        return cls(schema_version=1, user_id=user_id, session_id=session_id, trip_id=trip_id)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionWorkingMemory":
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            user_id=str(data.get("user_id", "")),
            session_id=str(data.get("session_id", "")),
            trip_id=data.get("trip_id"),
            items=[WorkingMemoryItem.from_dict(item) for item in data.get("items", [])],
        )


@dataclass
class EpisodeSlice:
    id: str
    user_id: str
    source_episode_id: str
    source_trip_id: str | None
    slice_type: str
    domains: list[str]
    entities: dict[str, Any]
    keywords: list[str]
    content: str
    applicability: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EpisodeSlice":
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            source_episode_id=str(data["source_episode_id"]),
            source_trip_id=data.get("source_trip_id"),
            slice_type=str(data.get("slice_type", "general")),
            domains=list(data.get("domains", [])),
            entities=dict(data.get("entities", {})),
            keywords=list(data.get("keywords", [])),
            content=str(data.get("content", "")),
            applicability=str(data.get("applicability", "")),
            created_at=str(data.get("created_at", "")),
        )
```

- [ ] **Step 4: Run model tests**

Run: `PYTHONPATH=backend pytest backend/tests/test_memory_v3_models.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/memory/v3_models.py backend/tests/test_memory_v3_models.py
git commit -m "feat: add memory v3 models"
```

---

### Task 2: Add v3 File Store

**Files:**
- Create: `backend/memory/v3_store.py`
- Test: `backend/tests/test_memory_v3_store.py`

- [ ] **Step 1: Write failing store tests**

Create `backend/tests/test_memory_v3_store.py`:

```python
import pytest

from memory.v3_models import (
    EpisodeSlice,
    MemoryProfileItem,
    SessionWorkingMemory,
    WorkingMemoryItem,
)
from memory.v3_store import FileMemoryV3Store


@pytest.mark.asyncio
async def test_profile_defaults_to_empty(tmp_path):
    store = FileMemoryV3Store(tmp_path)

    profile = await store.load_profile("u1")

    assert profile.schema_version == 3
    assert profile.user_id == "u1"
    assert profile.constraints == []


@pytest.mark.asyncio
async def test_upsert_profile_item_by_bucket(tmp_path):
    store = FileMemoryV3Store(tmp_path)
    item = MemoryProfileItem(
        id="constraints:flight:avoid_red_eye",
        domain="flight",
        key="avoid_red_eye",
        value=True,
        polarity="avoid",
        stability="explicit_declared",
        confidence=0.9,
        status="active",
        context={},
        applicability="适用于所有旅行。",
        recall_hints={"keywords": ["红眼航班"]},
        source_refs=[],
        created_at="2026-04-19T00:00:00",
        updated_at="2026-04-19T00:00:00",
    )

    await store.upsert_profile_item("u1", "constraints", item)
    item.confidence = 0.95
    await store.upsert_profile_item("u1", "constraints", item)

    profile = await store.load_profile("u1")
    assert len(profile.constraints) == 1
    assert profile.constraints[0].confidence == 0.95


@pytest.mark.asyncio
async def test_working_memory_is_session_scoped(tmp_path):
    store = FileMemoryV3Store(tmp_path)
    item = WorkingMemoryItem(
        id="wm_1",
        phase=3,
        kind="temporary_rejection",
        domains=["attraction"],
        content="先别考虑迪士尼。",
        reason="当前候选筛选需要避让。",
        status="active",
        expires={"on_trip_change": True},
        created_at="2026-04-19T00:00:00",
    )

    await store.upsert_working_memory_item("u1", "s1", "trip_1", item)

    memory = await store.load_working_memory("u1", "s1", "trip_1")
    other = await store.load_working_memory("u1", "s2", "trip_1")
    assert memory.items[0].id == "wm_1"
    assert other.items == []


@pytest.mark.asyncio
async def test_episode_slice_append_is_idempotent(tmp_path):
    store = FileMemoryV3Store(tmp_path)
    slice_ = EpisodeSlice(
        id="slice_1",
        user_id="u1",
        source_episode_id="ep_1",
        source_trip_id="trip_1",
        slice_type="pitfall",
        domains=["pace"],
        entities={"destination": "京都"},
        keywords=["坑"],
        content="上次下午安排过密。",
        applicability="仅供同类行程参考。",
        created_at="2026-04-19T00:00:00",
    )

    await store.append_episode_slice(slice_)
    await store.append_episode_slice(slice_)

    slices = await store.list_episode_slices("u1")
    assert [item.id for item in slices] == ["slice_1"]
```

- [ ] **Step 2: Run tests and verify failure**

Run: `PYTHONPATH=backend pytest backend/tests/test_memory_v3_store.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'memory.v3_store'`.

- [ ] **Step 3: Implement v3 store**

Create `backend/memory/v3_store.py` with atomic JSON writes, per-user locks, and this public interface:

```python
class FileMemoryV3Store:
    def __init__(self, data_dir: str | Path = "./data"):
        self.data_dir = Path(data_dir)
        self._lock_registry: dict[str, asyncio.Lock] = {}

    async def load_profile(self, user_id: str) -> UserMemoryProfile:
        """Return profile.json or an empty schema_version=3 profile."""

    async def save_profile(self, profile: UserMemoryProfile) -> None:
        """Atomically write data/users/{user_id}/memory/profile.json."""

    async def upsert_profile_item(
        self,
        user_id: str,
        bucket: str,
        item: MemoryProfileItem,
    ) -> None:
        """Replace same-id item inside one profile bucket, or append it."""

    async def load_working_memory(
        self,
        user_id: str,
        session_id: str,
        trip_id: str | None,
    ) -> SessionWorkingMemory:
        """Return session working memory only when the stored trip_id matches."""

    async def upsert_working_memory_item(
        self,
        user_id: str,
        session_id: str,
        trip_id: str | None,
        item: WorkingMemoryItem,
    ) -> None:
        """Replace same-id working memory item, or append it."""

    async def append_episode_slice(self, slice_: EpisodeSlice) -> None:
        """Append to episode_slices.jsonl unless the slice id already exists."""

    async def list_episode_slices(
        self,
        user_id: str,
        *,
        destination: str | None = None,
    ) -> list[EpisodeSlice]:
        """Return slices, optionally filtered by entities.destination."""
```

Implementation requirements:

- Base path is `data_dir / "users" / user_id / "memory"`.
- Profile path is `profile.json`.
- Working memory path is `sessions/{session_id}/working_memory.json`.
- Episode slices path is `episode_slices.jsonl`.
- `append_episode_slice()` must skip existing ids.
- `load_working_memory()` must return empty memory when file is absent or stored `trip_id` differs from requested `trip_id`.

- [ ] **Step 4: Run store tests**

Run: `PYTHONPATH=backend pytest backend/tests/test_memory_v3_store.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/memory/v3_store.py backend/tests/test_memory_v3_store.py
git commit -m "feat: add memory v3 file store"
```

---

### Task 3: Add Migration Script

**Files:**
- Create: `scripts/migrate_memory_v2_to_v3.py`
- Test: `backend/tests/test_memory_v3_migration.py`

- [ ] **Step 1: Write migration tests**

Create tests that build a temp `users/u1/memory.json`, `memory_events.jsonl`, and `trip_episodes.jsonl`, then assert:

```python
assert profile.stable_preferences[0].key == "preferred_pace"
assert profile.rejections[0].value == "青旅"
assert (memory_dir / "legacy_ignored.jsonl").exists()
assert migrated["ignored_trip_items"] == 1
```

Also test dry-run:

```python
result = migrate_user(tmp_path, "u1", dry_run=True)
assert result["would_write"] is True
assert not (tmp_path / "users" / "u1" / "memory" / "profile.json").exists()
```

- [ ] **Step 2: Run tests and verify failure**

Run: `PYTHONPATH=backend pytest backend/tests/test_memory_v3_migration.py -v`

Expected: FAIL with missing migration module/function.

- [ ] **Step 3: Implement migration**

Create `scripts/migrate_memory_v2_to_v3.py` with:

```python
def migrate_user(data_dir: Path, user_id: str, *, dry_run: bool = False) -> dict[str, int | bool]:
    """Migrate one user's v2 memory files into the v3 memory/ directory.

    Return counters:
    {
        "would_write": bool,
        "profile_items": int,
        "ignored_trip_items": int,
        "events": int,
        "episodes": int,
        "slices": int,
    }
    """
```

Rules:

- v2 `type=preference, scope=global` -> `profile.stable_preferences`.
- v2 `type=constraint, scope=global` -> `profile.constraints`.
- v2 `type=rejection, scope=global` -> `profile.rejections`.
- v2 `scope=trip` -> write row to `memory/legacy_ignored.jsonl`.
- `memory_events.jsonl` -> `memory/events.jsonl`.
- `trip_episodes.jsonl` -> `memory/episodes.jsonl`.
- Move originals to `legacy_memory_v2/` only when `dry_run=False`.
- Running twice must not duplicate profile items.

- [ ] **Step 4: Run migration tests**

Run: `PYTHONPATH=backend pytest backend/tests/test_memory_v3_migration.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_memory_v2_to_v3.py backend/tests/test_memory_v3_migration.py
git commit -m "feat: add memory v3 migration"
```

---

### Task 4: Generate Episode Slices

**Files:**
- Create: `backend/memory/episode_slices.py`
- Modify: `backend/main.py`
- Test: `backend/tests/test_episode_slices.py`

- [ ] **Step 1: Write slice generator tests**

Create tests that instantiate `TripEpisode` with `destination="京都"`, `selected_skeleton`, `accepted_items`, `rejected_items`, and `lessons`, then assert:

```python
slices = build_episode_slices(episode, now="2026-04-19T00:00:00")
assert any(item.slice_type == "accepted_pattern" for item in slices)
assert any(item.slice_type == "pitfall" for item in slices)
assert all(item.source_episode_id == episode.id for item in slices)
assert all("京都" in item.entities.values() or item.entities.get("destination") == "京都" for item in slices)
assert len(slices) <= 8
```

- [ ] **Step 2: Run tests and verify failure**

Run: `PYTHONPATH=backend pytest backend/tests/test_episode_slices.py -v`

Expected: FAIL with missing `memory.episode_slices`.

- [ ] **Step 3: Implement deterministic slice generator**

Create `backend/memory/episode_slices.py`:

```python
def build_episode_slices(episode: TripEpisode, *, now: str) -> list[EpisodeSlice]:
    """Build at most 8 deterministic EpisodeSlice objects from one TripEpisode."""
```

Minimum behavior:

- Create `accepted_pattern` from `selected_skeleton` when present.
- Create `rejected_option` from up to 2 `rejected_items`.
- Create `pitfall` from up to 2 `lessons`.
- Create `budget_signal` when `budget` exists.
- Use domain keyword maps from the spec.
- Truncate `content` to 180 characters.
- Return at most 8 slices.

- [ ] **Step 4: Wire slice append after episode append**

In `backend/main.py`, after `await memory_mgr.store.append_episode(episode)`, append:

```python
from memory.episode_slices import build_episode_slices

for slice_ in build_episode_slices(episode, now=_now_iso()):
    await memory_mgr.v3_store.append_episode_slice(slice_)
```

If `MemoryManager` owns the v3 store after Task 6, use `memory_mgr.v3_store`; otherwise instantiate `FileMemoryV3Store(config.data_dir)` in the same setup area as the existing memory manager.

- [ ] **Step 5: Run focused tests**

Run: `PYTHONPATH=backend pytest backend/tests/test_episode_slices.py backend/tests/test_memory_integration.py -v`

Expected: PASS after updating integration expectations to include slice generation.

- [ ] **Step 6: Commit**

```bash
git add backend/memory/episode_slices.py backend/main.py backend/tests/test_episode_slices.py backend/tests/test_memory_integration.py
git commit -m "feat: generate memory episode slices"
```

---

### Task 5: Add Query-Aware Symbolic Recall

**Files:**
- Create: `backend/memory/symbolic_recall.py`
- Test: `backend/tests/test_symbolic_recall.py`

- [ ] **Step 1: Write symbolic recall tests**

Create tests:

```python
from memory.symbolic_recall import build_recall_query, should_trigger_memory_recall


def test_history_question_triggers_recall():
    assert should_trigger_memory_recall("我上次去京都住哪里？") is True


def test_current_trip_question_does_not_trigger_recall():
    assert should_trigger_memory_recall("这次预算多少？") is False


def test_hotel_query_maps_domains_and_destination():
    query = build_recall_query("我上次去京都住哪里？")
    assert query.needs_memory is True
    assert "hotel" in query.domains
    assert query.entities["destination"] == "京都"
    assert query.include_slices is True


def test_long_term_preference_query_includes_profile():
    query = build_recall_query("我是不是说过不坐红眼航班？")
    assert query.include_profile is True
    assert "flight" in query.domains
```

- [ ] **Step 2: Run tests and verify failure**

Run: `PYTHONPATH=backend pytest backend/tests/test_symbolic_recall.py -v`

Expected: FAIL with missing module.

- [ ] **Step 3: Implement trigger and parser**

Create `backend/memory/symbolic_recall.py` with:

```python
@dataclass
class RecallQuery:
    needs_memory: bool
    domains: list[str]
    entities: dict[str, str]
    keywords: list[str]
    include_profile: bool
    include_slices: bool
    include_working_memory: bool
    matched_reason: str
```

Implement:

- `should_trigger_memory_recall(message: str) -> bool`
- `build_recall_query(message: str) -> RecallQuery`
- `rank_profile_items(query, profile) -> list[tuple[str, MemoryProfileItem, str]]`
- `rank_episode_slices(query, slices) -> list[tuple[EpisodeSlice, str]]`

Use conservative regex/string matching:

- Current-trip phrases `这次`, `本次`, `当前` + state words `预算`, `几号`, `出发`, `骨架`, `约束` return false.
- History phrases `上次`, `之前`, `以前`, `我是不是说过`, `按我的习惯`, `还记得吗`, `有没有记录` return true.
- Domain maps:
  - `住`, `住宿`, `酒店`, `民宿` -> `hotel`, `accommodation`
  - `航班`, `红眼`, `飞机` -> `flight`
  - `火车`, `高铁` -> `train`
  - `节奏`, `累`, `慢`, `松` -> `pace`
  - `吃`, `辣`, `餐厅` -> `food`

- [ ] **Step 4: Run symbolic recall tests**

Run: `PYTHONPATH=backend pytest backend/tests/test_symbolic_recall.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/memory/symbolic_recall.py backend/tests/test_symbolic_recall.py
git commit -m "feat: add symbolic memory recall"
```

---

### Task 6: Replace Memory Context Assembly

**Files:**
- Modify: `backend/memory/manager.py`
- Modify: `backend/memory/formatter.py`
- Modify: `backend/main.py`
- Test: `backend/tests/test_memory_manager.py`
- Test: `backend/tests/test_memory_formatter.py`
- Test: `backend/tests/test_context_manager.py`

- [ ] **Step 1: Write failing manager/formatter tests**

Update tests to assert:

```python
assert "## 长期用户画像" in context
assert "## 本轮请求命中的历史记忆" in context
assert "## 本次旅行记忆" not in context
assert "上次京都选择町屋" in context
assert "TripEpisode" not in context
```

Add a manager test where `req_message="我上次去京都住哪里？"` returns:

```python
context, recall = await manager.generate_context("u1", plan, user_message="我上次去京都住哪里？")
assert recall.sources["episode_slice"] == 1
assert recall.slice_ids == ["slice_1"]
```

- [ ] **Step 2: Run tests and verify failure**

Run: `PYTHONPATH=backend pytest backend/tests/test_memory_manager.py backend/tests/test_memory_formatter.py -v`

Expected: FAIL because `generate_context()` still returns v2 tuple and formatter still emits `本次旅行记忆`.

- [ ] **Step 3: Implement v3 formatter**

Add v3 structures to `backend/memory/formatter.py`:

```python
@dataclass
class MemoryRecallTelemetry:
    sources: dict[str, int]
    profile_ids: list[str]
    working_memory_ids: list[str]
    slice_ids: list[str]
    matched_reasons: list[str]
```

Add:

```python
def format_v3_memory_context(
    profile_items: list[tuple[str, MemoryProfileItem]],
    working_items: list[WorkingMemoryItem],
    query_profile_items: list[tuple[str, MemoryProfileItem, str]],
    query_slices: list[tuple[EpisodeSlice, str]],
) -> str:
    """Format v3 memory sections for system prompt injection."""
```

Formatter rules:

- Use section `## 长期用户画像` for fixed profile.
- Use section `## 当前会话工作记忆` only when working items exist.
- Use section `## 本轮请求命中的历史记忆` only when query hits exist.
- Include source/bucket, matched reason, content/value, and applicability.
- Return `暂无相关用户记忆` when all lists are empty.
- Sanitize text with existing `_sanitize_text()`.

- [ ] **Step 4: Implement manager v3 context**

In `backend/memory/manager.py`:

- Add `self.v3_store = FileMemoryV3Store(data_dir)`.
- Change `generate_context()` signature to:

```python
async def generate_context(
    self,
    user_id: str,
    plan: TravelPlanState,
    user_message: str = "",
) -> tuple[str, MemoryRecallTelemetry]:
```

Logic:

- Load profile from v3 store.
- Fixed profile = active `constraints`, `rejections`, `stable_preferences`, capped at 10.
- Load working memory for `plan.session_id` and `plan.trip_id`.
- If `should_trigger_memory_recall(user_message)`, query profile and episode slices.
- Format context.
- Build telemetry counts.

- [ ] **Step 5: Update `backend/main.py` call site**

Replace old call:

```python
memory_context, recalled_ids, mem_core, mem_trip, mem_phase = await memory_mgr.generate_context(req.user_id, plan)
```

with:

```python
memory_context, memory_recall = await memory_mgr.generate_context(
    req.user_id,
    plan,
    user_message=req.message,
)
```

Emit SSE:

```python
if any(memory_recall.sources.values()):
    yield json.dumps({"type": "memory_recall", **memory_recall.to_dict()}, ensure_ascii=False)
```

Update `SessionStats.memory_hits` only after Task 8 adapts telemetry types; until then, store the v3 payload in the existing list only if the dataclass supports it, or skip trace memory hit append in this task and cover it in Task 8.

- [ ] **Step 6: Run context tests**

Run: `PYTHONPATH=backend pytest backend/tests/test_memory_manager.py backend/tests/test_memory_formatter.py backend/tests/test_context_manager.py -v`

Expected: PASS after updating tests to v3 output.

- [ ] **Step 7: Commit**

```bash
git add backend/memory/manager.py backend/memory/formatter.py backend/main.py backend/tests/test_memory_manager.py backend/tests/test_memory_formatter.py backend/tests/test_context_manager.py
git commit -m "feat: assemble memory v3 context"
```

---

### Task 7: Add v3 Extraction Split Output

**Files:**
- Modify: `backend/memory/extraction.py`
- Modify: `backend/memory/policy.py`
- Modify: `backend/main.py`
- Test: `backend/tests/test_memory_extraction.py`
- Test: `backend/tests/test_memory_policy.py`

- [ ] **Step 1: Write failing extraction tests**

Add tests:

```python
def test_parse_v3_split_extraction_response():
    response = '{"profile_updates":{"constraints":[{"domain":"flight","key":"avoid_red_eye","value":true,"polarity":"avoid","stability":"explicit_declared","confidence":0.95,"status":"active","context":{},"applicability":"适用于所有旅行","recall_hints":{"keywords":["红眼航班"]},"source_refs":[]}],"rejections":[],"stable_preferences":[],"preference_hypotheses":[]},"working_memory":[],"episode_evidence":[],"state_observations":[],"drop":[]}'
    result = parse_v3_extraction_response(response)
    assert result.profile_updates.constraints[0].key == "avoid_red_eye"


def test_state_observation_does_not_become_profile_item():
    response = '{"profile_updates":{"constraints":[],"rejections":[],"stable_preferences":[],"preference_hypotheses":[]},"working_memory":[],"episode_evidence":[],"state_observations":[{"field":"destination","value":"京都"}],"drop":[]}'
    result = parse_v3_extraction_response(response)
    assert result.profile_updates.constraints == []
```

Policy tests:

```python
def test_policy_drops_payment_and_pii_in_v3_profile_item():
    assert policy.classify_v3_profile_item("constraints", item_with_payment_domain) == "drop"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `PYTHONPATH=backend pytest backend/tests/test_memory_extraction.py backend/tests/test_memory_policy.py -v`

Expected: FAIL because v3 parser/policy functions do not exist.

- [ ] **Step 3: Implement v3 extraction parser**

Add to `backend/memory/extraction.py`:

```python
@dataclass
class V3ProfileUpdates:
    constraints: list[MemoryProfileItem]
    rejections: list[MemoryProfileItem]
    stable_preferences: list[MemoryProfileItem]
    preference_hypotheses: list[MemoryProfileItem]

@dataclass
class V3ExtractionResult:
    profile_updates: V3ProfileUpdates
    working_memory: list[WorkingMemoryItem]
    episode_evidence: list[dict[str, Any]]
    state_observations: list[dict[str, Any]]
    drop: list[dict[str, Any]]
```

Add:

```python
def build_v3_extraction_prompt(
    user_messages: list[str],
    profile: UserMemoryProfile,
    working_memory: SessionWorkingMemory,
    plan_facts: dict[str, Any],
) -> str:
    """Build the split-output extraction prompt."""


def parse_v3_extraction_response(response: str) -> V3ExtractionResult:
    """Parse split extraction JSON; invalid input returns empty buckets."""
```

Prompt must explicitly say:

- Current destination/dates/budget/travelers/candidate pool/skeleton/daily plans are `state_observations`, not memory.
- Single observation preferences go to `preference_hypotheses`.
- Temporary session signals go to `working_memory`.
- Payment, membership, and PII go to `drop`.

- [ ] **Step 4: Implement v3 policy routing**

Add to `backend/memory/policy.py`:

```python
def classify_v3_profile_item(self, bucket: str, item: MemoryProfileItem) -> str:
    """Return active, pending, pending_conflict, or drop for one profile item."""


def sanitize_v3_profile_item(self, item: MemoryProfileItem) -> MemoryProfileItem:
    """Return a redacted copy safe for profile.json."""


def sanitize_working_memory_item(self, item: WorkingMemoryItem) -> WorkingMemoryItem:
    """Return a redacted copy safe for working_memory.json."""
```

Rules:

- `domain in {"payment", "membership"}` -> drop.
- PII in value/context/applicability/recall_hints/source_refs -> drop.
- `preference_hypotheses` default status `pending`.
- `constraints`/`rejections` explicit declarations can be `active` if low risk and confidence >= 0.8; health/family/documents stay pending.

- [ ] **Step 5: Wire extraction task to v3 store**

In `backend/main.py` extraction task:

- Build v3 prompt.
- Parse `V3ExtractionResult`.
- Upsert profile updates by bucket into v3 store.
- Upsert working memory items into session working memory.
- Do not write `state_observations`.
- Do not write v2 `MemoryItem` from v3 extraction.

- [ ] **Step 6: Run extraction/policy tests**

Run: `PYTHONPATH=backend pytest backend/tests/test_memory_extraction.py backend/tests/test_memory_policy.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/memory/extraction.py backend/memory/policy.py backend/main.py backend/tests/test_memory_extraction.py backend/tests/test_memory_policy.py
git commit -m "feat: split memory v3 extraction"
```

---

### Task 8: Update API, SSE, and Trace Memory Hits

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/telemetry/stats.py`
- Modify: `backend/api/trace.py`
- Test: existing API/trace tests or add `backend/tests/test_memory_v3_api.py`

- [ ] **Step 1: Write API/trace tests**

Add tests for:

```python
GET /api/memory/default_user/profile
GET /api/memory/default_user/episode-slices
GET /api/memory/default_user/sessions/{session_id}/working-memory
```

Add trace serialization test:

```python
record = MemoryHitRecord(
    sources={"profile_fixed": 1, "working_memory": 0, "query_profile": 1, "episode_slice": 1},
    profile_ids=["constraints:flight:avoid_red_eye"],
    working_memory_ids=[],
    slice_ids=["slice_1"],
    matched_reasons=["用户询问上次京都住宿"],
)
assert record.to_dict()["sources"]["episode_slice"] == 1
```

- [ ] **Step 2: Run tests and verify failure**

Run: `PYTHONPATH=backend pytest backend/tests/test_memory_v3_api.py backend/tests/test_trace_api.py -v`

Expected: FAIL until v3 routes and telemetry dataclass are implemented.

- [ ] **Step 3: Update telemetry dataclass**

In `backend/telemetry/stats.py`, replace old memory hit fields with:

```python
@dataclass
class MemoryHitRecord:
    sources: dict[str, int]
    profile_ids: list[str] = field(default_factory=list)
    working_memory_ids: list[str] = field(default_factory=list)
    slice_ids: list[str] = field(default_factory=list)
    matched_reasons: list[str] = field(default_factory=list)
    timestamp: float = 0.0
```

Update `to_dict()` and `backend/api/trace.py` consumers.

- [ ] **Step 4: Add v3 API routes**

In `backend/main.py`, add:

```python
@app.get("/api/memory/{user_id}/profile")
async def get_memory_profile(user_id: str):
    await _ensure_storage_ready()
    profile = await memory_mgr.v3_store.load_profile(user_id)
    return profile.to_dict()

@app.get("/api/memory/{user_id}/episode-slices")
async def list_memory_episode_slices(user_id: str):
    await _ensure_storage_ready()
    slices = await memory_mgr.v3_store.list_episode_slices(user_id)
    return {"slices": [slice_.to_dict() for slice_ in slices]}

@app.get("/api/memory/{user_id}/sessions/{session_id}/working-memory")
async def get_session_working_memory(user_id: str, session_id: str):
    await _ensure_storage_ready()
    session = sessions.get(session_id)
    trip_id = None
    if session is not None:
        plan = session.get("plan")
        trip_id = getattr(plan, "trip_id", None)
    memory = await memory_mgr.v3_store.load_working_memory(user_id, session_id, trip_id)
    return memory.to_dict()
```

Keep v2 routes temporarily but mark returned payload with `"deprecated": true` if they remain.

- [ ] **Step 5: Emit enriched SSE**

Ensure `memory_recall` uses:

```json
{
  "type": "memory_recall",
  "sources": {},
  "profile_ids": [],
  "working_memory_ids": [],
  "slice_ids": [],
  "matched_reasons": []
}
```

- [ ] **Step 6: Run API/trace tests**

Run: `PYTHONPATH=backend pytest backend/tests/test_memory_v3_api.py backend/tests/test_telemetry_phase_context.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/main.py backend/telemetry/stats.py backend/api/trace.py backend/tests/test_memory_v3_api.py backend/tests/test_telemetry_phase_context.py
git commit -m "feat: expose memory v3 api and telemetry"
```

---

### Task 9: Update Frontend Memory Center and Recall Display

**Files:**
- Modify: `frontend/src/types/memory.ts`
- Modify: `frontend/src/hooks/useMemory.ts`
- Modify: `frontend/src/components/MemoryCenter.tsx`
- Modify: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/types/trace.ts`
- Modify: `frontend/src/components/TraceViewer.tsx`

- [ ] **Step 1: Update TypeScript types**

In `frontend/src/types/memory.ts`, define:

```ts
export interface MemoryProfileItem {
  id: string
  domain: string
  key: string
  value: unknown
  polarity: string
  stability: string
  confidence: number
  status: string
  context: Record<string, unknown>
  applicability: string
  recall_hints: Record<string, unknown>
  source_refs: Array<Record<string, unknown>>
  created_at: string
  updated_at: string
}

export interface UserMemoryProfile {
  schema_version: 3
  user_id: string
  constraints: MemoryProfileItem[]
  rejections: MemoryProfileItem[]
  stable_preferences: MemoryProfileItem[]
  preference_hypotheses: MemoryProfileItem[]
}

export interface EpisodeSlice {
  id: string
  source_episode_id: string
  slice_type: string
  domains: string[]
  entities: Record<string, unknown>
  keywords: string[]
  content: string
  applicability: string
  created_at: string
}

export interface WorkingMemoryItem {
  id: string
  phase: number
  kind: string
  domains: string[]
  content: string
  reason: string
  status: string
  created_at: string
}
```

- [ ] **Step 2: Update hook endpoints**

In `frontend/src/hooks/useMemory.ts`, fetch:

- `/api/memory/${userId}/profile`
- `/api/memory/${userId}/episodes`
- `/api/memory/${userId}/episode-slices`

Return profile buckets, episodes, slices, loading/error, and actions for confirm/reject/delete profile item.

- [ ] **Step 3: Update MemoryCenter sections**

Render tabs:

```text
长期画像
偏好假设
历史旅行
历史切片
```

Display `constraints`, `rejections`, and `stable_preferences` in long-term profile. Display `preference_hypotheses` separately with pending badges.

- [ ] **Step 4: Update ChatPanel memory recall handling**

Replace old `event.item_ids` handling with:

```ts
const recallCount =
  (event.profile_ids?.length ?? 0) +
  (event.working_memory_ids?.length ?? 0) +
  (event.slice_ids?.length ?? 0)
```

Keep fallback for old payload:

```ts
const fallbackCount = event.item_ids?.length ?? 0
```

- [ ] **Step 5: Update TraceViewer**

Show:

```text
profile {n} / working {n} / query {n} / slice {n}
```

instead of core/trip/phase.

- [ ] **Step 6: Run frontend checks**

Run: `npm --prefix frontend run build`

Expected: TypeScript build passes.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/types/memory.ts frontend/src/hooks/useMemory.ts frontend/src/components/MemoryCenter.tsx frontend/src/components/ChatPanel.tsx frontend/src/types/trace.ts frontend/src/components/TraceViewer.tsx
git commit -m "feat: update frontend for memory v3"
```

---

### Task 10: Remove v2 Runtime Dependence and Update Docs

**Files:**
- Modify: `backend/memory/manager.py`
- Modify: `backend/main.py`
- Modify: `docs/learning/2026-04-12-记忆系统流程.md`
- Modify: `PROJECT_OVERVIEW.md`
- Test: full focused memory suite

- [ ] **Step 1: Remove v2 runtime reads from context path**

Ensure no production context path calls:

```text
memory_mgr.store.list_items(user_id)
retriever.retrieve_trip_memory
retriever.retrieve_phase_relevant
format_memory_context
```

for prompt assembly.

Use `rg`:

```bash
rg -n "retrieve_trip_memory|retrieve_phase_relevant|format_memory_context|list_items\\(user_id\\)" backend
```

Expected: hits only in deprecated tests/helpers or v2 API compatibility routes, not in chat context assembly.

- [ ] **Step 2: Update learning doc**

Update `docs/learning/2026-04-12-记忆系统流程.md` so it describes v3:

```text
profile fixed recall
working memory recall
query-aware symbolic recall
episode slice recall
no broad trip memory prompt section
```

- [ ] **Step 3: Update PROJECT_OVERVIEW**

In `PROJECT_OVERVIEW.md`, replace the memory bullet with current v3 architecture:

```text
Memory System | v3 profile / working memory / episode slice 分层记忆；当前旅行事实由 TravelPlanState 权威提供；query-aware symbolic recall 只在显式历史/偏好查询时触发
```

- [ ] **Step 4: Run focused backend tests**

Run:

```bash
PYTHONPATH=backend pytest \
  backend/tests/test_memory_v3_models.py \
  backend/tests/test_memory_v3_store.py \
  backend/tests/test_memory_v3_migration.py \
  backend/tests/test_episode_slices.py \
  backend/tests/test_symbolic_recall.py \
  backend/tests/test_memory_manager.py \
  backend/tests/test_memory_formatter.py \
  backend/tests/test_memory_extraction.py \
  backend/tests/test_memory_policy.py \
  backend/tests/test_memory_integration.py \
  backend/tests/test_context_manager.py \
  -v
```

Expected: PASS.

- [ ] **Step 5: Run frontend build**

Run: `npm --prefix frontend run build`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/memory/manager.py backend/main.py docs/learning/2026-04-12-记忆系统流程.md PROJECT_OVERVIEW.md
git commit -m "docs: update memory v3 architecture overview"
```

---

## Final Verification

- [ ] Run backend focused suite:

```bash
PYTHONPATH=backend pytest backend/tests/test_memory_v3_models.py backend/tests/test_memory_v3_store.py backend/tests/test_memory_v3_migration.py backend/tests/test_episode_slices.py backend/tests/test_symbolic_recall.py backend/tests/test_memory_manager.py backend/tests/test_memory_formatter.py backend/tests/test_memory_extraction.py backend/tests/test_memory_policy.py backend/tests/test_memory_integration.py backend/tests/test_context_manager.py -v
```

- [ ] Run frontend build:

```bash
npm --prefix frontend run build
```

- [ ] Run status check:

```bash
git status --short
```

Expected: clean working tree after final commit.

---

## Notes for Execution

- Do not introduce embedding, vector DB, or RAG in this implementation.
- Do not keep `MemoryItem(scope="trip")` as a prompt source.
- Do not inject full `TripEpisode` content into prompt.
- Treat missing v3 files as empty memory, not as permission to silently read v2.
- Keep query recall conservative; false negatives are acceptable in v1, false positives that pollute prompt are not.
