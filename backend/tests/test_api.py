# backend/tests/test_api.py
import asyncio
import json
import pytest
from datetime import date
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport

from agent.types import ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from main import _apply_message_fallbacks, create_app
from phase.router import PhaseRouter
from state.intake import parse_dates_value
from state.models import Accommodation, DateRange, TravelPlanState


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
async def test_create_session_wires_agent_intelligence_components(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/sessions")

    session_id = resp.json()["session_id"]
    agent = _get_sessions(app)[session_id]["agent"]

    assert agent.reflection is not None
    assert agent.tool_choice_decider is not None
    assert agent.guardrail is not None
    assert agent.parallel_tool_execution is True


@pytest.mark.asyncio
async def test_rebuilt_agent_reuses_session_reflection(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        sessions = _get_sessions(app)
        original_reflection = sessions[session_id]["agent"].reflection
        sessions[session_id]["needs_rebuild"] = True

        with patch("agent.loop.AgentLoop.run", fake_run):
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "继续"},
            )

    assert resp.status_code == 200
    assert _get_sessions(app)[session_id]["agent"].reflection is original_reflection


@pytest.mark.asyncio
async def test_phase1_to3_chat_extracts_memory_async(monkeypatch, tmp_path):
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
memory_extraction:
  enabled: true
  model: gpt-4o-mini
telemetry:
  enabled: false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class FakeProvider:
        async def chat(self, *args, **kwargs):
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA,
                content=json.dumps(
                    [
                        {
                            "type": "preference",
                            "domain": "food",
                            "key": "spicy",
                            "value": "no spicy food",
                            "scope": "global",
                            "polarity": "avoid",
                            "confidence": 0.82,
                            "risk": "low",
                            "evidence": "我不吃辣",
                            "reason": "用户明确表达",
                        }
                    ],
                    ensure_ascii=False,
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    def fake_provider(_config):
        return FakeProvider()

    async def fake_run(self, messages, phase, tools_override=None):
        self.plan.phase = 3
        yield LLMChunk(type=ChunkType.DONE)

    monkeypatch.setattr("main.create_llm_provider", fake_provider)

    with patch("agent.loop.AgentLoop.run", fake_run):
        app = create_app(str(config_file))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我不吃辣，喜欢住民宿", "user_id": "u_mem"},
            )

    assert resp.status_code == 200
    for _ in range(20):
        memory_path = data_dir / "users" / "u_mem" / "memory.json"
        if memory_path.exists():
            break
        await asyncio.sleep(0.01)

    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    assert any(
        item["key"] == "spicy"
        and item["value"] == "no spicy food"
        and item["status"] == "active"
        for item in data["items"]
    )


@pytest.mark.asyncio
async def test_quality_gate_blocks_low_score_and_injects_feedback(monkeypatch, tmp_path):
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
quality_gate:
  threshold: 3.5
  max_retries: 2
memory_extraction:
  enabled: false
telemetry:
  enabled: false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class FakeProvider:
        async def chat(self, *args, **kwargs):
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA,
                content=json.dumps(
                    {
                        "pace": 2,
                        "geography": 2,
                        "coherence": 2,
                        "personalization": 2,
                        "suggestions": ["补充交通住宿取舍"],
                    },
                    ensure_ascii=False,
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    monkeypatch.setattr("main.create_llm_provider", lambda _config: FakeProvider())
    app = create_app(str(config_file))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")

    session_id = session_resp.json()["session_id"]
    session = _get_sessions(app)[session_id]
    plan = session["plan"]
    plan.phase = 3
    plan.destination = "京都"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.selected_skeleton_id = "balanced"
    plan.accommodation = Accommodation(area="祇園")
    agent = session["agent"]

    changed = await agent.phase_router.check_and_apply_transition(
        plan,
        hooks=agent.hooks,
    )

    assert changed is False
    assert plan.phase == 3
    assert any(
        message.content and "质量门控" in message.content and "补充交通住宿取舍" in message.content
        for message in session["messages"]
    )


@pytest.mark.asyncio
async def test_quality_gate_allows_when_soft_judge_fails(monkeypatch, tmp_path):
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
quality_gate:
  threshold: 3.5
  max_retries: 2
memory_extraction:
  enabled: false
telemetry:
  enabled: false
""",
        encoding="utf-8",
    )

    class FailingProvider:
        async def chat(self, *args, **kwargs):
            raise RuntimeError("judge unavailable")
            yield

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    monkeypatch.setattr("main.create_llm_provider", lambda _config: FailingProvider())
    app = create_app(str(config_file))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")

    session_id = session_resp.json()["session_id"]
    session = _get_sessions(app)[session_id]
    plan = session["plan"]
    plan.phase = 3
    plan.destination = "京都"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.selected_skeleton_id = "balanced"
    plan.accommodation = Accommodation(area="祇園")
    agent = session["agent"]

    changed = await agent.phase_router.check_and_apply_transition(
        plan,
        hooks=agent.hooks,
    )

    assert changed is True
    assert plan.phase == 5


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
