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
