import pytest

from agent.hooks import GateResult, HookManager
from phase.router import PhaseRouter
from state.models import Budget, DateRange, TravelPlanState


def _make_plan(**overrides) -> TravelPlanState:
    defaults = {"session_id": "s1"}
    defaults.update(overrides)
    return TravelPlanState(**defaults)


@pytest.mark.asyncio
async def test_transition_allowed_when_no_gate():
    """No gate registered -> transition should proceed."""
    router = PhaseRouter()
    hooks = HookManager()
    plan = _make_plan(
        phase=1,
        destination="京都",
        dates=DateRange(start="2026-04-10", end="2026-04-12"),
        budget=Budget(total=30000),
    )

    changed = await router.check_and_apply_transition(plan, hooks=hooks)

    assert changed is True
    assert plan.phase == 3


@pytest.mark.asyncio
async def test_transition_blocked_when_gate_rejects():
    router = PhaseRouter()
    hooks = HookManager()

    async def reject_gate(**kwargs):
        return GateResult(allowed=False, feedback="质量不达标")

    hooks.register_gate("before_phase_transition", reject_gate)
    plan = _make_plan(
        phase=1,
        destination="京都",
        dates=DateRange(start="2026-04-10", end="2026-04-12"),
        budget=Budget(total=30000),
    )

    changed = await router.check_and_apply_transition(plan, hooks=hooks)

    assert changed is False
    assert plan.phase == 1


@pytest.mark.asyncio
async def test_transition_allowed_when_gate_passes():
    router = PhaseRouter()
    hooks = HookManager()

    async def allow_gate(**kwargs):
        return GateResult(allowed=True)

    hooks.register_gate("before_phase_transition", allow_gate)
    plan = _make_plan(
        phase=1,
        destination="京都",
        dates=DateRange(start="2026-04-10", end="2026-04-12"),
        budget=Budget(total=30000),
    )

    changed = await router.check_and_apply_transition(plan, hooks=hooks)

    assert changed is True
    assert plan.phase == 3


@pytest.mark.asyncio
async def test_no_transition_when_phase_unchanged():
    router = PhaseRouter()
    hooks = HookManager()
    plan = _make_plan(phase=1)

    changed = await router.check_and_apply_transition(plan, hooks=hooks)

    assert changed is False
