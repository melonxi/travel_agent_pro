# backend/tests/test_agent_loop.py
import pytest
from unittest.mock import AsyncMock, MagicMock

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from phase.router import PhaseRouter
from state.models import BacktrackEvent, TravelPlanState
from tools.engine import ToolEngine
from tools.base import tool
from tools.update_plan_state import make_update_plan_state_tool


class FakePhaseRouter:
    def get_prompt(self, phase: int) -> str:
        return f"phase-{phase}-prompt"

    def check_and_apply_transition(self, plan: TravelPlanState) -> bool:
        if plan.phase == 3:
            return True
        return False


class FakeContextManager:
    def __init__(self) -> None:
        self.compress_calls: list[tuple[int, int]] = []

    def build_system_message(
        self,
        plan: TravelPlanState,
        phase_prompt: str,
        user_summary: str = "",
    ) -> Message:
        return Message(
            role=Role.SYSTEM,
            content=f"system phase={plan.phase} prompt={phase_prompt} user={user_summary}",
        )

    async def compress_for_transition(
        self,
        messages: list[Message],
        from_phase: int,
        to_phase: int,
        llm_factory,
    ) -> str:
        self.compress_calls.append((from_phase, to_phase))
        return f"summary {from_phase}->{to_phase}"


class FakeMemoryManager:
    async def load(self, user_id: str):
        return {"user_id": user_id}

    def generate_summary(self, memory) -> str:
        return f"memory:{memory['user_id']}"


@pytest.fixture
def mock_llm():
    provider = AsyncMock()
    return provider


@pytest.fixture
def engine():
    @tool(
        name="greet",
        description="Greet",
        phases=[1, 2, 3, 4, 5, 7],
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )
    async def greet(name: str) -> dict:
        return {"greeting": f"Hello, {name}!"}

    eng = ToolEngine()
    eng.register(greet)
    return eng


@pytest.fixture
def hooks():
    return HookManager()


@pytest.fixture
def agent(mock_llm, engine, hooks):
    plan = TravelPlanState(session_id="s1", phase=1)
    return AgentLoop(
        llm=mock_llm,
        tool_engine=engine,
        hooks=hooks,
        max_retries=3,
        phase_router=FakePhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="test-user",
    )


@pytest.mark.asyncio
async def test_text_response(agent, mock_llm):
    """LLM returns plain text, no tool calls."""

    async def mock_chat(*args, **kwargs):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="你好！")
        yield LLMChunk(type=ChunkType.DONE)

    mock_llm.chat = mock_chat

    messages = [Message(role=Role.USER, content="你好")]
    chunks = []
    async for chunk in agent.run(messages, phase=1):
        chunks.append(chunk)

    assert any(c.content == "你好！" for c in chunks)


@pytest.mark.asyncio
async def test_tool_call_then_response(agent, mock_llm):
    """LLM calls a tool, then returns text."""
    call_count = 0

    async def mock_chat(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_1", name="greet", arguments={"name": "World"}
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
        else:
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="已打招呼")
            yield LLMChunk(type=ChunkType.DONE)

    mock_llm.chat = mock_chat

    messages = [Message(role=Role.USER, content="say hi")]
    chunks = []
    async for chunk in agent.run(messages, phase=1):
        chunks.append(chunk)

    # Should have tool_call event + text response
    assert any(c.type == ChunkType.TOOL_CALL_START for c in chunks)
    assert any(
        c.type == ChunkType.TOOL_RESULT
        and c.tool_result is not None
        and c.tool_result.status == "success"
        for c in chunks
    )
    assert any(c.content == "已打招呼" for c in chunks)
    # Messages should have tool result appended
    assert any(m.role == Role.TOOL for m in messages)


@pytest.mark.asyncio
async def test_hooks_called(agent, mock_llm, hooks):
    """Hooks fire after tool calls."""
    hook_called = []

    async def track_hook(**kwargs):
        hook_called.append(kwargs.get("tool_name"))

    hooks.register("after_tool_call", track_hook)

    call_count = 0

    async def mock_chat(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(id="tc_1", name="greet", arguments={"name": "X"}),
            )
            yield LLMChunk(type=ChunkType.DONE)
        else:
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
            yield LLMChunk(type=ChunkType.DONE)

    mock_llm.chat = mock_chat

    messages = [Message(role=Role.USER, content="hi")]
    async for _ in agent.run(messages, phase=1):
        pass

    assert "greet" in hook_called


@pytest.mark.asyncio
async def test_phase_change_runs_full_batch_then_rebuilds_context():
    plan = TravelPlanState(session_id="s1", phase=1)
    context_manager = FakeContextManager()
    executed: list[str] = []

    @tool(
        name="advance_phase",
        description="advance",
        phases=[1],
        parameters={"type": "object", "properties": {}},
    )
    async def advance_phase() -> dict:
        executed.append("advance_phase")
        plan.phase = 3
        return {"ok": True}

    @tool(
        name="phase3_only",
        description="phase3",
        phases=[3],
        parameters={"type": "object", "properties": {}},
    )
    async def phase3_only() -> dict:
        executed.append("phase3_only")
        return {"ok": True}

    @tool(
        name="should_not_run",
        description="skip",
        phases=[1],
        parameters={"type": "object", "properties": {}},
    )
    async def should_not_run() -> dict:
        executed.append("should_not_run")
        return {"ok": False}

    engine = ToolEngine()
    engine.register(advance_phase)
    engine.register(phase3_only)
    engine.register(should_not_run)

    call_index = 0
    observed_second_call: dict[str, object] = {}

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_index
        call_index += 1
        if call_index == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(id="tc1", name="advance_phase", arguments={}),
            )
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(id="tc2", name="should_not_run", arguments={}),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        observed_second_call["tool_names"] = [tool["name"] for tool in tools or []]
        observed_second_call["messages"] = [m.content for m in messages]
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="phase 3 ready")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=3,
        phase_router=FakePhaseRouter(),
        context_manager=context_manager,
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u1",
    )

    messages = [Message(role=Role.USER, content="帮我继续规划")]
    chunks = [chunk async for chunk in agent.run(messages, phase=1)]

    assert executed == ["advance_phase", "should_not_run"]
    assert context_manager.compress_calls == [(1, 3)]
    assert observed_second_call["tool_names"] == ["phase3_only"]
    assert observed_second_call["messages"] == [
        "system phase=3 prompt=phase-3-prompt user=memory:u1",
        "[前序阶段摘要]\nsummary 1->3",
    ]
    assert any(chunk.content == "phase 3 ready" for chunk in chunks)


