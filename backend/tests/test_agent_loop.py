# backend/tests/test_agent_loop.py
import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from agent.hooks import GateResult, HookManager
from agent.internal_tasks import InternalTask
from agent.loop import AgentLoop
from agent.types import Message, Role, ToolCall, ToolResult
from config import Phase3ParallelConfig
from harness.guardrail import GuardrailResult
from llm.errors import LLMError, LLMErrorCode
from llm.types import ChunkType, LLMChunk
from phase.router import PhaseRouter
from run import IterationProgress
from state.models import Accommodation, BacktrackEvent, DateRange, TravelPlanState
from telemetry.trace_recorder import TraceContext, TraceRecorder
from tools.engine import ToolEngine
from tools.base import tool
from tests.helpers.register_plan_tools import register_all_plan_tools
from tools.plan_tools.backtrack import make_request_backtrack_tool


class FakePhaseRouter:
    def get_prompt(self, phase: int) -> str:
        return f"phase-{phase}-prompt"

    def get_prompt_for_plan(self, plan) -> str:
        return f"phase-{plan.phase}-prompt"

    async def check_and_apply_transition(
        self, plan: TravelPlanState, hooks=None
    ) -> bool:
        if plan.phase == 2:
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

    def build_static_system_message(
        self,
        plan: TravelPlanState,
        phase_prompt: str,
    ) -> Message:
        return Message(
            role=Role.SYSTEM,
            content=f"system phase={plan.phase} prompt={phase_prompt}",
            transient=True,
        )

    def build_turn_context_message(
        self,
        *,
        plan: TravelPlanState,
        available_tools: list[str] | None = None,
        memory_context: str = "",
    ) -> Message:
        suffix = ""
        if available_tools:
            suffix = f" tools={','.join(available_tools)}"
        return Message(
            role=Role.USER,
            content=f"<turn_context>user={memory_context}{suffix}</turn_context>",
            transient=True,
        )

    def build_app_event_message(self, *, kind: str, content: str) -> Message:
        return Message(
            role=Role.USER,
            content=f'<app_event kind="{kind}">{content}</app_event>',
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

    def build_phase_handoff_note(self, *, plan, from_phase, to_phase) -> str:
        return f"handoff {from_phase}->{to_phase} phase={plan.phase}"


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


class TraceStoreStub:
    def __init__(self):
        self.events = []
        self.artifacts = []

    async def append_event(self, event):
        self.events.append(event)

    async def save_artifact_metadata(self, metadata):
        self.artifacts.append(metadata)


@pytest.fixture
def mock_llm():
    provider = AsyncMock()
    return provider


@pytest.fixture
def engine():
    @tool(
        name="greet",
        description="Greet",
        phases=[1, 2, 3, 4],
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
async def test_agent_loop_emits_phase_gate_and_transition_trace_events(hooks):
    plan = TravelPlanState(session_id="s1", phase=1)

    @tool(
        name="promote_phase",
        description="promote",
        phases=[1],
        parameters={"type": "object", "properties": {}, "required": []},
        side_effect="write",
    )
    async def promote_phase() -> dict:
        plan.phase = 2
        return {"phase": 2}

    class _LLM:
        provider_name = "test"
        model = "fake"
        temperature = 0
        max_tokens = 16

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="call-1",
                        name="promote_phase",
                        arguments={},
                    ),
                )
                yield LLMChunk(
                    type=ChunkType.USAGE,
                    usage_info={"input_tokens": 1, "output_tokens": 1},
                )
            else:
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
            yield LLMChunk(type=ChunkType.DONE)

    engine = ToolEngine()
    engine.register(promote_phase)
    store = TraceStoreStub()
    agent = AgentLoop(
        llm=_LLM(),
        tool_engine=engine,
        hooks=hooks,
        phase_router=FakePhaseRouter(),
        context_manager=FakeContextManager(),
        memory_mgr=FakeMemoryManager(),
        plan=plan,
        trace_recorder=TraceRecorder(trace_store=store),
        trace_context=TraceContext(run_id="run-1", session_id="s1", phase=1),
    )

    async for _chunk in agent.run([Message(role=Role.USER, content="go")], phase=1):
        pass

    phase_gate = next(event for event in store.events if event.event_type == "phase_gate")
    transition = next(
        event for event in store.events if event.event_type == "phase_transition"
    )
    assert phase_gate.payload["allowed"] is True
    assert phase_gate.payload["from_phase"] == 1
    assert phase_gate.payload["to_phase_candidate"] == 2
    assert transition.parent_event_id == phase_gate.event_id
    assert transition.payload["from_phase"] == 1
    assert transition.payload["to_phase"] == 2


