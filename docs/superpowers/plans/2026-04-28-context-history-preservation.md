# Context History Preservation Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert session message persistence to append-only history so phase rebuilds no longer erase old assistant/tool traces, while keeping the current runtime prompt shape and `/api/messages` behavior stable.

**Architecture:** Keep `session["messages"]` as the rebuildable runtime prompt list, and make SQLite `messages` the append-only history source with durable `history_seq`. Add a pre-rebuild flush callback on `AgentLoop` so the old runtime list is persisted before replacement; use per-message history flags plus session `next_history_seq` instead of any `len(runtime_messages)` cursor. Frontend message recovery continues to serialize the active runtime view when available and filters internal system rows when falling back to DB history.

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, pytest, pytest-asyncio, existing `AgentLoop`, `SessionPersistence`, `MessageStore`, and SSE orchestration modules.

---

## Scope

Implement only Phase 1 from `docs/superpowers/specs/2026-04-28-context-history-preservation-design.md`:

- Append-only history in SQLite.
- New message fields: `phase`, `phase3_step`, `history_seq`, `run_id`, `trip_id`.
- Durable `session["next_history_seq"]` initialized from `MAX(history_seq) + 1`.
- Pre-rebuild flush before phase and Phase 3 step runtime message rebuilds.
- Finalization and cancellation persistence that does not duplicate writes and does not lose writes.
- Preserve current prompt shape; do not restore old history into LLM prompts.
- Keep `/api/messages/{session_id}` frontend view stable.
- Prove Phase 1 -> 3 -> 5 leaves old Phase 1 tool history in DB.

Out of scope:

- Phase 2 runtime restore/history view split.
- Phase 3 `context_epoch` segmentation and debug history API.
- Any frontend timeline/debug UI.
- Reinjecting old phase history into LLM prompts.

## File Structure Map

- Modify `backend/agent/types.py`
  - Add non-provider metadata fields to `Message`: `history_persisted` and `history_seq`.
  - Keep `Message.to_dict()` unchanged for provider/frontend-visible payloads.
- Modify `backend/agent/execution/message_rebuild.py`
  - Preserve message persistence metadata in `copy_message()`.
  - This lets the copied original user anchor remain marked persisted after pre-rebuild flush.
- Modify `backend/storage/database.py`
  - Add new nullable columns and indexes/unique constraint support for fresh DBs.
  - Migrate legacy DBs by adding missing columns and indexes.
- Modify `backend/storage/message_store.py`
  - Accept and write new history metadata in `append()` and `append_batch()`.
  - Add `max_history_seq()` and `load_frontend_view()` helpers.
  - Order history by `history_seq ASC, id ASC`, falling back to `seq ASC, id ASC` for legacy rows.
- Modify `backend/api/orchestration/session/persistence.py`
  - Change `persist_messages()` from delete-and-rewrite to append coordinator.
  - Serialize only messages not yet marked `history_persisted`.
  - Assign durable `history_seq`, mark messages persisted after a successful write, and return the next cursor.
  - Initialize `next_history_seq` during restore.
- Modify `backend/agent/loop.py`
  - Add `on_before_message_rebuild` callback.
  - Invoke it before `_rebuild_messages_for_phase_change()` and `_rebuild_messages_for_phase3_step_change()`.
  - Catch/log callback errors and continue rebuild.
- Modify `backend/api/orchestration/agent/builder.py`
  - Thread optional `on_before_message_rebuild` into `AgentLoop`.
- Modify `backend/api/routes/chat_routes.py`
  - Install a per-run pre-rebuild flush callback on the active agent after `RunRecord` creation.
  - Initialize new sessions with `next_history_seq = 0`.
  - Ensure fallback/reset backtrack flushes old-trip runtime messages before rotating `trip_id`.
- Modify `backend/api/routes/session_routes.py`
  - Initialize new session dicts with `next_history_seq = 0`.
  - Keep `/api/messages` stable by serializing active runtime messages when the session is loaded; when falling back to DB, return `message_store.load_frontend_view()`.
- Modify `backend/api/orchestration/chat/finalization.py`
  - Add a shared `persist_unflushed_messages()` helper.
  - Use it in normal finalization and `finally` cancellation/error persistence.
  - Use message flags, not runtime length, to make repeated finalization calls idempotent.
- Modify tests:
  - `backend/tests/test_storage_database.py`
  - `backend/tests/test_storage_message.py`
  - `backend/tests/test_session_persistence.py`
  - `backend/tests/test_agent_loop.py`
  - `backend/tests/test_api.py`
  - Add `backend/tests/test_context_history_preservation.py`
- Modify `PROJECT_OVERVIEW.md`
  - Required by repo policy for commits. Update only the context persistence/session flow description after implementation.

## Commit Policy

`AGENTS.md` requires every commit to keep `PROJECT_OVERVIEW.md` current. Each task below includes `PROJECT_OVERVIEW.md` in its commit command. Before each commit, update the overview with the architecture that is true after that task lands; keep the edit small and focused on session/context persistence.

## Task 1: Database Schema And MessageStore Append Metadata

**Files:**
- Modify: `backend/storage/database.py`
- Modify: `backend/storage/message_store.py`
- Test: `backend/tests/test_storage_database.py`
- Test: `backend/tests/test_storage_message.py`

- [ ] **Step 1: Write failing database migration tests**

Append these tests to `backend/tests/test_storage_database.py`:

```python
@pytest.mark.asyncio
async def test_messages_schema_contains_history_columns_and_indexes(db: Database):
    columns = await db.fetch_all("PRAGMA table_info(messages)")
    column_names = {column["name"] for column in columns}

    assert {"phase", "phase3_step", "history_seq", "run_id", "trip_id"} <= column_names

    indexes = await db.fetch_all("PRAGMA index_list(messages)")
    index_names = {index["name"] for index in indexes}
    assert "idx_messages_history" in index_names
    assert "idx_messages_phase" in index_names
    assert "idx_messages_session_history_unique" in index_names


@pytest.mark.asyncio
async def test_initialize_migrates_legacy_messages_history_schema(tmp_path):
    db_path = tmp_path / "legacy-history-messages.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            session_id   TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL DEFAULT 'default_user',
            title        TEXT,
            phase        INTEGER NOT NULL DEFAULT 1,
            status       TEXT NOT NULL DEFAULT 'active',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        CREATE TABLE messages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
            role         TEXT NOT NULL,
            content      TEXT,
            tool_calls   TEXT,
            tool_call_id TEXT,
            provider_state TEXT,
            created_at   TEXT NOT NULL,
            seq          INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    database = Database(str(db_path))
    await database.initialize()
    columns = await database.fetch_all("PRAGMA table_info(messages)")
    indexes = await database.fetch_all("PRAGMA index_list(messages)")
    await database.close()

    column_names = {column["name"] for column in columns}
    index_names = {index["name"] for index in indexes}
    assert {"phase", "phase3_step", "history_seq", "run_id", "trip_id"} <= column_names
    assert "idx_messages_history" in index_names
    assert "idx_messages_phase" in index_names
    assert "idx_messages_session_history_unique" in index_names
```

- [ ] **Step 2: Write failing MessageStore tests**

Append these tests to `backend/tests/test_storage_message.py`:

```python
@pytest.mark.asyncio
async def test_append_writes_history_metadata(stores):
    _, message_store = stores

    await message_store.append(
        "sess_msg_test_001",
        "assistant",
        "查到东京适合亲子游",
        seq=9,
        phase=1,
        phase3_step=None,
        history_seq=0,
        run_id="run_1",
        trip_id="trip_1",
    )

    messages = await message_store.load_all("sess_msg_test_001")
    assert messages[0]["phase"] == 1
    assert messages[0]["phase3_step"] is None
    assert messages[0]["history_seq"] == 0
    assert messages[0]["run_id"] == "run_1"
    assert messages[0]["trip_id"] == "trip_1"


@pytest.mark.asyncio
async def test_load_all_orders_by_history_seq_before_legacy_seq(stores):
    _, message_store = stores

    await message_store.append(
        "sess_msg_test_001",
        "assistant",
        "new-second",
        seq=0,
        history_seq=11,
    )
    await message_store.append(
        "sess_msg_test_001",
        "assistant",
        "new-first",
        seq=99,
        history_seq=10,
    )
    await message_store.append(
        "sess_msg_test_001",
        "assistant",
        "legacy",
        seq=1,
    )

    messages = await message_store.load_all("sess_msg_test_001")
    assert [message["content"] for message in messages] == [
        "legacy",
        "new-first",
        "new-second",
    ]


@pytest.mark.asyncio
async def test_max_history_seq_ignores_legacy_rows(stores):
    _, message_store = stores
    await message_store.append("sess_msg_test_001", "user", "legacy", seq=20)
    await message_store.append(
        "sess_msg_test_001",
        "assistant",
        "new",
        seq=21,
        history_seq=4,
    )

    assert await message_store.max_history_seq("sess_msg_test_001") == 4


@pytest.mark.asyncio
async def test_load_frontend_view_filters_system_rows(stores):
    _, message_store = stores
    await message_store.append(
        "sess_msg_test_001",
        "system",
        "内部系统提示",
        seq=0,
        history_seq=0,
    )
    await message_store.append(
        "sess_msg_test_001",
        "user",
        "去东京",
        seq=1,
        history_seq=1,
    )

    messages = await message_store.load_frontend_view("sess_msg_test_001")
    assert [message["role"] for message in messages] == ["user"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_storage_database.py::test_messages_schema_contains_history_columns_and_indexes tests/test_storage_database.py::test_initialize_migrates_legacy_messages_history_schema tests/test_storage_message.py::test_append_writes_history_metadata tests/test_storage_message.py::test_load_all_orders_by_history_seq_before_legacy_seq tests/test_storage_message.py::test_max_history_seq_ignores_legacy_rows tests/test_storage_message.py::test_load_frontend_view_filters_system_rows -q
```

Expected: FAIL with missing columns/indexes or `TypeError: MessageStore.append() got an unexpected keyword argument 'phase'`.

- [ ] **Step 4: Implement schema columns, indexes, and MessageStore support**

In `backend/storage/database.py`, update `_SCHEMA` `messages` table and indexes:

```python
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role         TEXT NOT NULL,
    content      TEXT,
    tool_calls   TEXT,
    tool_call_id TEXT,
    provider_state TEXT,
    created_at   TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    phase        INTEGER,
    phase3_step  TEXT,
    history_seq  INTEGER,
    run_id       TEXT,
    trip_id      TEXT
);
```

Replace the old message index with:

```python
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_session_history_unique
    ON messages(session_id, history_seq)
    WHERE history_seq IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_history
    ON messages(session_id, history_seq);
CREATE INDEX IF NOT EXISTS idx_messages_phase
    ON messages(session_id, phase, phase3_step, history_seq);
```

Extend `_migrate_messages_table()`:

```python
    async def _migrate_messages_table(self) -> None:
        async with self.conn.execute("PRAGMA table_info(messages)") as cursor:
            rows = await cursor.fetchall()

        existing_columns = {row["name"] for row in rows}
        missing_columns = (
            ("provider_state", "TEXT"),
            ("phase", "INTEGER"),
            ("phase3_step", "TEXT"),
            ("history_seq", "INTEGER"),
            ("run_id", "TEXT"),
            ("trip_id", "TEXT"),
        )
        for column_name, column_type in missing_columns:
            if column_name in existing_columns:
                continue
            await self.conn.execute(
                f"ALTER TABLE messages ADD COLUMN {column_name} {column_type}"
            )

        await self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_session_history_unique "
            "ON messages(session_id, history_seq) WHERE history_seq IS NOT NULL"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_history "
            "ON messages(session_id, history_seq)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_phase "
            "ON messages(session_id, phase, phase3_step, history_seq)"
        )
```

