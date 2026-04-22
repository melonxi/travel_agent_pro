# Memory v3-Only Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把记忆系统彻底切到单一 v3 runtime：删除 v2/legacy 运行时读写、API、前端兼容层和旧数据文件，让 `profile.json`、trip-scoped `working_memory.json`、`episodes.jsonl`、`episode_slices.jsonl` 成为唯一权威记忆结构。

**Architecture:** 保留 `-A` 版的主结构：`ArchivedTripEpisode` 进入 `v3_models.py`，归档 builder 放在 `memory/archival.py`，recall 直接消费 `RecallRetrievalPlan`。增强点有三个：一是在 `TravelPlanState` 上显式记录 `decision_events / lesson_events`，避免归档阶段临时推断；二是把 legacy 文件清理定义为一次性 cutover 动作，而不是长期 startup 行为；三是把高风险删除动作拆成“先断引用、后删除文件”的顺序。

**Tech Stack:** Python 3.12, FastAPI, pytest, pytest-asyncio, React 19, TypeScript, Vite

---

## File Structure

- Modify: `backend/memory/v3_models.py`
  - Add `ArchivedTripEpisode`
  - Add `MemoryAuditEvent`
- Modify: `backend/memory/v3_store.py`
  - Add v3 `episodes.jsonl` read/write
  - Add v3 `events.jsonl` append
  - Move working memory path to `sessions/{session_id}/trips/{trip_id}/working_memory.json`
  - Add one-shot legacy file cleanup helpers
- Create: `backend/memory/archival.py`
  - Build `ArchivedTripEpisode` from `TravelPlanState`
  - Copy `decision_events / lesson_events` directly into `decision_log / lesson_log`
- Modify: `backend/memory/episode_slices.py`
  - Accept `ArchivedTripEpisode`
  - Generate only v3 taxonomy slices
- Modify: `backend/memory/recall_query.py`
  - Add `entities` to `RecallRetrievalPlan`
  - Allow `source` values `profile`, `episode_slice`, `hybrid_history`
- Modify: `backend/memory/symbolic_recall.py`
  - Remove `RecallQuery` from runtime path
  - Consume `RecallRetrievalPlan` directly
- Delete: `backend/memory/recall_query_adapter.py`
- Modify: `backend/memory/manager.py`
  - Remove `FileMemoryStore` and legacy summary/load/save behavior from runtime
  - Run recall directly from v3 stores + `RecallRetrievalPlan`
- Modify: `backend/memory/extraction.py`
  - Require route-aware v3 extraction payloads
- Modify: `backend/memory/policy.py`
  - Keep only v3 runtime policy methods
- Modify: `backend/main.py`
  - Remove legacy memory imports, pending SSE scan, legacy API routes, legacy archive path
  - Use `ArchivedTripEpisode` archival on Phase 7 completion
  - Publish only split background extraction tasks
  - Invoke one-shot legacy cleanup entrypoint during cutover/bootstrap path, not on every request
- Modify: `backend/state/models.py`
  - Add `decision_events` and `lesson_events`
- Modify: `backend/state/plan_writers.py`
  - Append explicit decision events when confirmed rejections/locks happen
  - Add `record_phase7_lesson(...)`
- Modify: `backend/memory/demo_seed.py`
  - Seed v3 profile, episodes, slices, and working memory only
- Modify frontend:
  - `frontend/src/types/memory.ts`
  - `frontend/src/hooks/useMemory.ts`
  - `frontend/src/components/MemoryCenter.tsx`
  - `frontend/src/components/SessionSidebar.tsx`
- Modify docs:
  - `PROJECT_OVERVIEW.md`

### Delete only after references are removed

- Delete: `backend/memory/store.py`
- Delete: `backend/memory/retriever.py`
- Delete: `scripts/migrate_memory_v2_to_v3.py`
- Delete tests:
  - `backend/tests/test_memory_store.py`
  - `backend/tests/test_memory_retriever.py`
  - `backend/tests/test_recall_query_adapter.py`
  - `backend/tests/test_memory_v3_migration.py`

