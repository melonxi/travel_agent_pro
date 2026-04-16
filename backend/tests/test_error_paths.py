# backend/tests/test_error_paths.py
"""Error-path tests: missing sessions, backtrack, budget overrun, time conflicts, tool failures."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

import httpx
from httpx import ASGITransport

from agent.loop import AgentLoop
from agent.types import ToolCall, ToolResult
from harness.validator import validate_hard_constraints
from llm.types import ChunkType, LLMChunk
from state.models import (
    TravelPlanState,
    DateRange,
    Budget,
    DayPlan,
    Activity,
    Location,
    Accommodation,
)
from tools.base import ToolDef, ToolError, tool
from tools.engine import ToolEngine

from main import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_activity(
    name: str,
    start: str,
    end: str,
    cost: float = 0,
    transport_duration_min: int = 0,
) -> Activity:
    return Activity(
        name=name,
        location=Location(lat=35.0, lng=135.7, name=name),
        start_time=start,
        end_time=end,
        category="景点",
        cost=cost,
        transport_duration_min=transport_duration_min,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    # Use a non-existent config path so load_config falls through to defaults
    application = create_app(config_path=str(tmp_path / "nonexistent.yaml"))
    return application


@pytest.fixture
def sessions(app):
    """Return the internal sessions dict by fishing it out of the app's route closures."""
    # create_session endpoint closes over `sessions`; we can also just call the API
    # and track returned session_ids. But for direct state manipulation we need the dict.
    #
    # The sessions dict lives as a free variable on the create_session endpoint.
    for route in app.routes:
        if hasattr(route, "endpoint") and getattr(route, "path", "") == "/api/sessions":
            closure = route.endpoint.__code__.co_freevars
            cells = route.endpoint.__closure__
            if cells:
                for name, cell in zip(closure, cells):
                    val = cell.cell_contents
                    if isinstance(val, dict):
                        return val
    # Fallback: look deeper — the sessions dict is shared across all endpoints
    for route in app.routes:
        if not hasattr(route, "endpoint") or not hasattr(route.endpoint, "__closure__"):
            continue
        closure_vars = route.endpoint.__code__.co_freevars
        cells = route.endpoint.__closure__ or ()
        for name, cell in zip(closure_vars, cells):
            try:
                val = cell.cell_contents
            except ValueError:
                continue
            if name == "sessions" and isinstance(val, dict):
                return val
    pytest.fail("Could not locate sessions dict from app routes")


# ---------------------------------------------------------------------------
# 1. test_chat_session_not_found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_session_not_found(app):
    """POST /api/chat to a nonexistent session returns 404."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/sess_000000000000",
            json={"message": "hello", "user_id": "u1"},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 2. test_backtrack_api_success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backtrack_api_success(app, sessions):
    """Create session, set plan to phase 5, backtrack to phase 1 — verify downstream cleared."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Create session
        create_resp = await client.post("/api/sessions")
        assert create_resp.status_code == 200
        session_id = create_resp.json()["session_id"]

    # Directly manipulate plan state to simulate phase 5
    session = sessions[session_id]
    plan: TravelPlanState = session["plan"]
    plan.phase = 5
    plan.destination = "京都"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-04")
    plan.accommodation = Accommodation(area="祇園", hotel="TEST HOTEL")
    plan.daily_plans = [DayPlan(day=1, date="2026-05-01")]

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/backtrack/{session_id}",
            json={"to_phase": 1, "reason": "换目的地"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["phase"] == 1

    # Destination re-selection should clear all destination-stage outputs
    updated_plan: TravelPlanState = sessions[session_id]["plan"]
    assert updated_plan.phase == 1
    assert updated_plan.destination is None
    assert updated_plan.dates is None
    assert updated_plan.accommodation is None
    assert updated_plan.daily_plans == []
    assert updated_plan.destination_candidates == []


# ---------------------------------------------------------------------------
# 3. test_backtrack_forward_rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backtrack_forward_rejected(app, sessions):
    """Backtrack with to_phase >= current phase returns 400."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

    # Plan starts at phase 1; set it to 3
    sessions[session_id]["plan"].phase = 3

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # to_phase=5 is forward → reject
        resp = await client.post(
            f"/api/backtrack/{session_id}",
            json={"to_phase": 5, "reason": "nope"},
        )
    assert resp.status_code == 400

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # to_phase == current phase → also rejected
        resp = await client.post(
            f"/api/backtrack/{session_id}",
            json={"to_phase": 3, "reason": "nope"},
        )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 4. test_implicit_backtrack_in_chat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_implicit_backtrack_in_chat(app, sessions):
    """Chat message with backtrack keywords triggers implicit backtrack."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

    # Set plan to phase 5 with some downstream data
    session = sessions[session_id]
    plan: TravelPlanState = session["plan"]
    plan.phase = 5
    plan.destination = "东京"
    plan.dates = DateRange(start="2026-06-01", end="2026-06-05")
    plan.accommodation = Accommodation(area="新宿")
    plan.daily_plans = [DayPlan(day=1, date="2026-06-01")]

    # Mock AgentLoop.run so we don't need a real LLM
    async def fake_run(messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="好的，我们换个目的地吧。")
        yield LLMChunk(type=ChunkType.DONE)

    with patch.object(AgentLoop, "run", side_effect=fake_run):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我不想去这里了，换个目的地", "user_id": "u1"},
            )
        # chat returns SSE (EventSourceResponse), so status is 200
        assert resp.status_code == 200
        assert '"type": "tool_call"' in resp.text
        # Now uses request_backtrack instead of update_plan_state with field=backtrack
        assert '"name": "request_backtrack"' in resp.text
        assert '"type": "tool_result"' in resp.text

    # "换个目的地" / "不想去这里" now maps back to the merged destination stage (phase 1)
    updated_plan: TravelPlanState = sessions[session_id]["plan"]
    assert updated_plan.phase == 1
    assert updated_plan.destination is None
    assert updated_plan.dates is None
    assert updated_plan.accommodation is None
    assert updated_plan.daily_plans == []
    assert len(updated_plan.backtrack_history) >= 1