In `backend/storage/message_store.py`, update `append()` and `append_batch()` signatures and inserts:

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
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO messages "
            "(session_id, role, content, tool_calls, tool_call_id, provider_state, created_at, seq, phase, phase3_step, history_seq, run_id, trip_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                role,
                content,
                tool_calls,
                tool_call_id,
                provider_state,
                now,
                seq,
                phase,
                phase3_step,
                history_seq,
                run_id,
                trip_id,
            ),
        )
```

Use one transaction for `append_batch()`:

```python
    async def append_batch(self, session_id: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.executemany(
            "INSERT INTO messages "
            "(session_id, role, content, tool_calls, tool_call_id, provider_state, created_at, seq, phase, phase3_step, history_seq, run_id, trip_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    session_id,
                    row["role"],
                    row.get("content"),
                    row.get("tool_calls"),
                    row.get("tool_call_id"),
                    row.get("provider_state"),
                    now,
                    row["seq"],
                    row.get("phase"),
                    row.get("phase3_step"),
                    row.get("history_seq"),
                    row.get("run_id"),
                    row.get("trip_id"),
                )
                for row in rows
            ],
        )
        await self._db.conn.commit()
```

Add helpers:

```python
    async def load_all(self, session_id: str) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? "
            "ORDER BY CASE WHEN history_seq IS NULL THEN 0 ELSE 1 END ASC, "
            "history_seq ASC, seq ASC, id ASC",
            (session_id,),
        )

    async def load_frontend_view(self, session_id: str) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? AND role != 'system' "
            "ORDER BY CASE WHEN history_seq IS NULL THEN 0 ELSE 1 END ASC, "
            "history_seq ASC, seq ASC, id ASC",
            (session_id,),
        )

    async def max_history_seq(self, session_id: str) -> int | None:
        row = await self._db.fetch_one(
            "SELECT MAX(history_seq) AS max_history_seq "
            "FROM messages WHERE session_id = ?",
            (session_id,),
        )
        if row is None or row["max_history_seq"] is None:
            return None
        return int(row["max_history_seq"])
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_storage_database.py tests/test_storage_message.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/storage/database.py backend/storage/message_store.py backend/tests/test_storage_database.py backend/tests/test_storage_message.py PROJECT_OVERVIEW.md
git commit -m "feat: add append-only message history schema"
```

## Task 2: SessionPersistence Append Coordinator And Durable Cursor

**Files:**
- Modify: `backend/agent/types.py`
- Modify: `backend/agent/execution/message_rebuild.py`
- Modify: `backend/api/orchestration/session/persistence.py`
- Test: `backend/tests/test_session_persistence.py`

- [ ] **Step 1: Write failing SessionPersistence tests**

Append these tests to `backend/tests/test_session_persistence.py`:

```python
@pytest.mark.asyncio
async def test_persist_messages_appends_without_delete_and_returns_next_history_seq():
    rows: list[dict[str, object]] = []
    deletes: list[tuple[str, tuple[object, ...]]] = []

    class _MessageStore:
        async def append_batch(self, session_id, payload):
            rows.extend(payload)

        async def load_all(self, session_id):
            return rows

    async def _execute(sql, params=()):
        if sql.startswith("DELETE FROM messages"):
            deletes.append((sql, params))

    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_execute),
        session_store=None,
        message_store=_MessageStore(),
        archive_store=None,
        state_mgr=None,
        phase_router=None,
        build_agent=lambda *args, **kwargs: None,
    )
    messages = [
        Message(role=Role.USER, content="去东京"),
        Message(
            role=Role.ASSISTANT,
            content=None,
            tool_calls=[ToolCall(id="tc_1", name="quick_travel_search", arguments={})],
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(tool_call_id="tc_1", status="success", data={"ok": True}),
        ),
    ]

    next_seq = await persistence.persist_messages(
        "sess_1",
        messages,
        phase=1,
        phase3_step=None,
        run_id="run_1",
        trip_id="trip_1",
        next_history_seq=7,
    )

    assert deletes == []
    assert next_seq == 10
    assert [row["history_seq"] for row in rows] == [7, 8, 9]
    assert [row["seq"] for row in rows] == [7, 8, 9]
    assert {row["phase"] for row in rows} == {1}
    assert {row["run_id"] for row in rows} == {"run_1"}
    assert {row["trip_id"] for row in rows} == {"trip_1"}
    assert all(message.history_persisted for message in messages)
    assert [message.history_seq for message in messages] == [7, 8, 9]


@pytest.mark.asyncio
async def test_persist_messages_skips_already_persisted_messages_without_len_cursor():
    rows: list[dict[str, object]] = []

    class _MessageStore:
        async def append_batch(self, session_id, payload):
            rows.extend(payload)

        async def load_all(self, session_id):
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
    already_flushed = Message(
        role=Role.USER,
        content="旧 runtime anchor",
        history_persisted=True,
        history_seq=3,
    )
    new_reply = Message(role=Role.ASSISTANT, content="继续规划")

    next_seq = await persistence.persist_messages(
        "sess_1",
        [already_flushed, new_reply],
        phase=3,
        phase3_step="candidate",
        run_id="run_2",
        trip_id="trip_1",
        next_history_seq=4,
    )

    assert next_seq == 5
    assert len(rows) == 1
    assert rows[0]["content"] == "继续规划"
    assert rows[0]["history_seq"] == 4
    assert already_flushed.history_seq == 3
    assert new_reply.history_persisted is True