@pytest.mark.asyncio
async def test_agent_loop_emits_blocked_quality_gate_trace_event():
    plan = TravelPlanState(session_id="s1", phase=1)
    internal_tasks: list[InternalTask] = []
    hooks = HookManager()

    async def block_transition(**kwargs):
        internal_tasks.append(
            InternalTask(
                id="quality_gate:s1:1:2",
                kind="quality_gate",
                label="gate",
                status="warning",
                message="blocked",
                blocking=True,
                result={"errors": ["missing budget"], "retry_count": 1},
            )
        )
        return GateResult(allowed=False, feedback="blocked")

    hooks.register_gate("before_phase_transition", block_transition)

    @tool(
        name="update_trip_basics",
        description="write basics",
        phases=[1],
        parameters={"type": "object", "properties": {}, "required": []},
        side_effect="write",
    )
    async def update_trip_basics() -> dict:
        plan.destination = "Tokyo"
        return {"destination": "Tokyo"}

    class _LLM:
        provider_name = "test"
        model = "fake"

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="call-1",
                        name="update_trip_basics",
                        arguments={},
                    ),
                )
            else:
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
            yield LLMChunk(type=ChunkType.DONE)

    engine = ToolEngine()
    engine.register(update_trip_basics)
    store = TraceStoreStub()
    agent = AgentLoop(
        llm=_LLM(),
        tool_engine=engine,
        hooks=hooks,
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        memory_mgr=FakeMemoryManager(),
        plan=plan,
        internal_task_events=internal_tasks,
        trace_recorder=TraceRecorder(trace_store=store),
        trace_context=TraceContext(run_id="run-1", session_id="s1", phase=1),
    )

    async for _chunk in agent.run([Message(role=Role.USER, content="go")], phase=1):
        pass

    phase_gate = next(event for event in store.events if event.event_type == "phase_gate")
    quality_gate = next(
        event for event in store.events if event.event_type == "quality_gate"
    )
    assert phase_gate.status == "blocked"
    assert phase_gate.payload["allowed"] is False
    assert "no_phase_or_step_change" in phase_gate.payload["blockers"]
    assert quality_gate.parent_event_id == phase_gate.event_id
    assert quality_gate.payload["blockers"] == ["missing budget"]
    assert quality_gate.payload["retry_count"] == 1
    assert not any(event.event_type == "phase_transition" for event in store.events)


