import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from main import create_app
from state.models import TravelPlanState
from tools.base import tool
from tools.engine import ToolEngine
from tools.plan_tools.trip_basics import make_update_trip_basics_tool
from tests.helpers.register_plan_tools import register_all_plan_tools


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
    register_all_plan_tools(engine, plan_phase1)

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
                    name="update_trip_basics",
                    arguments={"destination": "成都"},
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


@pytest.fixture
def agent_with_tool_that_writes_phase(plan_phase1):
    engine = ToolEngine()

    @tool(
        name="jump_phase",
        description="directly updates phase",
        phases=[1],
        parameters={"type": "object", "properties": {}},
        side_effect="write",
    )
    async def jump_phase() -> dict:
        plan_phase1.phase = 3
        return {"phase": 3}

    engine.register(jump_phase)

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
                    name="jump_phase",
                    arguments={},
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
    return agent


@pytest.fixture
def agent_with_backtrack_tool():
    plan = TravelPlanState(session_id="s1", phase=5)
    engine = ToolEngine()

    @tool(
        name="trigger_backtrack",
        description="moves plan back to an earlier phase",
        phases=[5],
        parameters={"type": "object", "properties": {}},
        side_effect="write",
    )
    async def trigger_backtrack() -> dict:
        plan.phase = 1
        return {"backtracked": True, "reason": "用户想换目的地"}

    engine.register(trigger_backtrack)

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
                    name="trigger_backtrack",
                    arguments={},
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
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=_PhaseTransitionMemoryManager(),
        user_id="u1",
    )
    return agent


