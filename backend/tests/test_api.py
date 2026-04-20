# backend/tests/test_api.py
import asyncio
import json
import pytest
from datetime import date
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport

from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from main import _apply_message_fallbacks, create_app
from phase.router import PhaseRouter
from state.intake import parse_dates_value
from state.models import Accommodation, DateRange, DayPlan, TravelPlanState


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
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


def _get_state_manager(app):
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not hasattr(endpoint, "__closure__"):
            continue
        free_vars = getattr(endpoint.__code__, "co_freevars", ())
        for name, cell in zip(free_vars, endpoint.__closure__ or ()):
            if name == "state_mgr":
                return cell.cell_contents
    raise RuntimeError("Cannot locate state_mgr")


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
    assert agent.phase5_parallel_config is not None
    assert agent.phase5_parallel_config.enabled is True


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
async def test_chat_stream_emits_internal_task_event(app):
    from agent.internal_tasks import InternalTask

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(
            type=ChunkType.INTERNAL_TASK,
            internal_task=InternalTask(
                id="soft_judge:tc_1",
                kind="soft_judge",
                label="行程质量评审",
                status="pending",
                message="正在检查行程质量…",
                related_tool_call_id="tc_1",
                started_at=100.0,
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
                json={"message": "继续"},
            )

    assert resp.status_code == 200
    assert '"type": "internal_task"' in resp.text
    assert '"kind": "soft_judge"' in resp.text
    assert '"label": "行程质量评审"' in resp.text
    assert '"status": "pending"' in resp.text


@pytest.mark.asyncio
async def test_quality_gate_emits_internal_task_when_blocking(monkeypatch, tmp_path):
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
  threshold: 4.5
  max_retries: 1
memory_extraction:
  enabled: false
telemetry:
  enabled: false
""",
        encoding="utf-8",
    )

    class LowScoreProvider:
        async def chat(self, *args, **kwargs):
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA,
                content='{"overall":3.0,"pace":3,"geography":3,"coherence":3,"personalization":3,"suggestions":["补强路线顺路性"]}',
            )
            yield LLMChunk(type=ChunkType.DONE)

        async def count_tokens(self, messages):
            return 0

        async def get_context_window(self):
            return 200000

    monkeypatch.setattr("main.create_llm_provider", lambda _config: LowScoreProvider())
    app = create_app(str(config_file))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]

    session = _get_sessions(app)[session_id]
    plan = session["plan"]
    plan.phase = 5
    plan.destination = "京都"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-01")
    plan.selected_skeleton_id = "s1"
    plan.skeleton_plans = [{"id": "s1", "days": [{"day": 1}]}]
    plan.accommodation = Accommodation(area="河原町", hotel="A")
    plan.daily_plans = [DayPlan(day=1, date="2026-05-01")]

    agent = session["agent"]
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_save_day",
                    name="save_day_plan",
                    arguments={
                        "mode": "replace_existing",
                        "day": 1,
                        "date": "2026-05-01",
                        "activities": [
                            {
                                "name": "清水寺",
                                "location": {
                                    "name": "清水寺",
                                    "lat": 34.9949,
                                    "lng": 135.7850,
                                },
                                "start_time": "09:00",
                                "end_time": "11:00",
                                "category": "景点",
                                "cost": 0,
                            }
                        ],
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
        else:
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="准备进入下一阶段")
            yield LLMChunk(type=ChunkType.DONE)

    agent.llm.chat = fake_chat

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "继续"},
        )

    assert resp.status_code == 200
    assert '"kind": "quality_gate"' in resp.text
    assert '"label": "阶段推进检查"' in resp.text
    assert '"status": "pending"' in resp.text
    assert '"status": "warning"' in resp.text
    assert "补强路线顺路性" in resp.text


@pytest.mark.asyncio
async def test_phase1_to3_chat_extracts_memory_in_same_stream(monkeypatch, tmp_path):
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
        async def chat(self, messages, tools=None, stream=True, tool_choice=None, **kwargs):
            tool_name = tools[0]["name"] if tools else ""
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "reason": "explicit_preference_signal",
                            "message": "检测到可复用偏好信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_memory",
                    name="extract_memory_candidates",
                    arguments={
                        "profile_updates": {
                            "constraints": [],
                            "rejections": [],
                            "stable_preferences": [
                                {
                                    "domain": "food",
                                    "key": "avoid_spicy",
                                    "value": "不吃辣",
                                    "polarity": "avoid",
                                    "stability": "explicit_declared",
                                    "confidence": 0.95,
                                    "context": {},
                                    "applicability": "通用旅行饮食偏好",
                                    "recall_hints": {"keywords": ["不吃辣"]},
                                    "source_refs": [],
                                }
                            ],
                            "preference_hypotheses": [],
                        },
                        "working_memory": [],
                    },
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
            profile_resp = await client.get("/api/memory/u_mem/profile")

    assert resp.status_code == 200
    assert '"kind": "memory_extraction_gate"' in resp.text
    assert '"kind": "memory_extraction"' in resp.text
    assert profile_resp.status_code == 200
    data = profile_resp.json()
    assert data["schema_version"] == 3
    assert any(
        item["key"] == "avoid_spicy"
        and item["value"] == "不吃辣"
        and item["status"] == "active"
        for item in data["stable_preferences"]
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
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_judge",
                    name="emit_soft_judge_score",
                    arguments={
                        "pace": 2,
                        "geography": 2,
                        "coherence": 2,
                        "personalization": 2,
                        "suggestions": ["补充交通住宿取舍"],
                    },
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
async def test_quality_gate_uses_forced_tool_call(monkeypatch, tmp_path):
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

    observed = {}

    class FakeProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None, **kwargs):
            observed["tools"] = tools
            observed["tool_choice"] = tool_choice
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_judge",
                    name="emit_soft_judge_score",
                    arguments={
                        "pace": 2,
                        "geography": 2,
                        "coherence": 2,
                        "personalization": 2,
                        "suggestions": ["补充交通住宿取舍"],
                    },
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
    assert observed["tool_choice"] == {
        "type": "function",
        "function": {"name": "emit_soft_judge_score"},
    }
    assert observed["tools"] is not None
    assert observed["tools"][0]["name"] == "emit_soft_judge_score"


@pytest.mark.asyncio
async def test_soft_judge_uses_forced_tool_call_after_replace(monkeypatch, tmp_path):
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
  enabled: false
telemetry:
  enabled: false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    observed = {"judge_calls": []}

    class FakeProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None, **kwargs):
            observed["judge_calls"].append({"tools": tools, "tool_choice": tool_choice})
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_judge",
                    name="emit_soft_judge_score",
                    arguments={
                        "pace": 4,
                        "geography": 4,
                        "coherence": 4,
                        "personalization": 4,
                        "suggestions": [],
                    },
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
    plan.phase = 5
    plan.destination = "京都"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-01")
    plan.selected_skeleton_id = "s1"
    plan.skeleton_plans = [{"id": "s1", "days": [{"day": 1}]}]
    plan.accommodation = Accommodation(area="河原町", hotel="A")
    plan.daily_plans = [DayPlan(day=1, date="2026-05-01")]

    agent = session["agent"]
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_save_day",
                    name="save_day_plan",
                    arguments={
                        "mode": "replace_existing",
                        "day": 1,
                        "date": "2026-05-01",
                        "activities": [
                            {
                                "name": "清水寺",
                                "location": {"name": "清水寺", "lat": 34.9949, "lng": 135.7850},
                                "start_time": "09:00",
                                "end_time": "11:00",
                                "category": "景点",
                                "cost": 0,
                            }
                        ],
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
        else:
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="准备进入下一阶段")
            yield LLMChunk(type=ChunkType.DONE)

    agent.llm.chat = fake_chat

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "继续"},
        )

    assert resp.status_code == 200
    assert observed["judge_calls"]
    assert observed["judge_calls"][0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "emit_soft_judge_score"},
    }
    assert observed["judge_calls"][0]["tools"][0]["name"] == "emit_soft_judge_score"


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
                name="update_trip_basics",
                arguments={"destination": "Tokyo"},
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
async def test_chat_stream_emits_error_event_when_agent_stream_raises(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="已完成搜索，正在整理")
        raise RuntimeError("Xunfei request failed: Engine Busy")

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]

            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "继续整理搜索结果"},
            )

    assert resp.status_code == 200
    assert '"type": "text_delta"' in resp.text
    assert '"type": "error"' in resp.text
    assert '"error_code": "AGENT_STREAM_ERROR"' in resp.text
    assert "Engine Busy" in resp.text


@pytest.mark.asyncio
async def test_chat_stream_emits_incremental_state_update_after_successful_plan_tool(app):
    async def fake_run(self, messages, phase, tools_override=None):
        call = ToolCall(
            id="tc_state_1",
            name="update_trip_basics",
            arguments={"destination": "东京"},
        )
        messages.append(Message(role=Role.ASSISTANT, tool_calls=[call]))
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=call)
        result = await self.tool_engine.execute(call)
        messages.append(
            Message(role=Role.TOOL, tool_result=result, name=result.tool_call_id)
        )
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
async def test_download_deliverable_success(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/sessions")
        session_id = resp.json()["session_id"]

    sessions = _get_sessions(app)
    state_mgr = _get_state_manager(app)
    plan = sessions[session_id]["plan"]
    plan.deliverables = {
        "travel_plan_md": "travel_plan.md",
        "checklist_md": "checklist.md",
        "generated_at": "2026-04-18T22:30:00+08:00",
    }
    await state_mgr.save_deliverable(session_id, "travel_plan.md", "# 东京计划\n")
    await state_mgr.save(plan)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/deliverables/travel_plan.md")

    assert resp.status_code == 200
    assert resp.text == "# 东京计划\n"
    assert "attachment" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_download_deliverable_rejects_unknown_filename(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/sessions")
        session_id = resp.json()["session_id"]
        bad = await client.get(f"/api/sessions/{session_id}/deliverables/random.txt")

    assert bad.status_code == 404


@pytest.mark.asyncio
async def test_download_deliverable_returns_404_for_missing_whitelisted_file(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/sessions")
        session_id = resp.json()["session_id"]
        missing = await client.get(
            f"/api/sessions/{session_id}/deliverables/travel_plan.md"
        )

    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_backtrack_endpoint_clears_deliverables_and_files(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/sessions")
        session_id = resp.json()["session_id"]

    sessions = _get_sessions(app)
    state_mgr = _get_state_manager(app)
    plan = sessions[session_id]["plan"]
    plan.phase = 7
    plan.deliverables = {
        "travel_plan_md": "travel_plan.md",
        "checklist_md": "checklist.md",
        "generated_at": "2026-04-18T22:30:00+08:00",
    }
    await state_mgr.save_deliverable(session_id, "travel_plan.md", "# plan\n")
    await state_mgr.save_deliverable(session_id, "checklist.md", "# list\n")
    await state_mgr.save(plan)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        backtrack = await client.post(
            f"/api/backtrack/{session_id}",
            json={"to_phase": 5, "reason": "重新生成交付物"},
        )
        missing = await client.get(
            f"/api/sessions/{session_id}/deliverables/travel_plan.md"
        )

    assert backtrack.status_code == 200
    assert backtrack.json()["plan"]["deliverables"] is None
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_chat_persists_messages_when_stream_is_cancelled(app):
    async def fake_run(self, messages, phase, tools_override=None):
        call = ToolCall(
            id="tc_cancel_1",
            name="update_trip_basics",
            arguments={"destination": "东京"},
        )
        messages.append(Message(role=Role.ASSISTANT, tool_calls=[call]))
        yield LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=call)
        result = await self.tool_engine.execute(call)
        messages.append(
            Message(role=Role.TOOL, tool_result=result, name=result.tool_call_id)
        )
        yield LLMChunk(type=ChunkType.TOOL_RESULT, tool_result=result)
        self.cancel_event.set()
        raise asyncio.CancelledError()

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]

            with pytest.raises((asyncio.CancelledError, AssertionError)):
                await client.post(
                    f"/api/chat/{session_id}",
                    json={"message": "去东京"},
                )

            messages_resp = await client.get(f"/api/messages/{session_id}")

    assert messages_resp.status_code == 200
    persisted = messages_resp.json()
    assert any(
        message["role"] == "user" and message["content"] == "去东京"
        for message in persisted
    )
    assert any(
        message["role"] == "tool"
        and message["tool_call_id"] == "tc_cancel_1"
        for message in persisted
    )


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
            id="tc_utb_1",
            name="update_trip_basics",
            arguments={"destination": "东京"},
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