@pytest.mark.asyncio
async def test_backtrack_rebuild_uses_hard_boundary_without_compression():
    plan = TravelPlanState(session_id="s1", phase=4, destination="东京")
    context_manager = FakeContextManager()

    @tool(
        name="trigger_backtrack",
        description="backtrack",
        phases=[4],
        parameters={"type": "object", "properties": {}},
    )
    async def trigger_backtrack() -> dict:
        plan.phase = 1
        plan.backtrack_history.append(
            BacktrackEvent(
                from_phase=4,
                to_phase=1,
                reason="用户想换目的地",
                snapshot_path="",
            )
        )
        return {"backtracked": True}

    engine = ToolEngine()
    engine.register(trigger_backtrack)

    call_index = 0
    observed_second_call: dict[str, object] = {}

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_index
        call_index += 1
        if call_index == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(id="tc1", name="trigger_backtrack", arguments={}),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        observed_second_call["messages"] = [m.content for m in messages]
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="重新选目的地")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=3,
        phase_router=FakePhaseRouter(),
        context_manager=context_manager,
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u2",
    )

    messages = [Message(role=Role.USER, content="不想去这里了，换个目的地")]
    async for _ in agent.run(messages, phase=4):
        pass

    assert context_manager.compress_calls == []
    assert observed_second_call["messages"] == [
        "system phase=1 prompt=phase-1-prompt user=memory:u2",
        "[阶段回退]\n用户从 phase 4 回退到 phase 1，原因：用户想换目的地",
        "不想去这里了，换个目的地",
    ]


@pytest.mark.asyncio
async def test_backtrack_skips_remaining_tool_calls_after_hard_boundary():
    plan = TravelPlanState(session_id="s1", phase=4, destination="东京")
    executed: list[str] = []

    @tool(
        name="trigger_backtrack",
        description="backtrack",
        phases=[4],
        parameters={"type": "object", "properties": {}},
    )
    async def trigger_backtrack() -> dict:
        executed.append("trigger_backtrack")
        plan.phase = 1
        plan.backtrack_history.append(
            BacktrackEvent(
                from_phase=4,
                to_phase=1,
                reason="用户想换目的地",
                snapshot_path="",
            )
        )
        return {"backtracked": True}

    @tool(
        name="should_not_run",
        description="skip",
        phases=[4],
        parameters={"type": "object", "properties": {}},
    )
    async def should_not_run() -> dict:
        executed.append("should_not_run")
        return {"ok": False}

    engine = ToolEngine()
    engine.register(trigger_backtrack)
    engine.register(should_not_run)

    call_index = 0
    llm = MagicMock()

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_index
        call_index += 1
        if call_index == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(id="tc1", name="trigger_backtrack", arguments={}),
            )
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(id="tc2", name="should_not_run", arguments={}),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="重新选目的地")
        yield LLMChunk(type=ChunkType.DONE)

    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=3,
        phase_router=FakePhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u3",
    )

    chunks = [chunk async for chunk in agent.run([Message(role=Role.USER, content="换个目的地")], phase=4)]

    skipped = [
        chunk.tool_result
        for chunk in chunks
        if chunk.type == ChunkType.TOOL_RESULT and chunk.tool_result is not None
    ]
    assert executed == ["trigger_backtrack"]
    assert any(result.status == "skipped" for result in skipped)


@pytest.mark.asyncio
async def test_redundant_update_plan_state_is_skipped_after_phase_rebuild():
    plan = TravelPlanState(session_id="s1", phase=1)
    context_manager = FakeContextManager()
    engine = ToolEngine()
    engine.register(make_update_plan_state_tool(plan))

    call_index = 0
    llm = MagicMock()

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_index
        call_index += 1
        if call_index == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc1",
                    name="update_plan_state",
                    arguments={"field": "destination", "value": "东京"},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        if call_index == 2:
            assert plan.phase == 3
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc2",
                    name="update_plan_state",
                    arguments={"field": "destination", "value": "东京"},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="继续确认日期")
        yield LLMChunk(type=ChunkType.DONE)

    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=4,
        phase_router=PhaseRouter(),
        context_manager=context_manager,
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u4",
    )

    chunks = [
        chunk
        async for chunk in agent.run(
            [Message(role=Role.USER, content="我想去东京")],
            phase=1,
        )
    ]

    tool_results = [
        chunk.tool_result
        for chunk in chunks
        if chunk.type == ChunkType.TOOL_RESULT and chunk.tool_result is not None
    ]
    assert [result.status for result in tool_results] == ["success", "skipped"]
    assert tool_results[1].error_code == "REDUNDANT_STATE_UPDATE"
    assert any(chunk.content == "继续确认日期" for chunk in chunks)
