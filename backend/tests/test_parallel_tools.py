# backend/tests/test_parallel_tools.py
import asyncio
import pytest

from agent.types import ToolCall, ToolResult
from tools.base import ToolDef
from tools.engine import ToolEngine


async def _slow_read(**kwargs):
    await asyncio.sleep(0.05)
    return {"query": kwargs.get("q", "")}


async def _write(**kwargs):
    return {"written": kwargs.get("field", "")}


def _make_engine() -> ToolEngine:
    engine = ToolEngine()
    engine.register(ToolDef(
        name="search_a", description="", phases=[1], parameters={},
        _fn=_slow_read, side_effect="read",
    ))
    engine.register(ToolDef(
        name="search_b", description="", phases=[1], parameters={},
        _fn=_slow_read, side_effect="read",
    ))
    engine.register(ToolDef(
        name="update_state", description="", phases=[1], parameters={},
        _fn=_write, side_effect="write",
    ))
    return engine


@pytest.mark.asyncio
async def test_execute_batch_returns_results_in_original_order():
    engine = _make_engine()
    calls = [
        ToolCall(id="1", name="search_a", arguments={"q": "a"}),
        ToolCall(id="2", name="update_state", arguments={"field": "x"}),
        ToolCall(id="3", name="search_b", arguments={"q": "b"}),
    ]
    results = await engine.execute_batch(calls)
    assert len(results) == 3
    assert results[0].tool_call_id == "1"
    assert results[1].tool_call_id == "2"
    assert results[2].tool_call_id == "3"


@pytest.mark.asyncio
async def test_execute_batch_reads_run_in_parallel():
    """Two 50ms reads should complete in ~50ms total, not ~100ms."""
    engine = _make_engine()
    calls = [
        ToolCall(id="1", name="search_a", arguments={"q": "a"}),
        ToolCall(id="2", name="search_b", arguments={"q": "b"}),
    ]
    start = asyncio.get_event_loop().time()
    results = await engine.execute_batch(calls)
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.09  # should be ~50ms, not ~100ms
    assert all(r.status == "success" for r in results)


@pytest.mark.asyncio
async def test_execute_batch_writes_after_reads():
    execution_order = []

    async def tracked_read(**kwargs):
        execution_order.append(f"read_{kwargs.get('q', '')}")
        return {}

    async def tracked_write(**kwargs):
        execution_order.append(f"write_{kwargs.get('field', '')}")
        return {}

    engine = ToolEngine()
    engine.register(ToolDef(
        name="search_a", description="", phases=[1], parameters={},
        _fn=tracked_read, side_effect="read",
    ))
    engine.register(ToolDef(
        name="update_state", description="", phases=[1], parameters={},
        _fn=tracked_write, side_effect="write",
    ))
    calls = [
        ToolCall(id="1", name="search_a", arguments={"q": "a"}),
        ToolCall(id="2", name="update_state", arguments={"field": "x"}),
    ]
    await engine.execute_batch(calls)
    # Write must come after read
    read_idx = execution_order.index("read_a")
    write_idx = execution_order.index("write_x")
    assert read_idx < write_idx


@pytest.mark.asyncio
async def test_execute_batch_single_tool_works():
    engine = _make_engine()
    calls = [ToolCall(id="1", name="search_a", arguments={"q": "a"})]
    results = await engine.execute_batch(calls)
    assert len(results) == 1
    assert results[0].status == "success"


@pytest.mark.asyncio
async def test_execute_batch_read_failure_does_not_block_others():
    async def failing_read(**kwargs):
        raise Exception("network error")

    engine = ToolEngine()
    engine.register(ToolDef(
        name="bad_search", description="", phases=[1], parameters={},
        _fn=failing_read, side_effect="read",
    ))
    engine.register(ToolDef(
        name="search_a", description="", phases=[1], parameters={},
        _fn=_slow_read, side_effect="read",
    ))
    calls = [
        ToolCall(id="1", name="bad_search", arguments={}),
        ToolCall(id="2", name="search_a", arguments={"q": "ok"}),
    ]
    results = await engine.execute_batch(calls)
    assert results[0].status == "error"
    assert results[1].status == "success"
