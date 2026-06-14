import pytest

from telemetry.trace_recorder import (
    LocalTraceArtifactStore,
    NoOpTraceRecorder,
    TraceContext,
    TraceRecorder,
    TraceRecorderConfig,
)


class FakeTraceStore:
    def __init__(self):
        self.runs = []
        self.summaries = []
        self.events = []
        self.artifacts = []

    async def create_run(self, **kwargs):
        self.runs.append(kwargs)

    async def update_run_summary(self, **kwargs):
        self.summaries.append(kwargs)

    async def append_event(self, event):
        self.events.append(event)

    async def save_artifact_metadata(self, metadata):
        self.artifacts.append(metadata)


class FailingTraceStore(FakeTraceStore):
    async def append_event(self, event):
        raise RuntimeError("write failed")


@pytest.mark.asyncio
async def test_trace_recorder_sequence_ordering_and_run_events():
    store = FakeTraceStore()
    recorder = TraceRecorder(trace_store=store)
    context = TraceContext(
        run_id="run-1",
        session_id="session-1",
        trip_id="trip-1",
        context_epoch=2,
        phase=1,
    )

    start_event = await recorder.start_run(
        context,
        started_at="2026-06-08T00:00:00+00:00",
        config_hash="sha256:config",
        prompt_version="p1",
        model_config={"api_key": "secret", "model": "gpt-4o"},
        tool_schema_hash="sha256:tools",
    )
    tool_event = await recorder.emit_event(
        context,
        event_type="tool_call",
        tool_name="web_search",
        status="success",
        payload={"tool_name": "web_search"},
    )
    end_event = await recorder.end_run(
        context,
        status="completed",
        ended_at="2026-06-08T00:00:01+00:00",
    )

    assert [event.sequence for event in store.events] == [1, 2, 3]
    assert start_event.event_type == "run_start"
    assert tool_event.session_id == "session-1"
    assert tool_event.trip_id == "trip-1"
    assert tool_event.context_epoch == 2
    assert tool_event.payload_schema_version == 2
    assert end_event.event_type == "run_end"
    assert store.runs[0]["run_id"] == "run-1"
    assert store.runs[0]["config_hash"] == "sha256:config"
    assert store.runs[0]["prompt_version"] == "p1"
    assert store.runs[0]["model_config_json"] == '{"api_key":"[REDACTED:secret]","model":"gpt-4o"}'
    assert store.runs[0]["tool_schema_hash"] == "sha256:tools"
    assert store.summaries[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_trace_recorder_child_context_links_parent_root_and_correlation():
    store = FakeTraceStore()
    recorder = TraceRecorder(trace_store=store)
    context = TraceContext(
        run_id="run-1",
        session_id="session-1",
        correlation_id="corr-1",
        actor="main_agent",
    )

    parent = await recorder.emit_event(
        context,
        event_type="llm_output",
        payload={"tool_call_ids": ["call-1"]},
    )
    child_context = recorder.child_context(
        context,
        parent_event=parent,
        actor="tool_engine",
    )
    child = await recorder.emit_event(
        child_context,
        event_type="tool_call",
        tool_name="web_search",
        payload={"tool_call_id": "call-1"},
    )

    assert child.parent_event_id == parent.event_id
    assert child.root_event_id == parent.event_id
    assert child.correlation_id == "corr-1"
    assert child.actor == "tool_engine"


@pytest.mark.asyncio
async def test_noop_trace_recorder_does_not_call_store():
    store = FakeTraceStore()
    recorder = TraceRecorder(
        trace_store=store,
        config=TraceRecorderConfig(enabled=False),
    )

    event = await recorder.emit_event(
        TraceContext(run_id="run-1", session_id="session-1"),
        event_type="internal_task",
        payload={},
    )

    assert event is None
    assert store.events == []
    assert await NoOpTraceRecorder().emit_event(
        TraceContext(run_id="run-1", session_id="session-1"),
        event_type="internal_task",
        payload={},
    ) is None


@pytest.mark.asyncio
async def test_trace_recorder_swallows_store_failures():
    recorder = TraceRecorder(trace_store=FailingTraceStore())

    event = await recorder.emit_event(
        TraceContext(run_id="run-1", session_id="session-1"),
        event_type="error",
        status="error",
        payload={"error_code": "TEST"},
    )

    assert event is not None
    assert event.event_type == "error"


@pytest.mark.asyncio
async def test_trace_recorder_attach_artifact_redacts_and_saves_metadata():
    store = FakeTraceStore()
    recorder = TraceRecorder(trace_store=store)
    context = TraceContext(run_id="run-1", session_id="session-1")

    metadata = await recorder.attach_artifact(
        context,
        event_id="evt-1",
        kind="tool_arguments",
        content={"api_key": "secret", "query": "tokyo"},
    )

    assert metadata is not None
    assert metadata.run_id == "run-1"
    assert metadata.event_id == "evt-1"
    assert metadata.kind == "tool_arguments"
    assert metadata.content_hash.startswith("sha256:")
    assert metadata.redaction_status == "hash_only"
    assert store.artifacts == [metadata]


@pytest.mark.asyncio
async def test_trace_recorder_attach_artifact_writes_local_file(tmp_path):
    store = FakeTraceStore()
    artifact_store = LocalTraceArtifactStore(
        tmp_path / "trace_artifacts",
        relative_to=tmp_path,
    )
    recorder = TraceRecorder(trace_store=store, artifact_store=artifact_store)
    context = TraceContext(
        run_id="run/1",
        session_id="session/1",
    )

    metadata = await recorder.attach_artifact(
        context,
        event_id="evt-1",
        kind="tool_result",
        content={"authorization": "Bearer secret-token", "result": "ok"},
    )

    assert metadata is not None
    assert metadata.redaction_status == "redacted"
    assert metadata.storage_path is not None
    artifact_path = tmp_path / metadata.storage_path
    assert artifact_path.exists()
    content = artifact_path.read_text()
    assert "secret-token" not in content
    assert "[REDACTED:secret]" in content