@pytest.mark.asyncio
async def test_restore_session_initializes_next_history_seq_from_database():
    class _SessionStore:
        async def load(self, session_id):
            return {"status": "active", "user_id": "user_1"}

    class _ArchiveStore:
        async def load_latest_snapshot(self, session_id):
            return None

    class _StateManager:
        async def load(self, session_id):
            return SimpleNamespace(session_id=session_id)

    class _MessageStore:
        async def load_all(self, session_id):
            return [
                {"role": "user", "content": "legacy", "seq": 1, "history_seq": None},
                {"role": "assistant", "content": "new", "seq": 2, "history_seq": 12},
            ]

        async def max_history_seq(self, session_id):
            return 12

    class _PhaseRouter:
        def sync_phase_state(self, plan):
            return None

    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=_SessionStore(),
        message_store=_MessageStore(),
        archive_store=_ArchiveStore(),
        state_mgr=_StateManager(),
        phase_router=_PhaseRouter(),
        build_agent=lambda *args, **kwargs: "agent",
    )

    restored = await persistence.restore_session("sess_1")

    assert restored["next_history_seq"] == 13
    assert all(message.history_persisted for message in restored["messages"])
    assert restored["messages"][1].history_seq == 12
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_session_persistence.py::test_persist_messages_appends_without_delete_and_returns_next_history_seq tests/test_session_persistence.py::test_persist_messages_skips_already_persisted_messages_without_len_cursor tests/test_session_persistence.py::test_restore_session_initializes_next_history_seq_from_database -q
```

Expected: FAIL because `Message` has no `history_persisted`, `persist_messages()` still deletes rows, and `restore_session()` does not set `next_history_seq`.

- [ ] **Step 3: Add in-memory persistence metadata to Message**

In `backend/agent/types.py`, extend `Message`:

```python
@dataclass
class Message:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_result: ToolResult | None = None
    name: str | None = None
    provider_state: dict[str, Any] | None = None
    incomplete: bool = False
    history_persisted: bool = False
    history_seq: int | None = None
```

Do not add these fields to `to_dict()`. They are local persistence metadata and must not change provider payloads, SSE payloads, or frontend message objects.

In `backend/agent/execution/message_rebuild.py`, update `copy_message()`:

```python
def copy_message(message: Message) -> Message:
    return Message(
        role=message.role,
        content=message.content,
        tool_calls=message.tool_calls,
        tool_result=message.tool_result,
        name=message.name,
        provider_state=message.provider_state,
        incomplete=message.incomplete,
        history_persisted=message.history_persisted,
        history_seq=message.history_seq,
    )
```

- [ ] **Step 4: Implement append-only SessionPersistence**

Change `persist_messages()` in `backend/api/orchestration/session/persistence.py`:

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
    ) -> int:
        await self.ensure_storage_ready()
        rows: list[dict[str, object]] = []
        messages_to_mark: list[tuple[Message, int]] = []
        cursor = next_history_seq

        for message in messages:
            if message.history_persisted:
                continue

            assigned_history_seq = cursor
            cursor += 1

            tool_calls_json = None
            if message.tool_calls:
                tool_calls_json = json.dumps(
                    [
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "arguments": tool_call.arguments,
                            "human_label": tool_call.human_label,
                        }
                        for tool_call in message.tool_calls
                    ],
                    ensure_ascii=False,
                )

            content = message.content
            tool_call_id = None
            if message.tool_result is not None:
                content = serialize_tool_result(message.tool_result)
                tool_call_id = message.tool_result.tool_call_id

            provider_state_json = None
            if message.provider_state:
                provider_state_json = json.dumps(
                    message.provider_state,
                    ensure_ascii=False,
                )

            rows.append(
                {
                    "role": message.role.value,
                    "content": content,
                    "tool_calls": tool_calls_json,
                    "tool_call_id": tool_call_id,
                    "provider_state": provider_state_json,
                    "seq": assigned_history_seq,
                    "phase": phase,
                    "phase3_step": phase3_step,
                    "history_seq": assigned_history_seq,
                    "run_id": run_id,
                    "trip_id": trip_id,
                }
            )
            messages_to_mark.append((message, assigned_history_seq))

        await self.message_store.append_batch(session_id, rows)

        for message, assigned_history_seq in messages_to_mark:
            message.history_persisted = True
            message.history_seq = assigned_history_seq

        return cursor
```

In `restore_session()`, set restored message flags and the cursor:

```python
            restored_messages.append(
                Message(
                    role=role,
                    content=row.get("content") if tool_result is None else None,
                    tool_calls=tool_calls,
                    tool_result=tool_result,
                    provider_state=provider_state,
                    history_persisted=True,
                    history_seq=(
                        int(row["history_seq"])
                        if row.get("history_seq") is not None
                        else None
                    ),
                )
            )
```

Before returning:

```python
        max_history_seq = await self.message_store.max_history_seq(session_id)
        next_history_seq = 0 if max_history_seq is None else max_history_seq + 1
```

Add to the returned dict:

```python
            "next_history_seq": next_history_seq,
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_session_persistence.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/agent/types.py backend/agent/execution/message_rebuild.py backend/api/orchestration/session/persistence.py backend/tests/test_session_persistence.py PROJECT_OVERVIEW.md
git commit -m "feat: append session messages with durable history cursor"
```

## Task 3: AgentLoop Pre-Rebuild Flush Callback

**Files:**
- Modify: `backend/agent/loop.py`
- Modify: `backend/api/orchestration/agent/builder.py`
- Test: `backend/tests/test_agent_loop.py`

- [ ] **Step 1: Write failing AgentLoop tests**

Append these tests to `backend/tests/test_agent_loop.py`:

```python
@pytest.mark.asyncio
async def test_phase_transition_flushes_messages_before_rebuild():
    plan = TravelPlanState(session_id="s1", phase=1)
    flushed: list[dict[str, object]] = []

    @tool(
        name="advance_phase",
        description="advance",
        phases=[1],
        parameters={"type": "object", "properties": {}},
    )
    async def advance_phase() -> dict:
        plan.phase = 3
        return {"destination": "东京"}

    engine = ToolEngine()
    engine.register(advance_phase)

    call_index = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_index
        call_index += 1
        if call_index == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(id="tc1", name="advance_phase", arguments={}),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="phase 3 ready")
        yield LLMChunk(type=ChunkType.DONE)

    async def flush_callback(*, messages, from_phase, from_phase3_step):
        flushed.append(
            {
                "from_phase": from_phase,
                "from_phase3_step": from_phase3_step,
                "roles": [message.role for message in messages],
                "tool_names": [
                    call.name
                    for message in messages
                    for call in (message.tool_calls or [])
                ],
            }
        )

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=3,
        phase_router=FakePhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u1",
        on_before_message_rebuild=flush_callback,
    )

    messages = [Message(role=Role.USER, content="去东京")]
    async for _ in agent.run(messages, phase=1):
        pass

    assert flushed == [
        {
            "from_phase": 1,
            "from_phase3_step": None,
            "roles": [Role.USER, Role.ASSISTANT, Role.TOOL],
            "tool_names": ["advance_phase"],
        }
    ]


@pytest.mark.asyncio
async def test_phase3_step_change_flushes_messages_before_rebuild():
    plan = TravelPlanState(session_id="s1", phase=3, phase3_step="brief")
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)
    flushed: list[dict[str, object]] = []
    observed_second_call: dict[str, object] = {}
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc1",
                    name="set_trip_brief",
                    arguments={"fields": {"destination": "东京", "goal": "轻松游"}},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return
        observed_second_call["messages"] = [message.content for message in messages]
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="继续规划")
        yield LLMChunk(type=ChunkType.DONE)

    async def flush_callback(*, messages, from_phase, from_phase3_step):
        flushed.append(
            {
                "from_phase": from_phase,
                "from_phase3_step": from_phase3_step,
                "contents": [message.content for message in messages],
            }
        )

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=3,
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u1",
        on_before_message_rebuild=flush_callback,
    )

    messages = [Message(role=Role.USER, content="帮我设定画像")]
    async for _ in agent.run(messages, phase=3):
        pass

    assert plan.phase3_step == "candidate"
    assert flushed[0]["from_phase"] == 3
    assert flushed[0]["from_phase3_step"] == "brief"
    assert "帮我设定画像" in flushed[0]["contents"]
    assert observed_second_call["messages"][0].startswith("system phase=3")


@pytest.mark.asyncio
async def test_pre_rebuild_flush_failure_logs_warning_and_rebuilds(caplog):
    plan = TravelPlanState(session_id="s1", phase=1)

    @tool(
        name="advance_phase",
        description="advance",
        phases=[1],
        parameters={"type": "object", "properties": {}},
    )
    async def advance_phase() -> dict:
        plan.phase = 3
        return {"destination": "东京"}

    engine = ToolEngine()
    engine.register(advance_phase)
    call_index = 0
    observed_second_call: dict[str, object] = {}

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_index
        call_index += 1
        if call_index == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(id="tc1", name="advance_phase", arguments={}),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return
        observed_second_call["messages"] = [message.content for message in messages]
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="phase 3 ready")
        yield LLMChunk(type=ChunkType.DONE)

    async def failing_flush(*, messages, from_phase, from_phase3_step):
        raise RuntimeError("disk unavailable")

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=3,
        phase_router=FakePhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u1",
        on_before_message_rebuild=failing_flush,
    )

    with caplog.at_level("WARNING"):
        async for _ in agent.run([Message(role=Role.USER, content="去东京")], phase=1):
            pass

    assert observed_second_call["messages"] == [
        "system phase=3 prompt=phase-3-prompt user=memory:u1 tools=",
        "handoff 1->3 phase=3",
        "去东京",
    ]
    assert "pre-rebuild message history flush failed" in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_agent_loop.py::test_phase_transition_flushes_messages_before_rebuild tests/test_agent_loop.py::test_phase3_step_change_flushes_messages_before_rebuild tests/test_agent_loop.py::test_pre_rebuild_flush_failure_logs_warning_and_rebuilds -q
```

Expected: FAIL with `TypeError: AgentLoop.__init__() got an unexpected keyword argument 'on_before_message_rebuild'`.

- [ ] **Step 3: Implement callback on AgentLoop**

In `backend/agent/loop.py`, add imports:

```python
import logging
from collections.abc import Awaitable, Callable
```

Add module logger:

```python
logger = logging.getLogger(__name__)
```

Add constructor parameter:

```python
        on_before_message_rebuild: Callable[..., Awaitable[None]] | None = None,
```

Store it:

```python
        self.on_before_message_rebuild = on_before_message_rebuild
```

Add helper:

```python
    async def _flush_before_message_rebuild(
        self,
        *,
        messages: list[Message],
        from_phase: int,
        from_phase3_step: str | None,
    ) -> None:
        if self.on_before_message_rebuild is None:
            return
        try:
            await self.on_before_message_rebuild(
                messages=messages,
                from_phase=from_phase,
                from_phase3_step=from_phase3_step,
            )
        except Exception:
            logger.warning(
                "pre-rebuild message history flush failed phase=%s phase3_step=%s",
                from_phase,
                from_phase3_step,
                exc_info=True,
            )
```

In `_handle_phase_transition()`, before `_rebuild_messages_for_phase_change()`:

```python
        await self._flush_before_message_rebuild(
            messages=messages,
            from_phase=request.from_phase,
            from_phase3_step=request.from_step,
        )
```

In the Phase 3 step-change branch inside `run()`, before `_rebuild_messages_for_phase3_step_change()`:

```python
                        await self._flush_before_message_rebuild(
                            messages=messages,
                            from_phase=current_phase,
                            from_phase3_step=phase3_step_before_batch,
                        )
```

In `backend/api/orchestration/agent/builder.py`, add optional parameter:

```python
    on_before_message_rebuild=None,
```

Pass it to `AgentLoop(...)`:

```python
        on_before_message_rebuild=on_before_message_rebuild,
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_agent_loop.py::test_phase_transition_flushes_messages_before_rebuild tests/test_agent_loop.py::test_phase3_step_change_flushes_messages_before_rebuild tests/test_agent_loop.py::test_pre_rebuild_flush_failure_logs_warning_and_rebuilds -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agent/loop.py backend/api/orchestration/agent/builder.py backend/tests/test_agent_loop.py PROJECT_OVERVIEW.md
git commit -m "feat: flush history before message rebuild"
```

## Task 4: Chat Finalization, Cancellation, And Active Frontend View

**Files:**
- Modify: `backend/api/orchestration/chat/finalization.py`
- Modify: `backend/api/orchestration/chat/stream.py`
- Modify: `backend/api/routes/chat_routes.py`
- Modify: `backend/api/routes/session_routes.py`
- Test: `backend/tests/test_api.py`