---

### Task 1: Add explicit decision and lesson events to `TravelPlanState`

**Files:**
- Modify: `backend/state/models.py`
- Modify: `backend/state/plan_writers.py`
- Create: `backend/tests/test_plan_state_event_fields.py`
- Create: `backend/tests/test_plan_writers_decision_events.py`

- [ ] **Step 1: Write failing state-model tests**

Create `backend/tests/test_plan_state_event_fields.py`:

```python
from state.models import TravelPlanState


def test_plan_state_roundtrip_preserves_event_fields():
    plan = TravelPlanState()
    plan.decision_events.append(
        {
            "type": "rejected",
            "category": "hotel",
            "value": {"name": "商务连锁"},
            "reason": "用户更想住町屋",
            "timestamp": "2026-04-22T10:00:00Z",
        }
    )
    plan.lesson_events.append(
        {
            "kind": "pitfall",
            "content": "上午排太满下午会累",
            "timestamp": "2026-04-22T18:00:00Z",
        }
    )

    restored = TravelPlanState.from_dict(plan.to_dict())

    assert restored.decision_events == plan.decision_events
    assert restored.lesson_events == plan.lesson_events
```

- [ ] **Step 2: Write failing writer tests using real current writer functions**

Create `backend/tests/test_plan_writers_decision_events.py`:

```python
from state.models import TravelPlanState
from state.plan_writers import (
    replace_all_daily_plans,
    write_accommodation,
    write_selected_skeleton_id,
    write_selected_transport,
    record_phase7_lesson,
)


def test_write_selected_skeleton_id_appends_decision_event():
    plan = TravelPlanState()
    write_selected_skeleton_id(plan, "slow")
    assert any(ev["category"] == "skeleton" for ev in plan.decision_events)


def test_write_selected_transport_appends_decision_event():
    plan = TravelPlanState()
    write_selected_transport(plan, {"mode": "train"})
    assert any(ev["category"] == "transport" for ev in plan.decision_events)


def test_write_accommodation_appends_decision_event():
    plan = TravelPlanState()
    write_accommodation(plan, "四条", "町屋")
    assert any(ev["category"] == "accommodation" for ev in plan.decision_events)


def test_replace_all_daily_plans_appends_decision_event():
    plan = TravelPlanState()
    replace_all_daily_plans(
        plan,
        [{"day": 1, "date": "2026-05-01", "activities": [], "notes": "轻松安排"}],
    )
    assert any(ev["category"] == "daily_plan" for ev in plan.decision_events)


def test_record_phase7_lesson_appends_lesson_event():
    plan = TravelPlanState()
    record_phase7_lesson(
        plan,
        kind="pitfall",
        note="上午排太满下午会累",
        now="2026-04-22T18:00:00Z",
    )
    assert plan.lesson_events == [
        {
            "kind": "pitfall",
            "content": "上午排太满下午会累",
            "timestamp": "2026-04-22T18:00:00Z",
        }
    ]
```

- [ ] **Step 3: Run tests to verify RED**

Run:

```bash
cd backend && python -m pytest tests/test_plan_state_event_fields.py tests/test_plan_writers_decision_events.py -v
```

Expected: FAIL because the fields and helper do not exist yet.

- [ ] **Step 4: Add event fields and writer helpers**

In `backend/state/models.py`, add these fields to `TravelPlanState` and wire them through `to_dict()/from_dict()`:

```python
decision_events: list[dict[str, Any]] = field(default_factory=list)
lesson_events: list[dict[str, Any]] = field(default_factory=list)
```

In `backend/state/plan_writers.py`, add these helpers near the other write functions:

```python
def _append_decision_event(
    plan: TravelPlanState,
    *,
    category: str,
    value: Any,
    reason: str,
    now: str = "",
) -> None:
    plan.decision_events.append(
        {
            "type": "accepted",
            "category": category,
            "value": value,
            "reason": reason,
            "timestamp": now,
        }
    )


def record_phase7_lesson(
    plan: TravelPlanState,
    *,
    kind: str,
    note: str,
    now: str,
) -> None:
    plan.lesson_events.append(
        {"kind": kind, "content": note, "timestamp": now}
    )
```

