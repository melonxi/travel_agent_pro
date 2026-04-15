import json
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role, ToolCall
from config import ApiKeysConfig, XhsConfig
from llm.types import ChunkType, LLMChunk
from main import create_app
from state.models import TravelPlanState
from tools.ai_travel_search import make_ai_travel_search_tool
from tools.assemble_day_plan import make_assemble_day_plan_tool
from tools.base import ToolDef, tool
from tools.calculate_route import make_calculate_route_tool
from tools.check_availability import make_check_availability_tool
from tools.check_weather import make_check_weather_tool
from tools.engine import ToolEngine
from tools.generate_summary import make_generate_summary_tool
from tools.get_poi_info import make_get_poi_info_tool
from tools.quick_travel_search import make_quick_travel_search_tool
from tools.search_accommodations import make_search_accommodations_tool
from tools.search_flights import make_search_flights_tool
from tools.search_trains import make_search_trains_tool
from tools.search_travel_services import make_search_travel_services_tool
from tools.update_plan_state import make_update_plan_state_tool
from tools.web_search import make_web_search_tool
from tools.xiaohongshu_search import make_xiaohongshu_search_tool


class DummyLLM:
    def __init__(self, chunk_batches):
        self._chunk_batches = chunk_batches
        self._call_index = 0

    async def chat(self, messages, **kwargs):
        batch_index = min(self._call_index, len(self._chunk_batches) - 1)
        self._call_index += 1
        for chunk in self._chunk_batches[batch_index]:
            yield chunk


@dataclass
class _FakeFlyAIClient:
    available: bool = True


class _FakeXhsClient:
    async def search_notes(self, *args, **kwargs):
        return {"items": []}

    async def read_note(self, *args, **kwargs):
        return {"note": {}}

    async def get_comments(self, *args, **kwargs):
        return {"comments": []}


def _get_sessions(app) -> dict:
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not hasattr(endpoint, "__closure__"):
            continue
        free_vars = getattr(endpoint.__code__, "co_freevars", ())
        for name, cell in zip(free_vars, endpoint.__closure__ or (), strict=False):
            if name == "sessions":
                return cell.cell_contents
    raise RuntimeError("Cannot locate sessions dict")


def _build_default_tool_engine(plan: TravelPlanState) -> ToolEngine:
    engine = ToolEngine()
    api_keys = ApiKeysConfig()
    flyai_client = _FakeFlyAIClient()

    engine.register(make_update_plan_state_tool(plan))
    engine.register(make_search_flights_tool(api_keys, flyai_client))
    engine.register(make_search_trains_tool(flyai_client))
    engine.register(make_ai_travel_search_tool(flyai_client))
    engine.register(make_search_accommodations_tool(api_keys, flyai_client))
    engine.register(make_get_poi_info_tool(api_keys, flyai_client))
    engine.register(make_calculate_route_tool(api_keys))
    engine.register(make_assemble_day_plan_tool())
    engine.register(make_check_availability_tool(api_keys))
    engine.register(make_check_weather_tool(api_keys))
    engine.register(make_generate_summary_tool())
    engine.register(make_quick_travel_search_tool(flyai_client))
    engine.register(make_search_travel_services_tool(flyai_client))
    engine.register(make_web_search_tool(api_keys))
    engine.register(
        make_xiaohongshu_search_tool(
            XhsConfig(enabled=False),
            xhs_client=_FakeXhsClient(),
        )
    )
    return engine


def test_tool_decorator_supports_human_label_default_none():
    @tool(name="plain_tool", description="test", phases=[1], parameters={})
    async def plain_tool() -> dict:
        return {"ok": True}

    @tool(
        name="labeled_tool",
        description="test",
        phases=[1],
        parameters={},
        human_label="给用户看的名字",
    )
    async def labeled_tool() -> dict:
        return {"ok": True}

    assert isinstance(plain_tool, ToolDef)
    assert plain_tool.human_label is None
    assert labeled_tool.human_label == "给用户看的名字"


def test_tool_call_supports_human_label():
    tool_call = ToolCall(
        id="tc_1",
        name="labeled_tool",
        arguments={"q": "京都"},
        human_label="检索标签",
    )

    assert tool_call.human_label == "检索标签"
    payload = Message(role=Role.ASSISTANT, tool_calls=[tool_call]).to_dict()
    assert payload["tool_calls"][0]["human_label"] == "检索标签"


@pytest.mark.asyncio
async def test_agent_loop_fills_human_label_on_tool_call_start_chunk():
    @tool(
        name="labeled_tool",
        description="test",
        phases=[1],
        parameters={},
        human_label="已标注工具",
    )
    async def labeled_tool() -> dict:
        return {"ok": True}

    engine = ToolEngine()
    engine.register(labeled_tool)
    llm = DummyLLM(
        [
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(id="tc_1", name="labeled_tool", arguments={}),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(type=ChunkType.TEXT_DELTA, content="完成"),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        plan=TravelPlanState(session_id="s1", phase=1),
    )

    chunks = [
        chunk
        async for chunk in agent.run([Message(role=Role.USER, content="开始")], phase=1)
    ]
    tool_call_chunk = next(
        chunk for chunk in chunks if chunk.type == ChunkType.TOOL_CALL_START
    )

    assert tool_call_chunk.tool_call is not None
    assert tool_call_chunk.tool_call.human_label == "已标注工具"


@pytest.mark.asyncio
async def test_sse_tool_call_event_includes_human_label(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    data_dir = tmp_path / "data"
    config_file.write_text(
        f"""
llm:
  provider: openai
  model: gpt-4o
data_dir: \"{data_dir}\"
flyai:
  enabled: false
xhs:
  enabled: false
telemetry:
  enabled: false
memory:
  enabled: false
guardrails:
  enabled: false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call=ToolCall(
                id="tc_1",
                name="web_search",
                arguments={"query": "京都天气"},
                human_label="上网查资料",
            ),
        )
        yield LLMChunk(type=ChunkType.DONE)

    app = create_app(str(config_file))
    transport = ASGITransport(app=app)

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "帮我查一下", "user_id": "u1"},
            )

    assert resp.status_code == 200
    payloads = [
        json.loads(line[len("data:") :].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:") and line[len("data:") :].strip()
    ]
    tool_call_event = next(p for p in payloads if p.get("type") == "tool_call")

    assert tool_call_event["tool_call"]["name"] == "web_search"
    assert tool_call_event["tool_call"]["human_label"] == "上网查资料"


def test_current_default_registered_tools_all_have_human_label():
    plan = TravelPlanState(session_id="s1", phase=1)
    engine = _build_default_tool_engine(plan)

    expected_default_names = {
        "update_plan_state",
        "search_flights",
        "search_trains",
        "ai_travel_search",
        "search_accommodations",
        "get_poi_info",
        "calculate_route",
        "assemble_day_plan",
        "check_availability",
        "check_weather",
        "generate_summary",
        "quick_travel_search",
        "search_travel_services",
        "web_search",
        "xiaohongshu_search",
    }

    missing_default_tools = sorted(expected_default_names - set(engine._tools))
    assert missing_default_tools == []

    missing = sorted(
        name
        for name, tool_def in engine._tools.items()
        if name in expected_default_names
        if not isinstance(tool_def.human_label, str) or not tool_def.human_label.strip()
    )
    assert missing == []
