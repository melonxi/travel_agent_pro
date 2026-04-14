import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role, ToolCall
from llm.types import ChunkType, LLMChunk
from main import create_app
from state.models import TravelPlanState
from tools.engine import ToolEngine
from tools.update_plan_state import make_update_plan_state_tool


class _PhaseTransitionContextManager:
    def build_system_message(
        self,
        plan: TravelPlanState,
        phase_prompt: str,
        memory_context: str = "",
        available_tools: list[str] | None = None,
    ) -> Message:
        suffix = ""
        if available_tools:
            suffix = f" tools={','.join(available_tools)}"
        return Message(
            role=Role.SYSTEM,
            content=f"system phase={plan.phase} prompt={phase_prompt} user={memory_context}{suffix}",
        )

    async def compress_for_transition(
        self,
        messages: list[Message],
        from_phase: int,
        to_phase: int,
        llm_factory,
    ) -> str:
        return f"summary {from_phase}->{to_phase}"


class _PhaseTransitionMemoryManager:
    async def generate_context(
        self, user_id: str, plan: TravelPlanState
    ) -> tuple[str, list[str], int, int, int]:
        return f"memory:{user_id}", [], 0, 0, 0


def _promote_phase(plan: TravelPlanState, to_phase: int):
    async def _apply(*args, **kwargs) -> bool:
        plan.phase = to_phase
        return True

    return _apply


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


@pytest.fixture
def plan_phase1():
    return TravelPlanState(session_id="s1", phase=1)


@pytest.fixture
def agent_with_router(plan_phase1):
    engine = ToolEngine()
    engine.register(make_update_plan_state_tool(plan_phase1))

    llm = MagicMock()
    mock_router = MagicMock()
    mock_router.get_prompt.side_effect = lambda phase: f"phase-{phase}-prompt"
    mock_router.check_and_apply_transition = AsyncMock(return_value=False)

    call_count = 0

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc1",
                    name="update_plan_state",
                    arguments={"field": "destination", "value": "成都"},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
        yield LLMChunk(type=ChunkType.DONE)

    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        phase_router=mock_router,
        context_manager=_PhaseTransitionContextManager(),
        plan=plan_phase1,
        llm_factory=lambda: MagicMock(),
        memory_mgr=_PhaseTransitionMemoryManager(),
        user_id="u1",
    )
    return agent, mock_router


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
async def test_loop_yields_phase_transition_on_check_and_apply(
    agent_with_router, plan_phase1
):
    """When check_and_apply_transition promotes phase 1 -> 3, loop yields a
    phase_transition chunk before re-entering the loop."""
    agent, mock_router = agent_with_router
    mock_router.check_and_apply_transition.side_effect = _promote_phase(
        plan_phase1, to_phase=3
    )

    chunks = [c async for c in agent.run([], phase=1)]
    phase_chunks = [c for c in chunks if c.type == ChunkType.PHASE_TRANSITION]
    assert len(phase_chunks) == 1
    assert phase_chunks[0].phase_info["from_phase"] == 1
    assert phase_chunks[0].phase_info["to_phase"] == 3
    assert phase_chunks[0].phase_info["from_step"] == "brief"
    assert phase_chunks[0].phase_info["to_step"] == "brief"
    assert phase_chunks[0].phase_info["reason"] == "check_and_apply_transition"


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
