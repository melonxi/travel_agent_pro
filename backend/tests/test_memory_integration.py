from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from agent.internal_tasks import InternalTask
from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from main import create_app
from memory.formatter import MemoryRecallTelemetry
from memory.recall_query import RecallRetrievalPlan
from memory.v3_models import ArchivedTripEpisode, MemoryAuditEvent, WorkingMemoryItem
from state.models import Budget, DateRange, TravelPlanState


def _get_closure_value(app, name: str):
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not hasattr(endpoint, "__closure__"):
            continue
        free_vars = getattr(endpoint.__code__, "co_freevars", ())
        for var_name, cell in zip(free_vars, endpoint.__closure__ or ()):
            if var_name == name:
                return cell.cell_contents
    raise RuntimeError(f"Cannot locate {name}")


def _get_function_closure_value(fn, name: str):
    free_vars = getattr(fn.__code__, "co_freevars", ())
    for var_name, cell in zip(free_vars, fn.__closure__ or ()):
        if var_name == name:
            return cell.cell_contents
    raise RuntimeError(f"Cannot locate {name}")


def _set_function_closure_value(fn, name: str, value):
    free_vars = getattr(fn.__code__, "co_freevars", ())
    for var_name, cell in zip(free_vars, fn.__closure__ or ()):
        if var_name == name:
            cell.cell_contents = value
            return
    raise RuntimeError(f"Cannot locate {name}")


def _parse_sse_data_events(body: str) -> list[dict]:
    events: list[dict] = []
    data_lines: list[str] = []

    def flush_event():
        if not data_lines:
            return
        events.append(json.loads("\n".join(data_lines)))
        data_lines.clear()

    for line in body.splitlines():
        if not line:
            flush_event()
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())

    flush_event()
    return events


async def _wait_for_memory_scheduler_idle(
    app,
    session_id: str,
    *,
    timeout: float = 1.0,
):
    runtimes = app.state.memory_scheduler_runtimes
    start = asyncio.get_running_loop().time()
    while session_id not in runtimes:
        if asyncio.get_running_loop().time() - start >= timeout:
            raise TimeoutError(f"Memory scheduler runtime not created for {session_id}")
        await asyncio.sleep(0.01)
    await asyncio.wait_for(runtimes[session_id].scheduler.wait_for_idle(), timeout=timeout)


@pytest.mark.asyncio
async def test_recent_internal_tasks_endpoint_replays_background_memory_tasks(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]

        app.state.memory_active_tasks[session_id] = {
            "memory_extraction:test": InternalTask(
                id="memory_extraction:test",
                kind="memory_extraction",
                label="记忆提取",
                status="skipped",
                message="本轮没有新的可复用记忆",
                blocking=False,
                scope="background",
                result={"count": 0},
                started_at=10.0,
                ended_at=12.0,
            )
        }

        resp = await client.get(f"/api/internal-tasks/{session_id}")

    assert resp.status_code == 200
    assert resp.json() == {
        "tasks": [
            {
                "id": "memory_extraction:test",
                "kind": "memory_extraction",
                "label": "记忆提取",
                "status": "skipped",
                "blocking": False,
                "scope": "background",
                "message": "本轮没有新的可复用记忆",
                "result": {"count": 0},
                "started_at": 10.0,
                "ended_at": 12.0,
            }
        ]
    }


@pytest.fixture
def app(monkeypatch, tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    data_dir = tmp_path / "data"
    config_file.write_text(
        f"""
llm:
  provider: openai
  model: gpt-4o
data_dir: "{data_dir}"
flyai:
  enabled: false
memory:
  enabled: true
  extraction:
    enabled: true
    model: gpt-4o-mini
    trigger: each_turn
    max_user_messages: 4
telemetry:
  enabled: false
guardrails:
  enabled: false
parallel_tool_execution: false
""",
        encoding="utf-8",
    )

    class FakeProvider:
        async def chat(self, *args, **kwargs):
            yield LLMChunk(type=ChunkType.DONE)

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("main.create_llm_provider", lambda _config: FakeProvider())
    return create_app(str(config_file))


@pytest.fixture
def app_memory_disabled(monkeypatch, tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    data_dir = tmp_path / "data"
    config_file.write_text(
        f"""
llm:
  provider: openai
  model: gpt-4o
data_dir: "{data_dir}"
flyai:
  enabled: false
memory:
  enabled: false
telemetry:
  enabled: false
guardrails:
  enabled: false
parallel_tool_execution: false
""",
        encoding="utf-8",
    )

    class FakeProvider:
        async def chat(self, *args, **kwargs):
            yield LLMChunk(type=ChunkType.DONE)

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("main.create_llm_provider", lambda _config: FakeProvider())
    return create_app(str(config_file))


@pytest.fixture
def app_recall_gate_disabled(monkeypatch, tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    data_dir = tmp_path / "data"
    config_file.write_text(
        f"""
llm:
  provider: openai
  model: gpt-4o
data_dir: "{data_dir}"
flyai:
  enabled: false
memory:
  enabled: true
  extraction:
    enabled: true
    model: gpt-4o-mini
    trigger: each_turn
    max_user_messages: 4
  retrieval:
    recall_gate_enabled: false
telemetry:
  enabled: false
guardrails:
  enabled: false
parallel_tool_execution: false
""",
        encoding="utf-8",
    )

    class FakeProvider:
        async def chat(self, *args, **kwargs):
            yield LLMChunk(type=ChunkType.DONE)

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("main.create_llm_provider", lambda _config: FakeProvider())
    return create_app(str(config_file))


@pytest.fixture
def app_extraction_disabled(monkeypatch, tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    data_dir = tmp_path / "data"
    config_file.write_text(
        f"""
llm:
  provider: openai
  model: gpt-4o
data_dir: "{data_dir}"
flyai:
  enabled: false
memory:
  enabled: true
  extraction:
    enabled: false
telemetry:
  enabled: false
guardrails:
  enabled: false
parallel_tool_execution: false
""",
        encoding="utf-8",
    )

    class FakeProvider:
        async def chat(self, *args, **kwargs):
            yield LLMChunk(type=ChunkType.DONE)

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("main.create_llm_provider", lambda _config: FakeProvider())
    return create_app(str(config_file))


@pytest.mark.asyncio
async def test_legacy_memory_routes_are_removed_in_integration_app(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        list_resp = await client.get("/api/memory/u1")
        confirm_resp = await client.post(
            "/api/memory/u1/confirm",
            json={"item_id": "mem-1"},
        )
        reject_resp = await client.post(
            "/api/memory/u1/reject",
            json={"item_id": "mem-1"},
        )
        events_resp = await client.post(
            "/api/memory/u1/events",
            json={
                "event_type": "accept",
                "object_type": "skeleton",
                "object_payload": {"id": "sk1"},
                "reason_text": "用户确认",
            },
        )
        delete_resp = await client.delete("/api/memory/u1/mem-1")

    assert list_resp.status_code == 404
    assert confirm_resp.status_code == 404
    assert reject_resp.status_code == 404
    assert events_resp.status_code == 404
    assert delete_resp.status_code == 404


@pytest.mark.asyncio
async def test_memory_events_and_episodes_endpoints(app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    event = MemoryAuditEvent(
        id="evt1",
        user_id="u1",
        session_id="s1",
        event_type="accept",
        object_type="skeleton",
        object_payload={"id": "sk1"},
        reason_text="用户确认",
        created_at="2026-04-11T00:00:00",
    )
    await memory_mgr.v3_store.append_event(event)
    episode = ArchivedTripEpisode(
        id="ep1",
        user_id="u1",
        session_id="s1",
        trip_id="trip1",
        destination="Tokyo",
        dates={"start": "2026-05-01", "end": "2026-05-03", "total_days": 3},
        travelers={"adults": 2},
        budget={"total": 30000, "currency": "CNY"},
        selected_skeleton={"id": "sk1"},
        selected_transport=None,
        accommodation=None,
        daily_plan_summary=[],
        final_plan_summary="Tokyo trip",
        decision_log=[{"type": "accepted", "category": "skeleton", "value": {"id": "sk1"}}],
        lesson_log=[],
        created_at="2026-04-11T00:00:00",
        completed_at="2026-04-11T00:00:00",
    )
    await memory_mgr.v3_store.append_episode(episode)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        episodes_resp = await client.get("/api/memory/u1/episodes")

    assert episodes_resp.status_code == 200
    assert episodes_resp.json()["episodes"] == [episode.to_dict()]
    path = Path(memory_mgr.v3_store.data_dir) / "users" / "u1" / "memory" / "events.jsonl"
    assert path.exists()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["event_type"] == "accept"


@pytest.mark.asyncio
async def test_chat_system_prompt_uses_generate_context(monkeypatch, app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    calls = {"context": 0}

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        calls["context"] += 1
        assert user_message == "住宿怎么安排比较好"
        return "memory-context-marker", MemoryRecallTelemetry()

    async def fake_run(self, messages, phase, tools_override=None):
        assert messages[0].role == Role.SYSTEM
        assert "memory-context-marker" in messages[0].content
        assert "暂无用户画像" not in messages[0].content
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住宿怎么安排比较好", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert calls["context"] >= 1


@pytest.mark.asyncio
async def test_chat_system_prompt_skips_memory_when_disabled(
    monkeypatch,
    app_memory_disabled,
):
    memory_mgr = _get_closure_value(app_memory_disabled, "memory_mgr")

    async def fake_generate_context(
        self, user_id: str, plan: TravelPlanState, **kwargs
    ) -> tuple[str, list[str], int, int, int]:
        raise AssertionError(
            "generate_context should not be called when memory is disabled"
        )

    async def fake_run(self, messages, phase, tools_override=None):
        assert "secret-memory" not in messages[0].content
        assert "## 相关用户记忆" in messages[0].content
        assert "暂无相关用户记忆" in messages[0].content
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app_memory_disabled), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "这次预算多少？", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"kind": "memory_recall"' not in resp.text
    assert '"type": "memory_recall"' not in resp.text


@pytest.mark.asyncio
async def test_chat_stream_skips_memory_recall_events_when_memory_disabled(
    monkeypatch, app_memory_disabled
):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="继续")
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app_memory_disabled), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住宿怎么安排比较好", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"kind": "memory_recall"' not in resp.text
    assert '"type": "memory_recall"' not in resp.text


@pytest.mark.asyncio
async def test_chat_stream_does_not_emit_legacy_memory_pending_events(monkeypatch, app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    await memory_mgr.v3_store.upsert_working_memory_item(
        "u1",
        "s1",
        "trip-1",
        WorkingMemoryItem(
            id="wm-1",
            phase=3,
            kind="temporary_rejection",
            domains=["attraction"],
            content="先别考虑迪士尼。",
            reason="当前候选筛选需要避让。",
            status="active",
            expires={
                "on_session_end": False,
                "on_trip_change": True,
                "on_phase_exit": False,
            },
            created_at="2026-04-11T00:00:00",
        ),
    )

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住宿怎么安排比较好", "user_id": "u1"},
        )

    body = resp.text
    assert '"memory_pending"' not in body


@pytest.mark.asyncio
async def test_chat_stream_emits_memory_recall_internal_task(monkeypatch, app):
    memory_mgr = _get_closure_value(app, "memory_mgr")

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        assert user_message == "住宿怎么安排比较好"
        return (
            "用户偏好：喜欢轻松行程",
            MemoryRecallTelemetry(
                sources={"query_profile": 1},
                profile_ids=["mem_1"],
            ),
        )

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="继续")
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住宿怎么安排比较好", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"type": "internal_task"' in resp.text
    assert '"kind": "memory_recall"' in resp.text
    assert '"status": "pending"' in resp.text
    assert '"status": "success"' in resp.text
    assert '"type": "memory_recall"' in resp.text


@pytest.mark.asyncio
async def test_chat_stream_emits_memory_recall_telemetry_without_hits(monkeypatch, app):
    memory_mgr = _get_closure_value(app, "memory_mgr")

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        return (
            "暂无相关用户记忆",
            MemoryRecallTelemetry(
                stage0_decision="skip_recall",
                stage0_reason="current_trip_fact_question",
                gate_needs_recall=False,
                gate_intent_type="no_recall_needed",
                gate_confidence=0.98,
                gate_reason="current trip fact question",
                final_recall_decision="no_recall_applied",
                candidate_count=4,
                reranker_selected_ids=["profile_1", "slice_2"],
                reranker_final_reason="two items directly answer the user's question",
                reranker_fallback="none",
            ),
        )

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="继续")
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "这次预算多少？", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"kind": "memory_recall"' in resp.text
    assert '"count": 0' in resp.text
    assert '"gate": false' in resp.text
    assert '"type": "memory_recall"' in resp.text
    assert '"gate_needs_recall": false' in resp.text
    assert '"candidate_count": 4' in resp.text
    assert '"reranker_selected_ids": ["profile_1", "slice_2"]' in resp.text
    assert '"reranker_final_reason": "two items directly answer the user' in resp.text
    assert '"reranker_fallback": "none"' in resp.text


@pytest.mark.asyncio
async def test_chat_stream_emits_structured_reranker_fields(monkeypatch, app):
    memory_mgr = _get_closure_value(app, "memory_mgr")

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        return "暂无相关用户记忆", MemoryRecallTelemetry(
            candidate_count=2,
            reranker_selected_ids=["profile_1"],
            reranker_final_reason="selected profile memory",
            reranker_fallback="none",
            reranker_per_item_reason={
                "profile_1": "bucket=0.82 domain=1.00 keyword=0.50"
            },
            reranker_per_item_scores={
                "profile_1": {
                    "rule_score": 0.71,
                    "evidence_score": 0.0,
                    "final_score": 2.0,
                }
            },
            reranker_intent_label="profile",
            reranker_selection_metrics={
                "selected_pairwise_similarity_max": None,
                "selected_pairwise_similarity_avg": None,
            },
        )

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="继续")
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住宿按我习惯", "user_id": "u1"},
        )

    assert resp.status_code == 200
    events = _parse_sse_data_events(resp.text)
    memory_event = next(event for event in events if event["type"] == "memory_recall")
    assert memory_event["reranker_intent_label"] == "profile"
    assert memory_event["reranker_per_item_scores"]["profile_1"]["final_score"] == 2.0
    assert (
        memory_event["reranker_selection_metrics"]["selected_pairwise_similarity_max"]
        is None
    )


