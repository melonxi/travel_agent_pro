# backend/tests/test_api.py
import pytest
from datetime import date, timedelta
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport

from agent.types import ToolCall
from llm.types import ChunkType, LLMChunk
from main import create_app


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return create_app()


@pytest.mark.asyncio
async def test_health(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_create_session(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert "session_id" in data


@pytest.mark.asyncio
async def test_get_plan(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Create session first
        resp = await client.post("/api/sessions")
        session_id = resp.json()["session_id"]

        resp = await client.get(f"/api/plan/{session_id}")
    assert resp.status_code == 200
    assert resp.json()["phase"] == 1


@pytest.mark.asyncio
async def test_get_plan_not_found(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/plan/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_chat_stream_emits_tool_call_event(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call=ToolCall(
                id="tc_1",
                name="update_plan_state",
                arguments={"field": "destination", "value": "Tokyo"},
            ),
        )
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]

            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "去东京"},
            )

    assert resp.status_code == 200
    assert '"type": "tool_call"' in resp.text


@pytest.mark.asyncio
async def test_chat_preloads_explicit_trip_facts_without_tool_call(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="继续确认同行人")
        yield LLMChunk(type=ChunkType.DONE)

    expected_start = date.today().replace(month=5, day=1)
    if expected_start < date.today():
        expected_start = expected_start.replace(year=expected_start.year + 1)

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]

            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我想五一去东京玩3天，预算1万元"},
            )
            plan_resp = await client.get(f"/api/plan/{session_id}")

    assert resp.status_code == 200
    plan = plan_resp.json()
    assert plan["destination"] == "东京"
    assert plan["budget"] == {"total": 10000.0, "currency": "CNY"}
    assert plan["dates"] == {
        "start": expected_start.isoformat(),
        "end": (expected_start + timedelta(days=3)).isoformat(),
    }
