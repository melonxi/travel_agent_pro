# backend/tests/test_agent_loop.py
import pytest
from unittest.mock import AsyncMock, MagicMock

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role, ToolCall, ToolResult
from harness.guardrail import GuardrailResult
from llm.types import ChunkType, LLMChunk
from phase.router import PhaseRouter
from state.models import Accommodation, BacktrackEvent, DateRange, TravelPlanState
from tools.engine import ToolEngine
from tools.base import tool
from tools.update_plan_state import make_update_plan_state_tool


class FakePhaseRouter:
    def get_prompt(self, phase: int) -> str:
        return f"phase-{phase}-prompt"

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
        user_summary: str = "",
        available_tools: list[str] | None = None,
    ) -> Message:
        suffix = ""
        if available_tools:
            suffix = f" tools={','.join(available_tools)}"
        return Message(
            role=Role.SYSTEM,
            content=f"system phase={plan.phase} prompt={phase_prompt} user={user_summary}{suffix}",
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
    forced_choice = {"type": "function", "function": {"name": "update_plan_state"}}

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
async def test_backtrack_rebuild_uses_hard_boundary_without_compression():
    plan = TravelPlanState(session_id="s1", phase=5, destination="东京",
                           dates=DateRange(start="2026-05-01", end="2026-05-05"),
                           accommodation=Accommodation(area="新宿"))
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
    plan = TravelPlanState(session_id="s1", phase=3, phase3_step="brief")

    @tool(
        name="update_plan_state",
        description="state",
        phases=[3],
        parameters={"type": "object", "properties": {"field": {"type": "string"}, "value": {}}},
    )
    async def update_plan_state(field: str, value):
        if field == "phase3_step":
            plan.phase3_step = value
        return {"ok": True}

    @tool(
        name="search_accommodations",
        description="stay",
        phases=[3],
        parameters={"type": "object", "properties": {}},
    )
    async def search_accommodations() -> dict:
        return {"ok": True}

    engine = ToolEngine()
    engine.register(update_plan_state)
    engine.register(search_accommodations)

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
                    name="update_plan_state",
                    arguments={"field": "phase3_step", "value": "lock"},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="已进入 lock")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
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

    messages = [Message(role=Role.USER, content="进入 lock")]
    async for _ in agent.run(messages, phase=3):
        pass

    assert observed_tool_names[0] == ["update_plan_state"]
    assert observed_tool_names[1] == ["update_plan_state", "search_accommodations"]


@pytest.mark.asyncio
async def test_phase3_inferred_substep_refreshes_tools_after_dates_written():
    plan = TravelPlanState(session_id="s1", phase=3, destination="东京")
    engine = ToolEngine()
    engine.register(make_update_plan_state_tool(plan))

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
                    name="update_plan_state",
                    arguments={
                        "field": "dates",
                        "value": {"start": "2026-05-01", "end": "2026-05-06"},
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

    assert observed_tool_names[0] == ["update_plan_state"]
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
    engine.register(make_update_plan_state_tool(plan))

    observed_messages: list[list[str | None]] = []
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        observed_messages.append([message.content for message in messages])

        if call_count == 1:
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="方案A：轻松版\n方案B：平衡版\n方案C：高密度版")
            yield LLMChunk(type=ChunkType.DONE)
            return

        if call_count == 2:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc1",
                    name="update_plan_state",
                    arguments={
                        "field": "skeleton_plans",
                        "value": [
                            {"id": "relaxed", "title": "轻松版"},
                            {"id": "balanced", "title": "平衡版"},
                            {"id": "dense", "title": "高密度版"},
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

    assert [item["id"] for item in plan.skeleton_plans] == ["relaxed", "balanced", "dense"]
    assert any(
        content and "skeleton_plans" in content
        for call_messages in observed_messages[1:]
        for content in call_messages
    )


@pytest.mark.asyncio
async def test_backtrack_skips_remaining_tool_calls_after_hard_boundary():
    plan = TravelPlanState(session_id="s1", phase=5, destination="东京",
                           dates=DateRange(start="2026-05-01", end="2026-05-05"),
                           accommodation=Accommodation(area="新宿"))
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

    chunks = [chunk async for chunk in agent.run([Message(role=Role.USER, content="换个目的地")], phase=5)]

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


@pytest.mark.asyncio
async def test_phase5_text_only_daily_plan_triggers_state_repair():
    """When Phase 5 LLM outputs day-by-day text but forgets to call
    update_plan_state, the repair mechanism should inject a reminder."""
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
    engine.register(make_update_plan_state_tool(plan))

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
                    name="update_plan_state",
                    arguments={
                        "field": "daily_plans",
                        "value": [
                            {"day": 1, "date": "2026-04-15", "activities": [
                                {"name": "道顿堀", "location": {"name": "道顿堀", "lat": 34.6, "lng": 135.5},
                                 "start_time": "09:00", "end_time": "12:00", "category": "food", "cost": 0}
                            ]},
                            {"day": 2, "date": "2026-04-16", "activities": [
                                {"name": "大阪城", "location": {"name": "大阪城", "lat": 34.6, "lng": 135.5},
                                 "start_time": "09:00", "end_time": "15:00", "category": "landmark", "cost": 600}
                            ]},
                            {"day": 3, "date": "2026-04-17", "activities": [
                                {"name": "环球影城", "location": {"name": "USJ", "lat": 34.6, "lng": 135.4},
                                 "start_time": "09:00", "end_time": "20:00", "category": "theme_park", "cost": 8600}
                            ]},
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
    engine.register(make_update_plan_state_tool(plan))

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
    engine.register(make_update_plan_state_tool(plan))

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