@pytest.mark.asyncio
async def test_agent_loop_emits_phase2_step_transition_trace_event():
    plan = TravelPlanState(
        session_id="s1",
        phase=2,
        phase2_step="brief",
        destination="Tokyo",
        dates=DateRange(start="2026-07-01", end="2026-07-03"),
    )

    @tool(
        name="set_trip_brief",
        description="write brief",
        phases=[2],
        parameters={
            "type": "object",
            "properties": {"fields": {"type": "object"}},
            "required": ["fields"],
        },
        side_effect="write",
    )
    async def set_trip_brief(fields: dict) -> dict:
        plan.trip_brief.update(fields)
        return {"updated_fields": ["trip_brief"]}

    class _LLM:
        provider_name = "test"
        model = "fake"

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="call-1",
                        name="set_trip_brief",
                        arguments={"fields": {"style": "food"}},
                    ),
                )
            else:
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
            yield LLMChunk(type=ChunkType.DONE)

    engine = ToolEngine()
    engine.register(set_trip_brief)
    store = TraceStoreStub()
    agent = AgentLoop(
        llm=_LLM(),
        tool_engine=engine,
        hooks=HookManager(),
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        memory_mgr=FakeMemoryManager(),
        plan=plan,
        trace_recorder=TraceRecorder(trace_store=store),
        trace_context=TraceContext(
            run_id="run-1",
            session_id="s1",
            phase=2,
            phase2_step="brief",
        ),
    )

    async for _chunk in agent.run([Message(role=Role.USER, content="go")], phase=2):
        pass

    phase_gate = next(event for event in store.events if event.event_type == "phase_gate")
    transition = next(
        event for event in store.events if event.event_type == "phase_transition"
    )
    assert plan.phase == 2
    assert plan.phase2_step == "candidate"
    assert phase_gate.payload["allowed"] is True
    assert phase_gate.payload["from_step"] == "brief"
    assert phase_gate.payload["to_step_candidate"] == "candidate"
    assert transition.parent_event_id == phase_gate.event_id
    assert transition.payload["from_phase"] == 2
    assert transition.payload["to_phase"] == 2
    assert transition.payload["from_step"] == "brief"
    assert transition.payload["to_step"] == "candidate"
    assert transition.payload["reason"] == "phase2_step_change"


@pytest.mark.asyncio
async def test_agent_loop_emits_backtrack_transition_trace_event():
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        phase2_step="lock",
        destination="Tokyo",
        dates=DateRange(start="2026-07-01", end="2026-07-02"),
        trip_brief={"style": "food"},
        daily_plans=[],
    )

    class _LLM:
        provider_name = "test"
        model = "fake"

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="call-1",
                        name="request_backtrack",
                        arguments={"to_phase": 1, "reason": "换目的地"},
                    ),
                )
            else:
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
            yield LLMChunk(type=ChunkType.DONE)

    engine = ToolEngine()
    engine.register(make_request_backtrack_tool(plan))
    store = TraceStoreStub()
    agent = AgentLoop(
        llm=_LLM(),
        tool_engine=engine,
        hooks=HookManager(),
        phase_router=FakePhaseRouter(),
        context_manager=FakeContextManager(),
        memory_mgr=FakeMemoryManager(),
        plan=plan,
        trace_recorder=TraceRecorder(trace_store=store),
        trace_context=TraceContext(
            run_id="run-1",
            session_id="s1",
            phase=3,
            phase2_step="lock",
        ),
    )

    chunks = [chunk async for chunk in agent.run([Message(role=Role.USER, content="go")], phase=3)]

    phase_chunks = [chunk for chunk in chunks if chunk.type == ChunkType.PHASE_TRANSITION]
    phase_gate = next(event for event in store.events if event.event_type == "phase_gate")
    transition = next(
        event for event in store.events if event.event_type == "phase_transition"
    )
    state_diff = next(event for event in store.events if event.event_type == "state_diff")
    assert plan.phase == 1
    assert plan.destination is None
    assert phase_chunks[0].phase_info == {
        "from_phase": 3,
        "to_phase": 1,
        "from_step": "lock",
        "to_step": "brief",
        "reason": "backtrack",
    }
    assert phase_gate.payload["allowed"] is True
    assert phase_gate.payload["needs_rebuild"] is True
    assert transition.parent_event_id == phase_gate.event_id
    assert transition.payload["reason"] == "backtrack"
    assert transition.payload["from_phase"] == 3
    assert transition.payload["to_phase"] == 1
    assert state_diff.payload["field_diffs"]["destination"]["after_hash"].startswith(
        "sha256:"
    )


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
async def test_tool_call_assistant_message_keeps_provider_state(agent, mock_llm):
    call_count = 0

    async def mock_chat(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.PROVIDER_STATE_DELTA,
                provider_state={"reasoning_content": "需要调用工具确认。"},
            )
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
    async for _ in agent.run(messages, phase=1):
        pass

    assistant_with_tool = next(
        m for m in messages if m.role == Role.ASSISTANT and m.tool_calls
    )
    assert assistant_with_tool.provider_state == {
        "reasoning_content": "需要调用工具确认。"
    }


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
async def test_tool_result_emitted_before_slow_after_tool_result_internal_task(
    mock_llm,
    engine,
    hooks,
):
    from agent.internal_tasks import InternalTask

    hook_started = asyncio.Event()
    release_hook = asyncio.Event()
    internal_task_events: list[InternalTask] = []

    async def slow_hook(**kwargs):
        hook_started.set()
        internal_task_events.append(
            InternalTask(
                id="soft_judge:tc_1",
                kind="soft_judge",
                label="行程质量评审",
                status="pending",
                related_tool_call_id="tc_1",
            )
        )
        await release_hook.wait()
        internal_task_events.append(
            InternalTask(
                id="soft_judge:tc_1",
                kind="soft_judge",
                label="行程质量评审",
                status="success",
                related_tool_call_id="tc_1",
            )
        )

    hooks.register("after_tool_result", slow_hook)

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
    agent = AgentLoop(
        llm=mock_llm,
        tool_engine=engine,
        hooks=hooks,
        plan=TravelPlanState(session_id="s1", phase=1),
        internal_task_events=internal_task_events,
    )

    stream = agent.run([Message(role=Role.USER, content="hi")], phase=1)

    try:
        while True:
            chunk = await asyncio.wait_for(stream.__anext__(), timeout=0.5)
            if chunk.type == ChunkType.TOOL_RESULT:
                assert chunk.tool_result is not None
                assert chunk.tool_result.status == "success"
                assert hook_started.is_set() is False
                break

        next_chunk = await asyncio.wait_for(stream.__anext__(), timeout=0.5)
        assert hook_started.is_set() is True
        assert next_chunk.type == ChunkType.INTERNAL_TASK
        assert next_chunk.internal_task is not None
        assert next_chunk.internal_task.status == "pending"

        release_hook.set()
        final_task_chunk = await asyncio.wait_for(stream.__anext__(), timeout=0.5)
        assert final_task_chunk.type == ChunkType.INTERNAL_TASK
        assert final_task_chunk.internal_task is not None
        assert final_task_chunk.internal_task.status == "success"
    finally:
        release_hook.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_tool_choice_decider_result_is_passed_to_llm(engine, hooks):
    plan = TravelPlanState(session_id="s1", phase=2, phase2_step="brief")
    forced_choice = {"type": "function", "function": {"name": "set_trip_brief"}}

    class FakeToolChoiceDecider:
        def decide(self, plan_arg, messages_arg, phase_arg):
            assert plan_arg is plan
            assert phase_arg == 2
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

    async for _ in agent.run([Message(role=Role.USER, content="继续")], phase=2):
        pass

    assert observed["tool_choice"] == forced_choice