Update real writer functions, not imaginary wrappers:

```python
def write_selected_skeleton_id(plan: TravelPlanState, skeleton_id: str) -> None:
    plan.selected_skeleton_id = skeleton_id
    _append_decision_event(
        plan,
        category="skeleton",
        value={"id": skeleton_id},
        reason="selected_skeleton_id updated in plan state",
    )


def write_selected_transport(plan: TravelPlanState, choice: dict) -> None:
    plan.selected_transport = choice
    _append_decision_event(
        plan,
        category="transport",
        value=dict(choice),
        reason="selected_transport updated in plan state",
    )


def write_accommodation(plan: TravelPlanState, area: str, hotel: str | None = None) -> None:
    plan.accommodation = Accommodation(area=area, hotel=hotel)
    _append_decision_event(
        plan,
        category="accommodation",
        value=plan.accommodation.to_dict(),
        reason="accommodation updated in plan state",
    )


def replace_all_daily_plans(plan: TravelPlanState, days: list[dict]) -> None:
    plan.daily_plans = [DayPlan.from_dict(day) for day in days]
    _sort_daily_plans(plan)
    _append_decision_event(
        plan,
        category="daily_plan",
        value={"days": [day.day for day in plan.daily_plans]},
        reason="daily_plans replaced in plan state",
    )
```

- [ ] **Step 5: Run tests to verify GREEN**

Run:

```bash
cd backend && python -m pytest tests/test_plan_state_event_fields.py tests/test_plan_writers_decision_events.py tests/test_plan_writers.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add backend/state/models.py backend/state/plan_writers.py \
        backend/tests/test_plan_state_event_fields.py \
        backend/tests/test_plan_writers_decision_events.py
git commit -m "feat(state): add explicit decision and lesson events"
```

---

### Task 2: Add v3 episode, audit-event, and store support

**Files:**
- Modify: `backend/memory/v3_models.py`
- Modify: `backend/memory/v3_store.py`
- Modify: `backend/tests/test_memory_v3_models.py`
- Modify: `backend/tests/test_memory_v3_store.py`

- [ ] **Step 1: Write failing v3 model tests**

Add to `backend/tests/test_memory_v3_models.py`:

```python
from memory.v3_models import ArchivedTripEpisode, MemoryAuditEvent


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
        selected_transport={"mode": "train"},
        accommodation={"area": "四条", "hotel": "町屋"},
        daily_plan_summary=[],
        final_plan_summary="京都慢游。",
        decision_log=[{"type": "accepted", "category": "skeleton", "value": {"id": "slow"}}],
        lesson_log=[{"kind": "pitfall", "content": "交通衔接要留余量。", "timestamp": "2026-05-05T00:00:00+00:00"}],
        created_at="2026-05-05T00:00:00+00:00",
        completed_at="2026-05-05T00:00:00+00:00",
    )

    assert ArchivedTripEpisode.from_dict(episode.to_dict()) == episode


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

    assert MemoryAuditEvent.from_dict(event.to_dict()) == event
```

- [ ] **Step 2: Write failing v3 store tests**

Add to `backend/tests/test_memory_v3_store.py`:

```python
@pytest.mark.asyncio
async def test_append_and_list_archived_episodes_is_idempotent(tmp_path):
    store = FileMemoryV3Store(tmp_path)
    episode = ArchivedTripEpisode(
        id="ep_trip_123",
        user_id="u1",
        session_id="s1",
        trip_id="trip_123",
        destination="京都",
        dates={"start": "2026-05-01", "end": "2026-05-05", "total_days": 5},
        travelers={"adults": 2, "children": 0},
        budget={"total": 20000, "currency": "CNY"},
        selected_skeleton=None,
        selected_transport=None,
        accommodation=None,
        daily_plan_summary=[],
        final_plan_summary="京都慢游。",
        decision_log=[],
        lesson_log=[],
        created_at="2026-05-05T00:00:00+00:00",
        completed_at="2026-05-05T00:00:00+00:00",
    )

    await store.append_episode(episode)
    await store.append_episode(episode)

    episodes = await store.list_episodes("u1")
    assert [item.id for item in episodes] == ["ep_trip_123"]


@pytest.mark.asyncio
async def test_delete_all_legacy_memory_files_removes_v2_files(tmp_path):
    user_dir = tmp_path / "users" / "u1"
    user_dir.mkdir(parents=True)
    for filename in ("memory.json", "memory_events.jsonl", "trip_episodes.jsonl"):
        (user_dir / filename).write_text("legacy", encoding="utf-8")
    store = FileMemoryV3Store(tmp_path)

    removed = await store.delete_all_legacy_memory_files()

    assert sorted(path.name for path in removed) == [
        "memory.json",
        "memory_events.jsonl",
        "trip_episodes.jsonl",
    ]
```

- [ ] **Step 3: Run focused tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_memory_v3_models.py tests/test_memory_v3_store.py -k "archived_trip_episode or memory_audit_event or archived_episodes or delete_all_legacy" -v
```

Expected: FAIL due to missing classes/methods.

- [ ] **Step 4: Implement v3 models and store methods**

In `backend/memory/v3_models.py`, add `ArchivedTripEpisode` and `MemoryAuditEvent` after `SessionWorkingMemory`.

In `backend/memory/v3_store.py`, add:

```python
def _working_memory_path(self, user_id: str, session_id: str, trip_id: str | None) -> Path:
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


async def delete_all_legacy_memory_files(self) -> list[Path]:
    return await asyncio.to_thread(self._delete_all_legacy_memory_files_sync)
```

Also add `append_episode()`, `list_episodes()`, `append_event()`, and update all `_working_memory_path()` callers to pass `trip_id`.

- [ ] **Step 5: Run focused tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_memory_v3_models.py tests/test_memory_v3_store.py -k "archived_trip_episode or memory_audit_event or archived_episodes or delete_all_legacy" -v
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add backend/memory/v3_models.py backend/memory/v3_store.py \
        backend/tests/test_memory_v3_models.py backend/tests/test_memory_v3_store.py
git commit -m "feat: add v3 episodes, audit events, and store cleanup helpers"
```

---

### Task 3: Build archived trips only from v3-native state and explicit events

**Files:**
- Create: `backend/memory/archival.py`
- Modify: `backend/tests/test_memory_archival.py`

- [ ] **Step 1: Write failing archival builder tests**

Create `backend/tests/test_memory_archival.py`:

```python
from memory.archival import build_archived_trip_episode
from state.models import Accommodation, Budget, DateRange, Travelers, TravelPlanState


def _plan() -> TravelPlanState:
    plan = TravelPlanState(
        session_id="s1",
        trip_id="trip_123",
        phase=7,
        destination="京都",
        dates=DateRange(start="2026-05-01", end="2026-05-05"),
        travelers=Travelers(adults=2, children=0),
        budget=Budget(total=20000, currency="CNY"),
        skeleton_plans=[{"id": "slow", "name": "慢游", "summary": "东山、四条、岚山慢节奏。"}],
        selected_skeleton_id="slow",
        selected_transport={"mode": "train", "arrival_station": "京都站"},
        accommodation=Accommodation(area="四条", hotel="町屋"),
    )
    plan.decision_events = [
        {"type": "accepted", "category": "skeleton", "value": {"id": "slow"}, "reason": "selected", "timestamp": "2026-05-05T00:00:00+00:00"},
        {"type": "rejected", "category": "hotel", "value": {"name": "商务连锁酒店"}, "reason": "用户更想住町屋", "timestamp": "2026-05-05T00:00:00+00:00"},
    ]
    plan.lesson_events = [
        {"kind": "pitfall", "content": "岚山返程要避开晚高峰。", "timestamp": "2026-05-05T00:00:00+00:00"}
    ]
    return plan


def test_build_archived_trip_episode_copies_explicit_events():
    episode = build_archived_trip_episode(
        user_id="default_user",
        session_id="s1",
        plan=_plan(),
        now="2026-05-05T00:00:00+00:00",
    )

    assert any(item["type"] == "accepted" and item["category"] == "skeleton" for item in episode.decision_log)
    assert any(item["type"] == "rejected" and item["category"] == "hotel" for item in episode.decision_log)
    assert episode.lesson_log == _plan().lesson_events
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_memory_archival.py -v
```

