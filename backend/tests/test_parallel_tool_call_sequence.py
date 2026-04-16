"""Integration tests: parallel tool_calls must not be split by SYSTEM injects.

Regression for the 2026-04-15 Hong Kong session bug where on_validate
appended [实时约束检查] between the 1st and 2nd tool responses of a
parallel plan-writing tool batch, causing Xunfei gateway to return 400.
"""

import pytest
import httpx

from agent.types import Message, Role, ToolCall
from llm.types import ChunkType, LLMChunk
from state.models import TravelPlanState


def _assert_toolcalls_block_is_contiguous(msgs: list[Message]) -> None:
    """Protocol check: every assistant.tool_calls must be followed by
    exactly len(tool_calls) consecutive role=tool messages, no other
    role in between."""
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m.role == Role.ASSISTANT and m.tool_calls:
            expected = len(m.tool_calls)
            for k in range(1, expected + 1):
                assert i + k < len(msgs), (
                    f"tool_calls group at msg {i} truncated: expected "
                    f"{expected} tool responses, got {len(msgs) - i - 1}"
                )
                follow = msgs[i + k]
                assert follow.role == Role.TOOL, (
                    f"tool_calls group at msg {i}: position {k} must be "
                    f"role=TOOL, got role={follow.role.value}; this is the "
                    f"bug we're preventing."
                )
            i += expected + 1
        else:
            i += 1


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")


@pytest.fixture
def app():
    from main import create_app

    return create_app(config_path="__nonexistent__.yaml")


@pytest.fixture
def sessions(app):
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        closure = getattr(endpoint, "__closure__", None)
        if endpoint is None or closure is None:
            continue
        for cell in closure:
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    pytest.fail("Could not locate sessions dict from app closure")


@pytest.mark.asyncio
async def test_parallel_plan_tools_with_constraints_flushes_after_group(
    app, sessions
):
    """When LLM issues 3 parallel plan-writing tools that trigger constraint
    errors, the [实时约束检查] SYSTEM message must appear AFTER the full
    tool group, never between tool responses."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]
        plan.phase = 3
        plan.destination = "香港"

        agent = session["agent"]

        captured_messages: list[list[Message]] = []
        call_count = 0

        async def fake_chat(messages, tools=None, stream=True, **kw):
            nonlocal call_count
            captured_messages.append([m for m in messages])
            call_count += 1
            if call_count == 1:
                # Return 3 parallel plan-writing tool_calls
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="call_0",
                        name="update_trip_basics",
                        arguments={"dates": {"start": "2026-05-06", "end": "2026-05-07"}},
                    ),
                )
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="call_1",
                        name="update_trip_basics",
                        arguments={"travelers": 1},
                    ),
                )
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="call_2",
                        name="add_constraints",
                        arguments={"constraints": ["住深圳"]},
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
            else:
                # Second call: finish with plain text so we stop looping
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
                yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat

        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "去香港玩"},
        )

    assert resp.status_code == 200
    assert call_count >= 2, (
        f"expected at least a second LLM call to trigger flush; got {call_count}"
    )

    # The second call is what flush affects; inspect its messages.
    second_call_msgs = captured_messages[1]
    _assert_toolcalls_block_is_contiguous(second_call_msgs)

    # The constraint note should be somewhere in the second-call messages,
    # strictly AFTER the tool group it was triggered by.
    system_contents = [
        (i, m.content)
        for i, m in enumerate(second_call_msgs)
        if m.role == Role.SYSTEM and m.content and "[实时约束检查]" in m.content
    ]
    assert system_contents, (
        "expected [实时约束检查] to be flushed into second-call messages"
    )


@pytest.mark.asyncio
async def test_parallel_tool_calls_without_constraints_have_no_inject(app, sessions):
    """If no constraint errors, messages should contain the tool group
    contiguously and no [实时约束检查] SYSTEM."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]
        plan.phase = 1  # phase 1 won't trigger most constraint checks

        agent = session["agent"]
        captured_messages: list[list[Message]] = []
        call_count = 0

        async def fake_chat(messages, tools=None, stream=True, **kw):
            nonlocal call_count
            captured_messages.append([m for m in messages])
            call_count += 1
            if call_count == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="call_only",
                        name="update_trip_basics",
                        arguments={"destination": "东京"},
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
            else:
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
                yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat

        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "去东京"},
        )

    assert resp.status_code == 200
    assert call_count >= 2

    second_call_msgs = captured_messages[1]
    _assert_toolcalls_block_is_contiguous(second_call_msgs)

    system_has_realtime = any(
        m.role == Role.SYSTEM and m.content and "[实时约束检查]" in m.content
        for m in second_call_msgs
    )
    assert not system_has_realtime, (
        "unexpected [实时约束检查] SYSTEM when no constraint violated"
    )


@pytest.mark.asyncio
async def test_pending_buffer_cleared_between_rounds(app, sessions):
    """After flush, the buffer is empty and the next round accumulates afresh."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]
        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]
        plan.phase = 3
        plan.destination = "香港"

        agent = session["agent"]
        call_count = 0

        async def fake_chat(messages, tools=None, stream=True, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="c1",
                        name="update_trip_basics",
                        arguments={
                            "dates": {"start": "2026-05-06", "end": "2026-05-07"},
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
            else:
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
                yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat

        await client.post(
            f"/api/chat/{session_id}",
            json={"message": "去香港"},
        )

    # After the chat turn, buffer must be empty (flushed on second LLM call).
    assert session.get("_pending_system_notes") == []