@pytest.mark.asyncio
async def test_reflection_message_is_injected_before_llm_call(engine, hooks):
    plan = TravelPlanState(session_id="s1", phase=2, phase2_step="lock")

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

    async for _ in agent.run([Message(role=Role.USER, content="继续")], phase=2):
        pass

    assert any(
        content
        and '<runtime_notice kind="reflection">' in content
        and "[自检] 请先检查方案" in content
        for content in observed_messages
    )


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
        plan.phase = 2
        return {"ok": True}

    @tool(
        name="phase2_only",
        description="phase2",
        phases=[2],
        parameters={"type": "object", "properties": {}},
    )
    async def phase2_only() -> dict:
        executed.append("phase2_only")
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
    engine.register(phase2_only)
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
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="phase 2 ready")
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
    assert context_manager.compress_calls == []
    assert observed_second_call["tool_names"] == ["phase2_only"]
    assert observed_second_call["messages"] == [
        "system phase=2 prompt=phase-2-prompt",
        "帮我继续规划",
        "handoff 1->2 phase=2",
        "<turn_context>user=memory:u1 tools=phase2_only</turn_context>",
    ]
    observed_roles = observed_second_call.get("roles")
    if observed_roles is not None:
        assert observed_roles == [Role.SYSTEM, Role.USER, Role.ASSISTANT, Role.USER]
    assert any(chunk.content == "phase 2 ready" for chunk in chunks)


