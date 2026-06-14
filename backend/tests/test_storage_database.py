import sqlite3

import aiosqlite
import pytest
import pytest_asyncio

from storage.database import Database


@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_initialize_creates_tables(db: Database):
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    table_names = [row["name"] for row in rows]
    assert "sessions" in table_names
    assert "messages" in table_names
    assert "plan_snapshots" in table_names
    assert "archives" in table_names


@pytest.mark.asyncio
async def test_initialize_creates_trace_tables(db: Database):
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    table_names = {row["name"] for row in rows}

    assert "trace_runs" in table_names
    assert "trace_events" in table_names
    assert "trace_artifacts" in table_names
    assert "trace_grades" in table_names


@pytest.mark.asyncio
async def test_trace_schema_contains_expected_indexes(db: Database):
    trace_run_indexes = {
        row["name"] for row in await db.fetch_all("PRAGMA index_list(trace_runs)")
    }
    trace_event_indexes = {
        row["name"] for row in await db.fetch_all("PRAGMA index_list(trace_events)")
    }
    trace_grade_indexes = {
        row["name"] for row in await db.fetch_all("PRAGMA index_list(trace_grades)")
    }
    trace_artifact_indexes = {
        row["name"] for row in await db.fetch_all("PRAGMA index_list(trace_artifacts)")
    }

    assert "idx_trace_runs_session_created" in trace_run_indexes
    assert "idx_trace_runs_trip" in trace_run_indexes
    assert "idx_trace_events_run_sequence" in trace_event_indexes
    assert "idx_trace_events_type" in trace_event_indexes
    assert "idx_trace_events_tool" in trace_event_indexes
    assert "idx_trace_events_session_created" in trace_event_indexes
    assert "idx_trace_events_session_run_sequence" in trace_event_indexes
    assert "idx_trace_events_correlation" in trace_event_indexes
    assert "idx_trace_events_parent" in trace_event_indexes
    assert "idx_trace_events_root" in trace_event_indexes
    assert "idx_trace_events_context_epoch" in trace_event_indexes
    assert "idx_trace_events_actor" in trace_event_indexes
    assert "idx_trace_artifacts_run" in trace_artifact_indexes
    assert "idx_trace_artifacts_event" in trace_artifact_indexes
    assert "idx_trace_artifacts_hash" in trace_artifact_indexes
    assert "idx_trace_artifacts_kind" in trace_artifact_indexes
    assert "idx_trace_grades_run" in trace_grade_indexes
    assert "idx_trace_grades_status" in trace_grade_indexes


@pytest.mark.asyncio
async def test_trace_schema_has_flight_recorder_columns(db: Database):
    run_columns = {
        row["name"] for row in await db.fetch_all("PRAGMA table_info(trace_runs)")
    }
    event_columns = {
        row["name"] for row in await db.fetch_all("PRAGMA table_info(trace_events)")
    }

    assert {
        "config_hash",
        "prompt_version",
        "model_config_json",
        "tool_schema_hash",
        "trace_schema_version",
    } <= run_columns
    assert {
        "session_id",
        "trip_id",
        "context_epoch",
        "parent_event_id",
        "root_event_id",
        "correlation_id",
        "actor",
        "started_at",
        "ended_at",
        "payload_schema_version",
    } <= event_columns


@pytest.mark.asyncio
async def test_initialize_is_idempotent(db: Database):
    await db.initialize()
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_execute_and_fetch(db: Database):
    await db.execute(
        "INSERT INTO sessions (session_id, user_id, title, phase, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "s1",
            "u1",
            "test",
            1,
            "active",
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
        ),
    )
    row = await db.fetch_one("SELECT * FROM sessions WHERE session_id = ?", ("s1",))
    assert row is not None
    assert row["session_id"] == "s1"
    assert row["user_id"] == "u1"


