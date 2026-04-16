# backend/tests/test_agent_loop.py
import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role, ToolCall, ToolResult
from harness.guardrail import GuardrailResult
from llm.errors import LLMError, LLMErrorCode
from llm.types import ChunkType, LLMChunk
from phase.router import PhaseRouter
from run import IterationProgress
from state.models import Accommodation, BacktrackEvent, DateRange, TravelPlanState
from tools.engine import ToolEngine
from tools.base import tool
from tests.helpers.register_plan_tools import register_all_plan_tools


class FakePhaseRouter:
    def get_prompt(self, phase: int) -> str:
        return f"phase-{phase}-prompt"

    def get_prompt_for_plan(self, plan) -> str:
        return f"phase-{plan.phase}-prompt"

    async def check_and_apply_transition(
        self, plan: TravelPlanState, hooks=None
    ) -> bool:
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
        self.compress_calls.append((from_phase, to_phase))
        return f"summary {from_phase}->{to_phase}"


class EmptySummaryContextManager(FakeContextManager):
    async def compress_for_transition(
        self,
        messages: list[Message],
        from_phase: int,
        to_phase: int,
        llm_factory,
    ) -> str:
        self.compress_calls.append((from_phase, to_phase))
        return ""


class FakeMemoryManager:
    async def load(self, user_id: str):
        return {"user_id": user_id}

    def generate_summary(self, memory) -> str:
        return f"memory:{memory['user_id']}"

    async def generate_context(
        self, user_id: str, plan: TravelPlanState
    ) -> tuple[str, list[str], int, int, int]:
        return f"memory:{user_id}", [], 0, 0, 0


@pytest.fixture
def mock_llm():
    provider = AsyncMock()
    return provider


@pytest.fixture
def engine():
    @tool(
        name="greet",
        description="Greet",
        phases=[1, 2, 3, 5, 7],
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
async def test_agent_loop_forwards_usage_chunks(agent, mock_llm):
    """Provider token usage must reach the API layer for SessionStats."""

    async def mock_chat(*args, **kwargs):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="你好")
        yield LLMChunk(
            type=ChunkType.USAGE,
            usage_info={"input_tokens": 100, "output_tokens": 20},
        )
        yield LLMChunk(type=ChunkType.DONE)

    mock_llm.chat = mock_chat

    messages = [Message(role=Role.USER, content="你好")]
    chunks = [chunk async for chunk in agent.run(messages, phase=1)]

    assert [chunk.type for chunk in chunks] == [
        ChunkType.AGENT_STATUS,
        ChunkType.TEXT_DELTA,
        ChunkType.USAGE,
        ChunkType.DONE,
    ]
    assert chunks[0].agent_status["stage"] == "thinking"
    assert chunks[2].usage_info == {"input_tokens": 100, "output_tokens": 20}


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
async def test_tool_choice_decider_result_is_passed_to_llm(engine, hooks):
    plan = TravelPlanState(session_id="s1", phase=3, phase3_step="brief")
    forced_choice = {"type": "function", "function": {"name": "set_trip_brief"}}

    class FakeToolChoiceDecider:
        def decide(self, plan_arg, messages_arg, phase_arg):
            assert plan_arg is plan
            assert phase_arg == 3
            return forced_choice

    observed: dict[str, object] = {}

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        observed["tool_choice"] = tool_choice
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="ok")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=hooks,
        plan=plan,
        tool_choice_decider=FakeToolChoiceDecider(),
    )

    async for _ in agent.run([Message(role=Role.USER, content="继续")], phase=3):
        pass

    assert observed["tool_choice"] == forced_choice


@pytest.mark.asyncio
async def test_reflection_message_is_injected_before_llm_call(engine, hooks):
    plan = TravelPlanState(session_id="s1", phase=3, phase3_step="lock")

    class FakeReflection:
        def check_and_inject(self, messages, plan_arg, prev_step):
            assert plan_arg is plan
            return "[自检] 请先检查方案"

    observed_messages: list[str | None] = []

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        observed_messages.extend(message.content for message in messages)
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="ok")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=hooks,
        plan=plan,
        reflection=FakeReflection(),
    )

    async for _ in agent.run([Message(role=Role.USER, content="继续")], phase=3):
        pass

    assert "[自检] 请先检查方案" in observed_messages


