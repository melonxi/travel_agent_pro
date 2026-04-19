from unittest.mock import MagicMock

import pytest

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role, ToolCall
from llm.types import ChunkType, LLMChunk
from state.models import TravelPlanState
from tools.base import tool
from tools.engine import ToolEngine


@pytest.fixture
def engine():
    @tool(
        name="greet",
        description="Greet",
        phases=[1],
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
def agent(engine, hooks):
    llm = MagicMock()
    return AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=hooks,
        max_retries=3,
        plan=TravelPlanState(session_id="s1", phase=1),
    )


@pytest.mark.asyncio
async def test_agent_loop_yields_thinking_status_before_each_llm_iteration(agent):
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_1",
                    name="greet",
                    arguments={"name": "World"},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="已打招呼")
        yield LLMChunk(type=ChunkType.DONE)

    agent.llm.chat = fake_chat

    chunks = [
        c async for c in agent.run([Message(role=Role.USER, content="hi")], phase=1)
    ]
    status_chunks = [
        c
        for c in chunks
        if c.type == ChunkType.AGENT_STATUS and c.agent_status is not None
    ]

    assert [chunk.agent_status["stage"] for chunk in status_chunks] == [
        "thinking",
        "summarizing",
    ]
    assert [chunk.agent_status["iteration"] for chunk in status_chunks] == [0, 1]


@pytest.mark.asyncio
async def test_agent_loop_yields_summarizing_after_tool_iteration_without_phase_change(
    agent,
):
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_1",
                    name="greet",
                    arguments={"name": "World"},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="已总结工具结果")
        yield LLMChunk(type=ChunkType.DONE)

    agent.llm.chat = fake_chat

    chunks = [
        c async for c in agent.run([Message(role=Role.USER, content="hi")], phase=1)
    ]
    status_chunks = [
        c.agent_status
        for c in chunks
        if c.type == ChunkType.AGENT_STATUS and c.agent_status is not None
    ]

    assert [status["stage"] for status in status_chunks] == [
        "thinking",
        "summarizing",
    ]


@pytest.mark.asyncio
async def test_agent_status_compacting_emitted_when_compression_events_present(
    engine, hooks
):
    """When the on_before_llm hook populates compression_events (meaning context
    compression is happening), the loop should yield agent_status(compacting)
    before the compression events and before thinking/summarizing."""

    compression_events: list[dict] = []
    llm = MagicMock()
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=hooks,
        max_retries=3,
        plan=TravelPlanState(session_id="s1", phase=1),
        compression_events=compression_events,
    )

    async def on_before_llm(**kwargs):
        # Simulate the hook detecting compaction is needed and populating events
        compression_events.append(
            {
                "timestamp": 1234567890,
                "message_count_before": 20,
                "message_count_after": 8,
                "must_keep_count": 3,
                "compressed_count": 12,
                "estimated_tokens_before": 5000,
                "estimated_tokens_after": 2000,
                "mode": "context_compression",
                "reason": "test",
            }
        )

    agent.hooks.register("before_llm_call", on_before_llm)

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
        yield LLMChunk(type=ChunkType.DONE)

    agent.llm.chat = fake_chat

    chunks = [
        c async for c in agent.run([Message(role=Role.USER, content="hi")], phase=1)
    ]

    status_chunks = [
        c for c in chunks if c.type == ChunkType.AGENT_STATUS and c.agent_status
    ]
    compression_chunks = [c for c in chunks if c.type == ChunkType.CONTEXT_COMPRESSION]

    # compacting status should be emitted
    stages = [c.agent_status["stage"] for c in status_chunks]
    assert "compacting" in stages, f"Expected 'compacting' in stages, got {stages}"

    # compacting should come before thinking
    compacting_idx = next(
        i
        for i, c in enumerate(chunks)
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status
        and c.agent_status["stage"] == "compacting"
    )
    thinking_idx = next(
        i
        for i, c in enumerate(chunks)
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status
        and c.agent_status["stage"] == "thinking"
    )
    assert compacting_idx < thinking_idx

    # compression event should also be emitted
    assert len(compression_chunks) >= 1


@pytest.mark.asyncio
async def test_context_compaction_emits_internal_task(engine, hooks):
    compression_events = [
        {
            "message_count_before": 10,
            "message_count_after": 6,
            "must_keep_count": 2,
            "compressed_count": 4,
            "estimated_tokens_before": 12000,
            "reason": "test compaction",
        }
    ]

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="ok")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=hooks,
        compression_events=compression_events,
    )

    chunks = [
        chunk
        async for chunk in agent.run([Message(role=Role.USER, content="继续")], phase=1)
    ]
    tasks = [chunk.internal_task for chunk in chunks if chunk.type == ChunkType.INTERNAL_TASK]

    assert any(
        task and task.kind == "context_compaction" and task.status == "pending"
        for task in tasks
    )
    assert any(
        task and task.kind == "context_compaction" and task.status == "success"
        for task in tasks
    )


@pytest.mark.asyncio
async def test_reflection_emits_internal_task_when_message_injected(engine, hooks):
    plan = TravelPlanState(session_id="s1", phase=3, phase3_step="lock")

    class FakeReflection:
        def check_and_inject(self, messages, plan_arg, prev_step):
            return "[自检] 请先检查方案"

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
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

    chunks = [
        chunk
        async for chunk in agent.run([Message(role=Role.USER, content="继续")], phase=3)
    ]
    tasks = [chunk.internal_task for chunk in chunks if chunk.type == ChunkType.INTERNAL_TASK]

    assert any(
        task and task.kind == "reflection" and task.status == "success"
        for task in tasks
    )


@pytest.mark.asyncio
async def test_agent_status_thinking_includes_narration_hint(engine, hooks):
    """agent_status(thinking) should include a narration hint based on the plan."""
    llm = MagicMock()
    plan = TravelPlanState(session_id="s1", phase=1, destination=None)
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=hooks,
        max_retries=3,
        plan=plan,
    )

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="ok")
        yield LLMChunk(type=ChunkType.DONE)

    agent.llm.chat = fake_chat

    chunks = [
        c async for c in agent.run([Message(role=Role.USER, content="hi")], phase=1)
    ]
    thinking = next(
        c
        for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status
        and c.agent_status["stage"] == "thinking"
    )
    assert thinking.agent_status["hint"] == "先搞清楚你想去哪，然后翻点真实游记"


@pytest.mark.asyncio
async def test_agent_status_hint_is_none_for_unknown_phase(engine, hooks):
    """agent_status hint should be None for an unrecognized phase."""
    llm = MagicMock()
    plan = TravelPlanState(session_id="s1", phase=99)
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=hooks,
        max_retries=3,
        plan=plan,
    )

    async def fake_chat(messages, tools=None, stream=True, tool_choice=None):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="ok")
        yield LLMChunk(type=ChunkType.DONE)

    agent.llm.chat = fake_chat

    chunks = [
        c async for c in agent.run([Message(role=Role.USER, content="hi")], phase=99)
    ]
    thinking = next(
        c
        for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status
        and c.agent_status["stage"] == "thinking"
    )
    assert thinking.agent_status["hint"] is None
