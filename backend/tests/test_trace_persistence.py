import json

import pytest
import pytest_asyncio

from api.orchestration.chat.trace_persistence import (
    build_trace_events_from_stats,
    persist_trace_run_safely,
)
from run import RunRecord
from state.models import TravelPlanState
from storage.database import Database
from storage.trace_store import TraceStore
from telemetry.stats import RecallTelemetryRecord, SessionStats


async def _insert_session(db: Database, session_id: str = "session-1") -> None:
    await db.execute(
        "INSERT INTO sessions (session_id, user_id, title, phase, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            "default_user",
            "Trace Persistence Test",
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
    await store.create_run(
        run_id="run-1",
        session_id="session-1",
        trip_id=None,
        context_epoch=0,
        started_at="2026-06-07T10:00:00+00:00",
        status="running",
    )
    yield store
    await db.close()


def test_build_trace_events_from_stats_orders_by_timestamp():
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        duration_ms=200.0,
        phase=1,
        iteration=1,
    )
    stats.record_tool_call(
        tool_name="web_search",
        duration_ms=150.0,
        status="success",
        error_code=None,
        phase=1,
        arguments_preview='{"q":"东京"}',
        result_preview="东京攻略",
    )

    events = build_trace_events_from_stats(
        run_id="run-1",
        stats=stats,
        phase2_step="brief",
        tool_side_effects={"web_search": "read"},
    )

    assert [event.sequence for event in events] == [1, 2]
    assert events[0].event_type == "llm_call"
    assert events[1].event_type == "tool_call"
    assert events[1].payload["side_effect"] == "read"


def test_build_trace_events_includes_memory_recall_payload():
    stats = SessionStats()
    stats.recall_telemetry.append(
        RecallTelemetryRecord(
            stage0_decision="skip_recall",
            stage0_matched_rule="P3_current_trip_fact",
            final_recall_decision="skip_recall",
        )
    )

    events = build_trace_events_from_stats(
        run_id="run-1",
        stats=stats,
        phase2_step=None,
        tool_side_effects={},
    )

    assert len(events) == 1
    assert events[0].event_type == "memory_recall"
    assert events[0].payload["stage0_matched_rule"] == "P3_current_trip_fact"


@pytest.mark.asyncio
async def test_persist_trace_run_safely_replaces_events(trace_store: TraceStore):
    stats = SessionStats()
    stats.record_tool_call(
        tool_name="web_search",
        duration_ms=10.0,
        status="success",
        error_code=None,
        phase=1,
    )
    session = {"stats": stats, "current_context_epoch": 0}
    plan = TravelPlanState(session_id="session-1")
    run = RunRecord(run_id="run-1", session_id="session-1", status="completed")

    await persist_trace_run_safely(
        trace_store=trace_store,
        session=session,
        plan=plan,
        run=run,
        tool_side_effects={"web_search": "read"},
    )
    await persist_trace_run_safely(
        trace_store=trace_store,
        session=session,
        plan=plan,
        run=run,
        tool_side_effects={"web_search": "read"},
    )

    events = await trace_store.load_events("run-1")
    run_row = await trace_store.load_run("run-1")

    assert len(events) == 1
    assert json.loads(events[0]["payload_json"])["tool_name"] == "web_search"
    assert run_row is not None
    assert run_row["status"] == "completed"


class FailingTraceStore:
    async def create_run(self, **kwargs):
        raise RuntimeError("trace store unavailable")


@pytest.mark.asyncio
async def test_persist_trace_run_safely_swallows_trace_store_failure():
    stats = SessionStats()
    stats.record_tool_call(
        tool_name="web_search",
        duration_ms=10.0,
        status="success",
        error_code=None,
        phase=1,
    )
    session = {"stats": stats, "current_context_epoch": 0}
    plan = TravelPlanState(session_id="session-1")
    run = RunRecord(run_id="run-1", session_id="session-1", status="completed")

    await persist_trace_run_safely(
        trace_store=FailingTraceStore(),
        session=session,
        plan=plan,
        run=run,
        tool_side_effects={"web_search": "read"},
    )
