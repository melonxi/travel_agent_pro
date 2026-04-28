# Context History Segmentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `context_epoch`-based history segmentation so backend debugging can distinguish repeated phase/substep visits without ever replaying old segment bodies into the LLM prompt.

**Architecture:** Phase 1 is assumed to have made `messages` append-only with `history_seq`, `phase`, `phase3_step`, `run_id`, and `trip_id`; Phase 2 is assumed to have added `runtime_view.py` and restore-time short runtime prompts. Phase 3 adds two nullable columns to `messages`, carries `current_context_epoch` in the session dict, increments it only at runtime-context rebuild boundaries, and derives `ContextSegment` objects from existing message rows without adding a `phase_segments` table. Debug access stays service/helper-level through `SessionPersistence` and `MessageStore`; no HTTP route is added.

**Tech Stack:** Python 3.11+, SQLite via `aiosqlite`, `pytest` / `pytest-asyncio`, project modules under `backend/storage`, `backend/api/orchestration/session`, and `backend/agent`.

---

## Scope And Preconditions

This plan implements only Phase 3 from `docs/superpowers/specs/2026-04-28-context-history-segmentation-design.md`.

Assume Phase 1 and Phase 2 have already landed with these contracts:

- `backend/storage/database.py` already has `messages.phase`, `messages.phase3_step`, `messages.history_seq`, `messages.run_id`, and `messages.trip_id`.
- `backend/storage/message_store.py::MessageStore.append()` and `append_batch()` already accept those Phase 1 metadata fields.
- `backend/api/orchestration/session/persistence.py::SessionPersistence.persist_messages()` appends rows and returns the next `history_seq`.
- `backend/api/orchestration/session/runtime_view.py::build_runtime_view_for_restore()` exists and returns a short runtime prompt, not complete history.
- `backend/agent/loop.py::AgentLoop` already has a pre-rebuild flush callback from Phase 1.

Do not implement a `phase_segments` table. Do not add or wire an HTTP debug route. Do not change `run_id` semantics; `run_id` identifies an SSE run, while `context_epoch` identifies a runtime-context boundary and may change within a run.

## File Structure

| Path | Responsibility |
|---|---|
| `backend/storage/database.py` | Add `messages.context_epoch`, `messages.rebuild_reason`, migration, and indexes. |
| `backend/storage/message_store.py` | Persist epoch metadata and provide ordered debug row queries. |
| `backend/api/orchestration/session/context_segments.py` | New pure helper module containing `ContextSegment` and `derive_context_segments()`. |
| `backend/api/orchestration/session/persistence.py` | Initialize `current_context_epoch`, pass epoch metadata into writes, and expose service-level debug helpers. |
| `backend/api/orchestration/session/runtime_view.py` | Keep restore prompt isolated; optionally use epoch metadata only to choose anchors. |
| `backend/agent/loop.py` | Increment `current_context_epoch` through one callback before phase forward, Phase 3 step change, and backtrack rebuilds. |
| `backend/api/orchestration/agent/builder.py` | Thread the epoch-advance callback into `AgentLoop` if Phase 1 builder wiring owns callbacks there. |
| `backend/api/orchestration/chat/stream.py` | Own session callback implementation if Phase 1 stores session runtime state in chat orchestration. |
| `backend/tests/test_storage_database.py` | Migration and new-schema assertions. |
| `backend/tests/test_storage_message.py` | MessageStore epoch write/query assertions. |
| `backend/tests/test_context_segments.py` | Segment derivation unit tests. |
| `backend/tests/test_session_persistence.py` | Restore initialization and helper-level debug query tests. |
| `backend/tests/test_agent_context_epoch.py` | Agent rebuild boundary epoch increment tests. |
| `backend/tests/test_runtime_view.py` | Prompt red-line tests proving old epoch tool bodies are not included. |
| `PROJECT_OVERVIEW.md` | Update in each implementation commit per `AGENTS.md`; keep edits limited to the storage/session/history summary. |

## Task 1: Store Epoch Metadata On Messages

**Files:**
- Modify: `backend/storage/database.py`
- Modify: `backend/storage/message_store.py`
- Test: `backend/tests/test_storage_database.py`
- Test: `backend/tests/test_storage_message.py`
- Modify before commit: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: Write failing database migration tests**

Append these tests to `backend/tests/test_storage_database.py`:

```python
from __future__ import annotations

import aiosqlite
import pytest

from storage.database import Database


@pytest.mark.asyncio
async def test_messages_schema_has_context_epoch_columns():
    db = Database(":memory:")
    await db.initialize()
    try:
        columns = {row["name"] for row in await db.fetch_all("PRAGMA table_info(messages)")}
        indexes = {row["name"] for row in await db.fetch_all("PRAGMA index_list(messages)")}
    finally:
        await db.close()

    assert "context_epoch" in columns
    assert "rebuild_reason" in columns
    assert "idx_messages_epoch" in indexes
    assert "idx_messages_trip_epoch" in indexes


@pytest.mark.asyncio
async def test_migrate_legacy_messages_table_adds_context_epoch_columns(tmp_path):
    db_path = tmp_path / "legacy-context-epoch.db"
    async with aiosqlite.connect(db_path) as raw:
        await raw.executescript(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT 'default_user',
                title TEXT,
                phase INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_run_id TEXT,
                last_run_status TEXT,
                last_run_error TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                provider_state TEXT,
                phase INTEGER,
                phase3_step TEXT,
                history_seq INTEGER,
                run_id TEXT,
                trip_id TEXT,
                created_at TEXT NOT NULL,
                seq INTEGER NOT NULL
            );
            """
        )
        await raw.commit()

    db = Database(str(db_path))
    await db.initialize()
    try:
        columns = {row["name"] for row in await db.fetch_all("PRAGMA table_info(messages)")}
    finally:
        await db.close()

    assert "context_epoch" in columns
    assert "rebuild_reason" in columns
```

- [ ] **Step 2: Run database tests and verify they fail**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_storage_database.py::test_messages_schema_has_context_epoch_columns tests/test_storage_database.py::test_migrate_legacy_messages_table_adds_context_epoch_columns -v
```

Expected: both tests fail because `context_epoch`, `rebuild_reason`, and the new indexes do not exist.

- [ ] **Step 3: Add schema columns, migration, and indexes**

In `backend/storage/database.py`, extend the `messages` table definition so the Phase 1 fields remain intact and the new fields are nullable:

```python
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role         TEXT NOT NULL,
    content      TEXT,
    tool_calls   TEXT,
    tool_call_id TEXT,
    provider_state TEXT,
    phase        INTEGER,
    phase3_step  TEXT,
    history_seq  INTEGER,
    run_id       TEXT,
    trip_id      TEXT,
    context_epoch INTEGER,
    rebuild_reason TEXT,
    created_at   TEXT NOT NULL,
    seq          INTEGER NOT NULL
);
```

Keep existing indexes and add:

```python
CREATE INDEX IF NOT EXISTS idx_messages_epoch
    ON messages(session_id, context_epoch, history_seq);