@pytest.mark.asyncio
async def test_chat_stream_keeps_conservative_recall_fields_when_gate_disabled(
    monkeypatch, app_recall_gate_disabled
):
    memory_mgr = _get_closure_value(app_recall_gate_disabled, "memory_mgr")

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        return ("暂无相关用户记忆", ["legacy-ignored"], 0, 0, 0)

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app_recall_gate_disabled),
        base_url="http://test",
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住宿怎么安排比较好", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"type": "memory_recall"' in resp.text
    assert '"gate": false' in resp.text
    assert '"gate_needs_recall": false' in resp.text
    assert '"gate_intent_type": "no_recall_needed"' in resp.text
    assert '"final_recall_decision": "no_recall_applied"' in resp.text


@pytest.mark.asyncio
async def test_chat_stream_falls_back_to_default_retrieval_plan_when_query_tool_invalid(
    monkeypatch, app
):
    memory_mgr = _get_closure_value(app, "memory_mgr")

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        retrieval_plan = kwargs["retrieval_plan"]
        assert isinstance(retrieval_plan, RecallRetrievalPlan)
        assert retrieval_plan.fallback_used == "none"
        assert retrieval_plan.source == "episode_slice"
        return "暂无相关用户记忆", MemoryRecallTelemetry(
            query_plan={
                "source": retrieval_plan.source,
                "buckets": list(retrieval_plan.buckets),
                "domains": list(retrieval_plan.domains),
                "destination": retrieval_plan.destination,
                "top_k": retrieval_plan.top_k,
            },
            query_plan_fallback=retrieval_plan.fallback_used,
        )

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class InvalidQueryPlanProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_recall":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "needs_recall": True,
                            "intent_type": "profile_preference_recall",
                            "reason": "need_preference_memory",
                            "confidence": 0.92,
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_query",
                    name=tool_name,
                    arguments={
                        "source": "episode_slice",
                        "domains": ["hotel"],
                        "destination": "",
                        "keywords": ["住宿"],
                        "top_k": 5,
                        "reason": "bad source",
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)
    monkeypatch.setattr("main.create_llm_provider", lambda _config: InvalidQueryPlanProvider())

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住宿怎么安排比较好", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"type": "memory_recall"' in resp.text
    assert '"query_plan_fallback": "none"' in resp.text
    assert '"fallback_used": "none"' in resp.text
    assert '"query_plan": {"source": "episode_slice", "buckets": []' in resp.text


@pytest.mark.asyncio
async def test_chat_stream_falls_back_to_default_retrieval_plan_when_query_tool_times_out(
    monkeypatch, app
):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    observed = {"recall_calls": []}

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        retrieval_plan = kwargs["retrieval_plan"]
        assert isinstance(retrieval_plan, RecallRetrievalPlan)
        assert retrieval_plan.source == "profile"
        assert kwargs["query_plan_source"] == "heuristic_fallback"
        assert kwargs["query_plan_fallback"] == "query_plan_timeout"
        return "暂无相关用户记忆", MemoryRecallTelemetry(
            query_plan={
                "source": retrieval_plan.source,
                "buckets": list(retrieval_plan.buckets),
                "domains": list(retrieval_plan.domains),
                "destination": retrieval_plan.destination,
                "top_k": retrieval_plan.top_k,
            },
            query_plan_source=kwargs["query_plan_source"],
            query_plan_fallback=kwargs["query_plan_fallback"],
        )

    async def fake_collect_forced_tool_call_arguments(llm, *, messages, tool_def):
        tool_name = tool_def["name"]
        if tool_name in {"decide_memory_recall", "build_recall_retrieval_plan"}:
            observed["recall_calls"].append(tool_name)
        if tool_name == "decide_memory_recall":
            return {
                "needs_recall": True,
                "intent_type": "profile_preference_recall",
                "reason": "need_preference_memory",
                "confidence": 0.92,
            }
        if tool_name == "build_recall_retrieval_plan":
            raise asyncio.TimeoutError
        return {
            "should_extract": False,
            "routes": {"profile": False, "working_memory": False},
            "reason": "trip_state_only",
            "message": "本轮只是当前行程事实",
        }

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("main._collect_forced_tool_call_arguments", fake_collect_forced_tool_call_arguments)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住宿怎么安排比较好", "user_id": "u1"},
        )

    assert observed["recall_calls"] == [
        "decide_memory_recall",
        "build_recall_retrieval_plan",
    ]
    assert resp.status_code == 200
    assert '"type": "memory_recall"' in resp.text
    assert '"query_plan_source": "heuristic_fallback"' in resp.text
    assert '"query_plan_fallback": "query_plan_timeout"' in resp.text
    assert '"fallback_used": "query_plan_timeout"' in resp.text


