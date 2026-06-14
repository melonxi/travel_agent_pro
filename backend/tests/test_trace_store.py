import json

import pytest
import pytest_asyncio

from evals.trace_models import RubricResult, TraceEvent
from storage.database import Database
from storage.trace_store import TraceStore


async def _insert_session(db: Database, session_id: str = "session-1") -> None:
    await db.execute(
        "INSERT INTO sessions (session_id, user_id, title, phase, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            "default_user",
            "Trace Test",
            1,
            "active",
            "2026-06-07T10:00:00+00:00",
            "2026-06-07T10:00:00+00:00",
        ),
    )


@pytest_asyncio.fixture
async def trace_store():
    db = Database(":memory:")
    await db.initialize()
    await _insert_session(db)
    store = TraceStore(db)
    yield store
    await db.close()


@pytest.mark.asyncio
async def test_create_and_load_trace_run(trace_store: TraceStore):
    await trace_store.create_run(
        run_id="run-1",
        session_id="session-1",
        trip_id="trip-1",
        context_epoch=3,
        started_at="2026-06-07T10:00:00+00:00",
        status="running",
    )

    row = await trace_store.load_run("run-1")

    assert row is not None
    assert row["run_id"] == "run-1"
    assert row["session_id"] == "session-1"
    assert row["trip_id"] == "trip-1"
    assert row["context_epoch"] == 3
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_replace_events_is_idempotent(trace_store: TraceStore):
    await trace_store.create_run(
        run_id="run-1",
        session_id="session-1",
        trip_id=None,
        context_epoch=None,
        started_at="2026-06-07T10:00:00+00:00",
        status="running",
    )
    first = TraceEvent(
        event_id="evt-1",
        run_id="run-1",
        sequence=1,
        event_type="tool_call",
        phase=1,
        phase2_step=None,
        iteration=None,
        tool_name="web_search",
        llm_provider=None,
        llm_model=None,
        status="success",
        duration_ms=10.0,
        cost_usd=None,
        payload={"tool_name": "web_search"},
        created_at="2026-06-07T10:00:01+00:00",
    )
    second = TraceEvent(
        event_id="evt-2",
        run_id="run-1",
        sequence=1,
        event_type="tool_call",
        phase=1,
        phase2_step=None,
        iteration=None,
        tool_name="quick_travel_search",
        llm_provider=None,
        llm_model=None,
        status="success",
        duration_ms=20.0,
        cost_usd=None,
        payload={"tool_name": "quick_travel_search"},
        created_at="2026-06-07T10:00:02+00:00",
    )

    await trace_store.replace_events("run-1", [first])
    await trace_store.replace_events("run-1", [second])
    events = await trace_store.load_events("run-1")

    assert len(events) == 1
    assert events[0]["event_id"] == "evt-2"
    assert json.loads(events[0]["payload_json"])["tool_name"] == "quick_travel_search"
    assert events[0]["payload_schema_version"] == 1


@pytest.mark.asyncio
async def test_append_event_persists_common_fields_and_queries(trace_store: TraceStore):
    await trace_store.create_run(
        run_id="run-1",
        session_id="session-1",
        trip_id="trip-1",
        context_epoch=2,
        started_at="2026-06-07T10:00:00+00:00",
        status="running",
        config_hash="sha256:config",
        prompt_version="p1",
        model_config_json='{"model":"gpt-4o"}',
        tool_schema_hash="sha256:tools",
    )
    event = TraceEvent(
        event_id="evt-1",
        run_id="run-1",
        sequence=1,
        event_type="tool_call",
        phase=2,
        phase2_step="brief",
        iteration=1,
        tool_name="web_search",
        llm_provider=None,
        llm_model=None,
        status="success",
        duration_ms=11.0,
        cost_usd=None,
        payload={"schema_version": 2, "arguments_hash": "sha256:args"},
        created_at="2026-06-07T10:00:01+00:00",
        session_id="session-1",
        trip_id="trip-1",
        context_epoch=2,
        parent_event_id="evt-parent",
        root_event_id="evt-root",
        correlation_id="corr-1",
        actor="tool_engine",
        started_at="2026-06-07T10:00:00+00:00",
        ended_at="2026-06-07T10:00:01+00:00",
        payload_schema_version=2,
    )

    await trace_store.append_event(event)

    run = await trace_store.load_run("run-1")
    events = await trace_store.load_events("run-1")
    session_events = await trace_store.load_events_by_session("session-1")
    correlation_events = await trace_store.load_events_by_correlation("run-1", "corr-1")

    assert run is not None
    assert run["config_hash"] == "sha256:config"
    assert run["prompt_version"] == "p1"
    assert run["tool_schema_hash"] == "sha256:tools"
    assert events == session_events == correlation_events
    assert events[0]["session_id"] == "session-1"
    assert events[0]["trip_id"] == "trip-1"
    assert events[0]["context_epoch"] == 2
    assert events[0]["parent_event_id"] == "evt-parent"
    assert events[0]["root_event_id"] == "evt-root"
    assert events[0]["correlation_id"] == "corr-1"
    assert events[0]["actor"] == "tool_engine"
    assert events[0]["payload_schema_version"] == 2