- [ ] **Step 1: Write failing finalization and API behavior tests**

Append these tests to `backend/tests/test_api.py`:

```python
@pytest.mark.asyncio
async def test_chat_finalization_does_not_duplicate_preflushed_messages(app):
    async def fake_run(self, messages, phase, tools_override=None):
        messages.append(Message(role=Role.ASSISTANT, content="查了小红书，东京适合"))
        await self.on_before_message_rebuild(
            messages=messages,
            from_phase=1,
            from_phase3_step=None,
        )
        messages[:] = [
            Message(role=Role.SYSTEM, content="phase 3 system"),
            Message(role=Role.ASSISTANT, content="进入方案设计"),
            messages[-2],
        ]
        messages.append(Message(role=Role.ASSISTANT, content="继续问预算"))
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="继续问预算")
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            await client.post(f"/api/chat/{session_id}", json={"message": "去东京"})

            from main import message_store

            rows = await message_store.load_all(session_id)

    assert [row["history_seq"] for row in rows] == list(range(len(rows)))
    assert [row["content"] for row in rows].count("去东京") == 1
    assert [row["content"] for row in rows].count("查了小红书，东京适合") == 1
    assert [row["content"] for row in rows].count("继续问预算") == 1


@pytest.mark.asyncio
async def test_api_messages_uses_active_runtime_view_not_full_internal_history(app):
    async def fake_run(self, messages, phase, tools_override=None):
        messages.append(Message(role=Role.ASSISTANT, content="旧阶段工具结论"))
        await self.on_before_message_rebuild(
            messages=messages,
            from_phase=1,
            from_phase3_step=None,
        )
        messages[:] = [
            Message(role=Role.SYSTEM, content="phase 3 system"),
            Message(role=Role.ASSISTANT, content="进入方案设计"),
            messages[-2],
        ]
        messages.append(Message(role=Role.ASSISTANT, content="当前 runtime 回复"))
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="当前 runtime 回复")
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            await client.post(f"/api/chat/{session_id}", json={"message": "去东京"})
            messages_resp = await client.get(f"/api/messages/{session_id}")

    payload = messages_resp.json()
    assert messages_resp.status_code == 200
    assert [message["role"] for message in payload] == ["assistant", "user", "assistant"]
    assert [message["content"] for message in payload] == [
        "进入方案设计",
        "去东京",
        "当前 runtime 回复",
    ]


@pytest.mark.asyncio
async def test_cancelled_stream_persists_unflushed_messages_once(app):
    async def fake_run(self, messages, phase, tools_override=None):
        messages.append(Message(role=Role.ASSISTANT, content="开始查"))
        await self.on_before_message_rebuild(
            messages=messages,
            from_phase=1,
            from_phase3_step=None,
        )
        messages.append(Message(role=Role.ASSISTANT, content="取消前新增"))
        self.cancel_event.set()
        raise asyncio.CancelledError()

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]

            with pytest.raises((asyncio.CancelledError, AssertionError)):
                await client.post(f"/api/chat/{session_id}", json={"message": "去东京"})

            from main import message_store

            rows = await message_store.load_all(session_id)

    assert [row["history_seq"] for row in rows] == list(range(len(rows)))
    assert [row["content"] for row in rows].count("去东京") == 1
    assert [row["content"] for row in rows].count("开始查") == 1
    assert [row["content"] for row in rows].count("取消前新增") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_api.py::test_chat_finalization_does_not_duplicate_preflushed_messages tests/test_api.py::test_api_messages_uses_active_runtime_view_not_full_internal_history tests/test_api.py::test_cancelled_stream_persists_unflushed_messages_once -q
```

Expected: FAIL because `on_before_message_rebuild` is not installed on the per-run agent and finalization still calls the old `persist_messages(plan.session_id, messages)` signature.

- [ ] **Step 3: Implement shared unflushed persistence helper**

In `backend/api/orchestration/chat/finalization.py`, add:

```python
async def persist_unflushed_messages(
    *,
    deps,
    session,
    plan,
    messages,
    phase: int,
    phase3_step: str | None,
    run_id: str | None,
    trip_id: str | None,
) -> None:
    next_history_seq = int(session.get("next_history_seq", 0))
    next_history_seq = await deps.persist_messages(
        plan.session_id,
        messages,
        phase=phase,
        phase3_step=phase3_step,
        run_id=run_id,
        trip_id=trip_id,
        next_history_seq=next_history_seq,
    )
    session["next_history_seq"] = next_history_seq
```

Replace both direct `deps.persist_messages(plan.session_id, messages)` calls:

```python
    await persist_unflushed_messages(
        deps=deps,
        session=session,
        plan=plan,
        messages=messages,
        phase=plan.phase,
        phase3_step=getattr(plan, "phase3_step", None),
        run_id=run.run_id,
        trip_id=getattr(plan, "trip_id", None),
    )
```

In `persist_run_safely()`, use the same helper after setting cancelled status.

- [ ] **Step 4: Install the pre-rebuild callback in chat and continue routes**

In `backend/api/routes/session_routes.py`, add `next_history_seq` to new sessions:

```python
            "next_history_seq": 0,
```

In `backend/api/routes/chat_routes.py`, import helper:

```python
from api.orchestration.chat.finalization import persist_unflushed_messages
```

After each `RunRecord` is created in `/api/chat/{session_id}` and `/continue`, install:

```python
            async def _flush_before_rebuild(*, messages, from_phase, from_phase3_step):
                await persist_unflushed_messages(
                    deps=chat_stream_deps,
                    session=session,
                    plan=plan,
                    messages=messages,
                    phase=from_phase,
                    phase3_step=from_phase3_step,
                    run_id=run.run_id,
                    trip_id=getattr(plan, "trip_id", None),
                )

            agent.on_before_message_rebuild = _flush_before_rebuild
```