@pytest.mark.asyncio
async def test_chat_stream_falls_back_to_default_retrieval_plan_when_query_tool_errors(
    monkeypatch, app
):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    observed = {"recall_calls": []}

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        retrieval_plan = kwargs["retrieval_plan"]
        assert isinstance(retrieval_plan, RecallRetrievalPlan)
        assert retrieval_plan.source == "profile"
        assert kwargs["query_plan_source"] == "heuristic_fallback"
        assert kwargs["query_plan_fallback"] == "query_plan_error"
        return "暂无相关用户记忆", MemoryRecallTelemetry(
            query_plan={
                "source": retrieval_plan.source,
                "buckets": list(retrieval_plan.buckets),
                "domains": list(retrieval_plan.domains),
                "destination": retrieval_plan.destination,
                "top_k": retrieval_plan.top_k,
            },
            query_plan_source=kwargs["query_plan_source"],
            query_plan_fallback=kwargs["query_plan_fallback"],
        )

    async def fake_collect_forced_tool_call_arguments(llm, *, messages, tool_def):
        tool_name = tool_def["name"]
        if tool_name in {"decide_memory_recall", "build_recall_retrieval_plan"}:
            observed["recall_calls"].append(tool_name)
        if tool_name == "decide_memory_recall":
            return {
                "needs_recall": True,
                "intent_type": "profile_preference_recall",
                "reason": "need_preference_memory",
                "confidence": 0.92,
            }
        if tool_name == "build_recall_retrieval_plan":
            raise RuntimeError("query tool boom")
        return {
            "should_extract": False,
            "routes": {"profile": False, "working_memory": False},
            "reason": "trip_state_only",
            "message": "本轮只是当前行程事实",
        }

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("main._collect_forced_tool_call_arguments", fake_collect_forced_tool_call_arguments)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住宿怎么安排比较好", "user_id": "u1"},
        )

    assert observed["recall_calls"] == [
        "decide_memory_recall",
        "build_recall_retrieval_plan",
    ]
    assert resp.status_code == 200
    assert '"type": "memory_recall"' in resp.text
    assert '"query_plan_source": "heuristic_fallback"' in resp.text
    assert '"query_plan_fallback": "query_plan_error"' in resp.text
    assert '"fallback_used": "query_plan_error"' in resp.text


@pytest.mark.asyncio
async def test_chat_stream_passes_stage0_style_signals_to_heuristic_fallback(
    monkeypatch, app
):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    observed = {"recall_calls": []}

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        retrieval_plan = kwargs["retrieval_plan"]
        assert isinstance(retrieval_plan, RecallRetrievalPlan)
        assert retrieval_plan.source == "profile"
        assert retrieval_plan.domains == ["flight"]
        assert kwargs["query_plan_source"] == "heuristic_fallback"
        assert kwargs["query_plan_fallback"] == "query_plan_timeout"
        assert kwargs["stage0_matched_rule"] == "P1"
        assert kwargs["stage0_signals"]["style"] == ["老样子"]
        return "暂无相关用户记忆", MemoryRecallTelemetry(
            stage0_matched_rule=kwargs["stage0_matched_rule"],
            stage0_signals=kwargs["stage0_signals"],
            query_plan={
                "source": retrieval_plan.source,
                "buckets": list(retrieval_plan.buckets),
                "domains": list(retrieval_plan.domains),
                "destination": retrieval_plan.destination,
                "top_k": retrieval_plan.top_k,
            },
            query_plan_source=kwargs["query_plan_source"],
            query_plan_fallback=kwargs["query_plan_fallback"],
        )

    async def fake_collect_forced_tool_call_arguments(llm, *, messages, tool_def):
        tool_name = tool_def["name"]
        if tool_name in {"decide_memory_recall", "build_recall_retrieval_plan"}:
            observed["recall_calls"].append(tool_name)
        if tool_name == "build_recall_retrieval_plan":
            raise asyncio.TimeoutError
        return {
            "should_extract": False,
            "routes": {"profile": False, "working_memory": False},
            "reason": "trip_state_only",
            "message": "本轮只是当前行程事实",
        }

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("main._collect_forced_tool_call_arguments", fake_collect_forced_tool_call_arguments)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "老样子给我订机票", "user_id": "u1"},
        )

    assert observed["recall_calls"] == ["build_recall_retrieval_plan"]
    assert resp.status_code == 200
    assert '"stage0_matched_rule": "P1"' in resp.text
    assert '"stage0_signals": {"history": [], "style": ["老样子"]' in resp.text
    assert '"query_plan_source": "heuristic_fallback"' in resp.text


@pytest.mark.asyncio
async def test_query_plan_prompt_includes_recent_user_window_for_context(
    monkeypatch, app
):
    observed_prompt = {"content": ""}

    async def fake_collect_forced_tool_call_arguments(llm, *, messages, tool_def):
        tool_name = tool_def["name"]
        if tool_name == "decide_memory_recall":
            return {
                "needs_recall": True,
                "intent_type": "profile_preference_recall",
                "reason": "need_preference_memory",
                "confidence": 0.92,
            }
        if tool_name == "build_recall_retrieval_plan":
            observed_prompt["content"] = messages[0].content
            return {
                "source": "profile",
                "buckets": ["constraints", "rejections", "stable_preferences"],
                "domains": ["hotel"],
                "destination": "",
                "keywords": ["住宿"],
                "top_k": 3,
                "reason": "profile_preference_recall -> hotel preference profile",
            }
        return {
            "should_extract": False,
            "routes": {"profile": False, "working_memory": False},
            "reason": "trip_state_only",
            "message": "本轮只是当前行程事实",
        }

    monkeypatch.setattr("main._collect_forced_tool_call_arguments", fake_collect_forced_tool_call_arguments)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        await client.post(
            f"/api/chat/{session_id}",
            json={"message": "还是按我以前住酒店的习惯来", "user_id": "u1"},
        )
        await client.post(
            f"/api/chat/{session_id}",
            json={"message": "换个吧", "user_id": "u1"},
        )

    assert "recent_user_window" in observed_prompt["content"]
    assert "还是按我以前住酒店的习惯来" in observed_prompt["content"]
    assert "换个吧" in observed_prompt["content"]


@pytest.mark.asyncio
async def test_chat_stream_reuses_gate_memory_summary_for_query_prompt(
    monkeypatch, app_extraction_disabled
):
    build_recall_retrieval_plan = _get_closure_value(
        app_extraction_disabled, "_build_recall_retrieval_plan"
    )
    original_summary = _get_function_closure_value(
        build_recall_retrieval_plan, "_build_gate_memory_summary"
    )
    call_count = {"count": 0}

    async def counting_summary(*args, **kwargs):
        call_count["count"] += 1
        return await original_summary(*args, **kwargs)

    _set_function_closure_value(
        build_recall_retrieval_plan, "_build_gate_memory_summary", counting_summary
    )

    async def fake_collect_forced_tool_call_arguments(llm, *, messages, tool_def):
        tool_name = tool_def["name"]
        if tool_name == "decide_memory_recall":
            return {
                "needs_recall": True,
                "intent_type": "profile_preference_recall",
                "reason": "need_preference_memory",
                "confidence": 0.92,
            }
        if tool_name == "build_recall_retrieval_plan":
            return {
                "source": "profile",
                "buckets": ["constraints", "rejections", "stable_preferences"],
                "domains": ["hotel"],
                "destination": "",
                "keywords": ["住宿"],
                "top_k": 3,
                "reason": "profile_preference_recall -> hotel preference profile",
            }
        return {
            "should_extract": False,
            "routes": {"profile": False, "working_memory": False},
            "reason": "trip_state_only",
            "message": "本轮只是当前行程事实",
        }

    monkeypatch.setattr("main._collect_forced_tool_call_arguments", fake_collect_forced_tool_call_arguments)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app_extraction_disabled), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "住宿还是按我常规偏好来", "user_id": "u1"},
            )
        assert resp.status_code == 200
        assert call_count["count"] == 1
    finally:
        _set_function_closure_value(
            build_recall_retrieval_plan, "_build_gate_memory_summary", original_summary
        )


@pytest.mark.asyncio
async def test_chat_stream_keeps_stage0_force_recall_when_recall_gate_disabled(
    monkeypatch, app_recall_gate_disabled
):
    memory_mgr = _get_closure_value(app_recall_gate_disabled, "memory_mgr")

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        assert kwargs["recall_gate"] is True
        assert kwargs["short_circuit"] == "force_recall"
        return "暂无相关用户记忆", MemoryRecallTelemetry()

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app_recall_gate_disabled),
        base_url="http://test",
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "我是不是说过不坐红眼航班？", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"type": "memory_recall"' in resp.text
    assert '"stage0_decision": "force_recall"' in resp.text
    assert '"stage0_reason": "explicit_profile_history_query"' in resp.text
    assert '"gate": true' in resp.text
    assert '"gate_needs_recall": true' in resp.text
    assert '"gate_intent_type": ""' in resp.text
    assert '"gate_reason": "explicit_profile_history_query"' in resp.text
    assert '"final_recall_decision": "query_recall_enabled"' in resp.text
    assert '"query_plan_fallback": "fallback_default_plan"' in resp.text
    assert '"fallback_used": "fallback_default_plan"' in resp.text
    assert '"query_plan": {"buckets": ["constraints", "rejections", "stable_preferences"]' in resp.text