Expected: FAIL because `memory.archival` does not exist.

- [ ] **Step 3: Implement archival builder as pure copy from state**

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
        decision_log=[dict(item) for item in plan.decision_events],
        lesson_log=[dict(item) for item in plan.lesson_events],
        created_at=now,
        completed_at=now,
    )
```

Do not derive `decision_log` from `alternatives`, `risks`, or heuristics.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_memory_archival.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add backend/memory/archival.py backend/tests/test_memory_archival.py
git commit -m "feat: build archived trips from v3-native state events"
```

---

### Task 4: Generate episode slices only from `ArchivedTripEpisode`

**Files:**
- Modify: `backend/memory/episode_slices.py`
- Modify: `backend/tests/test_episode_slices.py`

- [ ] **Step 1: Replace legacy slice tests with v3 taxonomy tests**

Replace `backend/tests/test_episode_slices.py` with tests that assert:

```python
assert "itinerary_pattern" in slice_types
assert "stay_choice" in slice_types
assert "transport_choice" in slice_types
assert "budget_signal" in slice_types
assert "rejected_option" in slice_types
assert "pitfall" in slice_types
assert "accepted_pattern" not in slice_types
```

And explicitly verify that `rejected_option` only comes from `decision_log` entries whose `type == "rejected"`, and `pitfall` only comes from `lesson_log`.

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_episode_slices.py -v
```

Expected: FAIL because the current builder still imports legacy `TripEpisode` and emits legacy taxonomy.

- [ ] **Step 3: Replace slice taxonomy and builder input**

In `backend/memory/episode_slices.py`, change:

```python
from memory.v3_models import ArchivedTripEpisode, EpisodeSlice


def build_episode_slices(episode: ArchivedTripEpisode, *, now: str) -> list[EpisodeSlice]:
```

Generate slices from:

1. `selected_skeleton + daily_plan_summary` -> `itinerary_pattern`
2. `accommodation` -> `stay_choice`
3. `selected_transport` -> `transport_choice`
4. `budget + final_plan_summary` -> `budget_signal`
5. `decision_log` rejected entries -> `rejected_option`
6. `lesson_log` entries -> `pitfall`

Remove all references to legacy `accepted_items`, `rejected_items`, and `MemoryItem.attributes.reason`.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_episode_slices.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```bash
git add backend/memory/episode_slices.py backend/tests/test_episode_slices.py
git commit -m "feat: build episode slices from archived trip episodes only"
```

---

### Task 5: Replace Phase 7 archive path and move audit events to v3 store

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/tests/test_memory_integration.py`

- [ ] **Step 1: Write failing integration tests for v3 archival**

Add tests asserting:

```python
episodes = await memory_mgr.v3_store.list_episodes("u1")
slices = await memory_mgr.v3_store.list_episode_slices("u1")

assert len(episodes) == 1
assert episodes[0].decision_log
assert all(slice_.source_episode_id == episodes[0].id for slice_ in slices)
```

And assert that `trip_episodes.jsonl` is not created anymore.

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_memory_integration.py -k "episode or archive" -v
```

Expected: FAIL because the current code still writes legacy episodes.

- [ ] **Step 3: Replace the archive path in `main.py`**

In `backend/main.py`:

1. Remove imports from `memory.models` that are only used for legacy runtime
2. Replace legacy memory event scheduling to use `MemoryAuditEvent` + `memory_mgr.v3_store.append_event()`
3. Delete `_build_trip_episode()` and `_append_trip_episode_once()`
4. Add:

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
```