@pytest.mark.asyncio
async def test_initialize_migrates_legacy_sessions_schema(tmp_path):
    db_path = tmp_path / "legacy.db"
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
        """
    )
    conn.commit()
    conn.close()

    database = Database(str(db_path))
    await database.initialize()
    columns = await database.fetch_all("PRAGMA table_info(sessions)")
    await database.close()

    column_names = {column["name"] for column in columns}
    assert "last_run_id" in column_names
    assert "last_run_status" in column_names
    assert "last_run_error" in column_names


@pytest.mark.asyncio
async def test_initialize_migrates_legacy_messages_schema(tmp_path):
    db_path = tmp_path / "legacy-messages.db"
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
    await database.close()

    column_names = {column["name"] for column in columns}
    assert "provider_state" in column_names


@pytest.mark.asyncio
async def test_messages_schema_contains_history_columns_and_indexes(db: Database):
    columns = await db.fetch_all("PRAGMA table_info(messages)")
    column_names = {column["name"] for column in columns}

    assert {"phase", "phase2_step", "history_seq", "run_id", "trip_id"} <= column_names

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
    assert {"phase", "phase2_step", "history_seq", "run_id", "trip_id"} <= column_names
    assert "idx_messages_history" in index_names
    assert "idx_messages_phase" in index_names
    assert "idx_messages_session_history_unique" in index_names


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
async def test_initialize_migrates_legacy_database_with_trace_tables(tmp_path):
    db_path = tmp_path / "legacy-no-trace.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
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
            phase2_step TEXT,
            history_seq INTEGER,
            run_id TEXT,
            trip_id TEXT,
            context_epoch INTEGER,
            rebuild_reason TEXT,
            created_at TEXT NOT NULL,
            seq INTEGER NOT NULL
        );
        CREATE TABLE plan_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            phase INTEGER NOT NULL,
            plan_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE archives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            summary TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    database = Database(str(db_path))
    await database.initialize()
    try:
        rows = await database.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    finally:
        await database.close()

    table_names = {row["name"] for row in rows}
    assert {"trace_runs", "trace_events", "trace_grades"} <= table_names


@pytest.mark.asyncio
async def test_initialize_migrates_legacy_trace_schema_without_losing_rows(tmp_path):
    db_path = tmp_path / "legacy-trace-schema.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'default_user',
            title TEXT,
            phase INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE trace_runs (
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            trip_id TEXT,
            context_epoch INTEGER,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL,
            final_phase INTEGER,
            final_phase2_step TEXT,
            total_input_tokens INTEGER NOT NULL DEFAULT 0,
            total_output_tokens INTEGER NOT NULL DEFAULT 0,
            total_cost_usd REAL NOT NULL DEFAULT 0,
            total_duration_ms REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE trace_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            phase INTEGER,
            phase2_step TEXT,
            iteration INTEGER,
            tool_name TEXT,
            llm_provider TEXT,
            llm_model TEXT,
            status TEXT,
            duration_ms REAL,
            cost_usd REAL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, sequence)
        );
        INSERT INTO sessions
        (session_id, user_id, title, phase, status, created_at, updated_at)
        VALUES ('session-legacy', 'default_user', 'Legacy', 1, 'active',
                '2026-06-07T10:00:00+00:00', '2026-06-07T10:00:00+00:00');
        INSERT INTO trace_runs
        (run_id, session_id, trip_id, context_epoch, started_at, status,
         created_at, updated_at)
        VALUES ('run-legacy', 'session-legacy', NULL, 0,
                '2026-06-07T10:00:00+00:00', 'completed',
                '2026-06-07T10:00:00+00:00', '2026-06-07T10:00:00+00:00');
        INSERT INTO trace_events
        (event_id, run_id, sequence, event_type, phase, phase2_step, iteration,
         tool_name, llm_provider, llm_model, status, duration_ms, cost_usd,
         payload_json, created_at)
        VALUES ('evt-legacy', 'run-legacy', 1, 'tool_call', 1, NULL, NULL,
                'web_search', NULL, NULL, 'success', 12.0, NULL,
                '{"tool_name":"web_search"}', '2026-06-07T10:00:01+00:00');
        """
    )
    conn.commit()
    conn.close()

    database = Database(str(db_path))
    await database.initialize()
    try:
        run_columns = {
            row["name"]
            for row in await database.fetch_all("PRAGMA table_info(trace_runs)")
        }
        event_columns = {
            row["name"]
            for row in await database.fetch_all("PRAGMA table_info(trace_events)")
        }
        artifact_tables = await database.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trace_artifacts'"
        )
        row = await database.fetch_one(
            "SELECT * FROM trace_events WHERE event_id = ?",
            ("evt-legacy",),
        )
    finally:
        await database.close()

    assert "trace_schema_version" in run_columns
    assert "payload_schema_version" in event_columns
    assert artifact_tables
    assert row is not None
    assert row["event_id"] == "evt-legacy"
    assert row["session_id"] is None
    assert row["payload_schema_version"] is None


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
                phase2_step TEXT,
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