@pytest.mark.asyncio
async def test_guardrail_rejects_tool_input_before_execution(hooks):
    executed: list[str] = []

    @tool(
        name="dangerous",
        description="danger",
        phases=[1],
        parameters={"type": "object", "properties": {}},
    )
    async def dangerous() -> dict:
        executed.append("dangerous")
        return {"ok": True}

    class RejectingGuardrail:
        def validate_input(self, tc):
            return GuardrailResult(allowed=False, reason="blocked")

        def validate_output(self, tool_name, data):
            return GuardrailResult()

    engine = ToolEngine()
    engine.register(dangerous)

    call_count = 0

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(id="tc1", name="dangerous", arguments={}),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=hooks,
        plan=TravelPlanState(session_id="s1", phase=1),
        guardrail=RejectingGuardrail(),
    )

    chunks = [
        chunk
        async for chunk in agent.run([Message(role=Role.USER, content="run")], phase=1)
    ]

    tool_results = [
        chunk.tool_result
        for chunk in chunks
        if chunk.type == ChunkType.TOOL_RESULT and chunk.tool_result is not None
    ]
    assert executed == []
    assert tool_results[0].status == "skipped"
    assert tool_results[0].error_code == "GUARDRAIL_REJECTED"


@pytest.mark.asyncio
async def test_consecutive_read_tools_use_execute_batch(hooks):
    @tool(
        name="read_one",
        description="read",
        phases=[1],
        parameters={"type": "object", "properties": {}},
    )
    async def read_one() -> dict:
        return {"one": True}

    @tool(
        name="read_two",
        description="read",
        phases=[1],
        parameters={"type": "object", "properties": {}},
    )
    async def read_two() -> dict:
        return {"two": True}

    class TrackingEngine(ToolEngine):
        def __init__(self) -> None:
            super().__init__()
            self.batch_sizes: list[int] = []

        async def execute_batch(self, calls):
            self.batch_sizes.append(len(calls))
            return await super().execute_batch(calls)

    engine = TrackingEngine()
    engine.register(read_one)
    engine.register(read_two)

    call_count = 0

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(id="tc1", name="read_one", arguments={}),
            )
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(id="tc2", name="read_two", arguments={}),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=hooks,
        plan=TravelPlanState(session_id="s1", phase=1),
    )

    async for _ in agent.run([Message(role=Role.USER, content="run")], phase=1):
        pass

    assert engine.batch_sizes == [2]


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
        observed_second_call["roles"] = [m.role for m in messages]
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
        "system phase=3 prompt=phase-3-prompt user=memory:u1 tools=phase3_only",
        "以下是阶段 1 的对话与工具调用回顾，现在进入阶段 3。\nsummary 1->3",
        "帮我继续规划",
    ]
    # The transition summary must ride on an assistant turn, not a second
    # system message — multi-system payloads are flaky across providers.
    observed_roles = observed_second_call.get("roles")
    if observed_roles is not None:
        assert observed_roles == [Role.SYSTEM, Role.ASSISTANT, Role.USER]
    assert any(chunk.content == "phase 3 ready" for chunk in chunks)


@pytest.mark.asyncio
async def test_phase_rebuild_skips_memory_when_disabled(mock_llm, engine, hooks):
    plan = TravelPlanState(session_id="s1", phase=3)
    agent = AgentLoop(
        llm=mock_llm,
        tool_engine=engine,
        hooks=hooks,
        max_retries=3,
        phase_router=FakePhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        memory_enabled=False,
        user_id="u1",
    )

    rebuilt = await agent._rebuild_messages_for_phase_change(
        [Message(role=Role.USER, content="继续")],
        from_phase=1,
        to_phase=3,
        original_user_message=Message(role=Role.USER, content="继续"),
        result=ToolResult(tool_call_id="tc1", status="success", data={}),
    )

    assert "memory:u1" not in rebuilt[0].content
    assert "暂无相关用户记忆" in rebuilt[0].content