CREATE INDEX IF NOT EXISTS idx_messages_trip_epoch
    ON messages(session_id, trip_id, context_epoch);
```

Update `_migrate_messages_table()` so the missing-column tuple includes all Phase 1 fields plus Phase 3 fields. The final tuple should contain:

```python
missing_columns: tuple[tuple[str, str], ...] = (
    ("provider_state", "TEXT"),
    ("phase", "INTEGER"),
    ("phase3_step", "TEXT"),
    ("history_seq", "INTEGER"),
    ("run_id", "TEXT"),
    ("trip_id", "TEXT"),
    ("context_epoch", "INTEGER"),
    ("rebuild_reason", "TEXT"),
)
```

- [ ] **Step 4: Write failing MessageStore tests**

Append these tests to `backend/tests/test_storage_message.py`:

```python
@pytest.mark.asyncio
async def test_append_batch_persists_context_epoch_and_rebuild_reason(stores):
    _, message_store = stores
    await message_store.append_batch(
        "sess_msg_test_001",
        [
            {
                "role": "system",
                "content": "phase handoff",
                "seq": 0,
                "history_seq": 0,
                "phase": 3,
                "phase3_step": "brief",
                "run_id": "run-1",
                "trip_id": "trip-1",
                "context_epoch": 1,
                "rebuild_reason": "phase_forward",
            }
        ],
    )

    rows = await message_store.load_all("sess_msg_test_001")

    assert rows[0]["context_epoch"] == 1
    assert rows[0]["rebuild_reason"] == "phase_forward"


@pytest.mark.asyncio
async def test_load_by_context_epoch_orders_by_history_seq(stores):
    _, message_store = stores
    await message_store.append_batch(
        "sess_msg_test_001",
        [
            {"role": "assistant", "content": "later", "seq": 2, "history_seq": 2, "context_epoch": 4},
            {"role": "user", "content": "earlier", "seq": 1, "history_seq": 1, "context_epoch": 4},
            {"role": "tool", "content": "old epoch body", "seq": 0, "history_seq": 0, "context_epoch": 3},
        ],
    )

    rows = await message_store.load_by_context_epoch("sess_msg_test_001", 4)

    assert [row["content"] for row in rows] == ["earlier", "later"]
```

- [ ] **Step 5: Run MessageStore tests and verify they fail**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_storage_message.py::test_append_batch_persists_context_epoch_and_rebuild_reason tests/test_storage_message.py::test_load_by_context_epoch_orders_by_history_seq -v
```

Expected: fail because `append_batch()` does not insert epoch columns and `load_by_context_epoch()` is missing.

- [ ] **Step 6: Implement MessageStore epoch persistence and debug row query**

In `backend/storage/message_store.py`, keep the Phase 1 parameters and add `context_epoch` / `rebuild_reason` to both single-row and batch inserts:

```python
async def append(
    self,
    session_id: str,
    role: str,
    content: str | None,
    *,
    tool_calls: str | None = None,
    tool_call_id: str | None = None,
    provider_state: str | None = None,
    seq: int,
    phase: int | None = None,
    phase3_step: str | None = None,
    history_seq: int | None = None,
    run_id: str | None = None,
    trip_id: str | None = None,
    context_epoch: int | None = None,
    rebuild_reason: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await self._db.execute(
        "INSERT INTO messages "
        "(session_id, role, content, tool_calls, tool_call_id, provider_state, "
        "phase, phase3_step, history_seq, run_id, trip_id, context_epoch, rebuild_reason, created_at, seq) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            role,
            content,
            tool_calls,
            tool_call_id,
            provider_state,
            phase,
            phase3_step,
            history_seq,
            run_id,
            trip_id,
            context_epoch,
            rebuild_reason,
            now,
            seq,
        ),
    )
```

Use the same column list in `append_batch()`, reading values with `row.get("context_epoch")` and `row.get("rebuild_reason")`.

Add the ordered debug loader:

```python
async def load_by_context_epoch(
    self,
    session_id: str,
    context_epoch: int,
) -> list[dict[str, Any]]:
    return await self._db.fetch_all(
        """
        SELECT *
        FROM messages
        WHERE session_id = ? AND context_epoch = ?
        ORDER BY history_seq ASC, id ASC
        """,
        (session_id, context_epoch),
    )
```

Ensure `load_all()` keeps the Phase 1 ordering:

```python
ORDER BY
    CASE WHEN history_seq IS NULL THEN 1 ELSE 0 END,
    history_seq ASC,
    seq ASC,
    id ASC
```

- [ ] **Step 7: Run storage tests and verify they pass**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_storage_database.py tests/test_storage_message.py -v
```

Expected: all selected tests pass.

- [ ] **Step 8: Update `PROJECT_OVERVIEW.md` and commit**

Update the storage schema row to mention `context_epoch` and `rebuild_reason` as message metadata used for derived context segments.

Run:

```bash
git add backend/storage/database.py backend/storage/message_store.py backend/tests/test_storage_database.py backend/tests/test_storage_message.py PROJECT_OVERVIEW.md
git commit -m "feat(storage): persist context epoch metadata"
```

Expected: commit succeeds.

## Task 2: Derive Context Segments From Message Rows

**Files:**
- Create: `backend/api/orchestration/session/context_segments.py`
- Test: `backend/tests/test_context_segments.py`
- Modify before commit: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: Write failing segment derivation tests**

Create `backend/tests/test_context_segments.py`:

```python
from __future__ import annotations

from api.orchestration.session.context_segments import (
    ContextSegment,
    derive_context_segments,
)