@pytest.mark.asyncio
async def test_phase_rebuild_skips_memory_when_disabled(mock_llm, engine, hooks):
    plan = TravelPlanState(session_id="s1", phase=2)
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
        to_phase=2,
        original_user_message=Message(role=Role.USER, content="继续"),
        result=ToolResult(tool_call_id="tc1", status="success", data={}),
    )

    assert "memory:u1" not in rebuilt[0].content
    assert "暂无相关用户记忆" in rebuilt[-1].content
    assert rebuilt[-1].transient is True
    assert rebuilt[2].content == "handoff 1->2 phase=2"


@pytest.mark.asyncio
async def test_rebuild_messages_for_forward_phase_change_uses_handoff_note_not_summary(
    mock_llm, engine, hooks
):
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
        user_id="u1",
    )
    original = Message(role=Role.USER, content="航班 ok 的，住宿就朵兰达+维也纳")
    messages = [Message(role=Role.USER, content="旧消息")]

    rebuilt = await agent._rebuild_messages_for_phase_change(
        messages=messages,
        from_phase=2,
        to_phase=3,
        original_user_message=original,
        result=ToolResult(tool_call_id="", status="success"),
    )

    assert [m.role for m in rebuilt] == [
        Role.SYSTEM,
        Role.USER,
        Role.ASSISTANT,
        Role.USER,
    ]
    assert rebuilt[0].transient is True
    assert rebuilt[1].content == "航班 ok 的，住宿就朵兰达+维也纳"
    assert rebuilt[2].content == "handoff 2->3 phase=3"
    assert rebuilt[3].transient is True


@pytest.mark.asyncio
async def test_forward_transition_does_not_call_compress_for_transition(
    mock_llm, engine, hooks
):
    plan = TravelPlanState(session_id="s1", phase=3)
    context_manager = FakeContextManager()
    called = False

    async def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("compress_for_transition should not be used")

    context_manager.compress_for_transition = fail_if_called
    context_manager.build_phase_handoff_note = lambda **kwargs: "handoff"

    agent = AgentLoop(
        llm=mock_llm,
        tool_engine=engine,
        hooks=hooks,
        max_retries=3,
        phase_router=FakePhaseRouter(),
        context_manager=context_manager,
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u1",
    )

    rebuilt = await agent._rebuild_messages_for_phase_change(
        messages=[Message(role=Role.USER, content="x")],
        from_phase=2,
        to_phase=3,
        original_user_message=Message(role=Role.USER, content="x"),
        result=ToolResult(tool_call_id="", status="success"),
    )

    assert not called
    assert rebuilt[2].content == "handoff"


@pytest.mark.asyncio
async def test_backtrack_rebuild_uses_hard_boundary_without_compression():
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
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
                from_phase=3,
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
    async for _ in agent.run(messages, phase=3):
        pass

    assert context_manager.compress_calls == []
    assert observed_second_call["messages"] == [
        "system phase=1 prompt=phase-1-prompt",
        "不想去这里了，换个目的地",
        '<app_event kind="backtrack">[阶段回退]\n用户从 phase 3 回退到 phase 1，原因：用户想换目的地</app_event>',
        "<turn_context>user=memory:u2</turn_context>",
    ]


@pytest.mark.asyncio
async def test_forward_phase_rebuild_uses_handoff_note_when_summary_helper_is_empty():
    plan = TravelPlanState(session_id="s1", phase=1)
    context_manager = EmptySummaryContextManager()

    @tool(
        name="advance_phase",
        description="advance",
        phases=[1],
        parameters={"type": "object", "properties": {}},
    )
    async def advance_phase() -> dict:
        plan.phase = 2
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
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="phase 2 ready")
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

    assert context_manager.compress_calls == []
    assert observed_second_call["messages"] == [
        "system phase=2 prompt=phase-2-prompt",
        "帮我继续规划",
        "handoff 1->2 phase=2",
        "<turn_context>user=memory:u-empty</turn_context>",
    ]


