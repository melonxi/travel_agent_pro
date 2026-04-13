from __future__ import annotations

import httpx
import pytest

from agent.types import ToolCall
from llm.types import ChunkType, LLMChunk
from state.models import Accommodation, Budget, DateRange, TravelPlanState


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")


@pytest.fixture
def app():
    from main import create_app

    return create_app(config_path="__nonexistent__.yaml")


@pytest.fixture
def sessions(app):
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        closure = getattr(endpoint, "__closure__", None)
        if endpoint is None or closure is None:
            continue
        for cell in closure:
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    pytest.fail("Could not locate sessions dict from app closure")


@pytest.mark.asyncio
async def test_update_plan_state_injects_realtime_incremental_feedback(app, sessions):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]
        plan.phase = 5
        plan.destination = "东京"
        plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
        plan.budget = Budget(total=10_000)
        plan.accommodation = Accommodation(area="新宿", hotel="A")

        agent = session["agent"]

        async def fake_chat(messages, tools=None, stream=True):
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_conflict",
                    name="update_plan_state",
                    arguments={
                        "field": "daily_plans",
                        "value": [
                            {
                                "day": 1,
                                "date": "2026-05-01",
                                "activities": [
                                    {
                                        "name": "浅草寺",
                                        "location": "浅草寺",
                                        "start_time": "09:00",
                                        "end_time": "10:00",
                                        "category": "景点",
                                    },
                                    {
                                        "name": "上野公园",
                                        "location": "上野公园",
                                        "start_time": "10:05",
                                        "end_time": "11:00",
                                        "category": "景点",
                                        "transport_duration_min": 20,
                                    },
                                ],
                            }
                        ],
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat

        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "安排第一天"},
        )

    assert resp.status_code == 200
    realtime_messages = [
        message.content
        for message in session["messages"]
        if message.role.value == "system" and message.content
    ]
    assert any("[实时约束检查]" in content for content in realtime_messages)
    assert any("时间冲突" in content for content in realtime_messages)


@pytest.mark.asyncio
async def test_on_validate_records_state_changes_to_stats(app, sessions):
    """update_plan_state should write state_changes to the latest ToolCallRecord."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]
        plan.phase = 1
        plan.destination = "北京"

        agent = session["agent"]

        call_count = 0

        async def fake_chat(messages, tools=None, stream=True, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_dest",
                        name="update_plan_state",
                        arguments={"field": "destination", "value": "东京"},
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
            else:
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content="好的")
                yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat

        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "去东京"},
        )

    assert resp.status_code == 200
    stats = session["stats"]
    ups_records = [
        r
        for r in stats.tool_calls
        if r.tool_name == "update_plan_state" and r.status == "success"
    ]
    assert len(ups_records) >= 1
    rec = ups_records[-1]
    assert rec.state_changes is not None
    assert len(rec.state_changes) == 1
    assert rec.state_changes[0]["field"] == "destination"
    assert rec.state_changes[0]["before"] == "北京"
    assert rec.state_changes[0]["after"] == "东京"