def test_derive_context_segments_groups_by_context_epoch():
    rows = [
        {
            "session_id": "sess-1",
            "context_epoch": 0,
            "phase": 1,
            "phase3_step": None,
            "trip_id": "trip-a",
            "run_id": "run-1",
            "history_seq": 0,
            "rebuild_reason": None,
        },
        {
            "session_id": "sess-1",
            "context_epoch": 1,
            "phase": 3,
            "phase3_step": "brief",
            "trip_id": "trip-a",
            "run_id": "run-1",
            "history_seq": 1,
            "rebuild_reason": "phase_forward",
        },
        {
            "session_id": "sess-1",
            "context_epoch": 1,
            "phase": 3,
            "phase3_step": "brief",
            "trip_id": "trip-a",
            "run_id": "run-2",
            "history_seq": 2,
            "rebuild_reason": None,
        },
    ]

    segments = derive_context_segments(rows)

    assert segments == [
        ContextSegment(
            session_id="sess-1",
            context_epoch=0,
            phase=1,
            phase3_step=None,
            trip_id="trip-a",
            run_ids=("run-1",),
            start_history_seq=0,
            end_history_seq=0,
            message_count=1,
            rebuild_reason=None,
        ),
        ContextSegment(
            session_id="sess-1",
            context_epoch=1,
            phase=3,
            phase3_step="brief",
            trip_id="trip-a",
            run_ids=("run-1", "run-2"),
            start_history_seq=1,
            end_history_seq=2,
            message_count=2,
            rebuild_reason="phase_forward",
        ),
    ]


def test_repeated_phase3_visits_after_backtrack_produce_distinct_segments():
    rows = [
        {"session_id": "sess-1", "context_epoch": 2, "phase": 3, "phase3_step": "skeleton", "trip_id": "trip-a", "run_id": "run-3", "history_seq": 20, "rebuild_reason": "phase3_step_change"},
        {"session_id": "sess-1", "context_epoch": 3, "phase": 5, "phase3_step": None, "trip_id": "trip-a", "run_id": "run-4", "history_seq": 30, "rebuild_reason": "phase_forward"},
        {"session_id": "sess-1", "context_epoch": 4, "phase": 3, "phase3_step": "skeleton", "trip_id": "trip-a", "run_id": "run-5", "history_seq": 40, "rebuild_reason": "backtrack"},
    ]

    segments = derive_context_segments(rows)

    phase3_segments = [segment for segment in segments if segment.phase == 3]
    assert [segment.context_epoch for segment in phase3_segments] == [2, 4]
    assert [segment.rebuild_reason for segment in phase3_segments] == [
        "phase3_step_change",
        "backtrack",
    ]


def test_legacy_rows_without_context_epoch_do_not_break_new_segments():
    rows = [
        {"session_id": "sess-1", "context_epoch": None, "phase": None, "phase3_step": None, "trip_id": None, "run_id": None, "history_seq": None, "rebuild_reason": None},
        {"session_id": "sess-1", "context_epoch": 0, "phase": 1, "phase3_step": None, "trip_id": "trip-a", "run_id": "run-1", "history_seq": 0, "rebuild_reason": None},
    ]

    segments = derive_context_segments(rows)

    assert len(segments) == 1
    assert segments[0].context_epoch == 0
    assert segments[0].message_count == 1
```

- [ ] **Step 2: Run segment tests and verify they fail**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_context_segments.py -v
```

Expected: import fails because `api.orchestration.session.context_segments` does not exist.

- [ ] **Step 3: Implement `ContextSegment` and `derive_context_segments()`**

Create `backend/api/orchestration/session/context_segments.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContextSegment:
    session_id: str
    context_epoch: int
    phase: int | None
    phase3_step: str | None
    trip_id: str | None
    run_ids: tuple[str, ...]
    start_history_seq: int
    end_history_seq: int
    message_count: int
    rebuild_reason: str | None


def derive_context_segments(rows: list[dict[str, Any]]) -> list[ContextSegment]:
    normalized_rows = [
        row
        for row in rows
        if row.get("context_epoch") is not None and row.get("history_seq") is not None
    ]
    normalized_rows.sort(
        key=lambda row: (
            str(row.get("session_id") or ""),
            int(row["context_epoch"]),
            int(row["history_seq"]),
        )
    )

    segments: list[ContextSegment] = []
    current_key: tuple[str, int] | None = None
    current_rows: list[dict[str, Any]] = []

    def flush_current() -> None:
        if not current_rows:
            return
        first = current_rows[0]
        tagged = next(
            (row for row in current_rows if row.get("phase") is not None),
            first,
        )
        run_ids = tuple(
            dict.fromkeys(
                str(row["run_id"])
                for row in current_rows
                if row.get("run_id") is not None
            )
        )
        history_seqs = [int(row["history_seq"]) for row in current_rows]
        rebuild_reason = next(
            (
                str(row["rebuild_reason"])
                for row in current_rows
                if row.get("rebuild_reason")
            ),
            None,
        )
        segments.append(
            ContextSegment(
                session_id=str(first["session_id"]),
                context_epoch=int(first["context_epoch"]),
                phase=tagged.get("phase"),
                phase3_step=tagged.get("phase3_step"),
                trip_id=tagged.get("trip_id"),
                run_ids=run_ids,
                start_history_seq=min(history_seqs),
                end_history_seq=max(history_seqs),
                message_count=len(current_rows),
                rebuild_reason=rebuild_reason,
            )
        )

    for row in normalized_rows:
        key = (str(row["session_id"]), int(row["context_epoch"]))
        if current_key is not None and key != current_key:
            flush_current()
            current_rows = []
        current_key = key
        current_rows.append(row)
    flush_current()

    return segments
```

- [ ] **Step 4: Run segment tests and verify they pass**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_context_segments.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

Update `PROJECT_OVERVIEW.md` to mention derived context segments are computed from `messages.context_epoch`.

Run:

```bash
git add backend/api/orchestration/session/context_segments.py backend/tests/test_context_segments.py PROJECT_OVERVIEW.md
git commit -m "feat(session): derive context segments from history"
```

Expected: commit succeeds.

## Task 3: Add Service-Level Debug Queries

**Files:**
- Modify: `backend/storage/message_store.py`
- Modify: `backend/api/orchestration/session/persistence.py`
- Test: `backend/tests/test_session_persistence.py`
- Modify before commit: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: Write failing debug helper tests**

Append to `backend/tests/test_session_persistence.py`:

```python
import pytest

from api.orchestration.session.context_segments import ContextSegment


@pytest.mark.asyncio
async def test_persistence_lists_context_segments_from_message_store_rows():
    rows = [
        {"session_id": "sess-1", "context_epoch": 0, "phase": 1, "phase3_step": None, "trip_id": "trip-a", "run_id": "run-1", "history_seq": 0, "rebuild_reason": None},
        {"session_id": "sess-1", "context_epoch": 1, "phase": 3, "phase3_step": "brief", "trip_id": "trip-a", "run_id": "run-2", "history_seq": 1, "rebuild_reason": "phase_forward"},
    ]

    class _MessageStore:
        async def load_all(self, session_id):
            assert session_id == "sess-1"
            return rows

    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=None,
        message_store=_MessageStore(),
        archive_store=None,
        state_mgr=None,
        phase_router=None,
        build_agent=lambda *args, **kwargs: None,
    )

    segments = await persistence.list_context_segments("sess-1")

    assert segments == [
        ContextSegment(
            session_id="sess-1",
            context_epoch=0,
            phase=1,
            phase3_step=None,
            trip_id="trip-a",
            run_ids=("run-1",),
            start_history_seq=0,
            end_history_seq=0,
            message_count=1,
            rebuild_reason=None,
        ),
        ContextSegment(
            session_id="sess-1",
            context_epoch=1,
            phase=3,
            phase3_step="brief",
            trip_id="trip-a",
            run_ids=("run-2",),
            start_history_seq=1,
            end_history_seq=1,
            message_count=1,
            rebuild_reason="phase_forward",
        ),
    ]


@pytest.mark.asyncio
async def test_persistence_loads_context_segment_messages_without_http_route():
    calls = []

    class _MessageStore:
        async def load_by_context_epoch(self, session_id, context_epoch):
            calls.append((session_id, context_epoch))
            return [
                {"role": "tool", "content": "raw tool body", "history_seq": 12, "context_epoch": 4}
            ]

    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=None,
        message_store=_MessageStore(),
        archive_store=None,
        state_mgr=None,
        phase_router=None,
        build_agent=lambda *args, **kwargs: None,
    )

    rows = await persistence.load_context_segment_messages("sess-1", 4)

    assert calls == [("sess-1", 4)]
    assert rows[0]["content"] == "raw tool body"
```

- [ ] **Step 2: Run debug helper tests and verify they fail**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_session_persistence.py::test_persistence_lists_context_segments_from_message_store_rows tests/test_session_persistence.py::test_persistence_loads_context_segment_messages_without_http_route -v
```

Expected: fail because `SessionPersistence.list_context_segments()` and `load_context_segment_messages()` do not exist.

- [ ] **Step 3: Implement service-level helpers**

In `backend/api/orchestration/session/persistence.py`, import:

```python
from api.orchestration.session.context_segments import (
    ContextSegment,
    derive_context_segments,
)
```

Add methods to `SessionPersistence`:

```python
async def list_context_segments(self, session_id: str) -> list[ContextSegment]:
    await self.ensure_storage_ready()
    rows = await self.message_store.load_all(session_id)
    return derive_context_segments(rows)


async def load_context_segment_messages(
    self,
    session_id: str,
    context_epoch: int,
) -> list[dict]:
    await self.ensure_storage_ready()
    return await self.message_store.load_by_context_epoch(session_id, context_epoch)
```

Do not add a route in `backend/api/routes` and do not change `/api/messages/{session_id}`.

- [ ] **Step 4: Run debug helper tests and verify they pass**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_session_persistence.py::test_persistence_lists_context_segments_from_message_store_rows tests/test_session_persistence.py::test_persistence_loads_context_segment_messages_without_http_route -v
```

Expected: both tests pass.

- [ ] **Step 5: Run route grep to prove no HTTP debug route was added**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
rg "context_segments|load_context_segment_messages|list_context_segments" backend/api/routes backend/main.py
```

Expected: no matches in `backend/api/routes` or `backend/main.py`. Matches in `session/persistence.py` are fine and should not be part of this command output.

- [ ] **Step 6: Commit**

Update `PROJECT_OVERVIEW.md` to say segment inspection is currently service/helper-level only.

Run:

```bash
git add backend/api/orchestration/session/persistence.py backend/tests/test_session_persistence.py PROJECT_OVERVIEW.md
git commit -m "feat(session): add context segment debug helpers"
```

Expected: commit succeeds.

## Task 4: Initialize And Persist Current Context Epoch

**Files:**
- Modify: `backend/api/orchestration/session/persistence.py`
- Test: `backend/tests/test_session_persistence.py`
- Modify before commit: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: Write failing persistence tests for epoch initialization and writes**

Append to `backend/tests/test_session_persistence.py`:

```python
@pytest.mark.asyncio
async def test_restore_session_initializes_current_context_epoch_from_history(monkeypatch):
    class _SessionStore:
        async def load(self, session_id):
            return {"session_id": session_id, "status": "active", "user_id": "user-1"}

    class _StateMgr:
        async def load(self, session_id):
            from state.models import TravelPlanState

            return TravelPlanState(session_id=session_id, phase=3, destination="杭州")

    class _MessageStore:
        async def load_all(self, session_id):
            return [
                {"role": "user", "content": "旧消息", "context_epoch": 2, "history_seq": 10}
            ]

    class _ArchiveStore:
        async def load_latest_snapshot(self, session_id):
            return None

    class _PhaseRouter:
        def sync_phase_state(self, plan):
            return None

    async def _fake_runtime_view(**kwargs):
        from agent.types import Message, Role

        return [Message(role=Role.SYSTEM, content="new system")]

    monkeypatch.setattr(
        "api.orchestration.session.persistence.build_runtime_view_for_restore",
        _fake_runtime_view,
    )

    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=_SessionStore(),
        message_store=_MessageStore(),
        archive_store=_ArchiveStore(),
        state_mgr=_StateMgr(),
        phase_router=_PhaseRouter(),
        build_agent=lambda *args, **kwargs: SimpleNamespace(tool_engine=object()),
    )

    restored = await persistence.restore_session("sess-1")

    assert restored["current_context_epoch"] == 2
    assert restored["history_messages"][0].content == "旧消息"
    assert len(restored["messages"]) == 1


@pytest.mark.asyncio
async def test_persist_messages_writes_context_epoch_and_rebuild_reason():
    rows: list[dict[str, object]] = []

    class _MessageStore:
        async def append_batch(self, session_id, payload):
            rows.extend(payload)

    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=None,
        message_store=_MessageStore(),
        archive_store=None,
        state_mgr=None,
        phase_router=None,
        build_agent=lambda *args, **kwargs: None,
    )

    next_seq = await persistence.persist_messages(
        "sess-1",
        [Message(role=Role.SYSTEM, content="handoff")],
        phase=3,
        phase3_step="brief",
        run_id="run-1",
        trip_id="trip-a",
        next_history_seq=7,
        context_epoch=4,
        rebuild_reason="phase_forward",
    )

    assert next_seq == 8
    assert rows[0]["history_seq"] == 7
    assert rows[0]["context_epoch"] == 4
    assert rows[0]["rebuild_reason"] == "phase_forward"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_session_persistence.py::test_restore_session_initializes_current_context_epoch_from_history tests/test_session_persistence.py::test_persist_messages_writes_context_epoch_and_rebuild_reason -v