@pytest.mark.asyncio
async def test_chat_stream_keeps_undecided_conservative_when_recall_gate_disabled(
    monkeypatch, app_recall_gate_disabled
):
    memory_mgr = _get_closure_value(app_recall_gate_disabled, "memory_mgr")

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        assert kwargs["recall_gate"] is False
        assert kwargs["short_circuit"] == "undecided"
        return "暂无相关用户记忆", MemoryRecallTelemetry()

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app_recall_gate_disabled),
        base_url="http://test",
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住宿怎么安排比较好", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"type": "memory_recall"' in resp.text
    assert '"stage0_decision": "undecided"' in resp.text
    assert '"stage0_reason": "needs_llm_gate_recommend"' in resp.text
    assert '"gate": false' in resp.text
    assert '"gate_needs_recall": false' in resp.text
    assert '"gate_intent_type": "no_recall_needed"' in resp.text
    assert '"gate_reason": "recall_gate_disabled"' in resp.text
    assert '"final_recall_decision": "no_recall_applied"' in resp.text


@pytest.mark.asyncio
async def test_chat_stream_keeps_stage0_skip_without_polluting_gate_intent_type(
    monkeypatch, app
):
    memory_mgr = _get_closure_value(app, "memory_mgr")

    class NoRecallGateProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_recall":
                raise AssertionError("stage0 skip_recall should bypass recall gate")
            yield LLMChunk(type=ChunkType.DONE)

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        assert kwargs["recall_gate"] is False
        assert kwargs["short_circuit"] == "skip_recall"
        return "暂无相关用户记忆", MemoryRecallTelemetry()

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)
    monkeypatch.setattr("main.create_llm_provider", lambda _config: NoRecallGateProvider())

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "这次预算多少？", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"stage0_decision": "skip_recall"' in resp.text
    assert '"stage0_reason": "current_trip_fact_question"' in resp.text
    assert '"gate_needs_recall": false' in resp.text
    assert '"gate_intent_type": ""' in resp.text
    assert '"gate_reason": "current_trip_fact_question"' in resp.text


@pytest.mark.asyncio
async def test_chat_stream_uses_stage0_force_recall_for_explicit_history_query(
    monkeypatch, app
):
    memory_mgr = _get_closure_value(app, "memory_mgr")

    class NoRecallGateProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_recall":
                raise AssertionError("stage0 force_recall should bypass recall gate")
            yield LLMChunk(type=ChunkType.DONE)

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        assert kwargs["recall_gate"] is True
        assert kwargs["short_circuit"] == "force_recall"
        return "暂无相关用户记忆", MemoryRecallTelemetry()

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)
    monkeypatch.setattr("main.create_llm_provider", lambda _config: NoRecallGateProvider())

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "我是不是说过不坐红眼航班？", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"type": "memory_recall"' in resp.text
    assert '"stage0_decision": "force_recall"' in resp.text
    assert '"stage0_reason": "explicit_profile_history_query"' in resp.text
    assert '"gate_needs_recall": true' in resp.text
    assert '"gate_intent_type": ""' in resp.text
    assert '"final_recall_decision": "query_recall_enabled"' in resp.text
    assert '"query_plan_fallback": "fallback_default_plan"' in resp.text
    assert '"fallback_used": "fallback_default_plan"' in resp.text
    assert '"query_plan": {"buckets": ["constraints", "rejections", "stable_preferences"]' in resp.text


@pytest.mark.asyncio
async def test_chat_stream_keeps_conservative_recall_fields_for_invalid_gate_payload(
    monkeypatch, app
):
    memory_mgr = _get_closure_value(app, "memory_mgr")

    class InvalidRecallGateProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_recall":
                yield LLMChunk(type=ChunkType.DONE)
                return
            yield LLMChunk(type=ChunkType.DONE)

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        assert kwargs["recall_gate"] is True
        assert kwargs["retrieval_plan"] is None
        assert kwargs["query_plan_source"] == "heuristic_fallback"
        assert kwargs["query_plan_fallback"] == "invalid_tool_payload_heuristic_recall"
        return "暂无相关用户记忆", MemoryRecallTelemetry()

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)
    monkeypatch.setattr("main.create_llm_provider", lambda _config: InvalidRecallGateProvider())

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住宿怎么安排比较好", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"type": "memory_recall"' in resp.text
    assert '"gate": true' in resp.text
    assert '"gate_needs_recall": true' in resp.text
    assert '"gate_intent_type": "gate_decision_unavailable"' in resp.text
    assert '"fallback_used": "invalid_tool_payload_heuristic_recall"' in resp.text
    assert '"query_plan_source": "heuristic_fallback"' in resp.text
    assert '"final_recall_decision": "query_recall_enabled"' in resp.text


@pytest.mark.asyncio
async def test_chat_stream_treats_mixed_ambiguous_gate_intent_as_recall(
    monkeypatch, app
):
    memory_mgr = _get_closure_value(app, "memory_mgr")

    class MixedAmbiguousRecallGateProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_recall":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "needs_recall": False,
                            "intent_type": "mixed_or_ambiguous",
                            "reason": "ambiguous between current trip and preference",
                            "confidence": 0.42,
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_query",
                    name=tool_name,
                    arguments={
                        "source": "profile",
                        "buckets": ["constraints", "rejections", "stable_preferences"],
                        "domains": ["hotel"],
                        "destination": "",
                        "keywords": ["住宿"],
                        "top_k": 5,
                        "reason": "ambiguous gate uses conservative profile recall",
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        assert kwargs["recall_gate"] is True
        assert kwargs["retrieval_plan"].source == "profile"
        return "暂无相关用户记忆", MemoryRecallTelemetry()

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)
    monkeypatch.setattr(
        "main.create_llm_provider", lambda _config: MixedAmbiguousRecallGateProvider()
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住宿怎么安排比较好", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"type": "memory_recall"' in resp.text
    assert '"gate": true' in resp.text
    assert '"gate_needs_recall": true' in resp.text
    assert '"gate_intent_type": "mixed_or_ambiguous"' in resp.text
    assert '"fallback_used": "mixed_or_ambiguous_conservative_recall"' in resp.text
    assert '"final_recall_decision": "query_recall_enabled"' in resp.text


@pytest.mark.asyncio
async def test_chat_stream_falls_back_when_recall_gate_times_out(monkeypatch, app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    real_wait_for = asyncio.wait_for

    class TimeoutRecallGateProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_recall":
                await asyncio.sleep(0.05)
            yield LLMChunk(type=ChunkType.DONE)

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        return "暂无相关用户记忆", MemoryRecallTelemetry()

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)
    monkeypatch.setattr("main.create_llm_provider", lambda _config: TimeoutRecallGateProvider())
    monkeypatch.setattr(
        "main.asyncio.wait_for",
        lambda awaitable, timeout: real_wait_for(awaitable, timeout=0.01),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住宿怎么安排比较好", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"type": "memory_recall"' in resp.text
    assert '"gate": true' in resp.text
    assert '"gate_needs_recall": true' in resp.text
    assert '"gate_intent_type": "gate_decision_unavailable"' in resp.text
    assert '"fallback_used": "gate_timeout_heuristic_recall"' in resp.text
    assert '"query_plan_source": "heuristic_fallback"' in resp.text
    assert '"final_recall_decision": "query_recall_enabled"' in resp.text


@pytest.mark.asyncio
async def test_chat_stream_uses_heuristic_recall_when_gate_times_out_with_profile_cue(
    monkeypatch, app
):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    observed = {"recall_calls": []}

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        assert kwargs["recall_gate"] is True
        assert kwargs["retrieval_plan"] is None
        assert kwargs["query_plan_source"] == "heuristic_fallback"
        assert kwargs["query_plan_fallback"] == "gate_timeout_heuristic_recall"
        return "暂无相关用户记忆", MemoryRecallTelemetry(
            final_recall_decision="query_recall_enabled",
            query_plan_source=kwargs["query_plan_source"],
            query_plan_fallback=kwargs["query_plan_fallback"],
            candidate_count=1,
        )

    async def fake_collect_forced_tool_call_arguments(llm, *, messages, tool_def):
        tool_name = tool_def["name"]
        if tool_name in {"decide_memory_recall", "build_recall_retrieval_plan"}:
            observed["recall_calls"].append(tool_name)
        if tool_name == "decide_memory_recall":
            raise asyncio.TimeoutError
        if tool_name == "build_recall_retrieval_plan":
            raise AssertionError("gate failure heuristic recall should skip query tool")
        return {
            "should_extract": False,
            "routes": {"profile": False, "working_memory": False},
            "reason": "trip_state_only",
            "message": "本轮只是当前行程事实",
        }

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("main._collect_forced_tool_call_arguments", fake_collect_forced_tool_call_arguments)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "我不住青旅吗？", "user_id": "u1"},
        )

    assert observed["recall_calls"] == ["decide_memory_recall"]
    assert resp.status_code == 200
    assert '"gate": true' in resp.text
    assert '"fallback_used": "gate_timeout_heuristic_recall"' in resp.text
    assert '"query_plan_source": "heuristic_fallback"' in resp.text
    assert '"query_plan_fallback": "gate_timeout_heuristic_recall"' in resp.text
    assert '"final_recall_decision": "query_recall_enabled"' in resp.text


