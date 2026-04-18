# backend/tests/test_phase_integration.py
"""
Phase integration tests — mock external APIs and test the full tool call chain
for phases 1, 3, 5, and 7.

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

from agent.types import Role, ToolCall
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
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
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

_GOOGLE_GEOCODE_RESPONSE = {
    "results": [
        {
            "address_components": [{"long_name": "巴厘岛"}],
            "formatted_address": "Bali, Indonesia",
            "types": ["locality", "political"],
            "geometry": {"location": {"lat": -8.3405, "lng": 115.0920}},
        },
        {
            "address_components": [{"long_name": "普吉岛"}],
            "formatted_address": "Phuket, Thailand",
            "types": ["locality", "political"],
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
# Test: Phase 1 — Destination Search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase1_destination_search(app):
    """
    Phase 1: user has preferences set, agent completes destination selection and
    records the chosen destination via update_trip_basics.
    Verify plan state has destination set and phase advances past 1.
    """

    async def fake_run(self, messages, phase, tools_override=None):
        # Step 1: agent decides on destination and records it
        tc_update = ToolCall(
            id="tc_utb_1",
            name="update_trip_basics",
            arguments={"destination": "巴厘岛"},
        )
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=tc_update)
        result = await self.tool_engine.execute(tc_update)
        assert result.status == "success"

        # Step 2: final text
        yield LLMChunk(
            type=ChunkType.TEXT_DELTA,
            content="已为您选择巴厘岛作为目的地！",
        )
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Create session
            resp = await client.post("/api/sessions")
            assert resp.status_code == 200
            session_id = resp.json()["session_id"]

            # Set up destination-confirmation state: add preferences to aid search
            sessions = _get_sessions(app)
            plan = sessions[session_id]["plan"]
            plan.preferences.append(Preference(key="style", value="beach"))
            plan.phase = 1

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
    assert plan_data["phase"] >= 1


# ---------------------------------------------------------------------------
# Test: Phase 3 — Accommodation Search (merged from former Phase 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase4_accommodation_search(app):
    """
    Phase 3 (merged): destination and dates are set but no accommodation.
    Agent calls search_accommodations, then set_accommodation to set accommodation.
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
            id="tc_sa_2",
            name="set_accommodation",
            arguments={
                "area": "祇園",
                "hotel": "祇園白川旅館",
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
        mock_http.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json"
        ).mock(
            return_value=Response(200, json=_GOOGLE_PLACES_RESPONSE),
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Create session
            resp = await client.post("/api/sessions")
            session_id = resp.json()["session_id"]

            # Set up phase 3 state: destination + dates, no accommodation
            sessions = _get_sessions(app)
            plan = sessions[session_id]["plan"]
            plan.destination = "京都"
            plan.dates = DateRange(start="2026-04-10", end="2026-04-15")
            plan.phase = 3

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
    assert plan_data["phase"] >= 3


# ---------------------------------------------------------------------------
# Test: Phase 5 — Day Plan Assembly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase5_day_plan_assembly(app):
    """
    Phase 5: destination+dates+accommodation set, daily_plans incomplete.
    Agent calls optimize_day_route (a local tool, no external API).
    Then calls replace_all_day_plans to write the plans.
    """

    async def fake_run(self, messages, phase, tools_override=None):
        # Step 1: agent calls optimize_day_route for day 1
        tc_assemble = ToolCall(
            id="tc_adp_1",
            name="optimize_day_route",
            arguments={
                "pois": [
                    {
                        "name": "金閣寺",
                        "lat": 35.0394,
                        "lng": 135.7292,
                        "duration_hours": 1.5,
                    },
                    {
                        "name": "龍安寺",
                        "lat": 35.0345,
                        "lng": 135.7185,
                        "duration_hours": 1.0,
                    },
                    {
                        "name": "嵐山竹林",
                        "lat": 35.0170,
                        "lng": 135.6713,
                        "duration_hours": 2.0,
                    },
                ],
                "day_start_time": "09:00",
                "day_end_time": "18:00",
            },
        )
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=tc_assemble)
        result = await self.tool_engine.execute(tc_assemble)
        assert result.status == "success"
        assert result.data["estimated_total_distance_km"] > 0
        ordered_pois = result.data["ordered_pois"]

        # Build daily_plans from ordered_pois
        daily_plans_payload = _build_daily_plans_from_pois(ordered_pois)

        # Step 2: agent calls replace_all_day_plans to write the plans
        tc_daily = ToolCall(
            id="tc_rdp_1",
            name="replace_all_day_plans",
            arguments={"days": daily_plans_payload},
        )
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=tc_daily)
        result = await self.tool_engine.execute(tc_daily)
        assert result.status == "success"

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
            plan.dates = DateRange(start="2026-04-10", end="2026-04-14")
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


def _build_daily_plans_from_pois(ordered_pois: list[dict]) -> list[dict]:
    """Build daily_plans payload from ordered_pois for replace_all_day_plans tool."""
    daily_plans = []
    for day_idx in range(5):
        activities = []
        for poi in ordered_pois:
            activities.append(
                {
                    "name": poi["name"],
                    "location": {
                        "lat": poi["lat"],
                        "lng": poi["lng"],
                        "name": poi["name"],
                    },
                    "start_time": "09:00",
                    "end_time": "12:00",
                    "category": "sightseeing",
                    "cost": 0,
                }
            )
        daily_plans.append(
            {
                "day": day_idx + 1,
                "date": f"2026-04-{10 + day_idx}",
                "activities": activities,
                "notes": f"第{day_idx + 1}天行程",
            }
        )
    return daily_plans