```

Expected: fail because restore does not return `current_context_epoch`, `persist_messages()` does not accept the new fields, or `build_runtime_view_for_restore` is not imported into `persistence.py`.

- [ ] **Step 3: Implement epoch initialization and write metadata**

In `backend/api/orchestration/session/persistence.py`, ensure Phase 2 runtime restore is imported:

```python
from api.orchestration.session.runtime_view import build_runtime_view_for_restore
```

Add a helper near the serialization helpers:

```python
def current_context_epoch_from_rows(rows: list[dict]) -> int:
    epochs = [
        int(row["context_epoch"])
        for row in rows
        if row.get("context_epoch") is not None
    ]
    return max(epochs) if epochs else 0
```

Extend `persist_messages()` signature:

```python
async def persist_messages(
    self,
    session_id: str,
    messages: list[Message],
    *,
    phase: int,
    phase3_step: str | None,
    run_id: str | None,
    trip_id: str | None,
    next_history_seq: int,
    context_epoch: int,
    rebuild_reason: str | None = None,
) -> int:
```

When building each row, include:

```python
"history_seq": next_history_seq + index,
"phase": phase,
"phase3_step": phase3_step,
"run_id": run_id,
"trip_id": trip_id,
"context_epoch": context_epoch,
"rebuild_reason": rebuild_reason if index == 0 else None,
```

Return:

```python
return next_history_seq + len(rows)
```

In `restore_session()`, after loading `history_rows = await self.message_store.load_all(session_id)`, compute:

```python
current_context_epoch = current_context_epoch_from_rows(history_rows)
```

Return it in the session dict:

```python
"history_messages": history_view,
"messages": runtime_view,
"current_context_epoch": current_context_epoch,
```

Restore must not increment epoch by itself. A restored session with max epoch `2` continues at epoch `2` until the next rebuild boundary advances it.

- [ ] **Step 4: Run persistence tests and verify they pass**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_session_persistence.py::test_restore_session_initializes_current_context_epoch_from_history tests/test_session_persistence.py::test_persist_messages_writes_context_epoch_and_rebuild_reason -v
```

Expected: both tests pass.

- [ ] **Step 5: Run session/storage regression tests**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_storage_message.py tests/test_session_persistence.py tests/test_session_restore.py -v
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

Update `PROJECT_OVERVIEW.md` to mention restored sessions carry `current_context_epoch`.

Run:

```bash
git add backend/api/orchestration/session/persistence.py backend/tests/test_session_persistence.py PROJECT_OVERVIEW.md
git commit -m "feat(session): track current context epoch"
```

Expected: commit succeeds.

## Task 5: Increment Epoch At Runtime Rebuild Boundaries

**Files:**
- Modify: `backend/agent/loop.py`
- Modify if callback wiring lives there after Phase 1: `backend/api/orchestration/agent/builder.py`
- Modify if session mutation lives there after Phase 1: `backend/api/orchestration/chat/stream.py`
- Test: `backend/tests/test_agent_context_epoch.py`
- Modify before commit: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: Write failing AgentLoop boundary tests**

Create `backend/tests/test_agent_context_epoch.py`:

```python
from __future__ import annotations

import pytest

from agent.loop import AgentLoop
from agent.types import Message, Role, ToolResult


class _ToolEngine:
    def get_tools_for_phase(self, phase, plan):
        return []


class _Hooks:
    async def run(self, *args, **kwargs):
        return None


class _Plan:
    phase = 3
    phase3_step = "candidate"


def _loop(on_context_rebuild):
    return AgentLoop(
        llm=object(),
        tool_engine=_ToolEngine(),
        hooks=_Hooks(),
        phase_router=object(),
        context_manager=object(),
        plan=_Plan(),
        memory_mgr=None,
        memory_enabled=False,
        on_context_rebuild=on_context_rebuild,
    )


@pytest.mark.asyncio
async def test_phase_forward_rebuild_advances_context_epoch_before_rebuild(monkeypatch):
    calls = []

    async def on_context_rebuild(**kwargs):
        calls.append(kwargs)

    loop = _loop(on_context_rebuild)

    async def fake_rebuild(**kwargs):
        assert calls[-1]["rebuild_reason"] == "phase_forward"
        return [Message(role=Role.SYSTEM, content="new phase")]

    monkeypatch.setattr("agent.loop.rebuild_messages_for_phase_change", fake_rebuild)

    await loop._rebuild_messages_for_phase_change(
        messages=[Message(role=Role.USER, content="go")],
        from_phase=1,
        to_phase=3,
        original_user_message=Message(role=Role.USER, content="go"),
        result=ToolResult(tool_call_id="tc", status="success", data={}),
    )

    assert calls == [
        {
            "messages": [Message(role=Role.USER, content="go")],
            "from_phase": 1,
            "from_phase3_step": None,
            "to_phase": 3,
            "to_phase3_step": "candidate",
            "rebuild_reason": "phase_forward",
        }
    ]


@pytest.mark.asyncio
async def test_backtrack_rebuild_advances_context_epoch_with_backtrack_reason(monkeypatch):
    calls = []

    async def on_context_rebuild(**kwargs):
        calls.append(kwargs)

    loop = _loop(on_context_rebuild)

    async def fake_rebuild(**kwargs):
        return [Message(role=Role.SYSTEM, content="backtracked")]

    monkeypatch.setattr("agent.loop.rebuild_messages_for_phase_change", fake_rebuild)

    await loop._rebuild_messages_for_phase_change(
        messages=[Message(role=Role.TOOL, content=None)],
        from_phase=5,
        to_phase=3,
        original_user_message=Message(role=Role.USER, content="重做框架"),
        result=ToolResult(
            tool_call_id="tc",
            status="success",
            data={"backtrack": {"from_phase": 5, "to_phase": 3}},
        ),
    )

    assert calls[0]["rebuild_reason"] == "backtrack"
    assert calls[0]["from_phase"] == 5
    assert calls[0]["to_phase"] == 3


@pytest.mark.asyncio
async def test_phase3_step_change_rebuild_advances_context_epoch(monkeypatch):
    calls = []

    async def on_context_rebuild(**kwargs):
        calls.append(kwargs)

    loop = _loop(on_context_rebuild)

    async def fake_rebuild(**kwargs):
        return [Message(role=Role.SYSTEM, content="new step")]

    monkeypatch.setattr("agent.loop.rebuild_messages_for_phase3_step_change", fake_rebuild)

    await loop._rebuild_messages_for_phase3_step_change(
        messages=[Message(role=Role.USER, content="候选池好了")],
        original_user_message=Message(role=Role.USER, content="候选池好了"),
        from_phase3_step="brief",
        to_phase3_step="candidate",
    )

    assert calls == [
        {
            "messages": [Message(role=Role.USER, content="候选池好了")],
            "from_phase": 3,
            "from_phase3_step": "brief",
            "to_phase": 3,
            "to_phase3_step": "candidate",
            "rebuild_reason": "phase3_step_change",
        }
    ]
```