For the fallback keyword backtrack branch in `backend/api/orchestration/chat/stream.py`, flush before `rotate_trip_on_reset_backtrack()` because that path rotates `trip_id` outside the AgentLoop rebuild callback:

```python
                await persist_unflushed_messages(
                    deps=deps,
                    session=session,
                    plan=plan,
                    messages=messages,
                    phase=from_phase,
                    phase3_step=getattr(plan, "phase3_step", None),
                    run_id=run.run_id,
                    trip_id=getattr(plan, "trip_id", None),
                )
```

Place this immediately after `from_phase = plan.phase` and before `deps.phase_router.prepare_backtrack(...)`.

- [ ] **Step 5: Keep `/api/messages` frontend view stable**

In `backend/api/routes/session_routes.py`, add a local serializer near `get_messages()`:

```python
    def _serialize_message_row(row):
        return {
            "role": row["role"],
            "content": row["content"],
            "tool_calls": (
                json.loads(row["tool_calls"]) if row.get("tool_calls") else None
            ),
            "tool_call_id": row.get("tool_call_id"),
            "seq": row["seq"],
        }

    def _serialize_runtime_message(message, index: int):
        tool_calls = None
        if message.tool_calls:
            tool_calls = [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "human_label": call.human_label,
                }
                for call in message.tool_calls
            ]
        tool_call_id = (
            message.tool_result.tool_call_id if message.tool_result is not None else None
        )
        return {
            "role": message.role.value,
            "content": message.content,
            "tool_calls": tool_calls,
            "tool_call_id": tool_call_id,
            "seq": index,
        }
```

Update `get_messages()`:

```python
        session = sessions.get(session_id)
        if session is not None:
            return [
                _serialize_runtime_message(message, index)
                for index, message in enumerate(session.get("messages", []))
                if message.role.value != "system"
            ]
        rows = await message_store.load_frontend_view(session_id)
        return [_serialize_message_row(row) for row in rows]
```

Do not expose `history_seq`, `phase`, `phase3_step`, `run_id`, or `trip_id` from this endpoint in Phase 1.

- [ ] **Step 6: Run tests to verify they pass**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_api.py::test_chat_finalization_does_not_duplicate_preflushed_messages tests/test_api.py::test_api_messages_uses_active_runtime_view_not_full_internal_history tests/test_api.py::test_cancelled_stream_persists_unflushed_messages_once tests/test_api.py::test_chat_persists_messages_when_stream_is_cancelled -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/api/orchestration/chat/finalization.py backend/api/orchestration/chat/stream.py backend/api/routes/chat_routes.py backend/api/routes/session_routes.py backend/tests/test_api.py PROJECT_OVERVIEW.md
git commit -m "feat: persist unflushed history across chat finalization"
```

## Task 5: End-To-End Phase History Preservation Regression

**Files:**
- Create: `backend/tests/test_context_history_preservation.py`

- [ ] **Step 1: Write failing integration tests**

Create `backend/tests/test_context_history_preservation.py`:

```python
import json

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk


@pytest.mark.asyncio
async def test_phase1_to_phase3_to_phase5_preserves_phase1_tool_history_in_db(app):
    async def fake_run(self, messages, phase, tools_override=None):
        # Simulate Phase 1 tool-supported destination convergence.
        phase1_call = ToolCall(
            id="tc_phase1_search",
            name="quick_travel_search",
            arguments={"query": "东京亲子游"},
        )
        messages.append(
            Message(
                role=Role.ASSISTANT,
                content=None,
                tool_calls=[phase1_call],
            )
        )
        messages.append(
            Message(
                role=Role.TOOL,
                tool_result=ToolResult(
                    tool_call_id="tc_phase1_search",
                    status="success",
                    data={"summary": "东京适合亲子游，有博物馆和公园"},
                ),
            )
        )
        messages.append(Message(role=Role.ASSISTANT, content="东京比较适合"))
        self.plan.phase = 3
        self.plan.phase3_step = "brief"
        await self.on_before_message_rebuild(
            messages=messages,
            from_phase=1,
            from_phase3_step=None,
        )

        # Simulate Phase 3 brief -> candidate rebuild.
        messages[:] = [
            Message(role=Role.SYSTEM, content="phase 3 brief system"),
            messages[0],
        ]
        messages.append(Message(role=Role.ASSISTANT, content="我来建立画像"))
        self.plan.phase3_step = "candidate"
        await self.on_before_message_rebuild(
            messages=messages,
            from_phase=3,
            from_phase3_step="brief",
        )

        # Simulate Phase 3 -> 5 rebuild.
        messages[:] = [
            Message(role=Role.SYSTEM, content="phase 3 candidate system"),
            messages[1],
        ]
        messages.append(Message(role=Role.ASSISTANT, content="候选池已收敛"))
        self.plan.phase = 5
        await self.on_before_message_rebuild(
            messages=messages,
            from_phase=3,
            from_phase3_step="candidate",
        )
        messages[:] = [
            Message(role=Role.SYSTEM, content="phase 5 system"),
            messages[1],
        ]
        messages.append(Message(role=Role.ASSISTANT, content="进入日程组装"))
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="进入日程组装")
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            await client.post(f"/api/chat/{session_id}", json={"message": "亲子游去哪"})

            from main import message_store

            rows = await message_store.load_all(session_id)
            frontend_resp = await client.get(f"/api/messages/{session_id}")

    phase1_rows = [row for row in rows if row["phase"] == 1]
    assert [row["history_seq"] for row in rows] == list(range(len(rows)))
    assert any(row["role"] == "assistant" and row["tool_calls"] for row in phase1_rows)
    assert any(row["role"] == "tool" and row["tool_call_id"] == "tc_phase1_search" for row in phase1_rows)
    assert any("东京比较适合" in (row["content"] or "") for row in phase1_rows)
    assert any(row["phase"] == 3 and row["phase3_step"] == "brief" for row in rows)
    assert any(row["phase"] == 3 and row["phase3_step"] == "candidate" for row in rows)
    assert rows[-1]["phase"] == 5

    frontend_payload = frontend_resp.json()
    assert frontend_resp.status_code == 200
    assert all(message["role"] != "system" for message in frontend_payload)
    assert [message["content"] for message in frontend_payload] == [
        "亲子游去哪",
        "进入日程组装",
    ]