@pytest.mark.asyncio
async def test_chat_stream_falls_back_when_recall_gate_errors(monkeypatch, app):
    memory_mgr = _get_closure_value(app, "memory_mgr")

    class ErrorRecallGateProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_recall":
                raise RuntimeError("gate boom")
            yield LLMChunk(type=ChunkType.DONE)

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    async def fake_generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        **kwargs,
    ):
        return "暂无相关用户记忆", MemoryRecallTelemetry()

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)
    monkeypatch.setattr("main.create_llm_provider", lambda _config: ErrorRecallGateProvider())

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住宿怎么安排比较好", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"type": "memory_recall"' in resp.text
    assert '"gate": true' in resp.text
    assert '"gate_needs_recall": true' in resp.text
    assert '"gate_intent_type": "gate_decision_unavailable"' in resp.text
    assert '"fallback_used": "gate_error_heuristic_recall"' in resp.text
    assert '"query_plan_source": "heuristic_fallback"' in resp.text
    assert '"final_recall_decision": "query_recall_enabled"' in resp.text


def test_project_overview_documents_memory_recall_payload_fields():
    overview = Path(__file__).resolve().parents[2] / "PROJECT_OVERVIEW.md"
    content = overview.read_text(encoding="utf-8")

    assert "| `memory_recall` |" in content
    assert "| Memory Recall SSE |" in content

    for field in (
        "gate",
        "stage0_decision",
        "stage0_reason",
        "stage0_matched_rule",
        "stage0_signals",
        "gate_needs_recall",
        "gate_intent_type",
        "gate_confidence",
        "gate_reason",
        "final_recall_decision",
        "fallback_used",
        "recall_skip_source",
        "query_plan_source",
        "recall_attempted_but_zero_hit",
    ):
        assert field in content


@pytest.mark.asyncio
async def test_append_archived_trip_episode_once_is_idempotent(app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    sessions = _get_closure_value(app, "sessions")

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        plan = sessions[session_id]["plan"]
        plan.phase = 7
        plan.destination = "Tokyo"
        plan.trip_id = "trip_tokyo"
        plan.decision_events = [
            {
                "type": "accepted",
                "category": "skeleton",
                "value": {"id": "balanced"},
                "reason": "selected",
                "timestamp": "2026-05-03T00:00:00+00:00",
            }
        ]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("agent.loop.AgentLoop.run", fake_run)
            first = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "完成规划", "user_id": "u1"},
            )
            second = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "再次确认", "user_id": "u1"},
            )

    episodes = await memory_mgr.v3_store.list_episodes("u1")
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(episodes) == 1
    assert episodes[0].session_id == session_id
    assert episodes[0].id == "ep_trip_tokyo"
    assert episodes[0].trip_id == "trip_tokyo"
    assert episodes[0].decision_log


@pytest.mark.asyncio
async def test_memory_audit_events_write_to_v3_store(app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    await memory_mgr.v3_store.append_event(
        MemoryAuditEvent(
            id="evt-reject-phase-output",
            user_id="u1",
            session_id="sess-1",
            event_type="reject",
            object_type="phase_output",
            object_payload={"to_phase": 3},
            reason_text="用户要求回退",
            created_at="2026-04-22T10:00:00Z",
        )
    )

    path = Path(memory_mgr.v3_store.data_dir) / "users" / "u1" / "memory" / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        MemoryAuditEvent(
            id="evt-reject-phase-output",
            user_id="u1",
            session_id="sess-1",
            event_type="reject",
            object_type="phase_output",
            object_payload={"to_phase": 3},
            reason_text="用户要求回退",
            created_at="2026-04-22T10:00:00Z",
        ).to_dict()
    ]


@pytest.mark.asyncio
async def test_phase7_archive_generates_episode_slices_once(app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    sessions = _get_closure_value(app, "sessions")

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        plan = sessions[session_id]["plan"]
        plan.phase = 7
        plan.trip_id = "trip_tokyo"
        plan.destination = "Tokyo"
        plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
        plan.budget = Budget(total=12000, currency="CNY")
        plan.skeleton_plans = [
            {
                "id": "balanced",
                "summary": "住新宿，白天分区游览，晚上控制移动距离。",
            }
        ]
        plan.selected_skeleton_id = "balanced"
        plan.decision_events = [
            {
                "type": "accepted",
                "category": "skeleton",
                "value": {"id": "balanced"},
                "reason": "selected",
                "timestamp": "2026-05-03T00:00:00+00:00",
            },
            {
                "type": "rejected",
                "category": "hotel",
                "value": {"name": "远离地铁的酒店"},
                "reason": "移动不方便",
                "timestamp": "2026-05-03T00:00:00+00:00",
            },
        ]
        plan.lesson_events = [
            {
                "kind": "pitfall",
                "content": "晚上跨区移动太累。",
                "timestamp": "2026-05-03T00:00:00+00:00",
            }
        ]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("agent.loop.AgentLoop.run", fake_run)
            first = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "完成规划", "user_id": "u1"},
            )
            second = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "再次确认", "user_id": "u1"},
            )

    episodes = await memory_mgr.v3_store.list_episodes("u1")
    slices = await memory_mgr.v3_store.list_episode_slices("u1")
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(episodes) == 1
    assert episodes[0].decision_log
    assert all(slice_.source_episode_id == episodes[0].id for slice_ in slices)
    assert {slice_.slice_type for slice_ in slices} == {
        "itinerary_pattern",
        "budget_signal",
        "rejected_option",
        "pitfall",
    }
    assert all(slice_.entities["destination"] == "Tokyo" for slice_ in slices)
    legacy_path = Path(memory_mgr.v3_store.data_dir) / "users" / "u1" / "trip_episodes.jsonl"
    assert not legacy_path.exists()


@pytest.mark.asyncio
async def test_v3_memory_cutover_cleanup_once_removes_legacy_files(app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    cleanup_once = app.state._run_v3_memory_cutover_cleanup_once
    user_dir = Path(memory_mgr.v3_store.data_dir) / "users" / "u1"
    user_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("memory.json", "memory_events.jsonl", "trip_episodes.jsonl"):
        (user_dir / filename).write_text("legacy", encoding="utf-8")
    keep = user_dir / "memory" / "profile.json"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("{}", encoding="utf-8")

    await cleanup_once()

    assert not (user_dir / "memory.json").exists()
    assert not (user_dir / "memory_events.jsonl").exists()
    assert not (user_dir / "trip_episodes.jsonl").exists()
    assert keep.exists()


@pytest.mark.asyncio
async def test_reset_backtrack_rotates_trip_id(app):
    rotate_trip = _get_closure_value(app, "_rotate_trip_on_reset_backtrack")
    plan = TravelPlanState(session_id="s1", trip_id="trip-old", phase=1)

    changed = await rotate_trip(
        user_id="u1",
        plan=plan,
        to_phase=1,
        reason_text="重新开始，换个目的地",
    )

    assert changed is True
    assert plan.trip_id != "trip-old"


@pytest.mark.asyncio
async def test_non_reset_backtrack_reuses_trip_memory(app):
    rotate_trip = _get_closure_value(app, "_rotate_trip_on_reset_backtrack")
    plan = TravelPlanState(session_id="s1", trip_id="trip-old", phase=3)

    changed = await rotate_trip(
        user_id="u1",
        plan=plan,
        to_phase=3,
        reason_text="改日期",
    )

    assert changed is False
    assert plan.trip_id == "trip-old"


@pytest.mark.asyncio
async def test_tool_backtrack_reset_rotates_trip_memory(monkeypatch, app):
    sessions = _get_closure_value(app, "sessions")

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call=ToolCall(
                id="tc1",
                name="request_backtrack",
                arguments={
                    "to_phase": 1,
                    "reason": "用户想换目的地",
                },
            ),
        )
        self.plan.phase = 1
        yield LLMChunk(
            type=ChunkType.TOOL_RESULT,
            tool_result=ToolResult(
                tool_call_id="tc1",
                status="success",
                data={
                    "backtracked": True,
                    "from_phase": 3,
                    "to_phase": 1,
                    "reason": "用户想换目的地",
                },
            ),
        )
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        plan = sessions[session_id]["plan"]
        plan.phase = 3
        plan.trip_id = "trip-old"
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "换个目的地", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert plan.trip_id != "trip-old"


@pytest.mark.asyncio
async def test_chat_stream_does_not_embed_background_memory_tasks(app):
    observed = {"extraction_calls": []}

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="助手回复")
        yield LLMChunk(type=ChunkType.DONE)

    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": False,
                            "reason": "no_reusable_memory_signal",
                            "message": "本轮未发现可复用记忆信号",
                        },
                    ),
                )
            elif tool_name in {
                "extract_memory_candidates",
                "extract_profile_memory",
                "extract_working_memory",
            }:
                observed["extraction_calls"].append(tool_name)
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_memory",
                        name=tool_name,
                        arguments={
                            "profile_updates": {
                                "constraints": [],
                                "rejections": [],
                                "stable_preferences": [],
                                "preference_hypotheses": [],
                            },
                            "working_memory": [],
                        },
                    ),
                )
            yield LLMChunk(type=ChunkType.DONE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: ExtractionProvider())

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "住宿怎么安排比较好", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)

    assert resp.status_code == 200
    assert '"type": "done"' in resp.text
    assert '"kind": "memory_recall"' in resp.text
    assert '"kind": "memory_extraction_gate"' not in resp.text
    assert '"kind": "memory_extraction"' not in resp.text
    assert observed["extraction_calls"] == []


