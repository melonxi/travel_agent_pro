# Memory v3-Only Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the memory system over to a single v3 runtime by deleting v2/legacy runtime reads, writes, APIs, frontend compatibility, and migration behavior.

**Architecture:** Make `profile.json`, trip-scoped `working_memory.json`, `episodes.jsonl`, `episode_slices.jsonl`, and audit-only `events.jsonl` the only memory runtime files. Add `ArchivedTripEpisode` as the authoritative historical trip model, generate `EpisodeSlice` only from that model, and have recall consume `RecallRetrievalPlan` directly instead of adapting through legacy `RecallQuery`.

**Tech Stack:** Python 3.12, FastAPI, pytest, pytest-asyncio, React 19, TypeScript, Vite.

---

## File Structure

- Modify: `backend/memory/v3_models.py`
  - Add `ArchivedTripEpisode`
  - Add `MemoryAuditEvent`
  - Keep `UserMemoryProfile`, `SessionWorkingMemory`, and `EpisodeSlice` as v3 runtime models
- Modify: `backend/memory/v3_store.py`
  - Add v3 `episodes.jsonl` read/write
  - Add v3 `events.jsonl` append
  - Move working memory path to `sessions/{session_id}/trips/{trip_id}/working_memory.json`
  - Add destructive legacy file cleanup for `memory.json`, `memory_events.jsonl`, and `trip_episodes.jsonl`
- Create: `backend/memory/archival.py`
  - Build `ArchivedTripEpisode` from `TravelPlanState`
  - Extract selected skeleton, daily summaries, decision log, and lesson log from v3-native state
- Modify: `backend/memory/episode_slices.py`
  - Accept `ArchivedTripEpisode`
  - Generate only `itinerary_pattern`, `stay_choice`, `transport_choice`, `budget_signal`, `rejected_option`, and `pitfall`
- Modify: `backend/memory/recall_query.py`
  - Add `entities` to `RecallRetrievalPlan`
  - Allow `source` values `profile`, `episode_slice`, and `hybrid_history`
  - Reject `working_memory`, `legacy`, and `profile_fixed`
- Modify: `backend/memory/symbolic_recall.py`
  - Remove `RecallQuery`
  - Make profile and episode ranking consume `RecallRetrievalPlan` directly
  - Keep a v3-only heuristic helper only for tests and `recall_gate=None` callers
- Delete: `backend/memory/recall_query_adapter.py`
  - Remove `plan_to_legacy_recall_query`
- Modify: `backend/memory/manager.py`
  - Remove `FileMemoryStore`
  - Remove `save()`, `load()`, and legacy summary behavior
  - Run recall directly from `RecallRetrievalPlan`
- Modify: `backend/memory/extraction.py`
  - Remove combined legacy-compatible extraction tool and parser from runtime
  - Make extraction gate parsing require route-aware v3 payloads
- Modify: `backend/memory/policy.py`
  - Keep v3 profile and working-memory policy methods
  - Remove runtime `MemoryCandidate -> MemoryItem` and legacy merge methods
- Modify: `backend/main.py`
  - Remove legacy memory imports, pending event helpers, pending SSE scan, legacy API routes, and legacy archive path
  - Use `ArchivedTripEpisode` archival on Phase 7 completion
  - Publish only split background extraction tasks, not the compatibility aggregate `memory_extraction` task
  - Run legacy file cleanup in startup
- Delete: `backend/memory/store.py`
- Delete: `backend/memory/retriever.py`
- Delete: `scripts/migrate_memory_v2_to_v3.py`
- Modify: `backend/memory/demo_seed.py`
  - Seed v3 profile, v3 episodes, v3 slices, and v3 working memory only
- Modify tests:
  - `backend/tests/test_memory_v3_models.py`
  - `backend/tests/test_memory_v3_store.py`
  - `backend/tests/test_episode_slices.py`
  - `backend/tests/test_memory_archival.py`
  - `backend/tests/test_recall_query.py`
  - `backend/tests/test_symbolic_recall.py`
  - `backend/tests/test_memory_manager.py`
  - `backend/tests/test_memory_extraction.py`
  - `backend/tests/test_memory_integration.py`
  - `backend/tests/test_memory_v3_api.py`
  - `backend/tests/test_memory_policy.py`
  - `backend/tests/test_demo_seed.py`
- Delete tests:
  - `backend/tests/test_memory_store.py`
  - `backend/tests/test_memory_retriever.py`
  - `backend/tests/test_recall_query_adapter.py`
  - `backend/tests/test_memory_v3_migration.py`
- Modify frontend:
  - `frontend/src/types/memory.ts`
  - `frontend/src/hooks/useMemory.ts`
  - `frontend/src/components/MemoryCenter.tsx`
  - `frontend/src/components/SessionSidebar.tsx`
- Modify docs:
  - `PROJECT_OVERVIEW.md`

---

### Task 1: Add v3 Episode, Audit Event, Store, and Legacy File Cleanup

**Files:**
- Modify: `backend/memory/v3_models.py`
- Modify: `backend/memory/v3_store.py`
- Test: `backend/tests/test_memory_v3_models.py`
- Test: `backend/tests/test_memory_v3_store.py`

- [ ] **Step 1: Write failing v3 model tests**

Add these imports to `backend/tests/test_memory_v3_models.py`:

```python
from memory.v3_models import ArchivedTripEpisode, MemoryAuditEvent
```

Add these tests:

```python
def test_archived_trip_episode_round_trip():
    episode = ArchivedTripEpisode(
        id="ep_trip_123",
        user_id="u1",
        session_id="s1",
        trip_id="trip_123",
        destination="京都",
        dates={"start": "2026-05-01", "end": "2026-05-05", "total_days": 5},
        travelers={"adults": 2, "children": 0},
        budget={"total": 20000, "currency": "CNY"},
        selected_skeleton={"id": "slow", "name": "慢游"},
        selected_transport={"mode": "train", "arrival": "京都站"},
        accommodation={"area": "四条", "hotel": "町屋"},
        daily_plan_summary=[
            {"day": 1, "date": "2026-05-01", "areas": ["四条"], "activity_count": 2}
        ],
        final_plan_summary="京都慢游。",
        decision_log=[
            {"type": "accepted", "category": "skeleton", "value": {"id": "slow"}}
        ],
        lesson_log=["交通衔接要留余量。"],
        created_at="2026-05-05T00:00:00+00:00",
        completed_at="2026-05-05T00:00:00+00:00",
    )

    restored = ArchivedTripEpisode.from_dict(episode.to_dict())

    assert restored == episode
    assert restored.dates["total_days"] == 5
    assert restored.decision_log[0]["category"] == "skeleton"


def test_memory_audit_event_round_trip():
    event = MemoryAuditEvent(
        id="evt_1",
        user_id="u1",
        session_id="s1",
        event_type="reject",
        object_type="phase_output",
        object_payload={"to_phase": 3},
        reason_text="用户要求回退",
        created_at="2026-05-05T00:00:00+00:00",
    )

    restored = MemoryAuditEvent.from_dict(event.to_dict())

    assert restored == event
    assert restored.object_payload == {"to_phase": 3}
```

- [ ] **Step 2: Write failing v3 store tests**

Add this import to `backend/tests/test_memory_v3_store.py`:

```python
from memory.v3_models import ArchivedTripEpisode, MemoryAuditEvent
```

Add these tests:

```python
def _archived_episode(**overrides):
    base = dict(
        id="ep_trip_123",
        user_id="u1",
        session_id="s1",
        trip_id="trip_123",
        destination="京都",
        dates={"start": "2026-05-01", "end": "2026-05-05", "total_days": 5},
        travelers={"adults": 2, "children": 0},
        budget={"total": 20000, "currency": "CNY"},
        selected_skeleton={"id": "slow", "name": "慢游"},
        selected_transport={"mode": "train"},
        accommodation={"area": "四条", "hotel": "町屋"},
        daily_plan_summary=[],
        final_plan_summary="京都慢游。",
        decision_log=[],
        lesson_log=[],
        created_at="2026-05-05T00:00:00+00:00",
        completed_at="2026-05-05T00:00:00+00:00",
    )
    base.update(overrides)
    return ArchivedTripEpisode(**base)


@pytest.mark.asyncio
async def test_append_and_list_archived_episodes_is_idempotent(tmp_path):
    store = FileMemoryV3Store(tmp_path)
    episode = _archived_episode()

    await store.append_episode(episode)
    await store.append_episode(episode)

    episodes = await store.list_episodes("u1")
    assert [item.id for item in episodes] == ["ep_trip_123"]
    assert episodes[0].destination == "京都"


@pytest.mark.asyncio
async def test_working_memory_path_is_session_and_trip_scoped(tmp_path):
    store = FileMemoryV3Store(tmp_path)
    item = WorkingMemoryItem(
        id="wm_1",
        phase=3,
        kind="temporary_rejection",
        domains=["attraction"],
        content="先别考虑迪士尼。",
        reason="当前候选筛选需要避让。",
        status="active",
        expires={"on_session_end": False, "on_trip_change": True, "on_phase_exit": False},
        created_at="2026-04-19T00:00:00",
    )

    await store.upsert_working_memory_item("u1", "s1", "trip_1", item)
    await store.upsert_working_memory_item(
        "u1",
        "s1",
        "trip_2",
        WorkingMemoryItem(
            id="wm_2",
            phase=3,
            kind="temporary_rejection",
            domains=["attraction"],
            content="先别考虑环球影城。",
            reason="新 trip 的临时避让。",
            status="active",
            expires={"on_session_end": False, "on_trip_change": True, "on_phase_exit": False},
            created_at="2026-04-19T00:00:00",
        ),
    )

    trip_1 = await store.load_working_memory("u1", "s1", "trip_1")
    trip_2 = await store.load_working_memory("u1", "s1", "trip_2")

    assert [item.id for item in trip_1.items] == ["wm_1"]
    assert [item.id for item in trip_2.items] == ["wm_2"]
    assert (
        tmp_path
        / "users"
        / "u1"
        / "memory"
        / "sessions"
        / "s1"
        / "trips"
        / "trip_1"
        / "working_memory.json"
    ).exists()


@pytest.mark.asyncio
async def test_append_memory_audit_event_writes_v3_events_jsonl(tmp_path):
    store = FileMemoryV3Store(tmp_path)
    event = MemoryAuditEvent(
        id="evt_1",
        user_id="u1",
        session_id="s1",
        event_type="reject",
        object_type="phase_output",
        object_payload={"to_phase": 3},
        reason_text="用户要求回退",
        created_at="2026-05-05T00:00:00+00:00",
    )

    await store.append_event(event)

    path = tmp_path / "users" / "u1" / "memory" / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows == [event.to_dict()]


@pytest.mark.asyncio
async def test_delete_legacy_user_memory_files_removes_v2_files(tmp_path):
    user_dir = tmp_path / "users" / "u1"
    user_dir.mkdir(parents=True)
    for filename in ("memory.json", "memory_events.jsonl", "trip_episodes.jsonl"):
        (user_dir / filename).write_text("legacy", encoding="utf-8")
    keep = user_dir / "memory" / "profile.json"
    keep.parent.mkdir(parents=True)
    keep.write_text("{}", encoding="utf-8")
    store = FileMemoryV3Store(tmp_path)

    removed = await store.delete_legacy_memory_files()

    assert sorted(path.name for path in removed) == [
        "memory.json",
        "memory_events.jsonl",
        "trip_episodes.jsonl",
    ]
    assert keep.exists()
    assert not (user_dir / "memory.json").exists()
    assert not (user_dir / "memory_events.jsonl").exists()
    assert not (user_dir / "trip_episodes.jsonl").exists()
```

