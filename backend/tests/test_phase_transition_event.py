import json
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from llm.types import ChunkType, LLMChunk
from main import create_app


def test_chunk_type_has_phase_transition_and_agent_status():
    assert ChunkType.PHASE_TRANSITION.value == "phase_transition"
    assert ChunkType.AGENT_STATUS.value == "agent_status"


def test_llm_chunk_accepts_phase_info_and_agent_status():
    chunk = LLMChunk(
        type=ChunkType.PHASE_TRANSITION,
        phase_info={
            "from_phase": 1,
            "to_phase": 3,
            "from_step": None,
            "to_step": "brief",
        },
    )
    assert chunk.phase_info["to_phase"] == 3

    chunk2 = LLMChunk(
        type=ChunkType.AGENT_STATUS,
        agent_status={"stage": "thinking", "iteration": 0},
    )
    assert chunk2.agent_status["stage"] == "thinking"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return create_app()


def _get_sessions(app) -> dict:
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not hasattr(endpoint, "__closure__"):
            continue
        free_vars = getattr(endpoint.__code__, "co_freevars", ())
        for name, cell in zip(free_vars, endpoint.__closure__ or ()):
            if name == "sessions":
                return cell.cell_contents
    raise RuntimeError("Cannot locate sessions dict")


@pytest.fixture
def sessions(app):
    return _get_sessions(app)


@pytest.fixture
async def session_id(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/sessions")
    assert resp.status_code == 200
    return resp.json()["session_id"]


@pytest.mark.asyncio
async def test_sse_emits_phase_transition_event(app, sessions, session_id):
    async def fake_agent_run(*args, **kwargs):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="hi")
        yield LLMChunk(
            type=ChunkType.PHASE_TRANSITION,
            phase_info={
                "from_phase": 1,
                "to_phase": 3,
                "from_step": None,
                "to_step": "brief",
                "reason": "check",
            },
        )
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_agent_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/chat/{session_id}", json={"message": "去成都", "user_id": "u1"}
            )
    assert '"type": "phase_transition"' in resp.text
    assert '"to_phase": 3' in resp.text


@pytest.mark.asyncio
async def test_sse_emits_agent_status_event(app, sessions, session_id):
    async def fake_agent_run(*args, **kwargs):
        yield LLMChunk(
            type=ChunkType.AGENT_STATUS,
            agent_status={
                "stage": "thinking",
                "iteration": 2,
                "max_iterations": 5,
            },
        )
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_agent_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/chat/{session_id}", json={"message": "去成都", "user_id": "u1"}
            )

    events = [
        json.loads(line[len("data:") :].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:") and line[len("data:") :].strip()
    ]
    assert {
        "type": "agent_status",
        "stage": "thinking",
        "iteration": 2,
        "max_iterations": 5,
    } in events


@pytest.mark.asyncio
async def test_sse_emits_phase_transition_event_with_empty_payload(app, sessions, session_id):
    async def fake_agent_run(*args, **kwargs):
        yield LLMChunk(type=ChunkType.PHASE_TRANSITION, phase_info={})
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_agent_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/chat/{session_id}", json={"message": "去成都", "user_id": "u1"}
            )

    events = [
        json.loads(line[len("data:") :].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:") and line[len("data:") :].strip()
    ]
    assert {"type": "phase_transition"} in events


@pytest.mark.asyncio
async def test_sse_emits_agent_status_event_with_empty_payload(app, sessions, session_id):
    async def fake_agent_run(*args, **kwargs):
        yield LLMChunk(type=ChunkType.AGENT_STATUS, agent_status={})
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_agent_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/chat/{session_id}", json={"message": "去成都", "user_id": "u1"}
            )

    events = [
        json.loads(line[len("data:") :].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:") and line[len("data:") :].strip()
    ]
    assert {"type": "agent_status"} in events
