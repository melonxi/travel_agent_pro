import pytest

from agent.types import ToolResult
from agent.hooks import GateResult, HookManager
from phase.router import PhaseRouter
from state.models import Budget, DateRange, TravelPlanState
from telemetry.stats import SessionStats


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


def test_phase1_to_phase3_feasibility_gate_handles_daterange():
    """The gate's date helper must accept the real DateRange model fields."""
    from main import _days_count_from_dates

    dates = DateRange(start="2026-06-01", end="2026-06-06")

    assert _days_count_from_dates(dates) == 5


def test_record_tool_result_stats_records_duration():
    """ToolResult metadata must be reflected in per-session stats."""
    from main import _record_tool_result_stats

    stats = SessionStats()
    tool_call_names = {"tc_1": "web_search"}
    result = ToolResult(
        tool_call_id="tc_1",
        status="success",
        data={"results": []},
        metadata={"duration_ms": 123.4},
    )

    _record_tool_result_stats(
        stats=stats,
        tool_call_names=tool_call_names,
        result=result,
        phase=3,
    )

    assert len(stats.tool_calls) == 1
    assert stats.total_tool_duration_ms == 123.4
    assert stats.to_dict()["by_tool"]["web_search"]["duration_ms"] == 123.4