5. Change the Phase 7 completion call site to `_append_archived_trip_episode_once`
6. Remove legacy trip-reset cleanup that walks `memory_mgr.store.list_items()`

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_memory_archival.py tests/test_memory_integration.py -k "episode or archive" -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 5**

```bash
git add backend/main.py backend/tests/test_memory_integration.py
git commit -m "feat: archive completed trips into v3 episodes and audit events"
```

---

### Task 6: Make recall consume `RecallRetrievalPlan` directly and delete adapter layer

**Files:**
- Modify: `backend/memory/recall_query.py`
- Modify: `backend/memory/symbolic_recall.py`
- Modify: `backend/memory/manager.py`
- Modify: `backend/tests/test_recall_query.py`
- Modify: `backend/tests/test_symbolic_recall.py`
- Modify: `backend/tests/test_memory_manager.py`
- Delete: `backend/memory/recall_query_adapter.py`
- Delete: `backend/tests/test_recall_query_adapter.py`

- [ ] **Step 1: Write failing parser tests for v3 history sources**

Add tests asserting:

```python
assert plan.source in {"profile", "episode_slice", "hybrid_history"}
assert plan.entities == {"destination": "京都"}
```

And reject `working_memory`, `legacy`, and `profile_fixed` by falling back to `hybrid_history`.

- [ ] **Step 2: Write failing symbolic recall tests that use `RecallRetrievalPlan` directly**

Add tests that call `rank_profile_items(plan, profile)` and `rank_episode_slices(plan, slices)` with `plan.entities` populated, rather than constructing `RecallQuery`.

- [ ] **Step 3: Run tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_recall_query.py tests/test_symbolic_recall.py tests/test_memory_manager.py -v
```

Expected: FAIL because runtime still expects `RecallQuery` and adapter wiring.

- [ ] **Step 4: Introduce `entities` on `RecallRetrievalPlan`, update ranking signatures, then delete adapter**

Sequence:

1. Extend `RecallRetrievalPlan` with `entities: dict[str, str]`
2. Update parser and fallback plan
3. Update `symbolic_recall.py` ranking functions to accept `RecallRetrievalPlan`
4. Update `memory.manager.generate_context()` to call v3 ranking directly
5. Delete `recall_query_adapter.py`

Do not delete `RecallQuery` until all tests are green against the new signatures.

- [ ] **Step 5: Run tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_recall_query.py tests/test_symbolic_recall.py tests/test_memory_manager.py tests/test_memory_integration.py -k "recall" -v
```

Expected: PASS.

- [ ] **Step 6: Commit Task 6**

```bash
git add backend/memory/recall_query.py backend/memory/symbolic_recall.py \
        backend/memory/manager.py backend/tests/test_recall_query.py \
        backend/tests/test_symbolic_recall.py backend/tests/test_memory_manager.py
git rm backend/memory/recall_query_adapter.py backend/tests/test_recall_query_adapter.py
git commit -m "feat: make recall pipeline native to v3 retrieval plans"
```

---

### Task 7: Remove legacy extraction compatibility and legacy API surface

**Files:**
- Modify: `backend/memory/extraction.py`
- Modify: `backend/memory/policy.py`
- Modify: `backend/main.py`
- Modify: `backend/tests/test_memory_extraction.py`
- Modify: `backend/tests/test_memory_v3_api.py`

- [ ] **Step 1: Write failing tests for v3-only extraction gate and API surface**

Add tests that assert:

1. Extraction gate parser rejects payloads without `routes`
2. No runtime code path constructs legacy `MemoryItem`
3. `/api/memory/{user_id}` is gone
4. `/api/memory/{user_id}/episodes` returns v3 episodes without `deprecated`
5. New profile-only mutation routes work

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py tests/test_memory_v3_api.py -v
```

Expected: FAIL.

- [ ] **Step 3: Make extraction/API runtime v3-only**

In `backend/memory/extraction.py`:

```python
if not isinstance(payload, dict) or not isinstance(payload.get("routes"), dict):
    return V3ExtractionGateDecision(
        should_extract=False,
        routes={"profile": False, "working_memory": False},
        reason="invalid_payload",
        message="invalid extraction gate payload",
        fallback_used="invalid_payload",
    )