@pytest.mark.asyncio
async def test_backtrack_rebuild_uses_hard_boundary_without_compression():
    plan = TravelPlanState(
        session_id="s1",
        phase=5,
        destination="东京",
        dates=DateRange(start="2026-05-01", end="2026-05-05"),
        accommodation=Accommodation(area="新宿"),
    )
    context_manager = FakeContextManager()

    @tool(
        name="trigger_backtrack",
        description="backtrack",
        phases=[5],
        parameters={"type": "object", "properties": {}},
        side_effect="write",
    )
    async def trigger_backtrack() -> dict:
        plan.phase = 1
        plan.backtrack_history.append(
            BacktrackEvent(
                from_phase=5,
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
    async for _ in agent.run(messages, phase=5):
        pass

    assert context_manager.compress_calls == []
    assert observed_second_call["messages"] == [
        "system phase=1 prompt=phase-1-prompt user=memory:u2",
        "[阶段回退]\n用户从 phase 5 回退到 phase 1，原因：用户想换目的地",
        "不想去这里了，换个目的地",
    ]


@pytest.mark.asyncio
async def test_forward_phase_rebuild_keeps_user_anchor_when_summary_empty():
    plan = TravelPlanState(session_id="s1", phase=1)
    context_manager = EmptySummaryContextManager()

    @tool(
        name="advance_phase",
        description="advance",
        phases=[1],
        parameters={"type": "object", "properties": {}},
    )
    async def advance_phase() -> dict:
        plan.phase = 3
        return {"ok": True}

    engine = ToolEngine()
    engine.register(advance_phase)

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
            yield LLMChunk(type=ChunkType.DONE)
            return

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
        user_id="u-empty",
    )

    messages = [Message(role=Role.USER, content="帮我继续规划")]
    async for _ in agent.run(messages, phase=1):
        pass

    assert context_manager.compress_calls == [(1, 3)]
    assert observed_second_call["messages"] == [
        "system phase=3 prompt=phase-3-prompt user=memory:u-empty",
        "帮我继续规划",
    ]


@pytest.mark.asyncio
async def test_phase3_substep_change_refreshes_tools():
    """Test that tool availability changes when phase3_step changes.

    Uses the real plan tools registered through register_all_plan_tools,
    which respects the engine's phase3_step-based tool filtering.
    """
    plan = TravelPlanState(session_id="s1", phase=3, phase3_step="brief")

    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    observed_tool_names: list[list[str]] = []
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        observed_tool_names.append([tool["name"] for tool in tools or []])
        if call_count == 1:
            # Call set_trip_brief - this writes trip_brief
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc1",
                    name="set_trip_brief",
                    arguments={"fields": {"destination": "东京", "goal": "轻松游"}},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="继续规划")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=3,
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u3",
    )

    messages = [Message(role=Role.USER, content="帮我设定trip brief")]
    async for _ in agent.run(messages, phase=3):
        pass

    # On first call (brief step), set_trip_brief is available
    assert "set_trip_brief" in observed_tool_names[0]
    # trip_brief should be set
    assert plan.trip_brief is not None
    assert plan.trip_brief.get("destination") == "东京"


@pytest.mark.asyncio
async def test_phase3_inferred_substep_refreshes_tools_after_dates_written():
    plan = TravelPlanState(session_id="s1", phase=3, destination="东京")
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    @tool(
        name="quick_travel_search",
        description="quick",
        phases=[3],
        parameters={"type": "object", "properties": {}},
    )
    async def quick_travel_search() -> dict:
        return {"ok": True}

    engine.register(quick_travel_search)

    observed_tool_names: list[list[str]] = []
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        observed_tool_names.append([tool["name"] for tool in tools or []])
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc1",
                    name="update_trip_basics",
                    arguments={
                        "dates": {"start": "2026-05-01", "end": "2026-05-06"},
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="进入 candidate")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=3,
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u4",
    )

    messages = [Message(role=Role.USER, content="五一去东京玩5天")]
    async for _ in agent.run(messages, phase=3):
        pass

    assert "update_trip_basics" in observed_tool_names[0]
    assert "quick_travel_search" in observed_tool_names[1]
    assert plan.phase3_step == "candidate"
    assert plan.trip_brief["destination"] == "东京"