- [ ] **Step 3: Run focused tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_memory_v3_models.py tests/test_memory_v3_store.py -k "archived_trip_episode or memory_audit_event or archived_episodes or trip_scoped or legacy_user_memory_files" -v
```

Expected: import or attribute failures for `ArchivedTripEpisode`, `MemoryAuditEvent`, `append_episode`, `list_episodes`, `append_event`, and `delete_legacy_memory_files`.

- [ ] **Step 4: Add v3 models**

In `backend/memory/v3_models.py`, add these dataclasses after `SessionWorkingMemory` and before `EpisodeSlice`:

```python
@dataclass
class ArchivedTripEpisode:
    id: str
    user_id: str
    session_id: str
    trip_id: str | None
    destination: str | None
    dates: dict[str, Any]
    travelers: dict[str, Any] | None
    budget: dict[str, Any] | None
    selected_skeleton: dict[str, Any] | None
    selected_transport: dict[str, Any] | None
    accommodation: dict[str, Any] | None
    daily_plan_summary: list[dict[str, Any]]
    final_plan_summary: str
    decision_log: list[dict[str, Any]] = field(default_factory=list)
    lesson_log: list[str] = field(default_factory=list)
    created_at: str = ""
    completed_at: str = ""

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
            "selected_transport": self.selected_transport,
            "accommodation": self.accommodation,
            "daily_plan_summary": self.daily_plan_summary,
            "final_plan_summary": self.final_plan_summary,
            "decision_log": self.decision_log,
            "lesson_log": self.lesson_log,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArchivedTripEpisode":
        return cls(
            id=str(data.get("id", "")),
            user_id=str(data.get("user_id", "")),
            session_id=str(data.get("session_id", "")),
            trip_id=data.get("trip_id"),
            destination=data.get("destination"),
            dates=_as_dict(data.get("dates")),
            travelers=data.get("travelers") if isinstance(data.get("travelers"), dict) else None,
            budget=data.get("budget") if isinstance(data.get("budget"), dict) else None,
            selected_skeleton=(
                data.get("selected_skeleton")
                if isinstance(data.get("selected_skeleton"), dict)
                else None
            ),
            selected_transport=(
                data.get("selected_transport")
                if isinstance(data.get("selected_transport"), dict)
                else None
            ),
            accommodation=(
                data.get("accommodation")
                if isinstance(data.get("accommodation"), dict)
                else None
            ),
            daily_plan_summary=[
                item for item in _as_list(data.get("daily_plan_summary")) if isinstance(item, dict)
            ],
            final_plan_summary=str(data.get("final_plan_summary", "")),
            decision_log=[
                item for item in _as_list(data.get("decision_log")) if isinstance(item, dict)
            ],
            lesson_log=[
                str(item) for item in _as_list(data.get("lesson_log")) if str(item).strip()
            ],
            created_at=str(data.get("created_at", "")),
            completed_at=str(data.get("completed_at", "")),
        )


@dataclass
class MemoryAuditEvent:
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
    def from_dict(cls, data: dict[str, Any]) -> "MemoryAuditEvent":
        return cls(
            id=str(data.get("id", "")),
            user_id=str(data.get("user_id", "")),
            session_id=str(data.get("session_id", "")),
            event_type=str(data.get("event_type", "")),
            object_type=str(data.get("object_type", "")),
            object_payload=_as_dict(data.get("object_payload")),
            reason_text=(
                str(data["reason_text"])
                if data.get("reason_text") is not None
                else None
            ),
            created_at=str(data.get("created_at", "")),
        )
```

- [ ] **Step 5: Add v3 store methods**

In `backend/memory/v3_store.py`, update imports:

```python
from memory.v3_models import (
    ArchivedTripEpisode,
    EpisodeSlice,
    MemoryAuditEvent,
    MemoryProfileItem,
    SessionWorkingMemory,
    UserMemoryProfile,
    WorkingMemoryItem,
)
```

Replace `_working_memory_path()` and add new path helpers:

```python
    def _working_memory_path(
        self, user_id: str, session_id: str, trip_id: str | None
    ) -> Path:
        trip_key = trip_id or "_none"
        return (
            self._user_memory_dir(user_id)
            / "sessions"
            / session_id
            / "trips"
            / trip_key
            / "working_memory.json"
        )

    def _episodes_path(self, user_id: str) -> Path:
        return self._user_memory_dir(user_id) / "episodes.jsonl"

    def _events_path(self, user_id: str) -> Path:
        return self._user_memory_dir(user_id) / "events.jsonl"
