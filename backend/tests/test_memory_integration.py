from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from agent.types import Message, Role
from llm.types import ChunkType, LLMChunk
from main import (
    _memory_pending_event,
    _memory_pending_event_from_items,
    create_app,
)
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

    async def fake_generate_context(self, user_id: str, plan: TravelPlanState) -> str:
        calls["context"] += 1
        return "memory-context-marker"

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
    await memory_mgr.store.upsert_item(_make_item(status="active", value="secret-memory"))

    async def fake_generate_context(self, user_id: str, plan: TravelPlanState) -> str:
        raise AssertionError("generate_context should not be called when memory is disabled")

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
async def test_append_trip_episode_once_is_idempotent(app):
    memory_mgr = _get_closure_value(app, "memory_mgr")
    append_once = _get_closure_value(app, "_append_trip_episode_once")
    await memory_mgr.store.upsert_item(
        _make_item(
            id="same-session",
            status="active",
            session_id="s1",
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
    plan = TravelPlanState(
        session_id="s1",
        trip_id=None,
        phase=7,
        destination="Tokyo",
    )

    first = await append_once(user_id="u1", session_id="s1", plan=plan)
    second = await append_once(user_id="u1", session_id="s1", plan=plan)

    episodes = await memory_mgr.store.list_episodes("u1")
    assert first is True
    assert second is False
    assert len(episodes) == 1
    assert episodes[0].session_id == "s1"
    accepted_ids = {item["id"] for item in episodes[0].accepted_items}
    assert accepted_ids == {"same-session"}


@pytest.mark.asyncio
async def test_memory_extraction_queues_latest_turn_when_task_running(app):
    schedule = _get_closure_value(app, "_schedule_memory_extraction")
    tasks = _get_function_closure_value(schedule, "memory_extraction_tasks")
    pending = _get_function_closure_value(schedule, "memory_extraction_pending")
    plan = TravelPlanState(session_id="s1", trip_id="trip1")

    schedule(
        session_id="s1",
        user_id="u1",
        messages_snapshot=[Message(role=Role.USER, content="第一轮")],
        plan_snapshot=plan,
    )
    schedule(
        session_id="s1",
        user_id="u1",
        messages_snapshot=[Message(role=Role.USER, content="第二轮")],
        plan_snapshot=plan,
    )

    try:
        assert "s1" in pending
        assert pending["s1"][1][0].content == "第二轮"
    finally:
        for task in list(tasks.values()):
            task.cancel()
        for task in list(tasks.values()):
            with suppress(asyncio.CancelledError):
                await task
        pending.clear()