```

In `backend/memory/policy.py`, remove runtime-only methods that produce legacy `MemoryItem` or merge legacy items.

In `backend/main.py`:

1. Remove `GET /api/memory/{user_id}`
2. Remove compatibility confirm/reject/delete routes
3. Add profile-only routes:
   - `POST /api/memory/{user_id}/profile/{item_id}/confirm`
   - `POST /api/memory/{user_id}/profile/{item_id}/reject`
   - `DELETE /api/memory/{user_id}/profile/{item_id}`
4. Ensure `/api/memory/{user_id}/episodes` reads `memory_mgr.v3_store.list_episodes()`

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py tests/test_memory_v3_api.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 7**

```bash
git add backend/memory/extraction.py backend/memory/policy.py backend/main.py \
        backend/tests/test_memory_extraction.py backend/tests/test_memory_v3_api.py
git commit -m "feat: remove legacy extraction compatibility and memory routes"
```

---

### Task 8: Remove legacy pending SSE and frontend compatibility UI

**Files:**
- Modify: `backend/main.py`
- Modify: `frontend/src/types/memory.ts`
- Modify: `frontend/src/hooks/useMemory.ts`
- Modify: `frontend/src/components/MemoryCenter.tsx`
- Modify: `frontend/src/components/SessionSidebar.tsx`

- [ ] **Step 1: Write failing tests or assertions for v3-only UI state**

Define the target shape in `frontend/src/types/memory.ts`:

```ts
export type UseMemoryReturn = {
  profile: UserMemoryProfile | null
  episodes: ArchivedTripEpisode[]
  episodeSlices: EpisodeSlice[]
  workingMemory: SessionWorkingMemory | null
  pendingCount: number
}
```

And remove `legacyMemories` / `pendingMemories` from the type.

- [ ] **Step 2: Run existing frontend checks to verify RED**

Run:

```bash
npm run test
```

Expected: FAIL or type/test errors because frontend still references legacy compat fields.

- [ ] **Step 3: Switch frontend to v3-only memory data**

In `frontend/src/hooks/useMemory.ts`, only fetch:

```ts
const [profileResp, episodesResp, slicesResp, workingResp] = await Promise.all([
  fetch(`/api/memory/${userId}/profile`),
  fetch(`/api/memory/${userId}/episodes`),
  fetch(`/api/memory/${userId}/episode-slices`),
  fetch(`/api/memory/${userId}/sessions/${sessionId}/working-memory`),
])
```

Remove:

1. `loadLegacyMemories()`
2. `legacyMemories`
3. `pendingMemories`
4. any `deprecated` handling

In `MemoryCenter.tsx`, remove:

1. `LegacyMemoryCard`
2. old profile compat sections
3. old pending compat sections
4. old trip-memory compat sections

In `SessionSidebar.tsx`, compute pending badge only from v3 profile pending items.

In `backend/main.py`, remove the `memory_pending` SSE helpers and the pre-chat legacy pending scan.

- [ ] **Step 4: Run frontend checks to verify GREEN**

Run:

```bash
npm run test
```

Expected: PASS.

- [ ] **Step 5: Commit Task 8**

```bash
git add backend/main.py frontend/src/types/memory.ts frontend/src/hooks/useMemory.ts \
        frontend/src/components/MemoryCenter.tsx frontend/src/components/SessionSidebar.tsx
git commit -m "feat: remove legacy memory UI and pending compatibility"
```

---

### Task 9: Execute one-shot cutover cleanup and then delete legacy files/modules

**Files:**
- Modify: `backend/main.py`
- Delete: `backend/memory/store.py`
- Delete: `backend/memory/retriever.py`
- Delete: `scripts/migrate_memory_v2_to_v3.py`
- Delete tests:
  - `backend/tests/test_memory_store.py`
  - `backend/tests/test_memory_retriever.py`
  - `backend/tests/test_memory_v3_migration.py`

- [ ] **Step 1: Write failing cleanup integration test**

Add a backend integration test that:

1. Seeds legacy files under `data/users/<user>/`
2. Calls the explicit cutover cleanup entrypoint
3. Asserts the files are deleted
4. Asserts v3 files remain

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_memory_integration.py -k "cutover_cleanup" -v
```