@pytest.mark.asyncio
async def test_phase3_substep_change_refreshes_tools():
    """Test that tool availability changes when phase2_step changes.

    Uses the real plan tools registered through register_all_plan_tools,
    which respects the engine's phase2_step-based tool filtering.
    """
    plan = TravelPlanState(session_id="s1", phase=2, phase2_step="brief")

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
    async for _ in agent.run(messages, phase=2):
        pass

    # On first call (brief step), set_trip_brief is available
    assert "set_trip_brief" in observed_tool_names[0]
    # trip_brief should be set
    assert plan.trip_brief is not None
    assert plan.trip_brief.get("destination") == "东京"


@pytest.mark.asyncio
async def test_phase3_inferred_substep_refreshes_tools_after_dates_written():
    plan = TravelPlanState(session_id="s1", phase=2, destination="东京")
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    @tool(
        name="quick_travel_search",
        description="quick",
        phases=[2],
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
    async for _ in agent.run(messages, phase=2):
        pass

    assert "update_trip_basics" in observed_tool_names[0]
    assert "quick_travel_search" in observed_tool_names[1]
    assert plan.phase2_step == "candidate"
    assert plan.trip_brief["destination"] == "东京"


@pytest.mark.asyncio
async def test_phase3_text_only_skeleton_response_triggers_state_repair():
    plan = TravelPlanState(
        session_id="s1",
        phase=2,
        phase2_step="skeleton",
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
    async for _ in agent.run(messages, phase=2):
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
        phase=2,
        phase2_step="candidate",
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
    async for _ in agent.run(messages, phase=2):
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
        phase=2,
        phase2_step="candidate",
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
    async for _ in agent.run(messages, phase=2):
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
        phase=2,
        phase2_step="lock",
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
    async for _ in agent.run(messages, phase=2):
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
        phase=2,
        phase2_step="skeleton",
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
    async for _ in agent.run(messages, phase=2):
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
        phase=2,
        phase2_step="candidate",
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
    async for chunk in agent.run(messages, phase=2):
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
        phase=3,
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
                from_phase=3,
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
            [Message(role=Role.USER, content="换个目的地")], phase=3
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
async def test_phase3_text_only_daily_plan_triggers_state_repair():
    """When Phase 3 LLM outputs day-by-day text but forgets to call
    plan tools, the repair mechanism should inject a reminder."""
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        destination="大阪",
        dates=DateRange(start="2026-04-15", end="2026-04-17"),
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
                    name="replace_all_day_plans",
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
    async for _ in agent.run(messages, phase=3):
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
    assert any(
        content and "save_day_plan" in content and "replace_all_day_plans" in content
        for call_messages in observed_messages[1:]
        for content in call_messages
        if content
    )


@pytest.mark.asyncio
async def test_phase3_repair_hint_not_repeated():
    """After a repair hint is sent once, it should not be repeated even if LLM
    outputs itinerary text again."""
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
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
    async for _ in agent.run(messages, phase=3):
        pass

    # Repair fires on call 1 (dedup key added), call 2 skips repair → agent ends.
    assert call_count == 2


@pytest.mark.asyncio
async def test_phase3_repair_detects_json_style_output():
    """Repair should also trigger when LLM outputs JSON-style itinerary."""
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
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
    async for _ in agent.run(messages, phase=3):
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


@pytest.mark.asyncio
async def test_progress_tracks_partial_text_when_llm_stream_errors():
    async def fake_chat(messages, **kwargs):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="hello")
        raise LLMError(
            code=LLMErrorCode.TRANSIENT,
            message="stream failed",
            retryable=True,
            provider="test",
            model="fake",
            failure_phase="streaming",
        )

    mock_llm = MagicMock()
    mock_llm.provider_name = "openai"
    mock_llm.model = "gpt-4o"
    mock_llm.chat = fake_chat
    engine = ToolEngine()
    hooks = HookManager()

    loop = AgentLoop(llm=mock_llm, tool_engine=engine, hooks=hooks)
    messages = [Message(role=Role.USER, content="hi")]
    with pytest.raises(LLMError):
        async for _ in loop.run(messages, phase=1):
            pass

    assert loop.progress == IterationProgress.PARTIAL_TEXT


@pytest.mark.asyncio
async def test_phase2_step_change_rebuilds_system_message():
    """子阶段从 brief 推进到 candidate 时，system message 必须被重建。"""
    plan = TravelPlanState(session_id="s1", phase=2, destination="东京")
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    observed_system_contents: list[str] = []
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        for m in messages:
            if m.role == Role.SYSTEM:
                observed_system_contents.append(m.content)
                break
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc1",
                    name="update_trip_basics",
                    arguments={"dates": {"start": "2026-05-01", "end": "2026-05-05"}},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="继续")
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
        user_id="u-step",
    )

    messages = [Message(role=Role.USER, content="五一去东京玩5天")]
    async for _ in agent.run(messages, phase=2):
        pass

    assert plan.phase2_step == "candidate"
    # 修复前：phase2_step 变化不重建 → 观察不到任何 SYSTEM
    # 修复后：phase2_step 变化触发重建 → 第二轮 messages 至少含一条 SYSTEM
    assert len(observed_system_contents) >= 1, "phase2_step 推进后未重建 system message"
    assert "phase=2" in observed_system_contents[-1]
    assert "已完成 Phase" not in observed_system_contents[-1]
    assert "handoff" not in observed_system_contents[-1]