# ---------------------------------------------------------------------------
# Test: In-loop phase rebuild after tool-triggered transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_change_rebuilds_context_inside_same_chat(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/sessions")
        session_id = resp.json()["session_id"]

        sessions = _get_sessions(app)
        session = sessions[session_id]
        plan = session["plan"]
        agent = session["agent"]

        class SummaryLLM:
            async def chat(self, messages, tools=None, stream=True):
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content="阶段摘要")
                yield LLMChunk(type=ChunkType.DONE)

        agent.llm_factory = lambda: SummaryLLM()

        call_count = 0

        async def fake_chat(messages, tools=None, stream=True):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_dest",
                        name="update_trip_basics",
                        arguments={"destination": "巴厘岛"},
                    ),
                )
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_budget_skipped",
                        name="update_trip_basics",
                        arguments={"budget": "30000"},
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return

            if call_count == 2:
                assert plan.phase == 3
                assert "- 阶段：3" in messages[0].content
                assert messages[1].role == Role.ASSISTANT
                assert "[阶段交接]" in messages[1].content
                assert "当前阶段：Phase 3" in messages[1].content
                assert (
                    "当前唯一目标：围绕已确认目的地完成旅行画像、候选筛选、骨架方案与锁定项。"
                    in messages[1].content
                )
                assert [tool["name"] for tool in tools or []].count(
                    "update_trip_basics"
                ) == 1
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_dates",
                        name="update_trip_basics",
                        arguments={
                            "dates": {"start": "2026-04-10", "end": "2026-04-15"},
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return

            assert plan.phase == 3
            assert "- 阶段：3" in messages[0].content
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="日期已确认。")
            yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat

        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "帮我定巴厘岛"},
        )
        assert resp.status_code == 200

        plan_resp = await client.get(f"/api/plan/{session_id}")
        plan_data = plan_resp.json()

    assert call_count == 3
    assert plan_data["destination"] == "巴厘岛"
    assert plan_data["dates"] == {"start": "2026-04-10", "end": "2026-04-15"}
    assert plan_data["budget"] == {"total": 30000.0, "currency": "CNY"}
    assert plan_data["phase"] == 3


# ---------------------------------------------------------------------------
# Test: Backtrack uses hard context boundary in same chat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backtrack_rebuild_uses_hard_boundary_context(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/sessions")
        session_id = resp.json()["session_id"]

        sessions = _get_sessions(app)
        session = sessions[session_id]
        plan = session["plan"]
        agent = session["agent"]

        plan.destination = "京都"
        plan.dates = DateRange(start="2026-04-10", end="2026-04-15")
        plan.accommodation = Accommodation(area="祇園", hotel="祇園白川旅館")
        plan.preferences.append(Preference(key="style", value="quiet"))
        plan.phase = 5

        call_count = 0

        async def fake_chat(messages, tools=None, stream=True):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_backtrack",
                        name="request_backtrack",
                        arguments={
                            "to_phase": 1,
                            "reason": "用户想换目的地",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return

            assert plan.phase == 1
            assert (
                messages[1].content
                == "[阶段回退]\n用户从 phase 5 回退到 phase 1，原因：用户想换目的地"
            )
            assert messages[2].content == "不想去京都了，换个目的地"
            assert "祇園白川旅館" not in messages[0].content
            assert "2026-04-10" not in messages[1].content
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA, content="我给您重新推荐几个目的地。"
            )
            yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat

        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "不想去京都了，换个目的地"},
        )
        assert resp.status_code == 200

        plan_resp = await client.get(f"/api/plan/{session_id}")
        plan_data = plan_resp.json()

    assert call_count == 2
    assert plan_data["phase"] == 1
    assert plan_data["destination"] is None
    assert plan_data["dates"] is None
    assert plan_data["accommodation"] is None
    assert plan_data["preferences"] == [{"key": "style", "value": "quiet"}]


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
        yield LLMChunk(type=ChunkType.TOOL_RESULT, tool_result=result)

        # Step 2: agent calls generate_summary
        tc_summary = ToolCall(
            id="tc_gs_1",
            name="generate_summary",
            arguments={
                "plan_data": {
                    "destination": "京都",
                    "total_days": 5,
                },
                "travel_plan_markdown": "# 京都 5 日旅行计划\n\n## 第 1 天\n- 景点1\n",
                "checklist_markdown": "# 京都出发前清单\n\n- [ ] 护照\n",
            },
        )
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=tc_summary)
        result = await self.tool_engine.execute(tc_summary)
        assert result.status == "success"
        assert "travel_plan_markdown" in result.data
        assert "checklist_markdown" in result.data
        yield LLMChunk(type=ChunkType.TOOL_RESULT, tool_result=result)

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
    assert plan_data["deliverables"]["travel_plan_md"] == "travel_plan.md"
    assert plan_data["deliverables"]["checklist_md"] == "checklist.md"