Expected: FAIL because the explicit cutover cleanup entrypoint does not exist yet.

- [ ] **Step 3: Add an explicit one-shot cutover cleanup entrypoint**

In `backend/main.py`, add:

```python
async def _run_v3_memory_cutover_cleanup_once() -> None:
    if getattr(app.state, "_v3_memory_cutover_cleanup_done", False):
        return
    await memory_mgr.v3_store.delete_all_legacy_memory_files()
    app.state._v3_memory_cutover_cleanup_done = True
```

Call it once from app startup/bootstrap, not on every request path.

Before deleting files, run:

```bash
rg "memory\.store|memory\.retriever|FileMemoryStore|MemoryItem|TripEpisode" backend
```

Only after runtime references are gone:

```bash
git rm backend/memory/store.py backend/memory/retriever.py scripts/migrate_memory_v2_to_v3.py
git rm backend/tests/test_memory_store.py backend/tests/test_memory_retriever.py backend/tests/test_memory_v3_migration.py
```

- [ ] **Step 4: Run full backend memory test suite to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_memory_v3_models.py tests/test_memory_v3_store.py tests/test_memory_archival.py tests/test_episode_slices.py tests/test_recall_query.py tests/test_symbolic_recall.py tests/test_memory_manager.py tests/test_memory_extraction.py tests/test_memory_integration.py tests/test_memory_v3_api.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 9**

```bash
git add backend/main.py
git commit -m "refactor: remove legacy memory runtime and cut over to v3 only"
```

---

### Task 10: Update docs and remove mixed-state descriptions

**Files:**
- Modify: `PROJECT_OVERVIEW.md`
- Modify: relevant memory docs under `docs/`

- [ ] **Step 1: Update `PROJECT_OVERVIEW.md` to v3-only current state**

Change the memory storage section to:

```text
memory/
  profile.json
  events.jsonl
  episodes.jsonl
  episode_slices.jsonl
  sessions/{session_id}/trips/{trip_id}/working_memory.json
```

Update API docs so they no longer mention deprecated legacy memory routes.

- [ ] **Step 2: Update learning/docs that currently describe mixed-state runtime as current**

For example, any document that still says `memory.json` or `trip_episodes.jsonl` are active runtime files should be rewritten as historical context, not current behavior.

- [ ] **Step 3: Run a doc scan**

Run:

```bash
rg "memory.json|trip_episodes.jsonl|deprecated|legacy" PROJECT_OVERVIEW.md docs
```

Expected: no current-state doc should present legacy runtime as active behavior.

- [ ] **Step 4: Commit Task 10**

```bash
git add PROJECT_OVERVIEW.md docs
git commit -m "docs: describe memory system as v3 only"
```

---

## Self-Review

### Spec coverage

This plan covers:

1. v3-only episodes as the authoritative historical trip store
2. episode slices generated only from archived episodes
3. working memory kept out of historical recall
4. deletion of legacy runtime APIs, frontend compatibility, and old files
5. explicit `decision_events / lesson_events` so archival does not guess

### Placeholder scan

No `TODO`, `TBD`, or “similar to Task N” placeholders remain. High-risk areas include explicit code or command snippets.

### Type consistency

The plan consistently uses:

1. `ArchivedTripEpisode`
2. `MemoryAuditEvent`
3. `RecallRetrievalPlan` with `entities`
4. `decision_events / lesson_events` on `TravelPlanState`
5. `delete_all_legacy_memory_files()` as a one-shot cutover helper

---

Plan complete and saved to `docs/superpowers/plans/2026-04-22-v3-only-memory-cutover-A-plus.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
