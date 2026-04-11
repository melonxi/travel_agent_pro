# backend/tests/test_api.py
import pytest
from datetime import date
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport

from agent.types import ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from main import _apply_message_fallbacks, create_app
from phase.router import PhaseRouter
from state.intake import parse_dates_value
from state.models import DateRange, TravelPlanState


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
async def test_chat_stream_emits_tool_result_event(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(
            type=ChunkType.TOOL_RESULT,
            tool_result=ToolResult(
                tool_call_id="tc_1",
                status="error",
                error="invalid accommodation",
                error_code="INVALID_VALUE",
                suggestion="Use area + hotel",
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
                json={"message": "记录住宿"},
            )

    assert resp.status_code == 200
    assert '"type": "tool_result"' in resp.text
    assert '"status": "error"' in resp.text


@pytest.mark.asyncio
async def test_chat_stream_emits_incremental_state_update_after_successful_update_plan_state(app):
    async def fake_run(self, messages, phase, tools_override=None):
        call = ToolCall(
            id="tc_state_1",
            name="update_plan_state",
            arguments={"field": "destination", "value": "东京"},
        )
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=call)
        result = await self.tool_engine.execute(call)
        yield LLMChunk(type=ChunkType.TOOL_RESULT, tool_result=result)
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
    assert resp.text.count('"type": "state_update"') >= 2
    assert '"destination": "东京"' in resp.text


@pytest.mark.asyncio
async def test_chat_does_not_mutate_plan_without_tool_call(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="继续确认同行人")
        yield LLMChunk(type=ChunkType.DONE)

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
    assert plan["destination"] is None
    assert plan["budget"] is None
    assert plan["dates"] is None
    assert plan["phase"] == 1


@pytest.mark.asyncio
async def test_chat_ambiguous_destination_question_does_not_create_fake_destination(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="我先给你几个方向")
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]

            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "现在这个季节你觉得去哪儿玩比较好"},
            )
            plan_resp = await client.get(f"/api/plan/{session_id}")

    assert resp.status_code == 200
    plan = plan_resp.json()
    assert plan["destination"] is None
    assert plan["phase"] == 1


@pytest.mark.asyncio
async def test_chat_updates_plan_only_via_tool_execution(app):
    async def fake_run(self, messages, phase, tools_override=None):
        call = ToolCall(
            id="tc_ups_1",
            name="update_plan_state",
            arguments={"field": "destination", "value": "东京"},
        )
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=call)
        result = await self.tool_engine.execute(call)
        assert result.status == "success"
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="已记录目的地")
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]

            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "想去东京"},
            )
            plan_resp = await client.get(f"/api/plan/{session_id}")

    assert resp.status_code == 200
    plan = plan_resp.json()
    assert plan["destination"] == "东京"


def test_parse_dates_value_accepts_start_date_aliases():
    parsed = parse_dates_value(
        {"start_date": "2026-05-01", "end_date": "2026-05-06", "duration_days": 5}
    )
    assert parsed is not None
    assert parsed.start == "2026-05-01"
    assert parsed.end == "2026-05-06"


def test_parse_dates_value_accepts_duration_and_time_window_aliases():
    parsed = parse_dates_value(
        {"duration_days": 5, "time_window": "五一假期"},
        today=date(2026, 4, 5),
    )
    assert parsed is not None
    assert parsed.start == "2026-05-01"
    assert parsed.end == "2026-05-06"


@pytest.mark.asyncio
async def test_apply_message_fallbacks_restores_destination_after_backtrack():
    plan = TravelPlanState(session_id="sess_fallback", phase=1)

    await _apply_message_fallbacks(
        plan,
        "换个目的地，我不想去东京了，改成大阪",
        PhaseRouter(),
        today=date(2026, 4, 5),
    )

    assert plan.destination == "大阪"
    assert plan.phase == 3


@pytest.mark.asyncio
async def test_apply_message_fallbacks_replaces_stale_dates_from_message():
    plan = TravelPlanState(
        session_id="sess_dates",
        phase=3,
        destination="东京",
        dates=DateRange(start="2025-05-01", end="2025-05-05"),
    )

    await _apply_message_fallbacks(
        plan,
        "我想五一去东京玩5天，预算2万元，2个大人",
        PhaseRouter(),
        today=date(2026, 4, 5),
    )

    assert plan.dates is not None
    assert plan.dates.start == "2026-05-01"
    assert plan.dates.end == "2026-05-06"
    assert plan.phase == 3


@pytest.mark.asyncio
async def test_apply_message_fallbacks_restores_travelers_from_message():
    plan = TravelPlanState(session_id="sess_travelers", phase=1)

    await _apply_message_fallbacks(
        plan,
        "我想五一去东京玩5天，预算2万元，2个大人",
        PhaseRouter(),
        today=date(2026, 4, 5),
    )

    assert plan.travelers is not None
    assert plan.travelers.adults == 2
    assert plan.travelers.children == 0