@pytest.mark.asyncio
async def test_phase3_text_only_skeleton_response_triggers_state_repair():
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        phase3_step="skeleton",
        destination="东京",
        dates=DateRange(start="2026-05-01", end="2026-05-06"),
        trip_brief={"goal": "慢旅行"},
    )
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    observed_messages: list[list[str | None]] = []
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        observed_messages.append([message.content for message in messages])

        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA,
                content="方案A：轻松版\n方案B：平衡版\n方案C：高密度版",
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        if call_count == 2:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc1",
                    name="set_skeleton_plans",
                    arguments={
                        "plans": [
                            {"id": "relaxed", "name": "轻松版"},
                            {"id": "balanced", "name": "平衡版"},
                            {"id": "dense", "name": "高密度版"},
                        ],
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="已写入骨架方案")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=4,
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u5",
    )

    messages = [Message(role=Role.USER, content="给我三套骨架方案")]
    async for _ in agent.run(messages, phase=3):
        pass

    assert [item["id"] for item in plan.skeleton_plans] == [
        "relaxed",
        "balanced",
        "dense",
    ]
    assert any(
        content and "skeleton_plans" in content
        for call_messages in observed_messages[1:]
        for content in call_messages
    )


@pytest.mark.asyncio
async def test_phase3_candidate_partial_split_write_triggers_repair():
    """Test that partial split-write (candidate_pool exists but shortlist missing) triggers repair hint."""
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        phase3_step="candidate",
        destination="东京",
        dates=DateRange(start="2026-05-01", end="2026-05-06"),
        trip_brief={"goal": "文化之旅"},
        candidate_pool=[
            {"place": "浅草寺", "reason": "经典景点"},
            {"place": "晴空塔", "reason": "现代地标"},
        ],
        # shortlist is missing -> partial failure
    )
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    observed_messages: list[list[str | None]] = []
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        observed_messages.append([message.content for message in messages])

        if call_count == 1:
            # LLM gives candidate analysis text but forgets to call set_shortlist
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA,
                content="经过筛选，推荐浅草寺作为首选，晴空塔作为备选。",
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        if call_count == 2:
            # After repair hint, LLM calls set_shortlist
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc1",
                    name="set_shortlist",
                    arguments={
                        "items": [{"place": "浅草寺", "rank": 1}],
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="shortlist 已写入")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=4,
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u6",
    )

    messages = [Message(role=Role.USER, content="帮我筛选候选方案")]
    async for _ in agent.run(messages, phase=3):
        pass

    # Verify repair hint was injected and only asks for shortlist repair.
    repair_messages = [
        content
        for call_messages in observed_messages[1:]
        for content in call_messages
        if content and "状态同步" in content
    ]
    assert repair_messages
    assert any("set_shortlist" in content for content in repair_messages)
    assert all("set_candidate_pool" not in content for content in repair_messages)
    assert all(
        "candidate_pool / shortlist 仍为空" not in content
        for content in repair_messages
    )
    # Verify shortlist was eventually written
    assert plan.shortlist is not None and len(plan.shortlist) > 0


@pytest.mark.asyncio
async def test_phase3_candidate_skeleton_leakage_triggers_repair():
    """When Agent is in candidate step but describes skeleton plans without
    calling set_skeleton_plans, repair should fire telling it to write
    skeleton_plans state."""
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        phase3_step="candidate",
        destination="四礵列岛",
        dates=DateRange(start="2026-05-01", end="2026-05-06"),
        trip_brief={"goal": "海岛探险"},
        candidate_pool=[
            {"place": "东礵岛", "reason": "主岛"},
            {"place": "西礵岛", "reason": "原生态"},
        ],
        shortlist=[
            {"place": "东礵岛", "rank": 1},
            {"place": "西礵岛", "rank": 2},
        ],
        # skeleton_plans intentionally empty
    )
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    observed_messages: list[list[str | None]] = []
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        observed_messages.append([message.content for message in messages])

        if call_count == 1:
            # Agent describes skeleton plans in text without calling tool
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA,
                content="方案A：轻松版——以东礵岛为主\n方案B：深度版——跳岛游览",
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        if call_count == 2:
            # After repair hint, Agent calls set_skeleton_plans
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc1",
                    name="set_skeleton_plans",
                    arguments={
                        "plans": [
                            {"id": "plan_A", "name": "轻松版"},
                            {"id": "plan_B", "name": "深度版"},
                        ],
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="骨架方案已写入")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=4,
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u_skel_leak",
    )

    messages = [Message(role=Role.USER, content="帮我设计骨架方案")]
    async for _ in agent.run(messages, phase=3):
        pass

    # Verify repair hint was injected mentioning set_skeleton_plans
    repair_messages = [
        content
        for call_messages in observed_messages[1:]
        for content in call_messages
        if content and "状态同步" in content
    ]
    assert repair_messages, "Should have injected a repair hint"
    assert any("set_skeleton_plans" in m for m in repair_messages)
    # Verify skeleton_plans was eventually written
    assert len(plan.skeleton_plans) == 2


