# backend/tests/test_tool_base.py
import pytest

from tools.base import ToolDef, ToolError, tool


def test_tool_decorator_registers():
    @tool(
        name="my_tool",
        description="A test tool",
        phases=[1, 2],
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    async def my_tool(query: str) -> dict:
        return {"result": query}

    assert isinstance(my_tool, ToolDef)
    assert my_tool.name == "my_tool"
    assert my_tool.phases == [1, 2]


@pytest.mark.asyncio
async def test_tool_def_call():
    @tool(
        name="echo",
        description="Echo input",
        phases=[1],
        parameters={
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        },
    )
    async def echo(msg: str) -> dict:
        return {"echo": msg}

    result = await echo(msg="hello")
    assert result == {"echo": "hello"}


def test_tool_error():
    err = ToolError("Bad input", error_code="INVALID", suggestion="Fix the input")
    assert err.error_code == "INVALID"
    assert err.suggestion == "Fix the input"
    assert str(err) == "Bad input"


def test_tool_to_schema():
    @tool(
        name="search",
        description="Search things",
        phases=[2],
        parameters={
            "type": "object",
            "properties": {"q": {"type": "string", "description": "query"}},
            "required": ["q"],
        },
    )
    async def search(q: str) -> dict:
        return {}

    schema = search.to_schema()
    assert schema["name"] == "search"
    assert schema["description"] == "Search things"
    assert schema["parameters"]["properties"]["q"]["type"] == "string"


def test_tool_def_default_side_effect():
    @tool(name="read_tool", description="test", phases=[1], parameters={})
    async def my_tool():
        return {}

    assert my_tool.side_effect == "read"


def test_tool_def_custom_side_effect():
    @tool(name="write_tool", description="test", phases=[1], parameters={}, side_effect="write")
    async def my_tool():
        return {}

    assert my_tool.side_effect == "write"
