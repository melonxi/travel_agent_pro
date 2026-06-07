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
