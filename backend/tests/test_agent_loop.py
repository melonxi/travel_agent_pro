# backend/tests/test_agent_loop.py
import pytest
from unittest.mock import AsyncMock, MagicMock

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from tools.engine import ToolEngine
from tools.base import tool


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
    return AgentLoop(llm=mock_llm, tool_engine=engine, hooks=hooks, max_retries=3)


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
