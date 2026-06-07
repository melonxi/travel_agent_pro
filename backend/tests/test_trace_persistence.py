import json

import pytest
import pytest_asyncio

from api.orchestration.chat.trace_persistence import (
    build_trace_events_from_stats,
    ensure_trace_run_started,
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


def test_build_trace_events_includes_record_metadata():
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=10,
        output_tokens=5,
        duration_ms=12.0,
        phase=3,
        iteration=2,
        metadata={"scope": "phase3_worker", "day": 2},
    )
    stats.record_tool_call(
        tool_name="calculate_route",
        duration_ms=34.0,
        status="success",
        error_code=None,
        phase=3,
        metadata={"scope": "phase3_worker", "day": 2, "attempt": 1},
    )

    events = build_trace_events_from_stats(
        run_id="run-1",
        stats=stats,
        phase2_step=None,
        tool_side_effects={"calculate_route": "read"},
    )

    assert events[0].event_type == "llm_call"
    assert events[0].payload["metadata"] == {"scope": "phase3_worker", "day": 2}
    assert events[1].event_type == "tool_call"
    assert events[1].payload["metadata"]["scope"] == "phase3_worker"
    assert events[1].payload["metadata"]["attempt"] == 1


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


@pytest.mark.asyncio
async def test_persist_trace_run_safely_uses_run_scoped_stats_offsets(
    trace_store: TraceStore,
):
    stats = SessionStats()
    session = {"stats": stats, "current_context_epoch": 0}
    plan = TravelPlanState(session_id="session-1")
    run_1 = RunRecord(run_id="run-1", session_id="session-1", status="completed")

    await ensure_trace_run_started(
        trace_store=trace_store,
        session=session,
        plan=plan,
        run=run_1,
    )
    stats.record_tool_call(
        tool_name="set_candidate_pool",
        duration_ms=10.0,
        status="success",
        error_code=None,
        phase=2,
    )
    await persist_trace_run_safely(
        trace_store=trace_store,
        session=session,
        plan=plan,
        run=run_1,
        tool_side_effects={"set_candidate_pool": "write"},
    )

    run_2 = RunRecord(run_id="run-2", session_id="session-1", status="completed")
    await ensure_trace_run_started(
        trace_store=trace_store,
        session=session,
        plan=plan,
        run=run_2,
    )
    stats.record_tool_call(
        tool_name="generate_summary",
        duration_ms=20.0,
        status="success",
        error_code=None,
        phase=4,
    )
    stats.record_llm_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=321,
        output_tokens=45,
        duration_ms=30.0,
        phase=4,
        iteration=1,
    )

    await persist_trace_run_safely(
        trace_store=trace_store,
        session=session,
        plan=plan,
        run=run_2,
        tool_side_effects={"set_candidate_pool": "write", "generate_summary": "write"},
    )

    run_1_events = await trace_store.load_events("run-1")
    run_2_events = await trace_store.load_events("run-2")
    run_2_row = await trace_store.load_run("run-2")

    run_1_tool_names = [
        row["tool_name"] for row in run_1_events if row["event_type"] == "tool_call"
    ]
    run_2_tool_names = [
        row["tool_name"] for row in run_2_events if row["event_type"] == "tool_call"
    ]

    assert run_1_tool_names == ["set_candidate_pool"]
    assert run_2_tool_names == ["generate_summary"]
    assert all(row["tool_name"] != "set_candidate_pool" for row in run_2_events)
    assert run_2_row is not None
    assert run_2_row["total_input_tokens"] == 321
    assert run_2_row["total_output_tokens"] == 45
    assert run_2_row["total_duration_ms"] == 50.0


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
