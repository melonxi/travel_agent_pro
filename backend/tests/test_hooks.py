# backend/tests/test_hooks.py
import pytest

from agent.hooks import HookManager


@pytest.fixture
def hooks():
    return HookManager()


@pytest.mark.asyncio
async def test_register_and_run_hook(hooks):
    results = []

    async def my_hook(data):
        results.append(data)

    hooks.register("after_tool_call", my_hook)
    await hooks.run("after_tool_call", "test_data")
    assert results == ["test_data"]


@pytest.mark.asyncio
async def test_multiple_hooks_run_in_order(hooks):
    order = []

    async def hook_a(data):
        order.append("a")

    async def hook_b(data):
        order.append("b")

    hooks.register("event", hook_a)
    hooks.register("event", hook_b)
    await hooks.run("event", None)
    assert order == ["a", "b"]


@pytest.mark.asyncio
async def test_run_nonexistent_event(hooks):
    # Should not raise
    await hooks.run("nonexistent", None)


@pytest.mark.asyncio
async def test_hook_receives_kwargs(hooks):
    captured = {}

    async def my_hook(**kwargs):
        captured.update(kwargs)

    hooks.register("event", my_hook)
    await hooks.run("event", tool_name="search", result={"ok": True})
    assert captured["tool_name"] == "search"