- [ ] **Step 2: Run AgentLoop tests and verify they fail**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_agent_context_epoch.py -v
```

Expected: fail because `AgentLoop.__init__()` does not accept `on_context_rebuild`, or rebuild helpers do not call it.

- [ ] **Step 3: Add one callback type and call it before every rebuild**

In `backend/agent/loop.py`, import:

```python
from collections.abc import Awaitable, Callable
```

Add a type alias near `PhaseTransitionOutcome`:

```python
ContextRebuildCallback = Callable[..., Awaitable[None]]
```

Add constructor parameter:

```python
on_context_rebuild: ContextRebuildCallback | None = None,
```

Store it:

```python
self.on_context_rebuild = on_context_rebuild
```

Add helper:

```python
async def _notify_context_rebuild(
    self,
    *,
    messages: list[Message],
    from_phase: int,
    from_phase3_step: str | None,
    to_phase: int,
    to_phase3_step: str | None,
    rebuild_reason: str,
) -> None:
    if self.on_context_rebuild is None:
        return
    await self.on_context_rebuild(
        messages=messages,
        from_phase=from_phase,
        from_phase3_step=from_phase3_step,
        to_phase=to_phase,
        to_phase3_step=to_phase3_step,
        rebuild_reason=rebuild_reason,
    )
```

In `_rebuild_messages_for_phase_change()`, call it before `rebuild_messages_for_phase_change(...)`:

```python
rebuild_reason = "backtrack" if self._is_backtrack_result(result) else "phase_forward"
await self._notify_context_rebuild(
    messages=messages,
    from_phase=from_phase,
    from_phase3_step=getattr(self.plan, "phase3_step", None) if from_phase == 3 else None,
    to_phase=to_phase,
    to_phase3_step=getattr(self.plan, "phase3_step", None),
    rebuild_reason=rebuild_reason,
)
```

In `_rebuild_messages_for_phase3_step_change()`, extend the method signature:

```python
async def _rebuild_messages_for_phase3_step_change(
    self,
    messages: list[Message],
    original_user_message: Message,
    *,
    from_phase3_step: str | None,
    to_phase3_step: str | None,
) -> list[Message]:
```

Call the notification before rebuilding:

```python
await self._notify_context_rebuild(
    messages=messages,
    from_phase=3,
    from_phase3_step=from_phase3_step,
    to_phase=3,
    to_phase3_step=to_phase3_step,
    rebuild_reason="phase3_step_change",
)
```

Update the call site after `phase3_step_after_batch != phase3_step_before_batch` to pass:

```python
messages[:] = await self._rebuild_messages_for_phase3_step_change(
    messages=messages,
    original_user_message=original_user_message,
    from_phase3_step=phase3_step_before_batch,
    to_phase3_step=phase3_step_after_batch,
)
```

- [ ] **Step 4: Wire the callback to mutate session epoch and flush old messages**

In the Phase 1 callback owner (`backend/api/orchestration/chat/stream.py` or `backend/api/orchestration/agent/builder.py`, whichever already wires `on_before_message_rebuild`), replace separate rebuild callbacks with one callback that does this ordering:

```python
async def on_context_rebuild(
    *,
    messages: list[Message],
    from_phase: int,
    from_phase3_step: str | None,
    to_phase: int,
    to_phase3_step: str | None,
    rebuild_reason: str,
) -> None:
    old_epoch = int(session.get("current_context_epoch", 0))
    session["next_history_seq"] = await persistence.persist_messages(
        session_id,
        messages,
        phase=from_phase,
        phase3_step=from_phase3_step,
        run_id=run_record.run_id,
        trip_id=getattr(session["plan"], "trip_id", None),
        next_history_seq=session["next_history_seq"],
        context_epoch=old_epoch,
    )
    new_epoch = old_epoch + 1
    session["current_context_epoch"] = new_epoch
    session["_next_rebuild_reason"] = rebuild_reason
```

Then finalization of messages produced after rebuild must write:

```python
context_epoch=session["current_context_epoch"],
rebuild_reason=session.pop("_next_rebuild_reason", None),
```

This preserves the spec ordering: old runtime messages flush under the old epoch, then rebuilt runtime messages and the handoff/backtrack/system anchor are written under the new epoch with the boundary reason on the first new row.

- [ ] **Step 5: Run AgentLoop tests and verify they pass**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_agent_context_epoch.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Run focused agent/session regression tests**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_agent_phase_transition.py tests/test_agent_loop.py tests/test_session_persistence.py -v
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

Update `PROJECT_OVERVIEW.md` with the rule that phase forward, Phase 3 step changes, and backtrack advance `current_context_epoch`.

Run:

```bash
git add backend/agent/loop.py backend/api/orchestration/agent/builder.py backend/api/orchestration/chat/stream.py backend/tests/test_agent_context_epoch.py PROJECT_OVERVIEW.md
git commit -m "feat(agent): advance context epoch on rebuild"
```

Expected: commit succeeds. If either builder or stream was not touched because the Phase 1 callback owner is only one file, omit the untouched file from `git add`.

## Task 6: Keep Runtime View From Replaying Old Epoch Bodies

**Files:**
- Modify: `backend/api/orchestration/session/runtime_view.py`
- Test: `backend/tests/test_runtime_view.py`
- Modify before commit: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: Write failing runtime prompt red-line tests**

Append to `backend/tests/test_runtime_view.py`:

```python
from __future__ import annotations