@pytest.mark.asyncio
async def test_chat_backtrack_restores_new_destination_from_message(app, sessions):
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

    session = sessions[session_id]
    plan: TravelPlanState = session["plan"]
    plan.phase = 5
    plan.destination = "东京"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-05")
    plan.accommodation = Accommodation(area="新宿", hotel="Hyatt Regency Tokyo")
    plan.daily_plans = [DayPlan(day=1, date="2026-05-01")]

    async def fake_run(self, messages, phase, tools_override=None):
        call = ToolCall(
            id="tc_backtrack_1",
            name="request_backtrack",
            arguments={
                "to_phase": 1,
                "reason": "用户想要更换目的地，从东京改为大阪",
            },
        )
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=call)
        result = await self.tool_engine.execute(call)
        yield LLMChunk(type=ChunkType.TOOL_RESULT, tool_result=result)
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="好的，改成大阪。")
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "换个目的地，我不想去东京了，改成大阪", "user_id": "u1"},
            )
        assert resp.status_code == 200

    updated_plan: TravelPlanState = sessions[session_id]["plan"]
    assert updated_plan.destination == "大阪"
    assert updated_plan.phase == 3
    assert updated_plan.dates is None
    assert updated_plan.accommodation is None
    assert updated_plan.daily_plans == []


# ---------------------------------------------------------------------------
# 5. test_hard_constraint_budget_overrun
# ---------------------------------------------------------------------------

def test_hard_constraint_budget_overrun():
    """Activities costing 8000 with budget 5000 triggers budget error."""
    plan = TravelPlanState(
        session_id="s_budget",
        budget=Budget(total=5000),
        daily_plans=[
            DayPlan(
                day=1,
                date="2026-04-10",
                activities=[
                    _make_activity("景点A", "09:00", "12:00", cost=3000),
                    _make_activity("景点B", "13:00", "17:00", cost=2500),
                ],
            ),
            DayPlan(
                day=2,
                date="2026-04-11",
                activities=[
                    _make_activity("景点C", "09:00", "12:00", cost=2500),
                ],
            ),
        ],
    )
    errors = validate_hard_constraints(plan)
    assert len(errors) >= 1
    assert any("预算" in e for e in errors)
    # Total cost = 3000 + 2500 + 2500 = 8000 > 5000
    assert any("8000" in e for e in errors)


# ---------------------------------------------------------------------------
# 6. test_hard_constraint_time_conflict
# ---------------------------------------------------------------------------

def test_hard_constraint_time_conflict():
    """Prev ends 14:00, transport 30min, next starts 14:10 → conflict."""
    plan = TravelPlanState(
        session_id="s_time",
        daily_plans=[
            DayPlan(
                day=1,
                date="2026-04-10",
                activities=[
                    _make_activity("景点A", "10:00", "14:00"),
                    _make_activity(
                        "景点B",
                        "14:10",
                        "16:00",
                        transport_duration_min=30,
                    ),
                ],
            ),
        ],
    )
    errors = validate_hard_constraints(plan)
    assert len(errors) == 1
    assert "时间冲突" in errors[0]
    # prev_end(14:00=840) + travel(30) = 870 > curr_start(14:10=850) → conflict
    assert "景点A" in errors[0]
    assert "景点B" in errors[0]


# ---------------------------------------------------------------------------
# 7. test_hard_constraint_day_count
# ---------------------------------------------------------------------------

def test_hard_constraint_day_count():
    """Dates span 3 days but 5 daily_plans → error."""
    plan = TravelPlanState(
        session_id="s_days",
        dates=DateRange(start="2026-04-10", end="2026-04-13"),  # 3 days
        daily_plans=[
            DayPlan(day=i + 1, date=f"2026-04-{10 + i}") for i in range(5)
        ],
    )
    errors = validate_hard_constraints(plan)
    assert len(errors) >= 1
    assert any("天数" in e for e in errors)


# ---------------------------------------------------------------------------
# 8. test_tool_engine_unknown_tool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_engine_unknown_tool():
    """Calling execute with an unknown tool name returns error result."""
    engine = ToolEngine()
    call = ToolCall(id="tc_bad", name="nonexistent_tool", arguments={})
    result = await engine.execute(call)
    assert result.status == "error"
    assert result.error_code == "UNKNOWN_TOOL"
    assert "nonexistent_tool" in (result.error or "")


# ---------------------------------------------------------------------------
# 9. test_tool_engine_tool_error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_engine_tool_error():
    """Tool that raises ToolError produces error result with correct code."""

    @tool(
        name="broken_tool",
        description="Always raises ToolError",
        phases=[1],
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def broken_tool() -> dict:
        raise ToolError(
            "API key is missing",
            error_code="MISSING_API_KEY",
            suggestion="Please configure an API key",
        )

    engine = ToolEngine()
    engine.register(broken_tool)

    call = ToolCall(id="tc_err", name="broken_tool", arguments={})
    result = await engine.execute(call)
    assert result.status == "error"
    assert result.error_code == "MISSING_API_KEY"
    assert result.suggestion == "Please configure an API key"
    assert "API key is missing" in (result.error or "")
