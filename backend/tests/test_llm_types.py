# backend/tests/test_llm_types.py
from llm.types import LLMChunk, ChunkType


def test_text_delta_chunk():
    chunk = LLMChunk(type=ChunkType.TEXT_DELTA, content="Hello")
    assert chunk.type == ChunkType.TEXT_DELTA
    assert chunk.content == "Hello"
    assert chunk.tool_call is None


def test_tool_call_start_chunk():
    from agent.types import ToolCall

    tc = ToolCall(id="tc_1", name="search_flights", arguments={})
    chunk = LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=tc)
    assert chunk.tool_call.name == "search_flights"


def test_done_chunk():
    chunk = LLMChunk(type=ChunkType.DONE)
    assert chunk.content is None
    assert chunk.tool_call is None