@pytest.mark.asyncio
async def test_phase3_to_phase3_transition_rechecks_parallel_routing():
    plan = TravelPlanState(
        session_id="s1",
        phase=2,
        phase2_step="lock",
        destination="东京",
        dates=DateRange(start="2026-05-01", end="2026-05-03"),
        skeleton_plans=[{"id": "plan_A", "days": [{}, {}, {}]}],
        selected_skeleton_id="plan_A",
    )

    @tool(
        name="promote_to_phase3",
        description="Promote plan to phase 3",
        phases=[3],
        parameters={"type": "object", "properties": {}, "required": []},
        side_effect="write",
    )
    async def promote_to_phase3() -> dict:
        plan.phase = 3
        return {"updated_field": "phase", "phase": 3}

    engine = ToolEngine()
    engine.register(promote_to_phase3)

    llm_call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal llm_call_count
        llm_call_count += 1
        yield LLMChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call=ToolCall(
                id="tc_phase3",
                name="promote_to_phase3",
                arguments={},
            ),
        )
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
        user_id="u-phase3-reentry",
        phase3_parallel_config=Phase3ParallelConfig(enabled=True),
    )

    parallel_calls = 0

    async def fake_parallel_runner(*, messages=None, original_user_message=None):
        nonlocal parallel_calls
        parallel_calls += 1
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="parallel phase3")
        yield LLMChunk(type=ChunkType.DONE)

    agent._run_parallel_phase3_orchestrator = fake_parallel_runner

    chunks = [c async for c in agent.run([Message(role=Role.USER, content="继续")], phase=2)]

    assert plan.phase == 3
    assert llm_call_count == 1
    assert parallel_calls == 1
    assert any(
        c.type == ChunkType.PHASE_TRANSITION and c.phase_info["to_phase"] == 3
        for c in chunks
    )
    assert any(
        c.type == ChunkType.TEXT_DELTA and c.content == "parallel phase3"
        for c in chunks
    )


@pytest.mark.asyncio
async def test_phase2_step_change_no_handoff_note():
    """phase2_step 变化重建时不得注入跨 phase handoff assistant note。"""
    plan = TravelPlanState(session_id="s1", phase=2, destination="东京")
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    observed_messages: list[list[Message]] = []
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        observed_messages.append(list(messages))
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc1",
                    name="update_trip_basics",
                    arguments={"dates": {"start": "2026-05-01", "end": "2026-05-05"}},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="ok")
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
        user_id="u-step2",
    )
    async for _ in agent.run([Message(role=Role.USER, content="定档")], phase=2):
        pass

    second_round = observed_messages[1]
    for m in second_round:
        if m.role == Role.ASSISTANT and m.content:
            assert "handoff" not in m.content
            assert "已完成 Phase" not in m.content