@pytest.fixture
def agent_with_split_writer_phase_transition(plan_phase1):
    engine = ToolEngine()
    engine.register(make_update_trip_basics_tool(plan_phase1))

    llm = MagicMock()
    mock_router = MagicMock()
    mock_router.get_prompt.side_effect = lambda phase: f"phase-{phase}-prompt"
    mock_router.check_and_apply_transition.side_effect = _promote_phase(
        plan_phase1, to_phase=3
    )

    call_count = 0

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_split_writer",
                    name="update_trip_basics",
                    arguments={"destination": "成都"},
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
async def test_sse_emits_state_update_for_set_skeleton_plans(
    app, sessions, session_id
):
    session = sessions[session_id]
    session["plan"].phase = 3
    agent = session["agent"]
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_phase3_step",
                    name="set_skeleton_plans",
                    arguments={
                        "plans": [
                            {"id": "sk-1", "name": "东京 citywalk", "days": [1, 2, 3]}
                        ]
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="已写入骨架方案")
        yield LLMChunk(type=ChunkType.DONE)

    agent.llm.chat = fake_chat

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/chat/{session_id}", json={"message": "写入骨架方案", "user_id": "u1"}
        )

    events = [
        json.loads(line[len("data:") :].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:") and line[len("data:") :].strip()
    ]
    state_update = next(event for event in events if event.get("type") == "state_update")

    assert state_update["plan"]["phase"] == 3
    assert state_update["plan"]["skeleton_plans"] == [
        {"id": "sk-1", "name": "东京 citywalk", "days": [1, 2, 3]}
    ]


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
async def test_split_plan_writer_triggers_check_and_apply_transition(
    agent_with_split_writer_phase_transition,
):
    agent, mock_router = agent_with_split_writer_phase_transition

    chunks = [c async for c in agent.run([], phase=1)]
    phase_chunks = [c for c in chunks if c.type == ChunkType.PHASE_TRANSITION]

    assert len(phase_chunks) == 1
    assert phase_chunks[0].phase_info["from_phase"] == 1
    assert phase_chunks[0].phase_info["to_phase"] == 3
    assert phase_chunks[0].phase_info["reason"] == "check_and_apply_transition"
    assert mock_router.check_and_apply_transition.call_count == 1


@pytest.mark.asyncio
async def test_loop_yields_phase_transition_on_explicit_path(
    agent_with_tool_that_writes_phase,
):
    """When a tool directly changes plan.phase, loop emits phase_transition in
    the explicit phase-change branch before rebuilding."""
    agent = agent_with_tool_that_writes_phase

    chunks = [c async for c in agent.run([], phase=1)]
    phase_chunks = [c for c in chunks if c.type == ChunkType.PHASE_TRANSITION]

    assert len(phase_chunks) == 1
    assert phase_chunks[0].phase_info == {
        "from_phase": 1,
        "to_phase": 3,
        "from_step": "brief",
        "to_step": "brief",
        "reason": "plan_tool_direct",
    }


@pytest.mark.asyncio
async def test_loop_yields_phase_transition_on_backtrack(agent_with_backtrack_tool):
    order = []
    original_rebuild = agent_with_backtrack_tool._rebuild_messages_for_phase_change

    async def wrapped_rebuild(*args, **kwargs):
        order.append("rebuild")
        return await original_rebuild(*args, **kwargs)

    with patch.object(
        agent_with_backtrack_tool,
        "_rebuild_messages_for_phase_change",
        AsyncMock(side_effect=wrapped_rebuild),
    ):
        chunks = []
        async for chunk in agent_with_backtrack_tool.run([], phase=5):
            if chunk.type == ChunkType.PHASE_TRANSITION:
                order.append("transition")
            chunks.append(chunk)

    phase_chunks = [c for c in chunks if c.type == ChunkType.PHASE_TRANSITION]

    assert len(phase_chunks) == 1
    assert phase_chunks[0].phase_info == {
        "from_phase": 5,
        "to_phase": 1,
        "from_step": "brief",
        "to_step": "brief",
        "reason": "backtrack",
    }
    assert order[:2] == ["transition", "rebuild"]


@pytest.mark.asyncio
async def test_request_backtrack_result_marks_session_for_rebuild(
    app, sessions, session_id
):
    session = sessions[session_id]
    result = ToolResult(
        tool_call_id="tc_backtrack",
        status="success",
        data={"backtracked": True, "to_phase": 1, "reason": "重新规划"},
    )

    await session["agent"].hooks.run(
        "after_tool_call",
        tool_name="request_backtrack",
        tool_call=ToolCall(
            id="tc_backtrack",
            name="request_backtrack",
            arguments={"to_phase": 1, "reason": "重新规划"},
        ),
        result=result,
    )

    assert session["needs_rebuild"] is True


@pytest.mark.asyncio
async def test_request_backtrack_does_not_track_state_changes(
    app, sessions, session_id
):
    session = sessions[session_id]
    result = ToolResult(
        tool_call_id="tc_backtrack_state_changes",
        status="success",
        data={"backtracked": True, "to_phase": 1, "reason": "重新规划"},
    )

    await session["agent"].hooks.run(
        "after_tool_call",
        tool_name="request_backtrack",
        tool_call=ToolCall(
            id="tc_backtrack_state_changes",
            name="request_backtrack",
            arguments={"to_phase": 1, "reason": "重新规划"},
        ),
        result=result,
    )

    assert "_pending_state_changes" not in session


@pytest.mark.asyncio
async def test_update_trip_basics_hook_tracks_state_changes_and_validation_errors(
    app, sessions, session_id
):
    session = sessions[session_id]
    result = ToolResult(
        tool_call_id="tc_basics",
        status="success",
        data={"updated_fields": ["budget"], "count": 1},
    )

    await session["agent"].hooks.run(
        "after_tool_call",
        tool_name="update_trip_basics",
        tool_call=ToolCall(
            id="tc_basics",
            name="update_trip_basics",
            arguments={"budget": {"total": 0, "currency": "CNY"}},
        ),
        result=result,
    )

    assert session["_pending_state_changes"] == [
        {
            "field": "budget",
            "before": None,
            "after": {"total": 0, "currency": "CNY"},
        }
    ]
    assert session["_pending_validation_errors"] == ["budget.total 不能为负数或零"]


@pytest.mark.asyncio
async def test_split_writer_hook_uses_result_values_for_state_changes(
    app, sessions, session_id
):
    session = sessions[session_id]
    result = ToolResult(
        tool_call_id="tc_select_transport",
        status="success",
        data={
            "updated_field": "selected_transport",
            "previous_value": {"mode": "plane"},
            "new_value": {"mode": "train"},
        },
    )

    await session["agent"].hooks.run(
        "after_tool_call",
        tool_name="select_transport",
        tool_call=ToolCall(
            id="tc_select_transport",
            name="select_transport",
            arguments={"choice": {"mode": "train"}},
        ),
        result=result,
    )

    assert session["_pending_state_changes"] == [
        {
            "field": "selected_transport",
            "before": {"mode": "plane"},
            "after": {"mode": "train"},
        }
    ]


@pytest.mark.asyncio
async def test_sse_emits_state_update_for_update_trip_basics(app, sessions, session_id):
    session = sessions[session_id]
    agent = session["agent"]
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_trip_basics",
                    name="update_trip_basics",
                    arguments={"destination": "东京", "budget": 10000},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="已更新")
        yield LLMChunk(type=ChunkType.DONE)

    agent.llm.chat = fake_chat

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/chat/{session_id}", json={"message": "去东京，预算一万", "user_id": "u1"}
        )

    events = [
        json.loads(line[len("data:") :].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:") and line[len("data:") :].strip()
    ]
    state_update = next(event for event in events if event.get("type") == "state_update")

    assert state_update["plan"]["destination"] == "东京"
    assert state_update["plan"]["budget"]["total"] == 10000.0


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
