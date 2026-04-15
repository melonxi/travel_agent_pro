"""Tests for keepalive interval (Task 23).

Verifies that the SSE keepalive cadence is 8 seconds and that keepalive
frames are actually emitted during long-running agent runs.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

from main import create_app, KEEPALIVE_INTERVAL_S
from agent.types import Message, Role
from llm.types import ChunkType, LLMChunk
from state.models import TravelPlanState


# ── constant check ──────────────────────────────────────────────


def test_keepalive_interval_is_8_seconds():
    assert KEEPALIVE_INTERVAL_S == 8


# ── integration: keepalive frames emitted ────────────────────────


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
async def test_keepalive_frames_emitted_during_slow_agent(
    app, sessions, session_id, monkeypatch
):
    """With KEEPALIVE_INTERVAL_S patched to 0.1s and a slow agent (~0.5s),
    we should see at least 2 keepalive comment frames in the SSE output."""

    monkeypatch.setattr("main.KEEPALIVE_INTERVAL_S", 0.1)

    async def fake_agent_run(*args, **kwargs):
        yield LLMChunk(
            type=ChunkType.AGENT_STATUS,
            agent_status={"stage": "thinking", "iteration": 0},
        )
        await asyncio.sleep(0.5)
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="hi")
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_agent_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "hello", "user_id": "u1"},
            )

    # Keepalive frames appear as data: {"type": "keepalive"} in the SSE output
    keepalive_count = resp.text.count('"type": "keepalive"')
    assert keepalive_count >= 2, (
        f"Expected at least 2 keepalive frames, got {keepalive_count}.\n"
        f"Full response:\n{resp.text[:2000]}"
    )
