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


@pytest.mark.asyncio
async def test_run_gate_returns_allow_by_default(hooks):
    """No gate handler registered → allow by default."""
    result = await hooks.run_gate("before_phase_transition")
    assert result.allowed is True
    assert result.feedback is None


@pytest.mark.asyncio
async def test_run_gate_returns_reject_when_handler_rejects(hooks):
    from agent.hooks import GateResult

    async def reject_gate(**kwargs):
        return GateResult(allowed=False, feedback="quality too low")

    hooks.register_gate("before_phase_transition", reject_gate)
    result = await hooks.run_gate("before_phase_transition", score=2.0)
    assert result.allowed is False
    assert result.feedback == "quality too low"


@pytest.mark.asyncio
async def test_run_gate_allows_when_handler_allows(hooks):
    from agent.hooks import GateResult

    async def allow_gate(**kwargs):
        return GateResult(allowed=True, feedback=None)

    hooks.register_gate("before_phase_transition", allow_gate)
    result = await hooks.run_gate("before_phase_transition")
    assert result.allowed is True


@pytest.mark.asyncio
async def test_run_gate_first_reject_wins(hooks):
    from agent.hooks import GateResult

    async def allow_gate(**kwargs):
        return GateResult(allowed=True)

    async def reject_gate(**kwargs):
        return GateResult(allowed=False, feedback="blocked")

    hooks.register_gate("event", allow_gate)
    hooks.register_gate("event", reject_gate)
    result = await hooks.run_gate("event")
    assert result.allowed is False
    assert result.feedback == "blocked"