@pytest.mark.asyncio
async def test_phase3_lock_repair_triggers_per_field():
    """Lock repair should fire when any individual field is missing,
    not require all 4 fields to be empty simultaneously."""
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        phase3_step="lock",
        destination="京都",
        dates=DateRange(start="2026-05-01", end="2026-05-06"),
        trip_brief={"goal": "文化之旅"},
        skeleton_plans=[{"id": "plan_A", "name": "经典京都"}],
        selected_skeleton_id="plan_A",
        # transport_options already filled
        transport_options=[{"type": "新干线", "price": 1200}],
        # accommodation_options intentionally empty
    )
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    observed_messages: list[list[str | None]] = []
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        observed_messages.append([message.content for message in messages])

        if call_count == 1:
            # Agent describes accommodation without writing state
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA,
                content="推荐住宿：京都祗园附近的民宿，价格约 800 元/晚。",
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="好的")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=4,
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u_lock_field",
    )

    messages = [Message(role=Role.USER, content="帮我锁定住宿")]
    async for _ in agent.run(messages, phase=3):
        pass

    # Verify repair was injected mentioning accommodation
    repair_messages = [
        content
        for call_messages in observed_messages[1:]
        for content in call_messages
        if content and "状态同步" in content
    ]
    assert repair_messages, "Should trigger repair for missing accommodation"
    assert any(
        "set_accommodation_options" in m or "set_accommodation" in m
        for m in repair_messages
    )
    # Should NOT mention transport since it's already filled
    assert all("set_transport_options" not in m for m in repair_messages)


@pytest.mark.asyncio
async def test_phase3_repair_retry_fires_twice_then_stops():
    """Repair should fire twice (original + retry) for the same step,
    then stop on the third consecutive text-only response."""
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        phase3_step="skeleton",
        destination="东京",
        dates=DateRange(start="2026-05-01", end="2026-05-06"),
        trip_brief={"goal": "文化之旅"},
    )
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        # Always output skeleton text without calling tools
        yield LLMChunk(
            type=ChunkType.TEXT_DELTA,
            content="方案A：轻松版\n方案B：平衡版\n方案C：高密度版",
        )
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=5,
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u_retry",
    )

    messages = [Message(role=Role.USER, content="给我骨架方案")]
    async for _ in agent.run(messages, phase=3):
        pass

    # call 1: text → repair fires (p3_skeleton)
    # call 2: text → retry repair fires (p3_skeleton_retry)
    # call 3: text → both keys exhausted → no repair → loop ends
    assert call_count == 3


@pytest.mark.asyncio
async def test_redundant_search_skipped_after_two_identical_queries():
    """After the same search query is used twice, the third identical
    search call should be skipped with a helpful message."""
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        phase3_step="candidate",
        destination="东京",
        dates=DateRange(start="2026-05-01", end="2026-05-06"),
        trip_brief={"goal": "文化之旅"},
    )
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    call_count = 0
    skipped_results: list[ToolResult] = []

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1

        if call_count <= 3:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id=f"tc{call_count}",
                    name="web_search",
                    arguments={"query": "东京 文化景点 推荐"},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="搜索完成")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=5,
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u_dup_search",
    )

    messages = [Message(role=Role.USER, content="搜索景点")]
    chunks = []
    async for chunk in agent.run(messages, phase=3):
        chunks.append(chunk)

    # The third search call (call_count==3) should have been skipped
    # because the same query appeared 2 times before.
    # After skip, LLM gets error result and makes call 4 → final text.
    tool_result_chunks = [c for c in chunks if c.type == ChunkType.TOOL_RESULT]
    skipped = [
        c
        for c in tool_result_chunks
        if hasattr(c, "tool_result")
        and c.tool_result
        and c.tool_result.status == "skipped"
    ]
    # At minimum, the third call should have been skipped
    assert any(
        c.tool_result.error_code == "REDUNDANT_SEARCH"
        for c in chunks
        if c.type == ChunkType.TOOL_RESULT and c.tool_result
    ), "Third identical search should be skipped with REDUNDANT_SEARCH"