import pytest

from agent.types import Message, Role, ToolResult
from api.orchestration.session.runtime_view import build_runtime_view_for_restore
from state.models import TravelPlanState


class _PhaseRouter:
    def get_prompt_for_plan(self, plan):
        return "phase prompt"


class _ContextManager:
    async def build_system_message(self, **kwargs):
        return Message(role=Role.SYSTEM, content="fresh system prompt")


class _MemoryMgr:
    async def generate_context(self, user_id, plan):
        return ""


class _ToolEngine:
    def get_tools_for_phase(self, phase, plan):
        return []


def _with_meta(message: Message, **metadata: object) -> Message:
    setattr(message, "metadata", metadata)
    return message


@pytest.mark.asyncio
async def test_runtime_view_does_not_include_old_epoch_tool_body_after_backtrack():
    plan = TravelPlanState(session_id="sess-1", phase=3, destination="成都")
    plan.phase3_step = "skeleton"
    history_view = [
        Message(role=Role.USER, content="第一次做框架"),
        Message(
            role=Role.TOOL,
            content=None,
            tool_result=ToolResult(
                tool_call_id="tc-old",
                status="success",
                data={"secret_body": "OLD_EPOCH_TOOL_BODY"},
            ),
        ),
        _with_meta(
            Message(
                role=Role.SYSTEM,
                content="backtrack notice",
            ),
            history_seq=20,
            context_epoch=4,
            phase=3,
            phase3_step="skeleton",
            rebuild_reason="backtrack",
        ),
        _with_meta(
            Message(
                role=Role.USER,
                content="重做框架，少走路",
            ),
            history_seq=21,
            context_epoch=4,
            phase=3,
        ),
    ]
    setattr(
        history_view[1],
        "metadata",
        {
                "history_seq": 10,
                "context_epoch": 2,
                "phase": 3,
                "phase3_step": "skeleton",
        },
    )

    runtime_view = await build_runtime_view_for_restore(
        history_view=history_view,
        plan=plan,
        user_id="user-1",
        phase_router=_PhaseRouter(),
        context_manager=_ContextManager(),
        memory_mgr=_MemoryMgr(),
        memory_enabled=False,
        tool_engine=_ToolEngine(),
    )

    prompt_text = "\n".join(str(message.content) for message in runtime_view if message.content)
    assert "OLD_EPOCH_TOOL_BODY" not in prompt_text
    assert "重做框架，少走路" in prompt_text
    assert runtime_view[0].role is Role.SYSTEM


@pytest.mark.asyncio
async def test_runtime_view_does_not_include_earlier_phase3_step_tool_body():
    plan = TravelPlanState(session_id="sess-1", phase=3, destination="成都")
    plan.phase3_step = "skeleton"
    history_view = [
        Message(
            role=Role.TOOL,
            content=None,
            tool_result=ToolResult(
                tool_call_id="tc-brief",
                status="success",
                data={"brief_tool": "BRIEF_EPOCH_TOOL_BODY"},
            ),
        ),
        _with_meta(
            Message(role=Role.SYSTEM, content="skeleton step handoff"),
            history_seq=9,
            context_epoch=3,
            phase=3,
            phase3_step="skeleton",
            rebuild_reason="phase3_step_change",
        ),
        _with_meta(
            Message(role=Role.USER, content="现在定骨架"),
            history_seq=10,
            context_epoch=3,
            phase=3,
            phase3_step="skeleton",
        ),
    ]
    setattr(
        history_view[0],
        "metadata",
        {"history_seq": 5, "context_epoch": 1, "phase": 3, "phase3_step": "brief"},
    )

    runtime_view = await build_runtime_view_for_restore(
        history_view=history_view,
        plan=plan,
        user_id="user-1",
        phase_router=_PhaseRouter(),
        context_manager=_ContextManager(),
        memory_mgr=_MemoryMgr(),
        memory_enabled=False,
        tool_engine=_ToolEngine(),
    )

    prompt_text = "\n".join(str(message.content) for message in runtime_view if message.content)
    assert "BRIEF_EPOCH_TOOL_BODY" not in prompt_text
    assert "现在定骨架" in prompt_text
```

- [ ] **Step 2: Run runtime view tests and verify they fail or expose the gap**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_runtime_view.py::test_runtime_view_does_not_include_old_epoch_tool_body_after_backtrack tests/test_runtime_view.py::test_runtime_view_does_not_include_earlier_phase3_step_tool_body -v
```

Expected before implementation: fail if Phase 2 builder reuses too much history, or pass if Phase 2 already had the stricter behavior. If they pass, still inspect the implementation in Step 3 and add the epoch-aware selection code only if it is missing and does not change existing behavior.

- [ ] **Step 3: Make runtime view selection epoch-aware without replaying segment bodies**

In `backend/api/orchestration/session/runtime_view.py`, keep the builder shape `[fresh system, minimal anchor(s)]`. Add small metadata helpers if they do not exist:

```python
def _message_meta(message: Message, key: str) -> object | None:
    metadata = getattr(message, "metadata", None)
    if isinstance(metadata, dict):
        return metadata.get(key)
    return None


def _latest_epoch(history_view: list[Message]) -> int | None:
    epochs = [
        int(epoch)
        for message in history_view
        if (epoch := _message_meta(message, "context_epoch")) is not None
    ]
    return max(epochs) if epochs else None


def _latest_user_anchor(history_view: list[Message]) -> Message | None:
    latest_epoch = _latest_epoch(history_view)
    candidates = [
        message
        for message in history_view
        if message.role is Role.USER
        and (latest_epoch is None or _message_meta(message, "context_epoch") == latest_epoch)
    ]
    if candidates:
        return candidates[-1]
    for message in reversed(history_view):
        if message.role is Role.USER:
            return message
    return None
```

Build the final runtime view from the fresh system message and the selected user/system anchor only:

```python
runtime_view = [system_message]
anchor = _latest_user_anchor(history_view)
if anchor is not None:
    runtime_view.append(anchor)
return runtime_view
```

Do not append old tool messages, assistant tool-call messages, or full segment bodies. Segment metadata can decide which anchor is latest; segment contents remain history/debug data only.

