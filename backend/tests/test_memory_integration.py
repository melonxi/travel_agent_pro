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
from main import (
    _memory_pending_event,
    _memory_pending_event_from_items,
    create_app,
)
from memory.formatter import MemoryRecallTelemetry
from memory.models import (
    MemoryCandidate,
    MemoryEvent,
    MemoryItem,
    MemorySource,
    TripEpisode,
)
from state.models import TravelPlanState


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


def _make_item(**overrides) -> MemoryItem:
    base = dict(
        id="mem-1",
        user_id="u1",
        type="preference",
        domain="pace",
        key="preferred_pace",
        value="节奏轻松",
        scope="global",
        polarity="neutral",
        confidence=0.8,
        status="pending",
        source=MemorySource(kind="message", session_id="s1"),
        created_at="2026-04-11T00:00:00",
        updated_at="2026-04-11T00:00:00",
    )
    base.update(overrides)
    return MemoryItem(**base)


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


@pytest.mark.asyncio
async def test_get_memory_returns_empty_items(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/memory/u1")

    assert resp.status_code == 200
    assert resp.json() == {"items": []}


@pytest.mark.asyncio
async def test_memory_status_endpoints_update_items(app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    await memory_mgr.store.upsert_item(_make_item(status="pending"))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        confirm = await client.post(
            "/api/memory/u1/confirm",
            json={"item_id": "mem-1"},
        )
        confirm_again = await client.post(
            "/api/memory/u1/confirm",
            json={"item_id": "mem-1"},
        )
        reject = await client.post(
            "/api/memory/u1/reject",
            json={"item_id": "mem-1"},
        )
        reject_again = await client.post(
            "/api/memory/u1/reject",
            json={"item_id": "mem-1"},
        )
        delete = await client.delete("/api/memory/u1/mem-1")
        delete_again = await client.delete("/api/memory/u1/mem-1")

    assert confirm.status_code == 200
    assert confirm_again.status_code == 200
    assert reject.status_code == 200
    assert reject_again.status_code == 200
    assert delete.status_code == 200
    assert delete_again.status_code == 200
    items = await memory_mgr.store.list_items("u1")
    assert items[0].status == "obsolete"


@pytest.mark.asyncio
async def test_memory_events_and_episodes_endpoints(app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    event_payload = {
        "event_type": "accept",
        "object_type": "skeleton",
        "object_payload": {"id": "sk1"},
        "reason_text": "用户确认",
    }
    episode = TripEpisode(
        id="ep1",
        user_id="u1",
        session_id="s1",
        trip_id="trip1",
        destination="Tokyo",
        dates="2026-05",
        travelers={"adults": 2},
        budget={"total": 30000, "currency": "CNY"},
        selected_skeleton={"id": "sk1"},
        final_plan_summary="Tokyo trip",
        accepted_items=[{"type": "skeleton", "id": "sk1"}],
        rejected_items=[],
        lessons=["user confirmed skeleton"],
        satisfaction=5,
        created_at="2026-04-11T00:00:00",
    )
    await memory_mgr.store.append_episode(episode)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        event_resp = await client.post("/api/memory/u1/events", json=event_payload)
        episodes_resp = await client.get("/api/memory/u1/episodes")

    assert event_resp.status_code == 200
    assert episodes_resp.status_code == 200
    assert episodes_resp.json()["episodes"] == [episode.to_dict()]
    path = Path(memory_mgr.store.data_dir) / "users" / "u1" / "memory_events.jsonl"
    assert path.exists()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["event_type"] == "accept"


@pytest.mark.asyncio
async def test_memory_pending_event_helpers_shape():
    candidate = MemoryCandidate(
        type="preference",
        domain="food",
        key="avoid_spicy",
        value="不吃辣",
        scope="global",
        polarity="avoid",
        confidence=0.92,
        risk="low",
        evidence="我不吃辣",
        reason="明确表达",
    )
    payload = json.loads(_memory_pending_event([candidate], ["mem-1"]))
    assert payload["type"] == "memory_pending"
    assert payload["item_ids"] == ["mem-1"]
    assert payload["items"][0]["summary"].startswith("[food] avoid_spicy")


@pytest.mark.asyncio
async def test_memory_pending_event_from_items_shape():
    item = _make_item(status="pending_conflict", key="preferred_pace")
    payload = json.loads(_memory_pending_event_from_items([item]))
    assert payload["type"] == "memory_pending"
    assert payload["item_ids"] == ["mem-1"]
    assert payload["items"][0]["status"] == "pending_conflict"


@pytest.mark.asyncio
async def test_chat_system_prompt_uses_generate_context(monkeypatch, app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    calls = {"context": 0, "summary": 0}

    async def fake_generate_context(
        self, user_id: str, plan: TravelPlanState, user_message: str = ""
    ):
        calls["context"] += 1
        assert user_message == "继续规划"
        return "memory-context-marker", MemoryRecallTelemetry()

    def fake_generate_summary(self, memory):
        calls["summary"] += 1
        raise AssertionError("generate_summary should not be used for chat prompts")

    async def fake_run(self, messages, phase, tools_override=None):
        assert messages[0].role == Role.SYSTEM
        assert "memory-context-marker" in messages[0].content
        assert "暂无用户画像" not in messages[0].content
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr(type(memory_mgr), "generate_context", fake_generate_context)
    monkeypatch.setattr(type(memory_mgr), "generate_summary", fake_generate_summary)
    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "继续规划", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert calls["context"] >= 1
    assert calls["summary"] == 0


@pytest.mark.asyncio
async def test_chat_system_prompt_skips_memory_when_disabled(
    monkeypatch,
    app_memory_disabled,
):
    memory_mgr = _get_closure_value(app_memory_disabled, "memory_mgr")
    await memory_mgr.store.upsert_item(
        _make_item(status="active", value="secret-memory")
    )

    async def fake_generate_context(
        self, user_id: str, plan: TravelPlanState
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
            json={"message": "继续规划", "user_id": "u1"},
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_chat_stream_emits_pending_memory_before_agent_run(monkeypatch, app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    await memory_mgr.store.upsert_item(_make_item(status="pending_conflict"))

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
            json={"message": "继续规划", "user_id": "u1"},
        )

    body = resp.text
    assert body.startswith('data: {"type": "memory_pending"')
    assert '"memory_pending"' in body


@pytest.mark.asyncio
async def test_chat_stream_dedupes_pending_memory_per_session(monkeypatch, app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    await memory_mgr.store.upsert_item(_make_item(status="pending_conflict"))

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr("agent.loop.AgentLoop.run", fake_run)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        first = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "继续规划", "user_id": "u1"},
        )
        second = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "继续规划", "user_id": "u1"},
        )

    assert '"memory_pending"' in first.text
    assert '"memory_pending"' not in second.text


@pytest.mark.asyncio
async def test_chat_stream_emits_memory_recall_internal_task(monkeypatch, app):
    memory_mgr = _get_closure_value(app, "memory_mgr")

    async def fake_generate_context(
        self, user_id: str, plan: TravelPlanState, user_message: str = ""
    ):
        assert user_message == "继续规划"
        return (
            "用户偏好：喜欢轻松行程",
            MemoryRecallTelemetry(
                sources={"profile_fixed": 1},
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
            json={"message": "继续规划", "user_id": "u1"},
        )

    assert resp.status_code == 200
    assert '"type": "internal_task"' in resp.text
    assert '"kind": "memory_recall"' in resp.text
    assert '"status": "pending"' in resp.text
    assert '"status": "success"' in resp.text
    assert '"type": "memory_recall"' in resp.text


@pytest.mark.asyncio
async def test_append_trip_episode_once_is_idempotent(app):
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
        await memory_mgr.store.upsert_item(
            _make_item(
                id="same-session",
                status="active",
                session_id=session_id,
                trip_id=None,
            )
        )
        await memory_mgr.store.upsert_item(
            _make_item(
                id="unrelated-global",
                status="active",
                session_id="other-session",
                trip_id=None,
                value="should-not-enter-episode",
            )
        )

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

    episodes = await memory_mgr.store.list_episodes("u1")
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(episodes) == 1
    assert episodes[0].session_id == session_id
    accepted_ids = {item["id"] for item in episodes[0].accepted_items}
    assert accepted_ids == {"same-session"}


@pytest.mark.asyncio
async def test_reset_backtrack_rotates_trip_and_obsoletes_old_trip_memory(app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    rotate_trip = _get_closure_value(app, "_rotate_trip_on_reset_backtrack")
    plan = TravelPlanState(session_id="s1", trip_id="trip-old", phase=1)
    await memory_mgr.store.upsert_item(
        _make_item(
            id="old-trip",
            status="active",
            scope="trip",
            trip_id="trip-old",
        )
    )
    await memory_mgr.store.upsert_item(
        _make_item(
            id="global",
            status="active",
            scope="global",
            trip_id=None,
        )
    )

    changed = await rotate_trip(
        user_id="u1",
        plan=plan,
        to_phase=1,
        reason_text="重新开始，换个目的地",
    )

    items = {item.id: item for item in await memory_mgr.store.list_items("u1")}
    assert changed is True
    assert plan.trip_id != "trip-old"
    assert items["old-trip"].status == "obsolete"
    assert items["global"].status == "active"


@pytest.mark.asyncio
async def test_non_reset_backtrack_reuses_trip_memory(app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    rotate_trip = _get_closure_value(app, "_rotate_trip_on_reset_backtrack")
    plan = TravelPlanState(session_id="s1", trip_id="trip-old", phase=3)
    await memory_mgr.store.upsert_item(
        _make_item(
            id="old-trip",
            status="active",
            scope="trip",
            trip_id="trip-old",
        )
    )

    changed = await rotate_trip(
        user_id="u1",
        plan=plan,
        to_phase=3,
        reason_text="改日期",
    )

    items = {item.id: item for item in await memory_mgr.store.list_items("u1")}
    assert changed is False
    assert plan.trip_id == "trip-old"
    assert items["old-trip"].status == "active"


@pytest.mark.asyncio
async def test_tool_backtrack_reset_rotates_trip_memory(monkeypatch, app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
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
        await memory_mgr.store.upsert_item(
            _make_item(
                id="old-trip",
                status="active",
                scope="trip",
                trip_id="trip-old",
            )
        )
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "换个目的地", "user_id": "u1"},
        )

    items = {item.id: item for item in await memory_mgr.store.list_items("u1")}
    assert resp.status_code == 200
    assert plan.trip_id != "trip-old"
    assert items["old-trip"].status == "obsolete"


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
                json={"message": "继续规划", "user_id": "u1"},
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
        "decide_memory_extraction",
        "extract_profile_memory",
    ]
    assert profile.json()["stable_preferences"][0]["key"] == "avoid_spicy"
    assert working.json()["items"] == []


@pytest.mark.asyncio
async def test_memory_extraction_working_route_writes_working_only(app):
    observed = {"calls": []}

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            observed["calls"].append(tool_name)
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
        "decide_memory_extraction",
        "extract_working_memory",
    ]
    assert profile.json()["stable_preferences"] == []
    assert working.json()["items"][0]["content"] == "这轮先别考虑迪士尼"


@pytest.mark.asyncio
async def test_memory_extraction_no_routes_skips_extractors(app):
    observed = {"calls": []}

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            observed["calls"].append(tool_name)
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
    assert observed["calls"] == ["decide_memory_extraction"]


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