@pytest.mark.asyncio
async def test_phase_history_rows_carry_run_id_and_trip_id(app):
    async def fake_run(self, messages, phase, tools_override=None):
        messages.append(Message(role=Role.ASSISTANT, content="第一轮回复"))
        await self.on_before_message_rebuild(
            messages=messages,
            from_phase=1,
            from_phase3_step=None,
        )
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="第一轮回复")
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            await client.post(f"/api/chat/{session_id}", json={"message": "去东京"})

            from main import message_store, state_mgr

            plan = await state_mgr.load(session_id)
            rows = await message_store.load_all(session_id)

    run_ids = {row["run_id"] for row in rows}
    trip_ids = {row["trip_id"] for row in rows}
    assert len(run_ids) == 1
    assert None not in run_ids
    assert trip_ids == {plan.trip_id}
```

- [ ] **Step 2: Run tests to verify they fail before full integration**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_context_history_preservation.py -q
```

Expected before Tasks 1-4 are complete: FAIL. Expected after Tasks 1-4 are complete: PASS.

- [ ] **Step 3: Run focused persistence/API regression suite**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_storage_database.py tests/test_storage_message.py tests/test_session_persistence.py tests/test_agent_loop.py::test_phase_transition_flushes_messages_before_rebuild tests/test_agent_loop.py::test_phase3_step_change_flushes_messages_before_rebuild tests/test_agent_loop.py::test_pre_rebuild_flush_failure_logs_warning_and_rebuilds tests/test_api.py::test_chat_persists_messages_when_stream_is_cancelled tests/test_api.py::test_chat_finalization_does_not_duplicate_preflushed_messages tests/test_api.py::test_api_messages_uses_active_runtime_view_not_full_internal_history tests/test_api.py::test_cancelled_stream_persists_unflushed_messages_once tests/test_context_history_preservation.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_context_history_preservation.py PROJECT_OVERVIEW.md
git commit -m "test: preserve phase history across context rebuilds"
```

## Task 6: Project Overview And Full Verification

**Files:**
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: Update project overview**

In `PROJECT_OVERVIEW.md`, update the session/context persistence sections to include:

```markdown
- 会话消息持久化采用 append-only history：SQLite `messages` 是完整历史事实源，`session["messages"]` 仍是可被 phase rebuild 替换的短 runtime prompt 工作集。
- 每条新历史消息写入 `history_seq`（session 内单调递增）、`phase`、`phase3_step`、`run_id`、`trip_id`；`next_history_seq` 存在 session dict 中，恢复时从数据库 `MAX(history_seq) + 1` 初始化。
- Phase 前进、Phase 3 子步骤切换和 backtrack runtime rebuild 前，`AgentLoop` 通过 pre-rebuild callback flush 旧 runtime 消息，避免旧阶段 assistant/tool/tool result 在 rebuild 后丢失。
- `/api/messages/{session_id}` 在一期仍返回前端聊天窗口可消费视图，不作为完整内部 history/debug API；完整历史由 `MessageStore.load_all()` 内部读取。
```

Keep the overview current-state oriented. Do not describe this as a future task or migration history.

- [ ] **Step 2: Run the targeted suite**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_storage_database.py tests/test_storage_message.py tests/test_session_persistence.py tests/test_agent_loop.py::test_phase_transition_flushes_messages_before_rebuild tests/test_agent_loop.py::test_phase3_step_change_flushes_messages_before_rebuild tests/test_agent_loop.py::test_pre_rebuild_flush_failure_logs_warning_and_rebuilds tests/test_api.py::test_chat_persists_messages_when_stream_is_cancelled tests/test_api.py::test_chat_finalization_does_not_duplicate_preflushed_messages tests/test_api.py::test_api_messages_uses_active_runtime_view_not_full_internal_history tests/test_api.py::test_cancelled_stream_persists_unflushed_messages_once tests/test_context_history_preservation.py -q
```

Expected: PASS.

- [ ] **Step 3: Run broader backend regression around affected surfaces**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend
pytest tests/test_api.py tests/test_phase_integration.py tests/test_session_restore.py tests/test_agent_loop.py tests/test_storage_database.py tests/test_storage_message.py tests/test_session_persistence.py tests/test_context_history_preservation.py -q
```

Expected: PASS.

- [ ] **Step 4: Verify no runtime-count cursor remains**

Run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro
rg -n "persisted_message_count|len\\(messages\\).*persist|persist_messages\\(plan\\.session_id, messages\\)" backend
```

Expected: no matches for `persisted_message_count`, no persistence cursor based on `len(messages)`, and no old two-argument `persist_messages(plan.session_id, messages)` calls.

- [ ] **Step 5: Commit**

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: document append-only context history persistence"
```

## Self-Review Checklist For Implementer

- [ ] `persist_messages()` never executes `DELETE FROM messages`.
- [ ] `history_seq` is assigned only from `session["next_history_seq"]`, never from runtime list length.
- [ ] Pre-rebuild flush runs before phase rebuild and Phase 3 step rebuild.
- [ ] Pre-rebuild flush failure logs a warning and does not block the live response.
- [ ] Normal finalization and `finally` persistence are idempotent because already flushed messages have `history_persisted=True`.
- [ ] Cancellation/error paths persist unflushed messages once.
- [ ] Reset/backtrack paths flush old runtime messages before `trip_id` rotation when rotation happens outside the AgentLoop rebuild callback.
- [ ] `/api/messages/{session_id}` does not expose `phase`, `phase3_step`, `history_seq`, `run_id`, or `trip_id`.
- [ ] Runtime prompt rebuild outputs stay the same as existing AgentLoop tests expect.
- [ ] Phase 1 -> 3 -> 5 integration test proves Phase 1 assistant/tool/tool result rows remain in DB.
- [ ] `PROJECT_OVERVIEW.md` reflects current architecture before the final commit.