- [ ] **Step 4: Run runtime view tests and verify they pass**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_runtime_view.py -v
```

Expected: all runtime view tests pass, including the old-epoch tool body assertions.

- [ ] **Step 5: Commit**

Update `PROJECT_OVERVIEW.md` to explicitly state runtime restore may inspect epoch metadata but never inject old segment bodies into the LLM prompt.

Run:

```bash
git add backend/api/orchestration/session/runtime_view.py backend/tests/test_runtime_view.py PROJECT_OVERVIEW.md
git commit -m "test(session): guard runtime view against segment replay"
```

Expected: commit succeeds.

## Task 7: End-To-End Segment Behavior Regression

**Files:**
- Test: `backend/tests/test_context_epoch_integration.py`
- Modify before commit: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: Write an integration-style test for repeated Phase 3 visits**

Create `backend/tests/test_context_epoch_integration.py`:

```python
from __future__ import annotations

import pytest

from api.orchestration.session.context_segments import derive_context_segments
from storage.database import Database
from storage.message_store import MessageStore
from storage.session_store import SessionStore


@pytest.mark.asyncio
async def test_repeated_phase3_visits_after_backtrack_are_distinct_persisted_segments():
    db = Database(":memory:")
    await db.initialize()
    try:
        sessions = SessionStore(db)
        messages = MessageStore(db)
        await sessions.create("sess-segments", "user-1")
        await messages.append_batch(
            "sess-segments",
            [
                {"role": "user", "content": "去成都", "seq": 0, "history_seq": 0, "phase": 1, "phase3_step": None, "run_id": "run-1", "trip_id": "trip-a", "context_epoch": 0},
                {"role": "system", "content": "进入框架", "seq": 1, "history_seq": 1, "phase": 3, "phase3_step": "brief", "run_id": "run-1", "trip_id": "trip-a", "context_epoch": 1, "rebuild_reason": "phase_forward"},
                {"role": "tool", "content": "第一次 Phase 3 工具体", "seq": 2, "history_seq": 2, "phase": 3, "phase3_step": "skeleton", "run_id": "run-2", "trip_id": "trip-a", "context_epoch": 2, "rebuild_reason": "phase3_step_change"},
                {"role": "system", "content": "进入逐日", "seq": 3, "history_seq": 3, "phase": 5, "phase3_step": None, "run_id": "run-3", "trip_id": "trip-a", "context_epoch": 3, "rebuild_reason": "phase_forward"},
                {"role": "system", "content": "回退框架", "seq": 4, "history_seq": 4, "phase": 3, "phase3_step": "skeleton", "run_id": "run-4", "trip_id": "trip-a", "context_epoch": 4, "rebuild_reason": "backtrack"},
                {"role": "user", "content": "第二次 Phase 3", "seq": 5, "history_seq": 5, "phase": 3, "phase3_step": "skeleton", "run_id": "run-4", "trip_id": "trip-a", "context_epoch": 4},
            ],
        )

        rows = await messages.load_all("sess-segments")
        segments = derive_context_segments(rows)
    finally:
        await db.close()

    phase3_skeleton_segments = [
        segment
        for segment in segments
        if segment.phase == 3 and segment.phase3_step == "skeleton"
    ]
    assert [segment.context_epoch for segment in phase3_skeleton_segments] == [2, 4]
    assert [segment.rebuild_reason for segment in phase3_skeleton_segments] == [
        "phase3_step_change",
        "backtrack",
    ]
    assert phase3_skeleton_segments[0].end_history_seq == 2
    assert phase3_skeleton_segments[1].start_history_seq == 4
```

- [ ] **Step 2: Run the integration test and verify it passes**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_context_epoch_integration.py -v
```

Expected: test passes. If it fails, inspect whether `MessageStore.load_all()` ordering or `derive_context_segments()` grouping is inconsistent with earlier tasks.

- [ ] **Step 3: Run the full focused Phase 3 suite**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest \
  tests/test_storage_database.py \
  tests/test_storage_message.py \
  tests/test_context_segments.py \
  tests/test_session_persistence.py \
  tests/test_agent_context_epoch.py \
  tests/test_runtime_view.py \
  tests/test_context_epoch_integration.py \
  -v
```

Expected: all selected tests pass.

- [ ] **Step 4: Run a route-surface safety check**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
rg "context_epoch|context_segments|rebuild_reason" backend/api/routes backend/main.py
```

Expected: no matches. Phase 3 exposes service/helper debug access only.

- [ ] **Step 5: Commit**

Update `PROJECT_OVERVIEW.md` with the final Phase 3 behavior summary if earlier task commits did not already cover repeated Phase 3 visits after backtrack.

Run:

```bash
git add backend/tests/test_context_epoch_integration.py PROJECT_OVERVIEW.md
git commit -m "test(session): cover repeated phase3 context segments"
```

Expected: commit succeeds.

## Final Verification

Run the focused suite:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest \
  tests/test_storage_database.py \
  tests/test_storage_message.py \
  tests/test_context_segments.py \
  tests/test_session_persistence.py \
  tests/test_agent_context_epoch.py \
  tests/test_runtime_view.py \
  tests/test_context_epoch_integration.py \
  -v
```

Expected: all selected tests pass.

Run the broader backend regression most likely to catch callback and restore breakage:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest \
  tests/test_agent_loop.py \
  tests/test_agent_phase_transition.py \
  tests/test_session_restore.py \
  tests/test_session_persistence.py \
  tests/test_storage_message.py \
  -v
```

Expected: all selected tests pass.

Run the no-route check:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
rg "context_segments|load_context_segment_messages|list_context_segments" backend/api/routes backend/main.py
```

Expected: no output.

## Implementation Notes And Red Lines

- `context_epoch` and `run_id` must remain separate fields. Never derive epoch from `run_id`; a single run can cross a rebuild boundary, and repeated visits can occur across multiple runs.
- `rebuild_reason` is written only on the first message persisted in the new epoch after a rebuild boundary. Normal user, assistant, and tool rows keep `rebuild_reason=None`.
- Restore initializes `current_context_epoch` from `MAX(messages.context_epoch)` and does not increment by itself.
- Phase forward, Phase 3 step change, and backtrack all increment epoch exactly once per runtime-context rebuild.
- `derive_context_segments()` is pure. It reads rows and returns dataclasses; it never writes session state.
- Legacy rows with `context_epoch IS NULL` are ignored by precise segment derivation, but they still remain in append-only history and `/api/messages/{session_id}` behavior remains unchanged.
- Segment debug helpers return raw internal rows and therefore must stay behind service/helper calls until a separate permission-controlled HTTP debug API design exists.
- Runtime restore may use epoch metadata to choose a recent anchor, but it must not append old epoch tool results, assistant tool-call bodies, or full segment contents to the LLM prompt.