@pytest.mark.asyncio
async def test_phase_transition_flushes_messages_before_rebuild():
    plan = TravelPlanState(session_id="s1", phase=1)
    flushed: list[dict[str, object]] = []

    @tool(
        name="advance_phase",
        description="advance",
        phases=[1],
        parameters={"type": "object", "properties": {}},
    )
    async def advance_phase() -> dict:
        plan.phase = 2
        return {"destination": "东京"}

    engine = ToolEngine()
    engine.register(advance_phase)

    call_index = 0

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
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="phase 2 ready")
        yield LLMChunk(type=ChunkType.DONE)

    async def flush_callback(*, messages, from_phase, from_phase2_step):
        flushed.append(
            {
                "from_phase": from_phase,
                "from_phase2_step": from_phase2_step,
                "roles": [message.role for message in messages],
                "tool_names": [
                    call.name
                    for message in messages
                    for call in (message.tool_calls or [])
                ],
            }
        )

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
        user_id="u1",
        on_before_message_rebuild=flush_callback,
    )

    messages = [Message(role=Role.USER, content="去东京")]
    async for _ in agent.run(messages, phase=1):
        pass

    assert flushed == [
        {
            "from_phase": 1,
            "from_phase2_step": "brief",
            "roles": [Role.USER, Role.ASSISTANT, Role.TOOL],
            "tool_names": ["advance_phase"],
        }
    ]


@pytest.mark.asyncio
async def test_phase2_step_change_flushes_messages_before_rebuild():
    plan = TravelPlanState(session_id="s1", phase=2, destination="东京")
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)
    flushed: list[dict[str, object]] = []
    observed_second_call: dict[str, object] = {}
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc1",
                    name="update_trip_basics",
                    arguments={"dates": {"start": "2026-05-01", "end": "2026-05-05"}},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return
        observed_second_call["messages"] = [message.content for message in messages]
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="继续规划")
        yield LLMChunk(type=ChunkType.DONE)

    async def flush_callback(*, messages, from_phase, from_phase2_step):
        flushed.append(
            {
                "from_phase": from_phase,
                "from_phase2_step": from_phase2_step,
                "contents": [message.content for message in messages],
            }
        )

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
        user_id="u1",
        on_before_message_rebuild=flush_callback,
    )

    messages = [Message(role=Role.USER, content="五一去东京玩5天")]
    async for _ in agent.run(messages, phase=2):
        pass

    assert plan.phase2_step == "candidate"
    assert flushed[0]["from_phase"] == 2
    assert flushed[0]["from_phase2_step"] == "brief"
    assert "五一去东京玩5天" in flushed[0]["contents"]
    assert observed_second_call["messages"][0].startswith("system phase=2")


@pytest.mark.asyncio
async def test_pre_rebuild_flush_failure_logs_warning_and_rebuilds(caplog):
    plan = TravelPlanState(session_id="s1", phase=1)

    @tool(
        name="advance_phase",
        description="advance",
        phases=[1],
        parameters={"type": "object", "properties": {}},
    )
    async def advance_phase() -> dict:
        plan.phase = 2
        return {"destination": "东京"}

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
        observed_second_call["messages"] = [message.content for message in messages]
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="phase 2 ready")
        yield LLMChunk(type=ChunkType.DONE)

    async def failing_flush(*, messages, from_phase, from_phase2_step):
        raise RuntimeError("disk unavailable")

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
        user_id="u1",
        on_before_message_rebuild=failing_flush,
    )

    with caplog.at_level("WARNING"):
        async for _ in agent.run([Message(role=Role.USER, content="去东京")], phase=1):
            pass

    assert observed_second_call["messages"] == [
        "system phase=2 prompt=phase-2-prompt",
        "去东京",
        "handoff 1->2 phase=2",
        "<turn_context>user=memory:u1</turn_context>",
    ]
    assert "pre-rebuild message history flush failed" in caplog.text