@pytest.mark.asyncio
async def test_append_events_rolls_back_atomically_on_duplicate_sequence(
    trace_store: TraceStore,
):
    await trace_store.create_run(
        run_id="run-1",
        session_id="session-1",
        trip_id=None,
        context_epoch=None,
        started_at="2026-06-07T10:00:00+00:00",
        status="running",
    )
    first = TraceEvent(
        event_id="evt-1",
        run_id="run-1",
        sequence=1,
        event_type="internal_task",
        phase=None,
        phase2_step=None,
        iteration=None,
        tool_name=None,
        llm_provider=None,
        llm_model=None,
        status="success",
        duration_ms=None,
        cost_usd=None,
        payload={},
        created_at="2026-06-07T10:00:01+00:00",
    )
    duplicate_sequence = TraceEvent(
        event_id="evt-2",
        run_id="run-1",
        sequence=1,
        event_type="internal_task",
        phase=None,
        phase2_step=None,
        iteration=None,
        tool_name=None,
        llm_provider=None,
        llm_model=None,
        status="success",
        duration_ms=None,
        cost_usd=None,
        payload={},
        created_at="2026-06-07T10:00:02+00:00",
    )

    with pytest.raises(Exception):
        await trace_store.append_events([first, duplicate_sequence])

    assert await trace_store.load_events("run-1") == []


@pytest.mark.asyncio
async def test_artifact_metadata_save_and_load_by_run_and_event(
    trace_store: TraceStore,
):
    await trace_store.create_run(
        run_id="run-1",
        session_id="session-1",
        trip_id=None,
        context_epoch=None,
        started_at="2026-06-07T10:00:00+00:00",
        status="running",
    )
    event = TraceEvent(
        event_id="evt-1",
        run_id="run-1",
        sequence=1,
        event_type="tool_call",
        phase=None,
        phase2_step=None,
        iteration=None,
        tool_name="web_search",
        llm_provider=None,
        llm_model=None,
        status="success",
        duration_ms=None,
        cost_usd=None,
        payload={},
        created_at="2026-06-07T10:00:01+00:00",
        session_id="session-1",
    )
    await trace_store.append_event(event)

    metadata = {
        "artifact_id": "artifact-1",
        "run_id": "run-1",
        "event_id": "evt-1",
        "kind": "tool_arguments",
        "content_type": "application/json",
        "content_hash": "sha256:abc",
        "redaction_status": "redacted",
        "storage_path": "trace_artifacts/session-1/run-1/artifact-1.json",
        "size_bytes": 42,
        "created_at": "2026-06-07T10:00:02+00:00",
    }

    await trace_store.save_artifact_metadata(metadata)

    by_run = await trace_store.load_artifact_metadata("run-1")
    by_event = await trace_store.load_artifact_metadata("run-1", event_id="evt-1")

    assert by_run == by_event
    assert by_run[0]["artifact_id"] == "artifact-1"
    assert by_run[0]["kind"] == "tool_arguments"
    assert by_run[0]["content_hash"] == "sha256:abc"


@pytest.mark.asyncio
async def test_save_grades_upserts_by_run_and_rubric(trace_store: TraceStore):
    await trace_store.create_run(
        run_id="run-1",
        session_id="session-1",
        trip_id=None,
        context_epoch=None,
        started_at="2026-06-07T10:00:00+00:00",
        status="completed",
    )
    first = RubricResult(
        rubric_id="state_write_uses_plan_writer",
        status="fail",
        score=0,
        reason="read tool changed state",
        evidence_event_ids=["evt-1"],
    )
    second = RubricResult(
        rubric_id="state_write_uses_plan_writer",
        status="pass",
        score=1,
        reason="all state changes came from writer tools",
        evidence_event_ids=["evt-2"],
    )

    await trace_store.save_grades("run-1", [first])
    await trace_store.save_grades("run-1", [second])
    grades = await trace_store.load_grades("run-1")

    assert len(grades) == 1
    assert grades[0]["rubric_id"] == "state_write_uses_plan_writer"
    assert grades[0]["status"] == "pass"
    assert json.loads(grades[0]["evidence_event_ids_json"]) == ["evt-2"]