@pytest.mark.asyncio
async def test_backtrack_skips_remaining_tool_calls_after_hard_boundary():
    plan = TravelPlanState(
        session_id="s1",
        phase=5,
        destination="东京",
        dates=DateRange(start="2026-05-01", end="2026-05-05"),
        accommodation=Accommodation(area="新宿"),
    )
    executed: list[str] = []

    @tool(
        name="trigger_backtrack",
        description="backtrack",
        phases=[5],
        parameters={"type": "object", "properties": {}},
        side_effect="write",
    )
    async def trigger_backtrack() -> dict:
        executed.append("trigger_backtrack")
        plan.phase = 1
        plan.backtrack_history.append(
            BacktrackEvent(
                from_phase=5,
                to_phase=1,
                reason="用户想换目的地",
                snapshot_path="",
            )
        )
        return {"backtracked": True}

    @tool(
        name="should_not_run",
        description="skip",
        phases=[5],
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

    chunks = [
        chunk
        async for chunk in agent.run(
            [Message(role=Role.USER, content="换个目的地")], phase=5
        )
    ]

    skipped = [
        chunk.tool_result
        for chunk in chunks
        if chunk.type == ChunkType.TOOL_RESULT and chunk.tool_result is not None
    ]
    assert executed == ["trigger_backtrack"]
    assert any(result.status == "skipped" for result in skipped)


@pytest.mark.asyncio
async def test_phase5_text_only_daily_plan_triggers_state_repair():
    """When Phase 5 LLM outputs day-by-day text but forgets to call
    plan tools, the repair mechanism should inject a reminder."""
    plan = TravelPlanState(
        session_id="s1",
        phase=5,
        destination="大阪",
        dates=DateRange(start="2026-04-15", end="2026-04-18"),
        skeleton_plans=[{"id": "plan_A", "theme": "经典大阪"}],
        selected_skeleton_id="plan_A",
        accommodation=Accommodation(area="心斋桥"),
    )
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    observed_messages: list[list[str | None]] = []
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        observed_messages.append([message.content for message in messages])

        if call_count == 1:
            # LLM outputs itinerary text but no tool call
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA,
                content="第1天（4/15）：道顿堀 + 心斋桥 09:00-18:00\n第2天（4/16）：大阪城\n第3天：环球影城",
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        if call_count == 2:
            # After repair hint, LLM writes daily_plans
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc1",
                    name="replace_daily_plans",
                    arguments={
                        "days": [
                            {
                                "day": 1,
                                "date": "2026-04-15",
                                "activities": [
                                    {
                                        "name": "道顿堀",
                                        "location": {
                                            "name": "道顿堀",
                                            "lat": 34.6,
                                            "lng": 135.5,
                                        },
                                        "start_time": "09:00",
                                        "end_time": "12:00",
                                        "category": "food",
                                        "cost": 0,
                                    }
                                ],
                            },
                            {
                                "day": 2,
                                "date": "2026-04-16",
                                "activities": [
                                    {
                                        "name": "大阪城",
                                        "location": {
                                            "name": "大阪城",
                                            "lat": 34.6,
                                            "lng": 135.5,
                                        },
                                        "start_time": "09:00",
                                        "end_time": "15:00",
                                        "category": "landmark",
                                        "cost": 600,
                                    }
                                ],
                            },
                            {
                                "day": 3,
                                "date": "2026-04-17",
                                "activities": [
                                    {
                                        "name": "环球影城",
                                        "location": {
                                            "name": "USJ",
                                            "lat": 34.6,
                                            "lng": 135.4,
                                        },
                                        "start_time": "09:00",
                                        "end_time": "20:00",
                                        "category": "theme_park",
                                        "cost": 8600,
                                    }
                                ],
                            },
                        ],
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="行程已写入")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=4,
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u_p5",
    )

    messages = [Message(role=Role.USER, content="帮我排出每天的行程")]
    async for _ in agent.run(messages, phase=5):
        pass

    # daily_plans should be written
    assert len(plan.daily_plans) == 3
    # The repair hint should have been injected
    assert any(
        content and "daily_plans" in content and "状态同步提醒" in content
        for call_messages in observed_messages[1:]
        for content in call_messages
        if content
    )