@pytest.mark.asyncio
async def test_memory_extraction_logs_non_structured_response(app, caplog):
    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": True, "working_memory": False},
                            "reason": "explicit_preference_signal",
                            "message": "检测到可复用偏好信号",
                        },
                    ),
                )
            yield LLMChunk(type=ChunkType.DONE)

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: ExtractionProvider())

        with caplog.at_level(logging.WARNING, logger="main"):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                session_resp = await client.post("/api/sessions")
                session_id = session_resp.json()["session_id"]
                resp = await client.post(
                    f"/api/chat/{session_id}",
                    json={"message": "我不吃辣，也不要住青旅", "user_id": "u1"},
                )
                await _wait_for_memory_scheduler_idle(app, session_id)

    assert resp.status_code == 200
    assert any(
        "记忆提取未产生任何结构化结果" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_memory_extraction_reuses_primary_llm_config(app):
    config = _get_closure_value(app, "config")
    seen_configs = []

    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content='{"unexpected": true}')
            yield LLMChunk(type=ChunkType.DONE)

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    def fake_create_llm_provider(llm_config):
        seen_configs.append(llm_config)
        return ExtractionProvider()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", fake_create_llm_provider)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我不吃辣，也不要住青旅", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)

    assert resp.status_code == 200
    assert seen_configs
    assert seen_configs[-1] == config.llm