```

Update every `_working_memory_path(user_id, session_id)` call to pass `trip_id`.

Add these methods near the episode slice methods:

```python
    async def append_episode(self, episode: ArchivedTripEpisode) -> None:
        async with self._lock_for(episode.user_id):
            await asyncio.to_thread(self._append_episode_sync, episode)

    def _append_episode_sync(self, episode: ArchivedTripEpisode) -> None:
        path = self._episodes_path(episode.user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            for row in self._read_jsonl_sync(path):
                if row.get("id") == episode.id:
                    return
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(episode.to_dict(), ensure_ascii=False) + "\n")

    async def list_episodes(self, user_id: str) -> list[ArchivedTripEpisode]:
        async with self._lock_for(user_id):
            return await asyncio.to_thread(self._list_episodes_sync, user_id)

    def _list_episodes_sync(self, user_id: str) -> list[ArchivedTripEpisode]:
        return [
            ArchivedTripEpisode.from_dict(row)
            for row in self._read_jsonl_sync(self._episodes_path(user_id))
        ]

    async def append_event(self, event: MemoryAuditEvent) -> None:
        async with self._lock_for(event.user_id):
            await asyncio.to_thread(self._append_event_sync, event)

    def _append_event_sync(self, event: MemoryAuditEvent) -> None:
        path = self._events_path(event.user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    async def delete_legacy_memory_files(self) -> list[Path]:
        return await asyncio.to_thread(self._delete_legacy_memory_files_sync)

    def _delete_legacy_memory_files_sync(self) -> list[Path]:
        users_dir = self.data_dir / "users"
        if not users_dir.exists():
            return []
        removed: list[Path] = []
        for user_dir in users_dir.iterdir():
            if not user_dir.is_dir():
                continue
            for filename in ("memory.json", "memory_events.jsonl", "trip_episodes.jsonl"):
                path = user_dir / filename
                if path.exists():
                    path.unlink()
                    removed.append(path)
        return removed
```

- [ ] **Step 6: Run focused tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_memory_v3_models.py tests/test_memory_v3_store.py -k "archived_trip_episode or memory_audit_event or archived_episodes or trip_scoped or legacy_user_memory_files" -v
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 1**

Run:

```bash
git add backend/memory/v3_models.py backend/memory/v3_store.py backend/tests/test_memory_v3_models.py backend/tests/test_memory_v3_store.py
git commit -m "feat: add v3 archived episodes store"
```

---

### Task 2: Generate Episode Slices from ArchivedTripEpisode Only

**Files:**
- Modify: `backend/memory/episode_slices.py`
- Modify: `backend/tests/test_episode_slices.py`

- [ ] **Step 1: Replace legacy slice tests with v3 taxonomy tests**

Replace `backend/tests/test_episode_slices.py` with:

```python
from memory.episode_slices import build_episode_slices
from memory.v3_models import ArchivedTripEpisode


def _episode(**overrides):
    base = dict(
        id="ep_trip_123",
        user_id="default_user",
        session_id="s1",
        trip_id="trip_123",
        destination="京都",
        dates={"start": "2026-05-01", "end": "2026-05-05", "total_days": 5},
        travelers={"adults": 2, "children": 0},
        budget={"total": 20000, "currency": "CNY"},
        selected_skeleton={
            "id": "balanced",
            "name": "轻松版",
            "summary": "节奏舒适，保留自由活动时间。",
        },
        selected_transport={"mode": "train", "arrival_station": "京都站"},
        accommodation={"area": "四条", "hotel": "町屋"},
        daily_plan_summary=[
            {"day": 1, "date": "2026-05-01", "areas": ["四条"], "activity_count": 2},
            {"day": 2, "date": "2026-05-02", "areas": ["岚山"], "activity_count": 3},
        ],
        final_plan_summary="这次京都之行选择了轻松节奏和町屋住宿。",
        decision_log=[
            {
                "type": "accepted",
                "category": "skeleton",
                "value": {"id": "balanced", "name": "轻松版"},
                "reason": "用户选择轻松节奏。",
            },
            {
                "type": "rejected",
                "category": "hotel",
                "value": {"name": "商务连锁酒店"},
                "reason": "用户更想住町屋。",
            },
        ],
        lesson_log=[
            "上午安排太满会让后半天疲劳。",
            "交通衔接要给步行留余量。",
        ],
        created_at="2026-04-19T00:00:00+00:00",
        completed_at="2026-04-19T00:00:00+00:00",
    )
    base.update(overrides)
    return ArchivedTripEpisode(**base)


def test_build_episode_slices_generates_v3_taxonomy():
    slices = build_episode_slices(_episode(), now="2026-04-19T00:00:00+00:00")

    slice_types = {item.slice_type for item in slices}

    assert "itinerary_pattern" in slice_types
    assert "stay_choice" in slice_types
    assert "transport_choice" in slice_types
    assert "budget_signal" in slice_types
    assert "rejected_option" in slice_types
    assert "pitfall" in slice_types
    assert "accepted_pattern" not in slice_types
    assert all(item.source_episode_id == "ep_trip_123" for item in slices)
    assert all(item.source_trip_id == "trip_123" for item in slices)


def test_itinerary_pattern_uses_skeleton_and_daily_summary():
    slices = build_episode_slices(_episode(), now="2026-04-19T00:00:00+00:00")

    itinerary = next(item for item in slices if item.slice_type == "itinerary_pattern")

    assert itinerary.domains == ["planning_style", "pace", "itinerary"]
    assert "轻松版" in itinerary.content
    assert "岚山" in itinerary.content
    assert itinerary.entities["destination"] == "京都"


def test_rejected_option_uses_decision_log_only():
    slices = build_episode_slices(
        _episode(decision_log=[{"type": "rejected", "category": "activity", "value": {"name": "高强度打卡"}}]),
        now="2026-04-19T00:00:00+00:00",
    )

    rejected = [item for item in slices if item.slice_type == "rejected_option"]

    assert len(rejected) == 1
    assert rejected[0].id == "slice_ep_trip_123_rejected_option_01"
    assert "高强度打卡" in rejected[0].content


def test_pitfall_uses_lesson_log_only():
    slices = build_episode_slices(
        _episode(lesson_log=["换乘预留 20 分钟更稳。"]),
        now="2026-04-19T00:00:00+00:00",
    )

    pitfalls = [item for item in slices if item.slice_type == "pitfall"]

    assert len(pitfalls) == 1
    assert "换乘预留" in pitfalls[0].content


def test_build_episode_slices_truncates_content_to_180_chars():
    slices = build_episode_slices(
        _episode(
            selected_skeleton={"summary": "A" * 260},
            accommodation={"area": "B" * 260},
            selected_transport={"notes": "C" * 260},
            lesson_log=["D" * 260],
        ),
        now="2026-04-19T00:00:00+00:00",
    )

    assert slices
    assert all(len(item.content) <= 180 for item in slices)
```

- [ ] **Step 2: Run focused tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_episode_slices.py -v
```

Expected: failures because `build_episode_slices()` still imports legacy `TripEpisode` and emits `accepted_pattern`.

- [ ] **Step 3: Replace slice taxonomy and builder input**

In `backend/memory/episode_slices.py`:

```python
from memory.v3_models import ArchivedTripEpisode, EpisodeSlice
```

Use this slice metadata:

```python
_SLICE_META: dict[str, dict[str, list[str]]] = {
    "itinerary_pattern": {
        "domains": ["planning_style", "pace", "itinerary"],
        "keywords": ["骨架", "节奏", "路线", "区域", "天数"],
    },
    "stay_choice": {
        "domains": ["hotel", "accommodation"],
        "keywords": ["住宿", "酒店", "民宿", "区域"],
    },
    "transport_choice": {
        "domains": ["transport", "train", "flight"],
        "keywords": ["交通", "航班", "高铁", "火车", "到达"],
    },
    "budget_signal": {
        "domains": ["budget"],
        "keywords": ["预算", "花费", "成本", "分配"],
    },
    "rejected_option": {
        "domains": ["general"],
        "keywords": ["拒绝", "排除", "不要", "避开"],
    },
    "pitfall": {
        "domains": ["general", "pace"],
        "keywords": ["坑", "教训", "注意", "疲劳", "风险"],
    },
}
```

Change the signature:

```python
def build_episode_slices(episode: ArchivedTripEpisode, *, now: str) -> list[EpisodeSlice]:
```

Build slices from these v3 fields:

```python
    if episode.selected_skeleton or episode.daily_plan_summary:
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="itinerary_pattern",
                index=1,
                content=_itinerary_pattern_content(episode),
                entities={
                    **base_entities,
                    "selected_skeleton": _entity_text(episode.selected_skeleton),
                    "daily_plan_summary": _entity_text(episode.daily_plan_summary),
                },
                applicability="仅供规划骨架、区域节奏和跨天组织参考；当前天数或同行人变化时需重新评估。",
            )
        )

    if episode.accommodation:
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="stay_choice",
                index=1,
                content=_stay_choice_content(episode),
                entities={**base_entities, "accommodation": _entity_text(episode.accommodation)},
                applicability="仅供住宿区域或类型参考；当前预算、季节和库存变化时需重新查询。",
            )
        )

    if episode.selected_transport:
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="transport_choice",
                index=1,
                content=_transport_choice_content(episode),
                entities={**base_entities, "selected_transport": _entity_text(episode.selected_transport)},
                applicability="仅供交通方式选择参考；当前班次、价格和日期变化时需重新查询。",
            )
        )
```

Generate rejected and pitfall slices only from explicit v3 logs:

```python
    rejected_decisions = [
        item for item in _as_list_or_empty(episode.decision_log)
        if isinstance(item, dict) and str(item.get("type")) == "rejected"
    ]
    for index, decision in enumerate(rejected_decisions[:2], start=1):
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="rejected_option",
                index=index,
                content=_rejected_option_content(decision),
                entities={**base_entities, "decision": _entity_text(decision)},
                applicability="仅供避让相似选项；不代表所有同类选项都要排除。",
            )
        )

    for index, lesson in enumerate(_as_list_or_empty(episode.lesson_log)[:2], start=1):
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="pitfall",
                index=index,
                content=_pitfall_content(lesson),
                entities={**base_entities, "lesson": _entity_text(lesson)},
                applicability="仅供风险提醒；具体行程需结合当前节奏和体力。",
            )
        )
```

Keep `_build_slice()`, `_render_value()`, `_entity_text()`, `_sanitize_text()`, and `_truncate()` with `ArchivedTripEpisode` type annotations.

- [ ] **Step 4: Add v3 content helpers**

In `backend/memory/episode_slices.py`, replace `_accepted_pattern_content()` with:

```python
def _itinerary_pattern_content(episode: ArchivedTripEpisode) -> str:
    parts: list[str] = []
    skeleton = _render_value(episode.selected_skeleton)
    if skeleton:
        parts.append(f"行程骨架：{skeleton}")
    daily = _render_value(episode.daily_plan_summary)
    if daily:
        parts.append(f"每日节奏：{daily}")
    if not parts:
        parts.append("历史行程骨架。")
    return "；".join(parts)


def _stay_choice_content(episode: ArchivedTripEpisode) -> str:
    rendered = _render_value(episode.accommodation)
    if rendered:
        return f"住宿选择：{rendered}"
    return "住宿选择。"


def _transport_choice_content(episode: ArchivedTripEpisode) -> str:
    rendered = _render_value(episode.selected_transport)
    if rendered:
        return f"交通选择：{rendered}"
    return "交通选择。"
```

Update `_budget_signal_content()` so it reads `budget.total` and `budget.amount`:

```python
def _budget_signal_content(episode: ArchivedTripEpisode) -> str:
    parts: list[str] = []
    budget = episode.budget or {}
    amount = budget.get("total", budget.get("amount"))
    currency = budget.get("currency")
    if amount is not None:
        amount_text = _render_value(amount)
        parts.append(f"预算：{amount_text} {currency}" if currency else f"预算：{amount_text}")
    elif budget:
        parts.append(f"预算：{_render_value(budget)}")
    summary = _render_value(episode.final_plan_summary)
    if summary:
        parts.append(f"总结：{summary}")
    return "；".join(parts) if parts else "预算信息。"
```

- [ ] **Step 5: Run focused tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_episode_slices.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 2**

Run:

```bash
git add backend/memory/episode_slices.py backend/tests/test_episode_slices.py
git commit -m "feat: build episode slices from v3 archived episodes"
```

---

### Task 3: Archive Phase 7 Trips into v3 Episodes

**Files:**
- Create: `backend/memory/archival.py`
- Modify: `backend/main.py`
- Create: `backend/tests/test_memory_archival.py`
- Modify: `backend/tests/test_memory_integration.py`

- [ ] **Step 1: Write failing archival unit tests**

Create `backend/tests/test_memory_archival.py`:

```python
from memory.archival import build_archived_trip_episode
from state.models import Accommodation, Activity, Budget, DateRange, DayPlan, Location, Travelers, TravelPlanState


def _plan() -> TravelPlanState:
    return TravelPlanState(
        session_id="s1",
        trip_id="trip_123",
        phase=7,
        destination="京都",
        dates=DateRange(start="2026-05-01", end="2026-05-05"),
        travelers=Travelers(adults=2, children=0),
        budget=Budget(total=20000, currency="CNY"),
        skeleton_plans=[
            {"id": "slow", "name": "慢游", "summary": "东山、四条、岚山慢节奏。"},
            {"id": "intense", "name": "高强度", "summary": "每天多点打卡。", "why_not": "太累。"},
        ],
        selected_skeleton_id="slow",
        selected_transport={"mode": "train", "arrival_station": "京都站"},
        accommodation=Accommodation(area="四条", hotel="町屋"),
        alternatives=[
            {"type": "hotel", "name": "商务连锁酒店", "decision": "rejected", "reason": "用户更想住町屋。"}
        ],
        risks=[{"level": "medium", "description": "岚山返程要避开晚高峰。"}],
        daily_plans=[
            DayPlan(
                day=1,
                date="2026-05-01",
                activities=[
                    Activity(
                        name="锦市场",
                        location=Location(lat=35.005, lng=135.764, name="锦市场"),
                        start_time="10:00",
                        end_time="12:00",
                        category="food",
                    )
                ],
                notes="抵达后轻松安排。",
            )
        ],
    )


def test_build_archived_trip_episode_uses_v3_state_only():
    episode = build_archived_trip_episode(
        user_id="default_user",
        session_id="s1",
        plan=_plan(),
        now="2026-05-05T00:00:00+00:00",
    )

    assert episode.id == "ep_trip_123"
    assert episode.user_id == "default_user"
    assert episode.trip_id == "trip_123"
    assert episode.destination == "京都"
    assert episode.dates == {"start": "2026-05-01", "end": "2026-05-05", "total_days": 5}
    assert episode.selected_skeleton == {"id": "slow", "name": "慢游", "summary": "东山、四条、岚山慢节奏。"}
    assert episode.selected_transport == {"mode": "train", "arrival_station": "京都站"}
    assert episode.accommodation == {"area": "四条", "hotel": "町屋"}
    assert episode.daily_plan_summary == [
        {
            "day": 1,
            "date": "2026-05-01",
            "areas": ["锦市场"],
            "activity_count": 1,
            "notes": "抵达后轻松安排。",
        }
    ]
    assert any(item["type"] == "accepted" and item["category"] == "skeleton" for item in episode.decision_log)
    assert any(item["type"] == "rejected" and item["category"] == "hotel" for item in episode.decision_log)
    assert episode.lesson_log == ["岚山返程要避开晚高峰。"]


def test_build_archived_trip_episode_handles_missing_trip_id():
    plan = _plan()
    plan.trip_id = None

    episode = build_archived_trip_episode(
        user_id="default_user",
        session_id="s1",
        plan=plan,
        now="2026-05-05T00:00:00+00:00",
    )

    assert episode.id == "ep_s1"
    assert episode.trip_id is None
```

- [ ] **Step 2: Run focused tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_memory_archival.py -v
```

Expected: import error because `memory.archival` does not exist.

- [ ] **Step 3: Implement archival builder**

Create `backend/memory/archival.py`:

```python
from __future__ import annotations

from typing import Any

from memory.v3_models import ArchivedTripEpisode
from state.models import TravelPlanState


def build_archived_trip_episode(
    *,
    user_id: str,
    session_id: str,
    plan: TravelPlanState,
    now: str,
) -> ArchivedTripEpisode:
    return ArchivedTripEpisode(
        id=f"ep_{plan.trip_id or session_id}",
        user_id=user_id,
        session_id=session_id,
        trip_id=plan.trip_id,
        destination=plan.destination,
        dates=_dates_payload(plan),
        travelers=plan.travelers.to_dict() if plan.travelers else None,
        budget=plan.budget.to_dict() if plan.budget else None,
        selected_skeleton=_selected_skeleton(plan),
        selected_transport=dict(plan.selected_transport) if isinstance(plan.selected_transport, dict) else None,
        accommodation=plan.accommodation.to_dict() if plan.accommodation else None,
        daily_plan_summary=_daily_plan_summary(plan),
        final_plan_summary=_final_plan_summary(plan),
        decision_log=_decision_log(plan),
        lesson_log=_lesson_log(plan),
        created_at=plan.created_at,
        completed_at=now,
    )


def _dates_payload(plan: TravelPlanState) -> dict[str, Any]:
    if not plan.dates:
        return {}
    return {
        "start": plan.dates.start,
        "end": plan.dates.end,
        "total_days": plan.dates.total_days,
    }


def _selected_skeleton(plan: TravelPlanState) -> dict[str, Any] | None:
    if not plan.selected_skeleton_id:
        return None
    for skeleton in plan.skeleton_plans:
        if not isinstance(skeleton, dict):
            continue
        if skeleton.get("id") == plan.selected_skeleton_id:
            return dict(skeleton)
    return {"id": plan.selected_skeleton_id}


def _daily_plan_summary(plan: TravelPlanState) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for day_plan in plan.daily_plans:
        areas: list[str] = []
        for activity in day_plan.activities:
            name = activity.location.name or activity.name
            if name and name not in areas:
                areas.append(name)
        summary = {
            "day": day_plan.day,
            "date": day_plan.date,
            "areas": areas,
            "activity_count": len(day_plan.activities),
            "notes": day_plan.notes,
        }
        summaries.append(summary)
    return summaries


def _final_plan_summary(plan: TravelPlanState) -> str:
    if plan.destination and plan.dates:
        return f"{plan.destination} · {plan.dates.total_days}天行程"
    if plan.destination:
        return f"{plan.destination}行程"
    return "历史旅行归档"


def _decision_log(plan: TravelPlanState) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    selected = _selected_skeleton(plan)
    if selected:
        decisions.append(
            {
                "type": "accepted",
                "category": "skeleton",
                "value": selected,
                "reason": "selected_skeleton_id matched archived plan state",
            }
        )
    if isinstance(plan.selected_transport, dict):
        decisions.append(
            {
                "type": "accepted",
                "category": "transport",
                "value": dict(plan.selected_transport),
                "reason": "selected_transport was locked in plan state",
            }
        )
    if plan.accommodation:
        decisions.append(
            {
                "type": "accepted",
                "category": "accommodation",
                "value": plan.accommodation.to_dict(),
                "reason": "accommodation was locked in plan state",
            }
        )
    for item in plan.alternatives:
        if not isinstance(item, dict):
            continue
        if item.get("decision") == "rejected" or item.get("status") == "rejected" or item.get("why_not"):
            decisions.append(
                {
                    "type": "rejected",
                    "category": str(item.get("type") or item.get("category") or "option"),
                    "value": dict(item),
                    "reason": str(item.get("reason") or item.get("why_not") or ""),
                }
            )
    return decisions


def _lesson_log(plan: TravelPlanState) -> list[str]:
    lessons: list[str] = []
    for risk in plan.risks:
        if not isinstance(risk, dict):
            continue
        description = str(risk.get("description") or risk.get("summary") or "").strip()
        if description and description not in lessons:
            lessons.append(description)
    return lessons[:5]
```

- [ ] **Step 4: Replace Phase 7 archive path in `main.py`**

Update imports:

```python
from memory.archival import build_archived_trip_episode
from memory.v3_models import MemoryAuditEvent, generate_profile_item_id
```

Remove this import:

```python
from memory.models import MemoryCandidate, MemoryEvent, MemoryItem, TripEpisode
```

Replace `_append_memory_event_nonfatal()` and `_schedule_memory_event()` to use `MemoryAuditEvent` and `memory_mgr.v3_store.append_event()`:

```python
    async def _append_memory_event_nonfatal(event: MemoryAuditEvent) -> None:
        if not config.memory.enabled:
            return
        try:
            await memory_mgr.v3_store.append_event(event)
        except Exception:
            return

    def _schedule_memory_event(
        *,
        user_id: str,
        session_id: str,
        event_type: str,
        object_type: str,
        object_payload: dict[str, Any],
        reason_text: str | None = None,
    ) -> None:
        if not config.memory.enabled:
            return
        event = MemoryAuditEvent(
            id=f"{session_id}:{event_type}:{_now_iso()}",
            user_id=user_id,
            session_id=session_id,
            event_type=event_type,
            object_type=object_type,
            object_payload=object_payload,
            reason_text=reason_text,
            created_at=_now_iso(),
        )
        asyncio.create_task(_append_memory_event_nonfatal(event))
```

Delete `_build_trip_episode()`, `_append_trip_episode_once()`, and the old `TripEpisode` typed `_append_episode_slices()`.

Add the v3 replacement:

```python
    async def _append_archived_trip_episode_once(
        *,
        user_id: str,
        session_id: str,
        plan: TravelPlanState,
    ) -> bool:
        episode = build_archived_trip_episode(
            user_id=user_id,
            session_id=session_id,
            plan=plan,
            now=_now_iso(),
        )
        existing = await memory_mgr.v3_store.list_episodes(user_id)
        if any(item.id == episode.id for item in existing):
            await _append_episode_slices(episode)
            return False
        await memory_mgr.v3_store.append_episode(episode)
        await _append_episode_slices(episode)
        return True

    async def _append_episode_slices(episode) -> None:
        now = _now_iso()
        for slice_ in build_episode_slices(episode, now=now):
            await memory_mgr.v3_store.append_episode_slice(slice_)
```

Change the Phase 7 completion call from `_append_trip_episode_once` to `_append_archived_trip_episode_once`.

- [ ] **Step 5: Remove legacy trip reset cleanup**

In `_rotate_trip_on_reset_backtrack()`, remove the loop that calls `memory_mgr.store.list_items()` and `update_status()`. The function should end with:

```python
        plan.trip_id = f"trip_{uuid.uuid4().hex[:12]}"
        return True
```

- [ ] **Step 6: Run focused tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_memory_archival.py tests/test_episode_slices.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Add integration assertion for v3 Phase 7 archival**

In `backend/tests/test_memory_integration.py`, replace any assertion that checks `memory_mgr.store.list_episodes("u1")` after Phase 7 with:

```python
episodes = await memory_mgr.v3_store.list_episodes("u1")
slices = await memory_mgr.v3_store.list_episode_slices("u1")

assert len(episodes) == 1
assert episodes[0].id.startswith("ep_")
assert episodes[0].decision_log
assert all(slice_.source_episode_id == episodes[0].id for slice_ in slices)
```

Run:

```bash
cd backend && pytest tests/test_memory_integration.py -k "episode or archive" -v
```

Expected: selected integration tests pass after old assertions are updated.

- [ ] **Step 8: Commit Task 3**

Run:

```bash
git add backend/memory/archival.py backend/main.py backend/tests/test_memory_archival.py backend/tests/test_memory_integration.py
git commit -m "feat: archive completed trips as v3 episodes"
```

---

### Task 4: Make Recall v3 Retrieval-Plan Native

**Files:**
- Modify: `backend/memory/recall_query.py`
- Modify: `backend/memory/symbolic_recall.py`
- Modify: `backend/memory/manager.py`
- Modify: `backend/main.py`
- Delete: `backend/memory/recall_query_adapter.py`
- Modify: `backend/tests/test_recall_query.py`
- Modify: `backend/tests/test_symbolic_recall.py`
- Modify: `backend/tests/test_memory_manager.py`
- Delete: `backend/tests/test_recall_query_adapter.py`

- [ ] **Step 1: Write failing retrieval plan parser tests**

In `backend/tests/test_recall_query.py`, replace the non-profile source rejection test with:

```python
def test_parse_recall_query_tool_arguments_accepts_v3_history_sources():
    for source in ("profile", "episode_slice", "hybrid_history"):
        plan = parse_recall_query_tool_arguments(
            {
                "source": source,
                "buckets": ["stable_preferences", "constraints"],
                "domains": ["hotel", "accommodation"],
                "entities": {"destination": "京都"},
                "keywords": ["住宿", "酒店"],
                "aliases": ["住哪里", "住宿偏好"],
                "strictness": "soft",
                "top_k": 8,
                "reason": "user wants historical accommodation memory",
            }
        )

        assert plan.source == source
        assert plan.entities == {"destination": "京都"}
        assert plan.fallback_used == "none"


def test_parse_recall_query_tool_arguments_rejects_legacy_sources():
    for source in ("working_memory", "legacy", "profile_fixed"):
        plan = parse_recall_query_tool_arguments(
            {
                "source": source,
                "buckets": ["stable_preferences"],
                "domains": [],
                "entities": {},
                "keywords": [],
                "aliases": [],
                "strictness": "soft",
                "top_k": 5,
                "reason": "bad source",
            }
        )

        assert plan.fallback_used == "invalid_query_plan"
        assert plan.source == "hybrid_history"
        assert plan.buckets == ["constraints", "rejections", "stable_preferences"]
```

Update `test_fallback_retrieval_plan_is_conservative()`:

```python
def test_fallback_retrieval_plan_is_conservative():
    plan = fallback_retrieval_plan()

    assert plan.source == "hybrid_history"
    assert plan.buckets == ["constraints", "rejections", "stable_preferences"]
    assert plan.entities == {}
    assert plan.strictness == "soft"
    assert plan.top_k == 5
```

- [ ] **Step 2: Write failing symbolic recall tests**

In `backend/tests/test_symbolic_recall.py`, stop importing `RecallQuery` and `build_recall_query`. Add this import:

```python
from memory.recall_query import RecallRetrievalPlan
```

Add a helper:

```python
def _plan(**overrides):
    base = dict(
        source="hybrid_history",
        buckets=["constraints", "rejections", "stable_preferences"],
        domains=["hotel"],
        entities={"destination": "京都"},
        keywords=["住宿", "青旅"],
        aliases=["住哪里"],
        strictness="soft",
        top_k=5,
        reason="test plan",
    )
    base.update(overrides)
    return RecallRetrievalPlan(**base)
```

Change ranking tests so they call:

```python
ranked = rank_profile_items(_plan(source="profile"), profile)
ranked = rank_episode_slices(_plan(source="episode_slice"), slices)
```

Add this test:

```python
def test_rankers_respect_retrieval_plan_source():
    profile = UserMemoryProfile(
        schema_version=3,
        user_id="u1",
        constraints=[_profile_item(domain="hotel", key="avoid_hostel", value="青旅")],
        rejections=[],
        stable_preferences=[],
        preference_hypotheses=[],
    )
    slices = [_slice()]

    assert rank_profile_items(_plan(source="episode_slice"), profile) == []
    assert rank_episode_slices(_plan(source="profile"), slices) == []
```

- [ ] **Step 3: Run focused tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_recall_query.py tests/test_symbolic_recall.py -v
```

Expected: parser failures because only `profile` source is accepted, and ranking failures because rankers still require `RecallQuery`.

- [ ] **Step 4: Update `RecallRetrievalPlan` and parser**

In `backend/memory/recall_query.py`, change the dataclass and fallback:

```python
@dataclass
class RecallRetrievalPlan:
    source: str
    buckets: list[str]
    domains: list[str]
    entities: dict[str, str]
    keywords: list[str]
    aliases: list[str]
    strictness: str
    top_k: int
    reason: str
    fallback_used: str = "none"


def fallback_retrieval_plan() -> RecallRetrievalPlan:
    return RecallRetrievalPlan(
        source="hybrid_history",
        buckets=["constraints", "rejections", "stable_preferences"],
        domains=[],
        entities={},
        keywords=[],
        aliases=[],
        strictness="soft",
        top_k=5,
        reason="fallback_default_plan",
        fallback_used="fallback_default_plan",
    )
```

Add:

```python
_ALLOWED_SOURCES = {"profile", "episode_slice", "hybrid_history"}
_FORBIDDEN_SOURCES = {"working_memory", "legacy", "profile_fixed"}


def _parse_string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    parsed: dict[str, str] = {}
    for key, raw in value.items():
        if isinstance(key, str) and isinstance(raw, str):
            parsed[key] = raw
    return parsed
```

Update `parse_recall_query_tool_arguments()`:

```python
    source = payload.get("source")
    if source not in _ALLOWED_SOURCES or source in _FORBIDDEN_SOURCES:
        fallback_plan = fallback_retrieval_plan()
        fallback_plan.reason = "invalid_query_plan"
        fallback_plan.fallback_used = "invalid_query_plan"
        return fallback_plan

    return RecallRetrievalPlan(
        source=source,
        buckets=_parse_string_list(payload.get("buckets")),
        domains=_parse_string_list(payload.get("domains")),
        entities=_parse_string_dict(payload.get("entities")),
        keywords=_parse_string_list(payload.get("keywords")),
        aliases=_parse_string_list(payload.get("aliases")),
        strictness=_parse_strictness(payload.get("strictness")),
        top_k=_parse_top_k(payload.get("top_k")),
        reason=_parse_string(payload.get("reason"), ""),
    )
```

- [ ] **Step 5: Make symbolic recall consume retrieval plans**

In `backend/memory/symbolic_recall.py`, remove `RecallQuery`. Keep the text-domain helpers and make rankers accept `RecallRetrievalPlan`:

```python
from memory.recall_query import RecallRetrievalPlan
```

Add:

```python
_PROFILE_SOURCES = {"profile", "hybrid_history"}
_EPISODE_SOURCES = {"episode_slice", "hybrid_history"}
```

Change signatures:

```python
def rank_profile_items(
    plan: RecallRetrievalPlan, profile: UserMemoryProfile
) -> list[RecallCandidate]:
    if plan.source not in _PROFILE_SOURCES:
        return []
```

```python
def rank_episode_slices(
    plan: RecallRetrievalPlan, slices: list[EpisodeSlice]
) -> list[RecallCandidate]:
    if plan.source not in _EPISODE_SOURCES:
        return []
```

In `_score_profile_item()` and `_score_episode_slice()`, replace `query` with `plan`, replace `query.keywords` with `list(plan.keywords) + list(plan.aliases)`, and replace `query.entities` with `plan.entities`.

Add a helper for callers that do not pass an LLM retrieval plan:

```python
def heuristic_retrieval_plan_from_message(message: str) -> RecallRetrievalPlan:
    text = _normalize_text(message)
    if not should_trigger_memory_recall(text):
        return RecallRetrievalPlan(
            source="hybrid_history",
            buckets=[],
            domains=[],
            entities={},
            keywords=[],
            aliases=[],
            strictness="soft",
            top_k=5,
            reason="no_historical_recall_cue",
        )
    domains = _extract_domains(text)
    destination = _extract_destination(text)
    return RecallRetrievalPlan(
        source="hybrid_history",
        buckets=list(_CONSERVATIVE_PROFILE_BUCKETS),
        domains=domains,
        entities={"destination": destination} if destination else {},
        keywords=_extract_keywords(text),
        aliases=[],
        strictness="soft",
        top_k=5,
        reason="heuristic_history_recall",
    )
```

- [ ] **Step 6: Remove recall adapter from manager**

In `backend/memory/manager.py`:

Remove imports:

```python
from memory.recall_query_adapter import plan_to_legacy_recall_query
from memory.symbolic_recall import build_recall_query
```

Import:

```python
from memory.symbolic_recall import (
    heuristic_retrieval_plan_from_message,
    rank_episode_slices,
    rank_profile_items,
    should_trigger_memory_recall,
)
```

Replace the recall decision block inside `generate_context()` with:

```python
        active_plan = retrieval_plan
        should_run_query_recall = False
        final_recall_decision = "no_recall_applied"

        if recall_gate is None:
            should_run_query_recall = bool(user_message and should_trigger_memory_recall(user_message))
            if should_run_query_recall and active_plan is None:
                active_plan = heuristic_retrieval_plan_from_message(user_message)
        elif recall_gate:
            should_run_query_recall = True
            if active_plan is None:
                active_plan = heuristic_retrieval_plan_from_message(user_message)

        if should_run_query_recall:
            final_recall_decision = "query_recall_enabled"

        if should_run_query_recall and active_plan is not None:
            query_profile_limit = active_plan.top_k or _QUERY_PROFILE_LIMIT
            recall_candidates.extend(
                rank_profile_items(active_plan, profile)[:query_profile_limit]
            )
            candidate_slices = await self.v3_store.list_episode_slices(
                user_id,
                destination=active_plan.entities.get("destination"),
            )
            recall_candidates.extend(
                rank_episode_slices(active_plan, candidate_slices)[:_QUERY_SLICE_LIMIT]
            )
```

In telemetry, use `active_plan` instead of `retrieval_plan`:

```python
        if active_plan is not None:
            telemetry.query_plan = {
                "source": active_plan.source,
                "buckets": list(active_plan.buckets),
                "domains": list(active_plan.domains),
                "entities": dict(active_plan.entities),
                "strictness": active_plan.strictness,
                "top_k": active_plan.top_k,
            }
            telemetry.query_plan_fallback = active_plan.fallback_used
```

- [ ] **Step 7: Update recall query tool schema and prompt**

In `backend/main.py`, update `_build_recall_query_tool()`:

```python
                "source": {"type": "string", "enum": ["profile", "episode_slice", "hybrid_history"]},
                "entities": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
```

Add `"entities"` to required fields.

Update `_build_recall_query_prompt()` text:

```python
            "source 必须是 profile、episode_slice 或 hybrid_history。",
            "profile 用于长期画像；episode_slice 用于历史旅行经验；hybrid_history 用于两者都可能相关。",
            "working_memory 不是历史召回源，不能出现在 source 中。",
```

Update `_query_plan_summary()`:

```python
        "source": plan.source,
        "entities": dict(plan.entities),
```

- [ ] **Step 8: Delete adapter test and file**

Run:

```bash
rm backend/memory/recall_query_adapter.py backend/tests/test_recall_query_adapter.py
```

- [ ] **Step 9: Update memory manager tests**

In `backend/tests/test_memory_manager.py`, remove imports of `MemoryItem`, `MemorySource`, `Rejection`, `TripSummary`, `UserMemory`, and `RecallQuery`. Replace tests that monkeypatch `build_recall_query` or expect legacy adapter behavior with assertions against `RecallRetrievalPlan(source="hybrid_history", entities={"destination": "京都"})`.

Use this assertion in the retrieval-plan preference test:

```python
assert recall.query_plan["source"] == "profile"
assert recall.query_plan["domains"] == ["hotel"]
assert recall.query_plan["entities"] == {}
```

Use this assertion in the mixed profile/slice test:

```python
assert recall.query_plan["source"] == "hybrid_history"
assert recall.sources["query_profile"] == 1
assert recall.sources["episode_slice"] == 1
```

- [ ] **Step 10: Run focused tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_recall_query.py tests/test_symbolic_recall.py tests/test_memory_manager.py -v
```

Expected: all selected tests pass and no import references `memory.recall_query_adapter`.

- [ ] **Step 11: Commit Task 4**

Run:

```bash
git add backend/memory/recall_query.py backend/memory/symbolic_recall.py backend/memory/manager.py backend/main.py backend/tests/test_recall_query.py backend/tests/test_symbolic_recall.py backend/tests/test_memory_manager.py
git rm backend/memory/recall_query_adapter.py backend/tests/test_recall_query_adapter.py
git commit -m "feat: make memory recall v3 retrieval-plan native"
```

---

### Task 5: Remove Legacy-Compatible Extraction and Aggregate Task

**Files:**
- Modify: `backend/memory/extraction.py`
- Modify: `backend/main.py`
- Modify: `backend/tests/test_memory_extraction.py`
- Modify: `backend/tests/test_memory_integration.py`

- [ ] **Step 1: Write failing extraction gate parser tests**

In `backend/tests/test_memory_extraction.py`, add:

```python
def test_parse_extraction_gate_requires_routes_object():
    result = parse_v3_extraction_gate_tool_arguments(
        {
            "should_extract": True,
            "reason": "legacy_bool_payload",
            "message": "旧格式",
        }
    )

    assert result.should_extract is False
    assert result.routes.profile is False
    assert result.routes.working_memory is False
    assert result.reason == "invalid_route_payload"


def test_combined_v3_extraction_tool_is_not_exported():
    import memory.extraction as extraction

    assert not hasattr(extraction, "build_v3_extraction_tool")
    assert not hasattr(extraction, "parse_v3_extraction_tool_arguments")
```

- [ ] **Step 2: Update integration tests for split tasks only**

In `backend/tests/test_memory_integration.py`, replace assertions that expect `kind="memory_extraction"` with assertions for split route tasks:

```python
task_kinds = [task.kind for task in published_tasks]

assert "memory_extraction_gate" in task_kinds
assert "memory_extraction" not in task_kinds
assert "profile_memory_extraction" in task_kinds or "working_memory_extraction" in task_kinds
```

- [ ] **Step 3: Run focused tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py -k "requires_routes_object or combined_v3_extraction_tool_is_not_exported" -v
```

Expected: failures because parser still falls back from legacy `should_extract`, and combined tool functions still exist.

- [ ] **Step 4: Remove combined extraction exports**

In `backend/memory/extraction.py`, delete:

```python
V3ExtractionResult.episode_evidence
V3ExtractionResult.state_observations
V3ExtractionResult.drop
_V3_EXTRACTION_TOOL_NAME
build_v3_extraction_tool()
v3_extraction_tool_name()
parse_v3_extraction_tool_arguments()
build_v3_extraction_prompt()
```

Keep `V3ExtractionResult` as:

```python
@dataclass
class V3ExtractionResult:
    profile_updates: V3ProfileUpdates = field(default_factory=V3ProfileUpdates)
    working_memory: list[WorkingMemoryItem] = field(default_factory=list)
```

Update `parse_v3_extraction_gate_tool_arguments()`:

```python
def parse_v3_extraction_gate_tool_arguments(
    arguments: dict[str, Any] | None,
) -> V3ExtractionGateResult:
    if not isinstance(arguments, dict):
        return V3ExtractionGateResult(reason="invalid_tool_payload")
    routes_raw = arguments.get("routes")
    if not isinstance(routes_raw, dict):
        return V3ExtractionGateResult(reason="invalid_route_payload")
    routes = V3ExtractionRoutes(
        profile=bool(routes_raw.get("profile", False)),
        working_memory=bool(routes_raw.get("working_memory", False)),
    )
    should_extract = bool(arguments.get("should_extract", False))
    if not should_extract:
        routes = V3ExtractionRoutes()
    reason = str(arguments.get("reason", "") or "").strip()
    message = str(arguments.get("message", "") or "").strip()
    return V3ExtractionGateResult(
        should_extract=routes.any,
        routes=routes,
        reason=reason or ("memory_routes_detected" if routes.any else "no_memory_routes"),
        message=message,
    )
```

Update `parse_v3_extraction_response()` return value:

```python
    return V3ExtractionResult(
        profile_updates=profile_updates,
        working_memory=working_memory,
    )
```

- [ ] **Step 5: Remove combined extraction runtime from `main.py`**

In `backend/main.py`, remove imports:

```python
    V3ExtractionResult,
    build_v3_extraction_tool,
    build_v3_extraction_prompt,
    parse_v3_extraction_tool_arguments,
```

Keep `V3ExtractionResult` import only if type annotations still need it.

Delete `_extract_combined_memory_items()`.

In `_run_memory_job()`, delete the aggregate task publication block with:

```python
        task_id = f"memory_extraction:{snapshot.session_id}:{snapshot.turn_id}"
        started_at = time.time()
        _publish_memory_task(
            snapshot.session_id,
            InternalTask(
                id=task_id,
                kind="memory_extraction",
                label="记忆提取",
                status="pending",
                message="正在提取可复用的旅行偏好…",
                blocking=False,
                scope="background",
                started_at=started_at,
            ),
        )
```

Call `_extract_memory_candidates()` directly after building `extraction_window`, and do not publish a `kind="memory_extraction"` task around it. Keep split task publications inside `_do_extract_memory_candidates()`.

In `event_stream()`, remove this compatibility skip:

```python
                if getattr(task, "kind", None) == "memory_extraction":
                    continue
```

- [ ] **Step 6: Run focused tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py tests/test_memory_integration.py -k "memory_extraction or extraction_gate or profile_memory_extraction or working_memory_extraction" -v
```

Expected: selected tests pass and no aggregate `memory_extraction` task is expected.

- [ ] **Step 7: Commit Task 5**

Run:

```bash
git add backend/memory/extraction.py backend/main.py backend/tests/test_memory_extraction.py backend/tests/test_memory_integration.py
git commit -m "feat: remove legacy-compatible memory extraction"
```

---

### Task 6: Replace Legacy Memory APIs with v3-Only APIs

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/tests/test_memory_v3_api.py`

- [ ] **Step 1: Write failing API tests**

In `backend/tests/test_memory_v3_api.py`, replace `_seed_legacy_episode()` with `_seed_archived_episode()` using `FileMemoryV3Store.append_episode()`.

Add:

```python
async def _seed_archived_episode(data_dir: Path, user_id: str):
    store = FileMemoryV3Store(data_dir)
    episode = ArchivedTripEpisode(
        id="ep_trip_legacy_free",
        user_id=user_id,
        session_id="session-1",
        trip_id="trip-1",
        destination="京都",
        dates={"start": "2026-03-01", "end": "2026-03-05", "total_days": 5},
        travelers={"adults": 2},
        budget={"total": 8000, "currency": "CNY"},
        selected_skeleton={"id": "sk-1", "title": "京都慢游"},
        selected_transport=None,
        accommodation=None,
        daily_plan_summary=[],
        final_plan_summary="一次轻松的京都旅行。",
        decision_log=[],
        lesson_log=["町屋住宿体验很好"],
        created_at="2026-03-06T00:00:00",
        completed_at="2026-03-06T00:00:00",
    )
    await store.append_episode(episode)
    return episode
```

Add tests:

```python
@pytest.mark.asyncio
async def test_v3_episodes_route_returns_archived_episodes_without_deprecated(v3_app):
    app, data_dir = v3_app
    user_id = "default_user"
    await _seed_archived_episode(data_dir, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/memory/{user_id}/episodes")

    assert resp.status_code == 200
    payload = resp.json()
    assert "deprecated" not in payload
    assert payload["episodes"][0]["id"] == "ep_trip_legacy_free"


@pytest.mark.asyncio
async def test_legacy_memory_routes_are_removed(v3_app):
    app, _data_dir = v3_app
    user_id = "default_user"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        get_resp = await client.get(f"/api/memory/{user_id}")
        confirm_resp = await client.post(
            f"/api/memory/{user_id}/confirm",
            json={"item_id": "constraints:flight:avoid_red_eye"},
        )
        reject_resp = await client.post(
            f"/api/memory/{user_id}/reject",
            json={"item_id": "constraints:flight:avoid_red_eye"},
        )
        delete_resp = await client.delete(f"/api/memory/{user_id}/constraints:flight:avoid_red_eye")
        events_resp = await client.post(
            f"/api/memory/{user_id}/events",
            json={
                "event_type": "reject",
                "object_type": "phase_output",
                "object_payload": {},
            },
        )

    assert get_resp.status_code == 404
    assert confirm_resp.status_code == 404
    assert reject_resp.status_code == 404
    assert delete_resp.status_code == 404
    assert events_resp.status_code == 404


@pytest.mark.asyncio
async def test_profile_item_actions_use_profile_specific_routes(v3_app):
    app, data_dir = v3_app
    user_id = "default_user"
    await _seed_profile(data_dir, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        reject_resp = await client.post(
            f"/api/memory/{user_id}/profile/constraints:flight:avoid_red_eye/reject"
        )
        confirm_resp = await client.post(
            f"/api/memory/{user_id}/profile/constraints:flight:avoid_red_eye/confirm"
        )
        delete_resp = await client.delete(
            f"/api/memory/{user_id}/profile/constraints:flight:avoid_red_eye"
        )
        profile_resp = await client.get(f"/api/memory/{user_id}/profile")

    assert reject_resp.status_code == 200
    assert confirm_resp.status_code == 200
    assert delete_resp.status_code == 200
    assert profile_resp.json()["constraints"] == []
```

- [ ] **Step 2: Run focused tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_memory_v3_api.py -k "episodes_route or legacy_memory_routes_are_removed or profile_item_actions" -v
```

Expected: failures because legacy routes still exist and profile-specific mutation routes do not.

- [ ] **Step 3: Delete legacy request models and routes**

In `backend/main.py`, delete:

```python
class MemoryItemRequest(BaseModel):
    item_id: str


class MemoryEventRequest(BaseModel):
    event_type: str
    object_type: str
    object_payload: dict[str, Any]
    reason_text: str | None = None
```

Delete routes:

```python
@app.get("/api/memory/{user_id}")
@app.post("/api/memory/{user_id}/confirm")
@app.post("/api/memory/{user_id}/reject")
@app.post("/api/memory/{user_id}/events")
@app.delete("/api/memory/{user_id}/{item_id}")
```

Delete `_set_memory_item_status()`.

- [ ] **Step 4: Add v3 profile mutation routes**

In `backend/main.py`, keep `_set_v3_profile_item_status()` and add:

```python
    @app.post("/api/memory/{user_id}/profile/{item_id}/confirm")
    async def confirm_profile_item(user_id: str, item_id: str):
        await _ensure_storage_ready()
        if not await _set_v3_profile_item_status(user_id, item_id, "active"):
            raise HTTPException(status_code=404, detail="Profile item not found")
        return {"item_id": item_id, "status": "active"}

    @app.post("/api/memory/{user_id}/profile/{item_id}/reject")
    async def reject_profile_item(user_id: str, item_id: str):
        await _ensure_storage_ready()
        if not await _set_v3_profile_item_status(user_id, item_id, "rejected"):
            raise HTTPException(status_code=404, detail="Profile item not found")
        return {"item_id": item_id, "status": "rejected"}

    @app.delete("/api/memory/{user_id}/profile/{item_id}")
    async def delete_profile_item(user_id: str, item_id: str):
        await _ensure_storage_ready()
        if not await _set_v3_profile_item_status(user_id, item_id, "obsolete"):
            raise HTTPException(status_code=404, detail="Profile item not found")
        return {"item_id": item_id, "status": "obsolete"}
```

Change `list_memory_episodes()`:

```python
    @app.get("/api/memory/{user_id}/episodes")
    async def list_memory_episodes(user_id: str):
        await _ensure_storage_ready()
        episodes = await memory_mgr.v3_store.list_episodes(user_id)
        return {"episodes": [episode.to_dict() for episode in episodes]}
```

- [ ] **Step 5: Add startup legacy file deletion**

In `lifespan()` after `await db.initialize()`:

```python
        if config.memory.enabled:
            removed_legacy_files = await memory_mgr.v3_store.delete_legacy_memory_files()
            if removed_legacy_files:
                logger.warning(
                    "Deleted legacy memory files during v3-only startup: %s",
                    [str(path) for path in removed_legacy_files],
                )
```

- [ ] **Step 6: Run focused tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_memory_v3_api.py -v
```

Expected: all v3 API tests pass.

- [ ] **Step 7: Commit Task 6**

Run:

```bash
git add backend/main.py backend/tests/test_memory_v3_api.py
git commit -m "feat: expose v3-only memory APIs"
```

---

### Task 7: Remove Legacy Pending SSE and Frontend Compatibility

**Files:**
- Modify: `backend/main.py`
- Modify: `frontend/src/types/memory.ts`
- Modify: `frontend/src/hooks/useMemory.ts`
- Modify: `frontend/src/components/MemoryCenter.tsx`
- Modify: `frontend/src/components/SessionSidebar.tsx`

- [ ] **Step 1: Write frontend/backend compatibility checks**

Run these commands before editing:

```bash
rg -n "memory_pending|legacyMemories|pendingMemories|LegacyMemoryCard|旧版|deprecated|/api/memory/\\$\\{userId\\}\\)" backend frontend
```

Expected: matches in `backend/main.py`, `frontend/src/types/memory.ts`, `frontend/src/hooks/useMemory.ts`, and `frontend/src/components/MemoryCenter.tsx`.

- [ ] **Step 2: Remove backend pending event helpers**

In `backend/main.py`, delete:

```python
def _memory_summary(candidate: MemoryCandidate) -> str
def _memory_pending_event(candidates: list[MemoryCandidate], item_ids: list[str]) -> str
def _memory_pending_event_from_items(items: list[MemoryItem]) -> str
memory_pending_seen: dict[tuple[str, str], set[str]] = {}
```

In `event_stream()`, delete the block that calls `memory_mgr.store.list_items()` and yields `_memory_pending_event_from_items(pending_items)`.

- [ ] **Step 3: Update frontend memory types**

In `frontend/src/types/memory.ts`, delete `MemorySource` and `MemoryItem`.

Rename `TripEpisode` to `ArchivedTripEpisode`:

```typescript
export interface ArchivedTripEpisode {
  id: string;
  user_id: string;
  session_id: string;
  trip_id?: string | null;
  destination?: string | null;
  dates: Record<string, unknown>;
  travelers?: Record<string, unknown> | null;
  budget?: Record<string, unknown> | null;
  selected_skeleton?: Record<string, unknown> | null;
  selected_transport?: Record<string, unknown> | null;
  accommodation?: Record<string, unknown> | null;
  daily_plan_summary: Array<Record<string, unknown>>;
  final_plan_summary: string;
  decision_log: Array<Record<string, unknown>>;
  lesson_log: string[];
  created_at: string;
  completed_at: string;
}
```

Update `UseMemoryReturn`:

```typescript
export interface UseMemoryReturn {
  profile: UserMemoryProfile;
  profileBuckets: MemoryProfileBuckets;
  sessionWorkingMemory: SessionWorkingMemory;
  episodes: ArchivedTripEpisode[];
  slices: EpisodeSlice[];
  loading: boolean;
  error: string | null;
  actions: MemoryActions;
  pendingCount: number;
}
```

- [ ] **Step 4: Update `useMemory` to call v3 APIs only**

In `frontend/src/hooks/useMemory.ts`:

Remove `MemoryItem`, `TripEpisode`, `legacyMemories`, `pendingMemories`, `memoriesRef`, and `loadLegacyMemories`.

Use:

```typescript
import type {
  ArchivedTripEpisode,
  EpisodeSlice,
  MemoryProfileItem,
  MemoryProfileBuckets,
  SessionWorkingMemory,
  UseMemoryReturn,
  UserMemoryProfile,
} from '../types/memory';
```

Use:

```typescript
const [episodes, setEpisodes] = useState<ArchivedTripEpisode[]>([]);
```

Fetch only v3 routes:

```typescript
const [profileRes, episodesRes, slicesRes, workingMemoryRes] = await Promise.all([
  fetch(`/api/memory/${userId}/profile`),
  fetch(`/api/memory/${userId}/episodes`),
  fetch(`/api/memory/${userId}/episode-slices`),
  workingMemoryPromise,
]);
```

Use profile-specific mutation routes:

```typescript
await fetch(`/api/memory/${userId}/profile/${itemId}/confirm`, { method: 'POST' });
await fetch(`/api/memory/${userId}/profile/${itemId}/reject`, { method: 'POST' });
await fetch(`/api/memory/${userId}/profile/${itemId}`, { method: 'DELETE' });
```

Set `pendingCount` to v3 profile pending only:

```typescript
const pendingCount = pendingProfileCount;
```

Return no legacy fields.

- [ ] **Step 5: Update `MemoryCenter` to pure v3**

In `frontend/src/components/MemoryCenter.tsx`:

Remove `MemoryItem`, `TripEpisode`, and `LegacyMemoryCard`.

Import `ArchivedTripEpisode`:

```typescript
import type {
  ArchivedTripEpisode,
  EpisodeSlice,
  MemoryProfileItem,
  UseMemoryReturn,
  WorkingMemoryItem,
} from '../types/memory';
```

Update:

```typescript
function EpisodeCard({ episode, recalled }: { episode: ArchivedTripEpisode; recalled?: boolean }) {
```

Use `lesson_log` and v3 dates:

```typescript
const datesText =
  typeof episode.dates?.start === 'string' && typeof episode.dates?.end === 'string'
    ? `${episode.dates.start} - ${episode.dates.end}`
    : '';
```

Remove these sections and counts:

```typescript
legacyProfileMemories
legacyTripMemories
pendingMemories
旧版画像兼容
旧版待确认记忆
旧版旅程记忆兼容
```

Set tab counts:

```typescript
const tabs: Array<{ key: TabKey; label: string; count: number; pending?: number }> = [
  {
    key: 'profile',
    label: '长期画像',
    count:
      profileBuckets.constraints.length +
      profileBuckets.rejections.length +
      profileBuckets.stable_preferences.length,
    pending: profilePendingCount,
  },
  {
    key: 'hypotheses',
    label: '待确认画像',
    count: profileBuckets.preference_hypotheses.length,
    pending: hypothesisPendingCount,
  },
  { key: 'episodes', label: '历史旅行', count: episodes.length },
  { key: 'slices', label: '历史切片', count: slices.length },
];
```

- [ ] **Step 6: Keep sidebar badge backed by v3 pending count**

In `frontend/src/components/SessionSidebar.tsx`, keep `memory.pendingCount`. No API change is needed after `useMemory` returns v3-only pending count.

- [ ] **Step 7: Verify compatibility strings are gone**

Run:

```bash
rg -n "memory_pending|legacyMemories|pendingMemories|LegacyMemoryCard|旧版|deprecated|/api/memory/\\$\\{userId\\}\\)" backend frontend
```

Expected: no matches for runtime code. Matches in historical docs are acceptable only if the command is narrowed to `backend frontend`.

- [ ] **Step 8: Build frontend**

Run:

```bash
cd frontend && npm run build
```

Expected: TypeScript and Vite build succeed.

- [ ] **Step 9: Commit Task 7**

Run:

```bash
git add backend/main.py frontend/src/types/memory.ts frontend/src/hooks/useMemory.ts frontend/src/components/MemoryCenter.tsx frontend/src/components/SessionSidebar.tsx
git commit -m "feat: remove legacy memory frontend compatibility"
```

---

### Task 8: Delete Legacy Store, Migration, Retriever, and Tests

**Files:**
- Delete: `backend/memory/store.py`
- Delete: `backend/memory/retriever.py`
- Delete: `scripts/migrate_memory_v2_to_v3.py`
- Modify: `backend/memory/manager.py`
- Modify: `backend/memory/policy.py`
- Modify: `backend/memory/extraction.py`
- Modify: `backend/memory/demo_seed.py`
- Delete: `backend/tests/test_memory_store.py`
- Delete: `backend/tests/test_memory_retriever.py`
- Delete: `backend/tests/test_memory_v3_migration.py`
- Modify: `backend/tests/test_memory_policy.py`
- Modify: `backend/tests/test_memory_models.py`
- Modify: `backend/tests/test_memory_extraction.py`
- Modify: `backend/tests/test_demo_seed.py`

- [ ] **Step 1: Find remaining runtime legacy imports**

Run:

```bash
rg -n "FileMemoryStore|MemoryRetriever|memory\\.store|from memory\\.models import .*MemoryItem|TripEpisode|MemoryCandidate|UserMemory|scripts/migrate_memory_v2_to_v3" backend scripts -g '*.py'
```

Expected: matches remain before this task.

- [ ] **Step 2: Simplify `MemoryManager`**

In `backend/memory/manager.py`, delete:

```python
from memory.models import MemoryItem, Rejection, UserMemory
from memory.store import FileMemoryStore
```

Delete `self.store = FileMemoryStore(data_dir)`.

Delete methods:

```python
save()
load()
_legacy_memory_from_items()
generate_summary()
```

The constructor should become:

```python
class MemoryManager:
    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)
        self.v3_store = FileMemoryV3Store(data_dir)
```

- [ ] **Step 3: Remove legacy policy methods**

In `backend/memory/policy.py`, remove imports of `MemoryCandidate`, `MemoryItem`, `MemorySource`, and `generate_memory_id`.

Delete methods:

```python
classify()
candidate_to_item()
_candidate_contains_forbidden_pii()
merge_items()
_merge_matched_items()
```

Keep these v3 methods:

```python
classify_v3_profile_item()
sanitize_v3_profile_item()
sanitize_working_memory_item()
```

Run the focused policy tests after editing:

```bash
cd backend && pytest tests/test_memory_policy.py -k "v3 or working_memory or profile" -v
```

Expected: v3 policy tests pass.

- [ ] **Step 4: Remove legacy extraction helpers**

In `backend/memory/extraction.py`, delete:

```python
build_extraction_prompt()
build_candidate_extraction_prompt()
parse_extraction_response()
parse_candidate_extraction_response()
class MemoryMerger
```

Remove imports of `MemoryCandidate`, `MemoryItem`, `Rejection`, and `UserMemory`.

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py -k "v3 or profile or working_memory or extraction_gate" -v
```

Expected: v3 extraction tests pass.

- [ ] **Step 5: Rewrite demo seed for v3-only memory**

In `backend/memory/demo_seed.py`, remove `FileMemoryStore`, `MemoryItem`, `MemorySource`, and `TripEpisode`. Seed through `FileMemoryV3Store`:

```python
store = FileMemoryV3Store(data_dir)
await store.upsert_profile_item(user_id, "stable_preferences", profile_item)
await store.append_episode(archived_episode)
for slice_ in build_episode_slices(archived_episode, now=now):
    await store.append_episode_slice(slice_)
```

Update `backend/tests/test_demo_seed.py` to assert:

```python
profile = await store.load_profile("default_user")
episodes = await store.list_episodes("default_user")
slices = await store.list_episode_slices("default_user")

assert profile.stable_preferences or profile.constraints or profile.rejections
assert episodes
assert slices
```

- [ ] **Step 6: Delete legacy files and tests**

Run:

```bash
git rm backend/memory/store.py backend/memory/retriever.py scripts/migrate_memory_v2_to_v3.py
git rm backend/tests/test_memory_store.py backend/tests/test_memory_retriever.py backend/tests/test_memory_v3_migration.py
```

- [ ] **Step 7: Remove or rewrite legacy-only test sections**

In these files, delete tests that instantiate `MemoryItem`, `TripEpisode`, `UserMemory`, or `MemoryCandidate` unless the file is explicitly testing legacy-free absence:

```text
backend/tests/test_memory_models.py
backend/tests/test_memory_policy.py
backend/tests/test_memory_extraction.py
backend/tests/test_memory_manager.py
backend/tests/test_memory_integration.py
```

Keep tests that cover v3 models, v3 profile policy, v3 working memory, v3 recall, v3 APIs, and v3 archival.

- [ ] **Step 8: Verify no runtime legacy imports remain**

Run:

```bash
rg -n "FileMemoryStore|MemoryRetriever|memory\\.store|from memory\\.models|TripEpisode|MemoryItem|MemoryCandidate|UserMemory|memory_events\\.jsonl|trip_episodes\\.jsonl|memory\\.json" backend scripts -g '*.py'
```

Expected: no matches in runtime files. Matches in deleted files are impossible; matches in docs are checked in Task 9.

- [ ] **Step 9: Run backend memory tests**

Run:

```bash
cd backend && pytest tests/test_memory_v3_models.py tests/test_memory_v3_store.py tests/test_episode_slices.py tests/test_memory_archival.py tests/test_recall_query.py tests/test_symbolic_recall.py tests/test_memory_manager.py tests/test_memory_extraction.py tests/test_memory_policy.py tests/test_memory_v3_api.py tests/test_memory_integration.py tests/test_demo_seed.py -v
```

Expected: all selected memory tests pass.

- [ ] **Step 10: Commit Task 8**

Run:

```bash
git add backend/memory/manager.py backend/memory/policy.py backend/memory/extraction.py backend/memory/demo_seed.py backend/tests/test_memory_models.py backend/tests/test_memory_policy.py backend/tests/test_memory_extraction.py backend/tests/test_memory_manager.py backend/tests/test_memory_integration.py backend/tests/test_demo_seed.py
git commit -m "refactor: remove legacy memory runtime"
```

---

### Task 9: Documentation, Overview, and Full Verification

**Files:**
- Modify: `PROJECT_OVERVIEW.md`
- Modify: memory-related docs if they claim current runtime supports v2 compatibility

- [ ] **Step 1: Update `PROJECT_OVERVIEW.md` memory section**

In `PROJECT_OVERVIEW.md`, replace the Memory System row text with a v3-only summary:

```markdown
| Memory System | v3-only 分层记忆；当前旅行事实由 TravelPlanState 权威提供；长期画像保存在 `data/users/{user_id}/memory/profile.json`，当前 session/trip 工作记忆保存在 `memory/sessions/{session_id}/trips/{trip_id}/working_memory.json`，完整历史旅行归档保存在 `memory/episodes.jsonl`，历史召回单元保存在 `memory/episode_slices.jsonl`，审计事件保存在 `memory/events.jsonl` 且不参与 recall；系统启动时直接删除 `memory.json`、`memory_events.jsonl`、`trip_episodes.jsonl` 旧文件，不迁移旧数据；同步 recall 只把 working memory 直接注入当前上下文，并通过 recall gate + retrieval plan 对 v3 profile / episode slices 做历史召回；前端 MemoryCenter 只读取 v3 profile / episodes / episode slices / working memory，pending badge 只统计 v3 profile pending。 | system prompt 构建前检索；每轮 chat 追加 user message 后立即后台排队 gate/job |
```

In the directory structure section, replace:

```markdown
├── memory/                 # v3 分层记忆：profile / working memory / episode slice + 兼容层：models / store / manager / extraction / policy / retriever / formatter
```

with:

```markdown
├── memory/                 # v3-only 分层记忆：profile / trip-scoped working memory / archived episodes / episode slices / audit events / recall / extraction / policy / formatter
```

- [ ] **Step 2: Add current-state warning to old design docs**

For specs and plans that describe v2 migration or compatibility as current behavior, add a one-line note near the top:

```markdown
> 当前实现已切换为 v3-only；本文保留为历史设计记录，不代表当前运行时兼容 v2。
```

Apply this note only to existing historical docs that mention v2 migration or deprecated compatibility, such as:

```text
docs/superpowers/specs/2026-04-19-memory-storage-recall-design.md
docs/superpowers/plans/2026-04-19-memory-storage-recall.md
```

- [ ] **Step 3: Run legacy reference audit**

Run:

```bash
rg -n "memory_pending|legacyMemories|pendingMemories|LegacyMemoryCard|FileMemoryStore|MemoryRetriever|recall_query_adapter|plan_to_legacy_recall_query|trip_episodes\\.jsonl|memory_events\\.jsonl|data/users/\\{user_id\\}/memory\\.json|deprecated" backend frontend scripts PROJECT_OVERVIEW.md
```

Expected: no matches. If a match appears in comments, tests, or runtime code, remove or rewrite it as v3-only current behavior.

- [ ] **Step 4: Run backend tests**

Run:

```bash
cd backend && pytest -v
```

Expected: all backend tests pass.

- [ ] **Step 5: Run frontend build**

Run:

```bash
cd frontend && npm run build
```

Expected: TypeScript and Vite build pass.

- [ ] **Step 6: Run full repository grep for forbidden runtime references**

Run:

```bash
rg -n "memory_pending|legacyMemories|pendingMemories|LegacyMemoryCard|FileMemoryStore|MemoryRetriever|recall_query_adapter|plan_to_legacy_recall_query|trip_episodes\\.jsonl|memory_events\\.jsonl|memory\\.json|deprecated" backend frontend scripts
```

Expected: no matches.

- [ ] **Step 7: Commit Task 9**

Run:

```bash
git add PROJECT_OVERVIEW.md docs/superpowers/specs/2026-04-19-memory-storage-recall-design.md docs/superpowers/plans/2026-04-19-memory-storage-recall.md
git commit -m "docs: document v3-only memory cutover"
```

---

## Final Verification

Run:

```bash
cd backend && pytest -v
cd ../frontend && npm run build
cd .. && rg -n "memory_pending|legacyMemories|pendingMemories|LegacyMemoryCard|FileMemoryStore|MemoryRetriever|recall_query_adapter|plan_to_legacy_recall_query|trip_episodes\\.jsonl|memory_events\\.jsonl|memory\\.json|deprecated" backend frontend scripts
```

Expected:

- Backend test suite passes.
- Frontend build passes.
- Final grep returns no matches in runtime code.
- `PROJECT_OVERVIEW.md` describes v3-only memory as the current architecture.

---

## Self-Review

**Spec coverage:** This plan covers v3 archived episodes, v3 episode store, trip-scoped working memory, slice generation from `ArchivedTripEpisode`, v3-native recall plans, removal of the recall adapter, v3 profile mutation APIs, deletion of legacy memory APIs, removal of legacy pending SSE, frontend v3-only reads/display, destructive old-file cleanup, test rewrites, and `PROJECT_OVERVIEW.md`.

**Known sequencing constraint:** Task 8 deletes legacy runtime files only after Tasks 1-7 remove all runtime imports and tests have been converted. Do not delete `backend/memory/store.py` earlier.

**Forbidden end-state references:** The final grep in Task 9 is part of the acceptance criteria. The cutover is incomplete if runtime code still references `memory_pending`, `FileMemoryStore`, `recall_query_adapter`, `trip_episodes.jsonl`, `memory_events.jsonl`, top-level `memory.json`, or frontend legacy memory fields.