@pytest.mark.asyncio
async def test_phase5_repair_hint_not_repeated():
    """After a repair hint is sent once, it should not be repeated even if LLM
    outputs itinerary text again."""
    plan = TravelPlanState(
        session_id="s1",
        phase=5,
        destination="大阪",
        dates=DateRange(start="2026-04-15", end="2026-04-17"),
        skeleton_plans=[{"id": "plan_A", "theme": "经典大阪"}],
        selected_skeleton_id="plan_A",
    )
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            # Keep outputting text without tool calls
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA,
                content="第1天：道顿堀 09:00-18:00 景点游览\n第2天：大阪城",
            )
            yield LLMChunk(type=ChunkType.DONE)
            return
        # Third call: give up with final text
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="好的")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=4,
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u_p5_dedup",
    )

    messages = [Message(role=Role.USER, content="帮我排出每天的行程")]
    async for _ in agent.run(messages, phase=5):
        pass

    # Repair fires on call 1 (dedup key added), call 2 skips repair → agent ends.
    assert call_count == 2


@pytest.mark.asyncio
async def test_phase5_repair_detects_json_style_output():
    """Repair should also trigger when LLM outputs JSON-style itinerary."""
    plan = TravelPlanState(
        session_id="s1",
        phase=5,
        destination="京都",
        dates=DateRange(start="2026-05-01", end="2026-05-03"),
        skeleton_plans=[{"id": "planB"}],
        selected_skeleton_id="planB",
    )
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # JSON-style output without tool call
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA,
                content='[{"day": 1, "date": "2026-05-01", "activities": [{"name": "金阁寺", "start_time": "09:00"}]}]',
            )
            yield LLMChunk(type=ChunkType.DONE)
            return
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="已完成")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=4,
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u_p5_json",
    )

    messages = [Message(role=Role.USER, content="排行程")]
    async for _ in agent.run(messages, phase=5):
        pass

    # Repair should have fired (call_count > 1)
    assert call_count >= 2


@pytest.mark.asyncio
async def test_cancel_event_stops_before_llm_call():
    cancel_event = asyncio.Event()
    cancel_event.set()  # 已经取消

    mock_llm = MagicMock()
    mock_llm.provider_name = "openai"
    mock_llm.model = "gpt-4o"
    engine = ToolEngine()
    hooks = HookManager()

    loop = AgentLoop(
        llm=mock_llm,
        tool_engine=engine,
        hooks=hooks,
        cancel_event=cancel_event,
    )
    messages = [Message(role=Role.USER, content="hi")]
    with pytest.raises(LLMError) as exc_info:
        async for _ in loop.run(messages, phase=1):
            pass
    assert exc_info.value.failure_phase == "cancelled"


@pytest.mark.asyncio
async def test_cancel_event_stops_during_streaming():
    cancel_event = asyncio.Event()

    async def fake_chat(messages, **kwargs):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="hello")
        cancel_event.set()  # 模拟第一个 chunk 后取消
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content=" world")

    mock_llm = MagicMock()
    mock_llm.provider_name = "openai"
    mock_llm.model = "gpt-4o"
    mock_llm.chat = fake_chat
    engine = ToolEngine()
    hooks = HookManager()

    loop = AgentLoop(
        llm=mock_llm,
        tool_engine=engine,
        hooks=hooks,
        cancel_event=cancel_event,
    )
    messages = [Message(role=Role.USER, content="hi")]
    chunks = []
    with pytest.raises(LLMError) as exc_info:
        async for chunk in loop.run(messages, phase=1):
            chunks.append(chunk)
    # 第一个 chunk 是 agent_status(thinking)，第二个是 text_delta("hello")
    assert len(chunks) == 2
    assert chunks[0].type == ChunkType.AGENT_STATUS
    assert chunks[1].content == "hello"
    assert exc_info.value.failure_phase == "cancelled"


@pytest.mark.asyncio
async def test_progress_tracks_partial_text():
    async def fake_chat(messages, **kwargs):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="hello")
        yield LLMChunk(type=ChunkType.DONE)

    mock_llm = MagicMock()
    mock_llm.provider_name = "openai"
    mock_llm.model = "gpt-4o"
    mock_llm.chat = fake_chat
    engine = ToolEngine()
    hooks = HookManager()

    loop = AgentLoop(llm=mock_llm, tool_engine=engine, hooks=hooks)
    messages = [Message(role=Role.USER, content="hi")]
    async for _ in loop.run(messages, phase=1):
        pass
    assert loop.progress == IterationProgress.PARTIAL_TEXT