@pytest.mark.asyncio
async def test_memory_extraction_profile_route_writes_profile_only(app):
    observed = {"calls": []}

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            observed["calls"].append(tool_name)
            if tool_name == "decide_memory_recall":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_recall_gate",
                        name=tool_name,
                        arguments={
                            "needs_recall": False,
                            "intent_type": "no_recall_needed",
                            "reason": "current_preference_statement",
                            "confidence": 0.9,
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": True, "working_memory": False},
                            "reason": "profile_memory_signal",
                            "message": "检测到长期旅行偏好信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            assert tool_name == "extract_profile_memory"
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_profile",
                    name="extract_profile_memory",
                    arguments={
                        "profile_updates": {
                            "constraints": [],
                            "rejections": [],
                            "stable_preferences": [
                                {
                                    "domain": "food",
                                    "key": "avoid_spicy",
                                    "value": "不吃辣",
                                    "polarity": "avoid",
                                    "stability": "explicit_declared",
                                    "confidence": 0.95,
                                    "reason": "用户明确声明长期饮食偏好",
                                    "evidence": "我不吃辣",
                                }
                            ],
                            "preference_hypotheses": [],
                        },
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: ExtractionProvider())

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我不吃辣", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)
            profile = await client.get("/api/memory/u1/profile")
            working = await client.get(
                f"/api/memory/u1/sessions/{session_id}/working-memory"
            )

    assert resp.status_code == 200
    assert profile.status_code == 200
    assert working.status_code == 200
    assert observed["calls"] == [
        "decide_memory_recall",
        "decide_memory_extraction",
        "extract_profile_memory",
    ]
    assert profile.json()["stable_preferences"][0]["key"] == "avoid_spicy"
    assert working.json()["items"] == []


@pytest.mark.asyncio
async def test_memory_extraction_profile_route_normalizes_recall_metadata(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_recall":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_recall_gate",
                        name=tool_name,
                        arguments={
                            "needs_recall": False,
                            "intent_type": "no_recall_needed",
                            "reason": "current_preference_statement",
                            "confidence": 0.91,
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": True, "working_memory": False},
                            "reason": "profile_memory_signal",
                            "message": "检测到长期饮食偏好信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            assert tool_name == "extract_profile_memory"
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_profile",
                    name="extract_profile_memory",
                    arguments={
                        "profile_updates": {
                            "constraints": [],
                            "rejections": [],
                            "stable_preferences": [
                                {
                                    "domain": "food",
                                    "key": "dislike_spicy_food",
                                    "value": "不吃辣",
                                    "polarity": "avoid",
                                    "stability": "explicit_declared",
                                    "confidence": 0.95,
                                    "reason": "用户明确声明长期饮食偏好",
                                    "evidence": "我不吃辣",
                                    "applicability": "",
                                    "recall_hints": {
                                        "domains": [],
                                        "keywords": ["不吃辣"],
                                        "aliases": ["忌辣", "不能吃辣"],
                                    },
                                    "source_refs": [
                                        {
                                            "kind": "message",
                                            "session_id": "s1",
                                            "quote": "我不吃辣",
                                        },
                                        {
                                            "kind": "message",
                                            "session_id": "s1",
                                            "quote": "以后都别推荐辣的",
                                        },
                                    ],
                                }
                            ],
                            "preference_hypotheses": [],
                        },
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: ExtractionProvider())

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我不吃辣，以后都别推荐辣的", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)
            profile = await client.get("/api/memory/u1/profile")

    assert resp.status_code == 200
    assert profile.status_code == 200
    item = profile.json()["stable_preferences"][0]
    assert item["key"] == "avoid_spicy"
    assert item["applicability"] == "适用于大多数旅行。"
    assert item["recall_hints"] == {
        "domains": ["food"],
        "keywords": ["不吃辣"],
        "aliases": ["忌辣", "不能吃辣", "不吃辣", "避开辣味"],
    }
    assert item["source_refs"] == [
        {"kind": "message", "session_id": "s1", "quote": "我不吃辣"},
        {"kind": "message", "session_id": "s1", "quote": "以后都别推荐辣的"},
    ]


@pytest.mark.asyncio
async def test_memory_extraction_working_route_writes_working_only(app):
    observed = {"calls": []}

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            observed["calls"].append(tool_name)
            if tool_name == "decide_memory_recall":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_recall_gate",
                        name=tool_name,
                        arguments={
                            "needs_recall": False,
                            "intent_type": "no_recall_needed",
                            "reason": "current_turn_instruction",
                            "confidence": 0.9,
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": False, "working_memory": True},
                            "reason": "working_memory_signal",
                            "message": "检测到当前会话临时记忆信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            assert tool_name == "extract_working_memory"
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_working",
                    name="extract_working_memory",
                    arguments={
                        "working_memory": [
                            {
                                "phase": 3,
                                "kind": "temporary_rejection",
                                "domains": ["attraction"],
                                "content": "这轮先别考虑迪士尼",
                                "reason": "当前候选筛选需要避让",
                                "status": "active",
                                "expires": {
                                    "on_session_end": True,
                                    "on_trip_change": True,
                                    "on_phase_exit": False,
                                },
                            }
                        ],
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: ExtractionProvider())

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "这轮先别考虑迪士尼", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)
            profile = await client.get("/api/memory/u1/profile")
            working = await client.get(
                f"/api/memory/u1/sessions/{session_id}/working-memory"
            )

    assert resp.status_code == 200
    assert profile.status_code == 200
    assert working.status_code == 200
    assert observed["calls"] == [
        "decide_memory_recall",
        "decide_memory_extraction",
        "extract_working_memory",
    ]
    assert profile.json()["stable_preferences"] == []
    assert working.json()["items"][0]["content"] == "这轮先别考虑迪士尼"


@pytest.mark.asyncio
async def test_memory_extraction_repeated_hypothesis_promotes_to_stable_preferences(app):
    extraction_quotes = iter(["想住安静一点", "还是想住安静一点"])

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_recall":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_recall_gate",
                        name=tool_name,
                        arguments={
                            "needs_recall": False,
                            "intent_type": "no_recall_needed",
                            "reason": "current_preference_statement",
                            "confidence": 0.88,
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": True, "working_memory": False},
                            "reason": "profile_memory_signal",
                            "message": "检测到住宿偏好信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            assert tool_name == "extract_profile_memory"
            quote = next(extraction_quotes)
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id=f"tc_profile_{quote}",
                    name="extract_profile_memory",
                    arguments={
                        "profile_updates": {
                            "constraints": [],
                            "rejections": [],
                            "stable_preferences": [],
                            "preference_hypotheses": [
                                {
                                    "domain": "hotel",
                                    "key": "prefer_quiet_room",
                                    "value": True,
                                    "polarity": "prefer",
                                    "stability": "soft_constraint",
                                    "confidence": 0.82,
                                    "reason": "用户提到住宿想要安静一些",
                                    "evidence": quote,
                                    "applicability": "",
                                    "recall_hints": {
                                        "domains": ["hotel"],
                                        "keywords": ["安静房间"],
                                        "aliases": [],
                                    },
                                    "source_refs": [
                                        {
                                            "kind": "message",
                                            "session_id": "s1",
                                            "quote": quote,
                                        }
                                    ],
                                }
                            ],
                        },
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: ExtractionProvider())

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            first = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "想住安静一点", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)
            second = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "还是想住安静一点", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)
            profile = await client.get("/api/memory/u1/profile")

    assert first.status_code == 200
    assert second.status_code == 200
    assert profile.status_code == 200
    body = profile.json()
    assert body["preference_hypotheses"] == []
    assert len(body["stable_preferences"]) == 1
    item = body["stable_preferences"][0]
    assert item["key"] == "prefer_quiet_room"
    assert item["stability"] == "pattern_observed"
    assert item["status"] == "active"
    assert item["context"]["observation_count"] == 2
    assert item["source_refs"] == [
        {"kind": "message", "session_id": "s1", "quote": "想住安静一点"},
        {"kind": "message", "session_id": "s1", "quote": "还是想住安静一点"},
    ]


@pytest.mark.asyncio
async def test_memory_extraction_stable_preference_reuses_matching_hypothesis_signal(app):
    second_turn_quote = "这次住宿也想安静一点"

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class ExtractionProvider:
        def __init__(self):
            self.profile_calls = 0

        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_recall":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_recall_gate",
                        name=tool_name,
                        arguments={
                            "needs_recall": False,
                            "intent_type": "no_recall_needed",
                            "reason": "current_preference_statement",
                            "confidence": 0.9,
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": True, "working_memory": False},
                            "reason": "profile_memory_signal",
                            "message": "检测到住宿偏好信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            assert tool_name == "extract_profile_memory"
            self.profile_calls += 1
            if self.profile_calls == 1:
                arguments = {
                    "profile_updates": {
                        "constraints": [],
                        "rejections": [],
                        "stable_preferences": [
                            {
                                "domain": "hotel",
                                "key": "prefer_quiet_room",
                                "value": True,
                                "polarity": "prefer",
                                "stability": "explicit_declared",
                                "confidence": 0.93,
                                "reason": "用户明确说住宿想安静",
                                "evidence": "我住酒店想安静一点",
                                "applicability": "适用于大多数住宿选择。",
                                "recall_hints": {
                                    "domains": ["hotel"],
                                    "keywords": ["安静房间"],
                                    "aliases": ["安静一点"],
                                },
                                "source_refs": [
                                    {
                                        "kind": "message",
                                        "session_id": "s1",
                                        "quote": "我住酒店想安静一点",
                                    }
                                ],
                            }
                        ],
                        "preference_hypotheses": [],
                    }
                }
            else:
                arguments = {
                    "profile_updates": {
                        "constraints": [],
                        "rejections": [],
                        "stable_preferences": [],
                        "preference_hypotheses": [
                            {
                                "domain": "hotel",
                                "key": "prefer_quiet_room",
                                "value": True,
                                "polarity": "prefer",
                                "stability": "soft_constraint",
                                "confidence": 0.8,
                                "reason": "用户再次提到想要安静住宿",
                                "evidence": second_turn_quote,
                                "applicability": "",
                                "recall_hints": {
                                    "domains": ["hotel"],
                                    "keywords": ["安静房间"],
                                    "aliases": [],
                                },
                                "source_refs": [
                                    {
                                        "kind": "message",
                                        "session_id": "s1",
                                        "quote": second_turn_quote,
                                    }
                                ],
                            }
                        ],
                    }
                }
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id=f"tc_profile_{self.profile_calls}",
                    name="extract_profile_memory",
                    arguments=arguments,
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

    provider = ExtractionProvider()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: provider)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            first = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我住酒店想安静一点", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)
            second = await client.post(
                f"/api/chat/{session_id}",
                json={"message": second_turn_quote, "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)
            profile = await client.get("/api/memory/u1/profile")

    assert first.status_code == 200
    assert second.status_code == 200
    assert profile.status_code == 200
    body = profile.json()
    assert body["preference_hypotheses"] == []
    assert len(body["stable_preferences"]) == 1
    item = body["stable_preferences"][0]
    assert item["key"] == "prefer_quiet_room"
    assert item["status"] == "active"
    assert item["context"]["observation_count"] == 2
    assert item["source_refs"] == [
        {"kind": "message", "session_id": "s1", "quote": "我住酒店想安静一点"},
        {"kind": "message", "session_id": "s1", "quote": second_turn_quote},
    ]


@pytest.mark.asyncio
async def test_memory_extraction_no_routes_skips_extractors(app):
    observed = {"calls": []}

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            observed["calls"].append(tool_name)
            if tool_name == "decide_memory_recall":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_recall_gate",
                        name=tool_name,
                        arguments={
                            "needs_recall": False,
                            "intent_type": "no_recall_needed",
                            "reason": "current_trip_fact_question",
                            "confidence": 0.95,
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            assert tool_name == "decide_memory_extraction"
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_gate",
                    name=tool_name,
                    arguments={
                        "should_extract": False,
                        "routes": {"profile": False, "working_memory": False},
                        "reason": "trip_state_only",
                        "message": "本轮只是当前行程事实",
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: ExtractionProvider())

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "预算还是三万", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)

    assert resp.status_code == 200
    assert observed["calls"] == ["decide_memory_recall", "decide_memory_extraction"]


@pytest.mark.asyncio
async def test_memory_extraction_uses_routed_forced_tool_calls(app):
    observed = {"calls": []}

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            observed["calls"].append(
                {
                    "tool_name": tool_name,
                    "tools": tools,
                    "tool_choice": tool_choice,
                }
            )
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "reason": "explicit_preference_signal",
                            "message": "检测到可复用偏好信号",
                            "routes": {"profile": True, "working_memory": True},
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            if tool_name == "extract_profile_memory":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_profile",
                        name="extract_profile_memory",
                        arguments={
                            "profile_updates": {
                                "constraints": [],
                                "rejections": [],
                                "stable_preferences": [
                                    {
                                        "domain": "food",
                                        "key": "avoid_spicy",
                                        "value": "不吃辣",
                                        "polarity": "avoid",
                                        "stability": "explicit_declared",
                                        "confidence": 0.95,
                                        "reason": "用户明确声明长期饮食偏好",
                                        "evidence": "我不吃辣",
                                    }
                                ],
                                "preference_hypotheses": [],
                            },
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_working",
                    name="extract_working_memory",
                    arguments={
                        "working_memory": [
                            {
                                "phase": 3,
                                "kind": "temporary_rejection",
                                "domains": ["attraction"],
                                "content": "这轮先别考虑迪士尼",
                                "reason": "当前候选筛选需要避让",
                                "status": "active",
                                "expires": {
                                    "on_session_end": True,
                                    "on_trip_change": True,
                                    "on_phase_exit": False,
                                },
                            }
                        ],
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: ExtractionProvider())

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我不吃辣，也不要住青旅", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)

    assert resp.status_code == 200
    assert [call["tool_name"] for call in observed["calls"]] == [
        "decide_memory_recall",
        "decide_memory_extraction",
        "extract_profile_memory",
        "extract_working_memory",
    ]
    for call in observed["calls"]:
        assert call["tool_choice"] == {
            "type": "function",
            "function": {"name": call["tool_name"]},
        }
        assert call["tools"][0]["name"] == call["tool_name"]


@pytest.mark.asyncio
async def test_memory_extraction_publishes_split_internal_tasks(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": True, "working_memory": True},
                            "reason": "mixed_profile_and_working_signal",
                            "message": "检测到长期偏好和临时规划信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            if tool_name == "extract_profile_memory":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_profile",
                        name=tool_name,
                        arguments={
                            "profile_updates": {
                                "constraints": [],
                                "rejections": [],
                                "stable_preferences": [],
                                "preference_hypotheses": [],
                            },
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            assert tool_name == "extract_working_memory"
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_working",
                    name=tool_name,
                    arguments={"working_memory": []},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

    run_memory_job = app.state.run_memory_job
    original_publish = _get_function_closure_value(run_memory_job, "_publish_memory_task")
    published_tasks = []

    def recording_publish(session_id: str, task):
        published_tasks.append(task)
        original_publish(session_id, task)

    with pytest.MonkeyPatch.context() as mp:
        _set_function_closure_value(
            run_memory_job, "_publish_memory_task", recording_publish
        )
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: ExtractionProvider())

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我不吃辣，这轮先别考虑迪士尼", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)

    _set_function_closure_value(
        run_memory_job, "_publish_memory_task", original_publish
    )

    assert resp.status_code == 200
    task_kinds = {getattr(task, "kind", None) for task in published_tasks}
    assert "memory_extraction_gate" in task_kinds
    assert "memory_extraction" in task_kinds
    assert "profile_memory_extraction" in task_kinds
    assert "working_memory_extraction" in task_kinds


@pytest.mark.asyncio
async def test_memory_extraction_partial_failure_keeps_consumed_count_unadvanced(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": True, "working_memory": True},
                            "reason": "mixed_profile_and_working_signal",
                            "message": "检测到长期偏好和临时规划信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            if tool_name == "extract_profile_memory":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_profile",
                        name=tool_name,
                        arguments={
                            "profile_updates": {
                                "constraints": [],
                                "rejections": [],
                                "stable_preferences": [],
                                "preference_hypotheses": [],
                            },
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            raise RuntimeError("working extraction failed")

    run_memory_job = app.state.run_memory_job
    original_publish = _get_function_closure_value(run_memory_job, "_publish_memory_task")
    published_tasks = []

    def recording_publish(session_id: str, task):
        published_tasks.append(task)
        original_publish(session_id, task)

    with pytest.MonkeyPatch.context() as mp:
        _set_function_closure_value(
            run_memory_job, "_publish_memory_task", recording_publish
        )
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: ExtractionProvider())

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我不吃辣，这轮先别考虑迪士尼", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)

    _set_function_closure_value(
        run_memory_job, "_publish_memory_task", original_publish
    )

    assert resp.status_code == 200
    runtime = app.state.memory_scheduler_runtimes[session_id]
    assert runtime.last_consumed_user_count == 0
    extraction_tasks = [
        task for task in published_tasks if getattr(task, "kind", None) == "memory_extraction"
    ]
    assert extraction_tasks[-1].status == "warning"
    assert extraction_tasks[-1].result["reason"] == "partial_failure"
    assert extraction_tasks[-1].error == "working_memory_extraction_failed"


@pytest.mark.asyncio
async def test_memory_extraction_partial_save_preserves_aggregate_progress(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": True, "working_memory": False},
                            "reason": "profile_memory_signal",
                            "message": "检测到长期偏好信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            assert tool_name == "extract_profile_memory"
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_profile",
                    name=tool_name,
                    arguments={
                        "profile_updates": {
                            "constraints": [],
                            "rejections": [],
                            "stable_preferences": [],
                            "preference_hypotheses": [
                                {
                                    "domain": "hotel",
                                    "key": "prefer_quiet_room",
                                    "value": "安静房间",
                                    "polarity": "prefer",
                                    "stability": "pattern_observed",
                                    "confidence": 0.65,
                                    "reason": "用户多次提到想住安静一点",
                                    "evidence": "想住安静一点",
                                },
                                {
                                    "domain": "hotel",
                                    "key": "prefer_high_floor",
                                    "value": "高楼层",
                                    "polarity": "prefer",
                                    "stability": "pattern_observed",
                                    "confidence": 0.62,
                                    "reason": "用户提到想住高一些",
                                    "evidence": "最好高一点",
                                },
                            ],
                        },
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

    memory_mgr = _get_closure_value(app, "memory_mgr")
    original_upsert_profile_item = memory_mgr.v3_store.upsert_profile_item
    upsert_calls = {"count": 0}

    async def flaky_upsert_profile_item(user_id, bucket, item):
        upsert_calls["count"] += 1
        if upsert_calls["count"] == 2:
            raise RuntimeError("profile upsert failed on second item")
        return await original_upsert_profile_item(user_id, bucket, item)

    run_memory_job = app.state.run_memory_job
    original_publish = _get_function_closure_value(run_memory_job, "_publish_memory_task")
    published_tasks = []

    def recording_publish(session_id: str, task):
        published_tasks.append(task)
        original_publish(session_id, task)

    with pytest.MonkeyPatch.context() as mp:
        _set_function_closure_value(
            run_memory_job, "_publish_memory_task", recording_publish
        )
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: ExtractionProvider())
        mp.setattr(
            memory_mgr.v3_store,
            "upsert_profile_item",
            flaky_upsert_profile_item,
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "想住安静一点，最好高一点", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)
            profile = await client.get("/api/memory/u1/profile")

    _set_function_closure_value(
        run_memory_job, "_publish_memory_task", original_publish
    )

    assert resp.status_code == 200
    runtime = app.state.memory_scheduler_runtimes[session_id]
    assert runtime.last_consumed_user_count == 0
    assert profile.status_code == 200
    assert profile.json()["preference_hypotheses"][0]["key"] == "prefer_quiet_room"
    extraction_tasks = [
        task for task in published_tasks if getattr(task, "kind", None) == "memory_extraction"
    ]
    assert extraction_tasks[-1].status == "warning"
    assert extraction_tasks[-1].result["reason"] == "partial_failure"
    assert extraction_tasks[-1].result["saved_profile_count"] == 1
    assert extraction_tasks[-1].result["item_ids"]
    profile_tasks = [
        task
        for task in published_tasks
        if getattr(task, "kind", None) == "profile_memory_extraction"
    ]
    assert profile_tasks[-1].result["saved_profile_count"] == 1
    assert profile_tasks[-1].result["pending_profile_count"] == 1


@pytest.mark.asyncio
async def test_memory_extraction_timeout_is_emitted_as_warning(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class SlowExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": True, "working_memory": False},
                            "reason": "explicit_preference_signal",
                            "message": "检测到可复用偏好信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            await asyncio.sleep(0.05)
            yield LLMChunk(type=ChunkType.DONE)

    run_memory_job = app.state.run_memory_job
    extract_memory_candidates = app.state.extract_memory_candidates
    original_timeout = _get_function_closure_value(
        extract_memory_candidates, "_EXTRACTION_TIMEOUT_SECONDS"
    )
    original_publish = _get_function_closure_value(run_memory_job, "_publish_memory_task")
    published_tasks = []

    def recording_publish(session_id: str, task):
        published_tasks.append(task)
        original_publish(session_id, task)

    with pytest.MonkeyPatch.context() as mp:
        _set_function_closure_value(
            extract_memory_candidates, "_EXTRACTION_TIMEOUT_SECONDS", 0.01
        )
        _set_function_closure_value(
            run_memory_job, "_publish_memory_task", recording_publish
        )
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr(
            "main.create_llm_provider", lambda _config: SlowExtractionProvider()
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我不吃辣", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)

    _set_function_closure_value(
        extract_memory_candidates, "_EXTRACTION_TIMEOUT_SECONDS", original_timeout
    )
    _set_function_closure_value(
        run_memory_job, "_publish_memory_task", original_publish
    )

    assert resp.status_code == 200
    assert '"kind": "memory_extraction_gate"' not in resp.text
    assert '"kind": "memory_extraction"' not in resp.text
    extraction_tasks = [
        task for task in published_tasks if getattr(task, "kind", None) == "memory_extraction"
    ]
    assert extraction_tasks[-1].status == "warning"
    assert "记忆提取超时" in extraction_tasks[-1].message
    split_tasks = [
        task
        for task in published_tasks
        if getattr(task, "kind", None) in {
            "profile_memory_extraction",
            "working_memory_extraction",
        }
        and getattr(task, "ended_at", None) is not None
    ]
    assert split_tasks
    assert all("超时" not in task.message for task in split_tasks)


@pytest.mark.asyncio
async def test_memory_extraction_timeout_preserves_partial_progress(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class SlowWorkingProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": True, "working_memory": True},
                            "reason": "mixed_profile_and_working_signal",
                            "message": "检测到长期偏好和临时规划信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            if tool_name == "extract_profile_memory":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_profile",
                        name=tool_name,
                        arguments={
                            "profile_updates": {
                                "constraints": [],
                                "rejections": [],
                                "stable_preferences": [
                                    {
                                        "domain": "food",
                                        "key": "avoid_spicy",
                                        "value": "不吃辣",
                                        "polarity": "avoid",
                                        "stability": "explicit_declared",
                                        "confidence": 0.95,
                                        "reason": "用户明确声明长期饮食偏好",
                                        "evidence": "我不吃辣",
                                    }
                                ],
                                "preference_hypotheses": [],
                            },
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            await asyncio.sleep(0.05)
            yield LLMChunk(type=ChunkType.DONE)

    run_memory_job = app.state.run_memory_job
    extract_memory_candidates = app.state.extract_memory_candidates
    original_timeout = _get_function_closure_value(
        extract_memory_candidates, "_EXTRACTION_TIMEOUT_SECONDS"
    )
    original_publish = _get_function_closure_value(run_memory_job, "_publish_memory_task")
    published_tasks = []

    def recording_publish(session_id: str, task):
        published_tasks.append(task)
        original_publish(session_id, task)

    with pytest.MonkeyPatch.context() as mp:
        _set_function_closure_value(
            extract_memory_candidates, "_EXTRACTION_TIMEOUT_SECONDS", 0.01
        )
        _set_function_closure_value(
            run_memory_job, "_publish_memory_task", recording_publish
        )
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: SlowWorkingProvider())

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我不吃辣，这轮先别考虑迪士尼", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)
            profile = await client.get("/api/memory/u1/profile")

    _set_function_closure_value(
        extract_memory_candidates, "_EXTRACTION_TIMEOUT_SECONDS", original_timeout
    )
    _set_function_closure_value(
        run_memory_job, "_publish_memory_task", original_publish
    )

    assert resp.status_code == 200
    assert profile.status_code == 200
    assert profile.json()["stable_preferences"][0]["key"] == "avoid_spicy"
    extraction_tasks = [
        task for task in published_tasks if getattr(task, "kind", None) == "memory_extraction"
    ]
    assert extraction_tasks[-1].status == "warning"
    assert extraction_tasks[-1].result["reason"] == "timeout"
    assert extraction_tasks[-1].result["saved_profile_count"] == 1
    assert extraction_tasks[-1].message == "记忆提取超时，已保留部分写入结果，剩余内容将稍后重试。"


@pytest.mark.asyncio
async def test_memory_extraction_success_when_auto_saved_items_written(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "reason": "explicit_preference_signal",
                            "message": "检测到可复用偏好信号",
                            "routes": {"profile": True, "working_memory": True},
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            if tool_name == "extract_working_memory":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_working",
                        name=tool_name,
                        arguments={"working_memory": []},
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_profile",
                    name="extract_profile_memory",
                    arguments={
                        "profile_updates": {
                            "constraints": [],
                            "rejections": [],
                            "stable_preferences": [
                                {
                                    "domain": "food",
                                    "key": "avoid_spicy",
                                    "value": "不吃辣",
                                    "polarity": "avoid",
                                    "stability": "explicit_declared",
                                    "confidence": 0.95,
                                    "reason": "用户明确声明长期饮食偏好",
                                    "evidence": "我不吃辣",
                                }
                            ],
                            "preference_hypotheses": [],
                        },
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

    run_memory_job = app.state.run_memory_job
    original_publish = _get_function_closure_value(run_memory_job, "_publish_memory_task")
    published_tasks = []

    def recording_publish(session_id: str, task):
        published_tasks.append(task)
        original_publish(session_id, task)

    with pytest.MonkeyPatch.context() as mp:
        _set_function_closure_value(
            run_memory_job, "_publish_memory_task", recording_publish
        )
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: ExtractionProvider())

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我不吃辣", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)
            profile = await client.get("/api/memory/u1/profile")

    _set_function_closure_value(
        run_memory_job, "_publish_memory_task", original_publish
    )

    assert resp.status_code == 200
    assert '"kind": "memory_extraction_gate"' not in resp.text
    assert '"kind": "memory_extraction"' not in resp.text
    extraction_tasks = [
        task for task in published_tasks if getattr(task, "kind", None) == "memory_extraction"
    ]
    assert extraction_tasks[-1].status == "success"
    assert "已提取 1 条记忆" in extraction_tasks[-1].message
    assert profile.json()["stable_preferences"][0]["key"] == "avoid_spicy"
