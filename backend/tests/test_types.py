# backend/tests/test_types.py
from agent.types import Message, Role, ToolCall, ToolResult


def test_message_user():
    msg = Message(role=Role.USER, content="hello")
    assert msg.role == Role.USER
    assert msg.content == "hello"
    assert msg.tool_calls is None


def test_message_assistant_with_tool_calls():
    tc = ToolCall(id="tc_1", name="search_flights", arguments={"origin": "PVG"})
    msg = Message(role=Role.ASSISTANT, content=None, tool_calls=[tc])
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].name == "search_flights"


def test_tool_result():
    tr = ToolResult(
        tool_call_id="tc_1",
        status="success",
        data={"flights": []},
        metadata={"source": "amadeus", "latency_ms": 123},
    )
    assert tr.status == "success"
    assert tr.metadata["source"] == "amadeus"


def test_tool_result_error():
    tr = ToolResult(
        tool_call_id="tc_1",
        status="error",
        error="API 超时",
        error_code="TIMEOUT",
        suggestion="请稍后重试",
    )
    assert tr.status == "error"
    assert tr.error_code == "TIMEOUT"


def test_message_to_dict():
    msg = Message(role=Role.USER, content="hello")
    d = msg.to_dict()
    assert d["role"] == "user"
    assert d["content"] == "hello"


def test_message_to_dict_with_tool_calls():
    tc = ToolCall(id="tc_1", name="search_flights", arguments={"origin": "PVG"})
    msg = Message(role=Role.ASSISTANT, content=None, tool_calls=[tc])
    d = msg.to_dict()
    assert d["role"] == "assistant"
    assert "content" not in d
    assert len(d["tool_calls"]) == 1
    assert d["tool_calls"][0]["name"] == "search_flights"


def test_message_to_dict_with_tool_result_error():
    tr = ToolResult(
        tool_call_id="tc_1",
        status="error",
        error="API timeout",
        error_code="TIMEOUT",
        suggestion="Retry later",
    )
    msg = Message(role=Role.TOOL, content=None, tool_result=tr, name="search_flights")
    d = msg.to_dict()
    assert d["role"] == "tool"
    assert d["name"] == "search_flights"
    assert d["tool_result"]["error"] == "API timeout"
    assert d["tool_result"]["error_code"] == "TIMEOUT"
    assert d["tool_result"]["suggestion"] == "Retry later"


def test_internal_task_to_dict_omits_none_fields():
    from agent.internal_tasks import InternalTask

    task = InternalTask(
        id="soft_judge:tc_1",
        kind="soft_judge",
        label="行程质量评审",
        status="pending",
        message="正在检查行程质量…",
        blocking=True,
        scope="turn",
        related_tool_call_id="tc_1",
        started_at=100.0,
    )

    assert task.to_dict() == {
        "id": "soft_judge:tc_1",
        "kind": "soft_judge",
        "label": "行程质量评审",
        "status": "pending",
        "message": "正在检查行程质量…",
        "blocking": True,
        "scope": "turn",
        "related_tool_call_id": "tc_1",
        "started_at": 100.0,
    }


def test_internal_task_requires_known_status_and_scope():
    import pytest
    from agent.internal_tasks import InternalTask

    with pytest.raises(ValueError, match="status"):
        InternalTask(
            id="bad",
            kind="soft_judge",
            label="行程质量评审",
            status="running",
        )

    with pytest.raises(ValueError, match="scope"):
        InternalTask(
            id="bad",
            kind="soft_judge",
            label="行程质量评审",
            status="pending",
            scope="global",
        )


def test_llm_chunk_accepts_internal_task():
    from agent.internal_tasks import InternalTask
    from llm.types import ChunkType, LLMChunk

    task = InternalTask(
        id="quality_gate:5:7",
        kind="quality_gate",
        label="阶段推进检查",
        status="success",
        message="可以进入下一阶段",
    )
    chunk = LLMChunk(type=ChunkType.INTERNAL_TASK, internal_task=task)

    assert ChunkType.INTERNAL_TASK.value == "internal_task"
    assert chunk.internal_task is task
