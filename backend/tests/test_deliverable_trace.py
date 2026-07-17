from types import SimpleNamespace

import pytest

from agent.types import ToolResult
from api.orchestration.chat.deliverables import finalize_pending_phase4_deliverables
from api.orchestration.chat.stream_trace import (
    emit_deliverable_draft_trace,
    emit_deliverable_gap_trace,
)
from state.models import TravelPlanState
from telemetry.trace_recorder import TraceContext, TraceRecorder


class _TraceStore:
    def __init__(self):
        self.events = []
        self.artifacts = []

    async def append_event(self, event):
        self.events.append(event)

    async def save_artifact_metadata(self, metadata):
        self.artifacts.append(metadata)


class _StateManager:
    def __init__(self):
        self.saved = []

    async def save(self, plan):
        self.saved.append(plan.session_id)


class _SessionStore:
    def __init__(self):
        self.updates = []

    async def update(self, session_id, **kwargs):
        self.updates.append((session_id, kwargs))


def _result_data() -> dict:
    return {
        "travel_plan_markdown": "# 东京旅行计划\n\n## 第 1 天\n- 浅草寺\n",
        "checklist_markdown": "# 东京出发前清单\n\n- [ ] 护照\n",
    }


@pytest.mark.asyncio
async def test_emit_deliverable_draft_trace_links_to_tool_result():
    trace_store = _TraceStore()
    recorder = TraceRecorder(trace_store=trace_store)
    plan = TravelPlanState(session_id="s-deliverable", phase=4)
    agent = SimpleNamespace(
        trace_recorder=recorder,
        trace_context=TraceContext(run_id="run-1", session_id=plan.session_id, phase=4),
    )
    session = {"_phase4_deliverables_quality": {"status": "approved"}}
    tool_result = ToolResult(
        tool_call_id="call-generate",
        status="success",
        data=_result_data(),
        metadata={
            "trace_event_id": "evt_tool_result",
            "trace_parent_event_id": "evt_tool_call",
        },
    )

    await emit_deliverable_draft_trace(
        session=session,
        plan=plan,
        agent=agent,
        tool_result=tool_result,
        result_data=_result_data(),
    )

    event = trace_store.events[0]
    assert event.event_type == "deliverable_draft"
    assert event.parent_event_id == "evt_tool_result"
    assert event.root_event_id == "evt_tool_call"
    assert event.payload["tool_call_id"] == "call-generate"
    assert event.payload["source_state_hash"]
    assert event.payload["travel_plan_markdown_hash"]
    assert event.payload["checklist_markdown_hash"]
    assert len(event.payload["draft_artifacts"]) == 2
    assert session["_pending_phase4_deliverables_trace"]["draft_event_id"] == event.event_id
    assert {artifact.kind for artifact in trace_store.artifacts} == {"deliverable_draft"}


@pytest.mark.asyncio
async def test_emit_deliverable_gap_trace_warns_for_unfrozen_phase4():
    trace_store = _TraceStore()
    recorder = TraceRecorder(trace_store=trace_store)
    plan = TravelPlanState(session_id="s-gap", phase=4)
    agent = SimpleNamespace(
        trace_recorder=recorder,
        trace_context=TraceContext(run_id="run-gap", session_id=plan.session_id, phase=4),
    )

    await emit_deliverable_gap_trace(plan=plan, agent=agent)

    assert len(trace_store.events) == 1
    event = trace_store.events[0]
    assert event.event_type == "deliverable_gap"
    assert event.status == "warning"
    assert event.payload["phase"] == 4


@pytest.mark.asyncio
async def test_emit_deliverable_gap_trace_skips_frozen_deliverables():
    trace_store = _TraceStore()
    recorder = TraceRecorder(trace_store=trace_store)
    plan = TravelPlanState(
        session_id="s-complete",
        phase=4,
        deliverables={"travel_plan_md": "travel_plan.md"},
    )
    agent = SimpleNamespace(
        trace_recorder=recorder,
        trace_context=TraceContext(
            run_id="run-complete",
            session_id=plan.session_id,
            phase=4,
        ),
    )

    await emit_deliverable_gap_trace(plan=plan, agent=agent)

    assert trace_store.events == []


@pytest.mark.asyncio
async def test_finalize_pending_phase4_deliverables_emits_trace_event():
    trace_store = _TraceStore()
    recorder = TraceRecorder(trace_store=trace_store)
    plan = TravelPlanState(session_id="s-deliverable", phase=4)
    state_mgr = _StateManager()
    session_store = _SessionStore()

    async def persist_phase4_deliverables(plan_arg, result_data):
        assert plan_arg is plan
        assert result_data["travel_plan_markdown"].startswith("# 东京")
        plan_arg.deliverables = {
            "travel_plan_md": "travel_plan.md",
            "checklist_md": "checklist.md",
            "generated_at": "2026-06-08T00:00:00Z",
        }

    deps = SimpleNamespace(
        persist_phase4_deliverables=persist_phase4_deliverables,
        state_mgr=state_mgr,
        session_store=session_store,
        generate_title=lambda plan_arg: "东京旅行",
    )
    session = {
        "_trace_recorder": recorder,
        "_trace_context": TraceContext(run_id="run-1", session_id=plan.session_id, phase=4),
        "_pending_phase4_deliverables": {
            "tool_call_id": "call-generate",
            "result_data": _result_data(),
            "tool_result_trace_event_id": "evt_tool_result",
        },
        "_phase4_deliverables_quality": {
            "tool_call_id": "call-generate",
            "status": "approved",
        },
        "_pending_phase4_deliverables_trace": {
            "draft_event_id": "evt_draft",
            "tool_result_event_id": "evt_tool_result",
            "root_event_id": "evt_tool_call",
        },
    }

    finalized = await finalize_pending_phase4_deliverables(
        deps=deps,
        session=session,
        plan=plan,
    )

    assert finalized is True
    event = trace_store.events[0]
    assert event.event_type == "deliverable_finalize"
    assert event.parent_event_id == "evt_draft"
    assert event.root_event_id == "evt_tool_call"
    assert event.payload["tool_call_id"] == "call-generate"
    assert event.payload["tool_result_event_id"] == "evt_tool_result"
    assert event.payload["final_artifact_paths"]["travel_plan_md"] == "travel_plan.md"
    assert event.payload["final_state_hash"]
    assert len(event.payload["final_artifacts"]) == 2
    assert {artifact.kind for artifact in trace_store.artifacts} == {"deliverable_final"}
    assert state_mgr.saved == ["s-deliverable"]
    assert session_store.updates[0][0] == "s-deliverable"
    assert "_pending_phase4_deliverables" not in session
