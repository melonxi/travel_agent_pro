from types import SimpleNamespace

import pytest

from agent.types import Message, Role
from api.orchestration.memory.contracts import MemoryRecallDecision
from api.orchestration.memory.turn import build_memory_context_for_turn
from memory.formatter import MemoryRecallTelemetry
from telemetry.trace_recorder import TraceContext, TraceRecorder


class TraceStoreStub:
    def __init__(self):
        self.events = []

    async def append_event(self, event):
        self.events.append(event)

    async def save_artifact_metadata(self, metadata):
        pass


class MemoryManagerStub:
    async def generate_context(self, *args, **kwargs):
        return (
            "remember profile p1",
            MemoryRecallTelemetry(
                sources={"query_profile": 1, "working_memory": 0, "episode_slice": 0},
                profile_ids=["p1"],
                matched_reasons=["pref"],
                candidate_count=2,
                reranker_selected_ids=["p1"],
                reranker_per_item_scores={"p1": {"final_score": 0.9}},
                reranker_final_reason="selected",
            ),
        )


async def _decide_memory_recall(**kwargs):
    return MemoryRecallDecision(
        needs_recall=True,
        stage0_decision="force_recall",
        stage0_reason="history_query",
        stage0_matched_rule="P1",
        stage0_signals={"history": ["上次"]},
        intent_type="profile",
        reason="user asked",
        confidence=0.9,
    )


async def _no_query_plan(**kwargs):
    return SimpleNamespace(
        plan=None,
        query_plan_source="none",
        query_plan_fallback="none",
    )


class EmptyMemoryManagerStub:
    async def generate_context(self, *args, **kwargs):
        return (
            "暂无相关用户记忆",
            MemoryRecallTelemetry(
                candidate_count=0,
                recall_attempted_but_zero_hit=True,
                fallback_used="gate_timeout_heuristic_recall",
                recall_skip_source="gate_failure_no_heuristic",
            ),
        )


@pytest.mark.asyncio
async def test_build_memory_context_for_turn_emits_recall_and_hit_trace_events():
    store = TraceStoreStub()
    result = await build_memory_context_for_turn(
        config=SimpleNamespace(memory=SimpleNamespace(enabled=True)),
        memory_mgr=MemoryManagerStub(),
        session={"stats": None},
        plan=SimpleNamespace(session_id="session-1", phase=1, phase2_step=None),
        messages=[
            Message(role=Role.USER, content="上次喜欢安静酒店"),
            Message(role=Role.USER, content="这次还按上次来"),
        ],
        user_id="user-1",
        user_message="这次还按上次来",
        decide_memory_recall=_decide_memory_recall,
        build_recall_retrieval_plan=_no_query_plan,
        trace_recorder=TraceRecorder(trace_store=store),
        trace_context=TraceContext(run_id="run-1", session_id="session-1", phase=1),
    )

    assert result.memory_recall_event_id is not None
    assert result.memory_hit_event_id is not None
    assert [event.event_type for event in store.events] == [
        "memory_recall",
        "memory_hit",
    ]
    recall_event, hit_event = store.events
    assert recall_event.payload["stage0_decision"] == "force_recall"
    assert recall_event.payload["latest_user_message_hash"].startswith("sha256:")
    assert recall_event.payload["previous_user_message_count"] == 1
    assert recall_event.payload["stage4_selected_ids"]["combined"] == ["p1"]
    assert hit_event.parent_event_id == recall_event.event_id
    assert hit_event.payload["selected_ids"] == ["p1"]
    assert hit_event.payload["memory_context_hash"].startswith("sha256:")


@pytest.mark.asyncio
async def test_build_memory_context_for_turn_emits_zero_hit_and_fallback_evidence():
    store = TraceStoreStub()

    async def decide(**kwargs):
        return MemoryRecallDecision(
            needs_recall=True,
            stage0_decision="allow_recall",
            stage0_reason="gate_timeout",
            fallback_used="gate_timeout_heuristic_recall",
            recall_skip_source="gate_timeout",
        )

    result = await build_memory_context_for_turn(
        config=SimpleNamespace(memory=SimpleNamespace(enabled=True)),
        memory_mgr=EmptyMemoryManagerStub(),
        session={"stats": None},
        plan=SimpleNamespace(session_id="session-1", phase=1, phase2_step=None),
        messages=[Message(role=Role.USER, content="想按以前的偏好")],
        user_id="user-1",
        user_message="想按以前的偏好",
        decide_memory_recall=decide,
        build_recall_retrieval_plan=_no_query_plan,
        trace_recorder=TraceRecorder(trace_store=store),
        trace_context=TraceContext(run_id="run-1", session_id="session-1", phase=1),
    )

    assert result.memory_hit_event_id is None
    assert [event.event_type for event in store.events] == ["memory_recall"]
    recall_event = store.events[0]
    assert recall_event.status == "skipped"
    assert recall_event.payload["recall_attempted_but_zero_hit"] is True
    assert recall_event.payload["fallback_source"] == "gate_timeout_heuristic_recall"
    assert recall_event.payload["error_path"] == "gate_timeout"


@pytest.mark.asyncio
async def test_build_memory_context_for_turn_emits_false_skip_evidence():
    store = TraceStoreStub()

    async def decide(**kwargs):
        return MemoryRecallDecision(
            needs_recall=False,
            stage0_decision="skip_recall",
            stage0_reason="current_trip_fact",
            stage0_matched_rule="P3",
            recall_skip_source="stage0_current_trip_fact",
        )

    await build_memory_context_for_turn(
        config=SimpleNamespace(memory=SimpleNamespace(enabled=True)),
        memory_mgr=EmptyMemoryManagerStub(),
        session={"stats": None},
        plan=SimpleNamespace(session_id="session-1", phase=1, phase2_step=None),
        messages=[Message(role=Role.USER, content="这次预算多少")],
        user_id="user-1",
        user_message="这次预算多少",
        decide_memory_recall=decide,
        build_recall_retrieval_plan=_no_query_plan,
        trace_recorder=TraceRecorder(trace_store=store),
        trace_context=TraceContext(run_id="run-1", session_id="session-1", phase=1),
    )

    recall_event = store.events[0]
    assert recall_event.payload["gate_needs_recall"] is False
    assert recall_event.payload["stage0_decision"] == "skip_recall"
    assert recall_event.payload["stage0_matched_rule"] == "P3"
    assert recall_event.payload["recall_skip_source"] == "stage0_current_trip_fact"
