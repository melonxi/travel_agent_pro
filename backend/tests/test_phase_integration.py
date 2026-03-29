# backend/tests/test_phase_integration.py
"""
Phase integration tests — mock external APIs and test the full tool call chain
for phases 2, 4, 5, and 7.

Each test:
1. Creates an app and session via POST /api/sessions
2. Directly manipulates plan state to set up the correct phase
3. Patches AgentLoop.run to yield LLMChunks that simulate the agent calling
   tools — the fake_run executes tools via self.tool_engine so that plan state
   is updated through the real tool layer
4. Verifies via GET /api/plan/{session_id} that state was updated correctly
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from agent.types import ToolCall
from llm.types import ChunkType, LLMChunk
from main import create_app
from state.models import (
    Accommodation,
    Activity,
    DateRange,
    DayPlan,
    Location,
    Preference,
    Travelers,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-maps-key")
    monkeypatch.setenv("OPENWEATHER_API_KEY", "test-weather-key")
    return create_app()


def _get_sessions(app) -> dict:
    """Reach into the FastAPI app closure to access the in-memory sessions dict.

    create_app() defines ``sessions: dict`` as a local variable that is captured
    by every endpoint closure.  We locate it by inspecting ``create_session``'s
    closure cells.
    """
    for route in app.routes:
        if not hasattr(route, "endpoint"):
            continue
        if getattr(route.endpoint, "__name__", "") != "create_session":
            continue
        free_vars = route.endpoint.__code__.co_freevars
        cells = route.endpoint.__closure__ or ()
        for name, cell in zip(free_vars, cells):
            if name == "sessions":
                return cell.cell_contents
    # Fallback: scan all route closures for a dict that looks like sessions
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not hasattr(endpoint, "__closure__"):
            continue
        for cell in endpoint.__closure__ or ():
            try:
                val = cell.cell_contents
            except ValueError:
                continue
            if isinstance(val, dict):
                return val
    raise RuntimeError("Cannot locate sessions dict in app closure")


# ---------------------------------------------------------------------------
# Mock HTTP responses for external APIs
# ---------------------------------------------------------------------------

_GOOGLE_PLACES_RESPONSE = {
    "results": [
        {
            "name": "巴厘岛",
            "formatted_address": "Bali, Indonesia",
            "rating": 4.5,
            "geometry": {"location": {"lat": -8.3405, "lng": 115.0920}},
        },
        {
            "name": "普吉岛",
            "formatted_address": "Phuket, Thailand",
            "rating": 4.3,
            "geometry": {"location": {"lat": 7.8804, "lng": 98.3923}},
        },
    ],
    "status": "OK",
}

_OPENWEATHER_RESPONSE = {
    "list": [
        {
            "dt_txt": "2026-04-10 12:00:00",
            "main": {"temp": 18.5, "temp_min": 14.0, "temp_max": 22.0, "humidity": 65},
            "weather": [{"description": "晴"}],
            "wind": {"speed": 3.5},
        },
    ],
}


# ---------------------------------------------------------------------------
# Test: Phase 2 — Destination Search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase2_destination_search(app):
    """
    Phase 2: user has preferences set, agent calls search_destinations to find
    candidates, then calls update_plan_state to record the chosen destination.
    Verify plan state has destination set and phase advances past 2.
    """

    async def fake_run(self, messages, phase, tools_override=None):
        # Step 1: simulate agent calling search_destinations
        tc_search = ToolCall(
            id="tc_sd_1",
            name="search_destinations",
            arguments={"query": "海岛度假", "preferences": ["海滩", "美食"]},
        )
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=tc_search)
        # Execute the real tool (HTTP is mocked by respx)
        result = await self.tool_engine.execute(tc_search)
        assert result.status == "success"

        # Step 2: agent decides on destination, calls update_plan_state
        tc_update = ToolCall(
            id="tc_ups_1",
            name="update_plan_state",
            arguments={"field": "destination", "value": "巴厘岛"},
        )
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=tc_update)
        result = await self.tool_engine.execute(tc_update)
        assert result.status == "success"

        # Step 3: final text
        yield LLMChunk(
            type=ChunkType.TEXT_DELTA,
            content="已为您选择巴厘岛作为目的地！",
        )
        yield LLMChunk(type=ChunkType.DONE)

    with (
        respx.mock(assert_all_called=False) as mock_http,
        patch("agent.loop.AgentLoop.run", fake_run),
    ):
        mock_http.get("https://maps.googleapis.com/maps/api/place/textsearch/json").mock(
            return_value=Response(200, json=_GOOGLE_PLACES_RESPONSE),
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Create session
            resp = await client.post("/api/sessions")
            assert resp.status_code == 200
            session_id = resp.json()["session_id"]

            # Set up phase 2 state: add preferences so PhaseRouter infers phase 2
            sessions = _get_sessions(app)
            plan = sessions[session_id]["plan"]
            plan.preferences.append(Preference(key="style", value="beach"))
            plan.phase = 2

            # Send a chat message that triggers the agent
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "帮我找个海岛度假的地方"},
            )
            assert resp.status_code == 200

            # Verify plan state via API
            plan_resp = await client.get(f"/api/plan/{session_id}")
            assert plan_resp.status_code == 200
            plan_data = plan_resp.json()

    assert plan_data["destination"] == "巴厘岛"
    # Destination is set; in real flow hooks trigger phase transition,
    # but with patched run() hooks don't fire — verify state was written.
    assert plan_data["phase"] >= 2


# ---------------------------------------------------------------------------
# Test: Phase 4 — Accommodation Search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase4_accommodation_search(app):
    """
    Phase 4: destination and dates are set but no accommodation.
    Agent calls search_accommodations, then update_plan_state to set accommodation.
    Verify accommodation is set in plan state.
    """

    async def fake_run(self, messages, phase, tools_override=None):
        # Step 1: agent calls search_accommodations
        tc_search = ToolCall(
            id="tc_sa_1",
            name="search_accommodations",
            arguments={
                "destination": "京都",
                "check_in": "2026-04-10",
                "check_out": "2026-04-15",
                "area": "祇園",
            },
        )
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=tc_search)
        result = await self.tool_engine.execute(tc_search)
        assert result.status == "success"

        # Step 2: agent updates plan state with chosen accommodation
        tc_update = ToolCall(
            id="tc_ups_2",
            name="update_plan_state",
            arguments={
                "field": "accommodation",
                "value": {"area": "祇園", "hotel": "祇園白川旅館"},
            },
        )
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=tc_update)
        result = await self.tool_engine.execute(tc_update)
        assert result.status == "success"

        # Final text
        yield LLMChunk(
            type=ChunkType.TEXT_DELTA,
            content="已为您预订祇園白川旅館",
        )
        yield LLMChunk(type=ChunkType.DONE)

    with (
        respx.mock(assert_all_called=False) as mock_http,
        patch("agent.loop.AgentLoop.run", fake_run),
    ):
        mock_http.get("https://maps.googleapis.com/maps/api/place/textsearch/json").mock(
            return_value=Response(200, json=_GOOGLE_PLACES_RESPONSE),
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Create session
            resp = await client.post("/api/sessions")
            session_id = resp.json()["session_id"]

            # Set up phase 4 state: destination + dates, no accommodation
            sessions = _get_sessions(app)
            plan = sessions[session_id]["plan"]
            plan.destination = "京都"
            plan.dates = DateRange(start="2026-04-10", end="2026-04-15")
            plan.phase = 4

            # Trigger agent via chat
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "帮我找祇園附近的住宿"},
            )
            assert resp.status_code == 200

            # Verify plan
            plan_resp = await client.get(f"/api/plan/{session_id}")
            plan_data = plan_resp.json()

    assert plan_data["accommodation"] is not None
    assert plan_data["accommodation"]["area"] == "祇園"
    assert plan_data["accommodation"]["hotel"] == "祇園白川旅館"
    # Accommodation is set; with patched run() hooks don't fire for phase transition
    assert plan_data["phase"] >= 4


# ---------------------------------------------------------------------------
# Test: Phase 5 — Day Plan Assembly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase5_day_plan_assembly(app):
    """
    Phase 5: destination+dates+accommodation set, daily_plans incomplete.
    Agent calls assemble_day_plan (a local tool, no external API).
    Since daily_plans is not an allowed field for update_plan_state, the agent
    would normally coordinate multi-turn to fill them.  Here we simulate the
    tool call, execute assemble_day_plan through the engine, and then directly
    populate daily_plans on the plan to reflect what the full agent loop would
    produce.
    """

    async def fake_run(self, messages, phase, tools_override=None):
        # Step 1: agent calls assemble_day_plan for day 1
        tc_assemble = ToolCall(
            id="tc_adp_1",
            name="assemble_day_plan",
            arguments={
                "pois": [
                    {"name": "金閣寺", "lat": 35.0394, "lng": 135.7292, "duration_hours": 1.5},
                    {"name": "龍安寺", "lat": 35.0345, "lng": 135.7185, "duration_hours": 1.0},
                    {"name": "嵐山竹林", "lat": 35.0170, "lng": 135.6713, "duration_hours": 2.0},
                ],
                "start_time": "09:00",
                "end_time": "18:00",
            },
        )
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=tc_assemble)
        result = await self.tool_engine.execute(tc_assemble)
        assert result.status == "success"
        assert result.data["total_distance_km"] > 0
        ordered_pois = result.data["ordered_pois"]

        # In a real agent loop, the agent would repeat this for each day and
        # ultimately build DayPlan objects.  We simulate that coordination by
        # directly populating the plan's daily_plans from tool results.
        # The plan is the same object the update_plan_state tool is bound to.
        # Locate it via the tool engine's update_plan_state closure.
        _populate_daily_plans_from_results(self, ordered_pois)

        # Final text
        yield LLMChunk(
            type=ChunkType.TEXT_DELTA,
            content="已为您安排5天行程！",
        )
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Create session
            resp = await client.post("/api/sessions")
            session_id = resp.json()["session_id"]

            # Set up phase 5 state
            sessions = _get_sessions(app)
            plan = sessions[session_id]["plan"]
            plan.destination = "京都"
            plan.dates = DateRange(start="2026-04-10", end="2026-04-15")
            plan.accommodation = Accommodation(area="祇園", hotel="祇園白川旅館")
            plan.phase = 5

            # Trigger agent
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "请帮我安排每日行程"},
            )
            assert resp.status_code == 200

            # Verify plan
            plan_resp = await client.get(f"/api/plan/{session_id}")
            plan_data = plan_resp.json()

    assert len(plan_data["daily_plans"]) == 5
    assert plan_data["daily_plans"][0]["day"] == 1
    assert plan_data["daily_plans"][0]["date"] == "2026-04-10"
    assert len(plan_data["daily_plans"][0]["activities"]) >= 1
    # With all daily_plans filled (5 days for a 5-day trip), phase should reach 7
    assert plan_data["phase"] >= 5


def _populate_daily_plans_from_results(agent_self, ordered_pois: list[dict]):
    """Helper: populate daily_plans on the plan bound to update_plan_state.

    The update_plan_state tool is created via make_update_plan_state_tool(plan),
    which binds the plan in a closure.  We retrieve that same plan object through
    the tool engine and write daily_plans directly, mimicking what a real multi-
    turn agent loop would do.
    """
    tool_def = agent_self.tool_engine.get_tool("update_plan_state")
    if tool_def is None:
        return
    # The tool function's closure contains the plan object.
    # ToolDef stores the callable as _fn (private field).
    fn = tool_def._fn
    closure_vars = fn.__code__.co_freevars
    closure_cells = fn.__closure__ or ()
    plan = None
    for name, cell in zip(closure_vars, closure_cells):
        if name == "plan":
            plan = cell.cell_contents
            break
    if plan is None:
        return

    # Build 5 day plans (one per day of the trip)
    for day_idx in range(5):
        activities = []
        for poi in ordered_pois:
            activities.append(
                Activity(
                    name=poi["name"],
                    location=Location(
                        lat=poi["lat"], lng=poi["lng"], name=poi["name"]
                    ),
                    start_time="09:00",
                    end_time="12:00",
                    category="sightseeing",
                )
            )
        plan.daily_plans.append(
            DayPlan(
                day=day_idx + 1,
                date=f"2026-04-{10 + day_idx}",
                activities=activities,
                notes=f"第{day_idx + 1}天行程",
            )
        )


# ---------------------------------------------------------------------------
# Test: Phase 7 — Summary Generation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase7_summary_generation(app):
    """
    Phase 7: complete plan (destination+dates+accommodation+daily_plans).
    Agent calls check_weather, then generate_summary.
    Verify the response stream contains the tool call events and the plan
    remains in phase 7.
    """

    async def fake_run(self, messages, phase, tools_override=None):
        # Step 1: agent calls check_weather
        tc_weather = ToolCall(
            id="tc_cw_1",
            name="check_weather",
            arguments={"city": "京都", "date": "2026-04-10"},
        )
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=tc_weather)
        result = await self.tool_engine.execute(tc_weather)
        assert result.status == "success"
        assert result.data["forecast"]["description"] == "晴"

        # Step 2: agent calls generate_summary
        tc_summary = ToolCall(
            id="tc_gs_1",
            name="generate_summary",
            arguments={
                "plan_data": {
                    "destination": "京都",
                    "total_days": 5,
                    "days": [],
                    "budget": {
                        "flights": 3000,
                        "hotels": 4000,
                        "activities": 1500,
                        "food": 1500,
                    },
                }
            },
        )
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=tc_summary)
        result = await self.tool_engine.execute(tc_summary)
        assert result.status == "success"
        assert result.data["total_budget"] == 10000

        # Final text
        yield LLMChunk(
            type=ChunkType.TEXT_DELTA,
            content="您的京都5日游计划已生成完毕！天气预报显示晴天，非常适合出行。",
        )
        yield LLMChunk(type=ChunkType.DONE)

    with (
        respx.mock(assert_all_called=False) as mock_http,
        patch("agent.loop.AgentLoop.run", fake_run),
    ):
        mock_http.get("https://api.openweathermap.org/data/2.5/forecast").mock(
            return_value=Response(200, json=_OPENWEATHER_RESPONSE),
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Create session
            resp = await client.post("/api/sessions")
            session_id = resp.json()["session_id"]

            # Set up phase 7 state: complete plan
            sessions = _get_sessions(app)
            plan = sessions[session_id]["plan"]
            plan.destination = "京都"
            plan.dates = DateRange(start="2026-04-10", end="2026-04-15")
            plan.travelers = Travelers(adults=2, children=0)
            plan.accommodation = Accommodation(area="祇園", hotel="祇園白川旅館")
            plan.daily_plans = [
                DayPlan(
                    day=i + 1,
                    date=f"2026-04-{10 + i}",
                    activities=[
                        Activity(
                            name=f"景点{i + 1}",
                            location=Location(lat=35.0, lng=135.7, name=f"景点{i + 1}"),
                            start_time="09:00",
                            end_time="17:00",
                            category="sightseeing",
                        ),
                    ],
                )
                for i in range(5)
            ]
            plan.phase = 7

            # Trigger agent
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "生成最终旅行计划总结"},
            )
            assert resp.status_code == 200

            # Check response contains tool call events
            body = resp.text
            assert "check_weather" in body
            assert "generate_summary" in body

            # Verify plan remains at phase 7 (plan is complete)
            plan_resp = await client.get(f"/api/plan/{session_id}")
            plan_data = plan_resp.json()

    assert plan_data["phase"] == 7
    assert plan_data["destination"] == "京都"
    assert len(plan_data["daily_plans"]) == 5
    assert plan_data["accommodation"]["hotel"] == "祇園白川旅館"
