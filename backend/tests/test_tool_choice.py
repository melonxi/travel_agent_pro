# backend/tests/test_tool_choice.py
import pytest

from agent.tool_choice import ToolChoiceDecider
from agent.types import Message, Role
from state.models import TravelPlanState


@pytest.fixture
def decider():
    return ToolChoiceDecider()


def _make_plan(**overrides) -> TravelPlanState:
    defaults = {"session_id": "s1", "destination": "京都"}
    defaults.update(overrides)
    return TravelPlanState(**defaults)


def _msg(role: Role, content: str) -> Message:
    return Message(role=role, content=content)


def test_auto_by_default(decider):
    plan = _make_plan(phase=1)
    messages = [_msg(Role.USER, "我想去旅游")]
    result = decider.decide(plan, messages, phase=1)
    assert result == "auto"


def test_force_when_phase3_brief_empty_after_conversation(decider):
    plan = _make_plan(phase=3, phase3_step="brief")
    messages = [
        _msg(Role.USER, "我想去京都5天"),
        _msg(Role.ASSISTANT, "好的，我来帮你规划京都5天的旅行"),
        _msg(Role.USER, "预算3万，2个人"),
        _msg(Role.ASSISTANT, "了解，3万预算2个人"),
    ]
    result = decider.decide(plan, messages, phase=3)
    assert result != "auto"
    assert result["type"] == "function"
    assert result["function"]["name"] == "update_plan_state"


def test_no_force_when_brief_already_filled(decider):
    plan = _make_plan(phase=3, phase3_step="brief", trip_brief={"destination": "京都"})
    messages = [
        _msg(Role.USER, "确认"),
        _msg(Role.ASSISTANT, "好的"),
        _msg(Role.USER, "继续"),
        _msg(Role.ASSISTANT, "继续推进"),
    ]
    result = decider.decide(plan, messages, phase=3)
    assert result == "auto"


def test_force_when_phase5_has_itinerary_text(decider):
    from state.models import DateRange
    plan = _make_plan(
        phase=5,
        dates=DateRange(start="2026-04-10", end="2026-04-12"),
        daily_plans=[],
    )
    messages = [
        _msg(Role.USER, "开始排行程"),
        _msg(Role.ASSISTANT, "第1天 09:00 金阁寺 第2天 10:00 伏见稻荷"),
    ]
    result = decider.decide(plan, messages, phase=5)
    assert result != "auto"


def test_no_force_when_phase5_daily_plans_filled(decider):
    from state.models import DateRange, DayPlan
    plan = _make_plan(
        phase=5,
        dates=DateRange(start="2026-04-10", end="2026-04-12"),
        daily_plans=[DayPlan(day=1, date="2026-04-10"), DayPlan(day=2, date="2026-04-11")],
    )
    messages = [_msg(Role.USER, "看看行程")]
    result = decider.decide(plan, messages, phase=5)
    assert result == "auto"


def test_force_when_phase3_skeleton_text_present(decider):
    plan = _make_plan(phase=3, phase3_step="skeleton")
    messages = [
        _msg(Role.USER, "给我几个方案"),
        _msg(Role.ASSISTANT, "方案A 轻松版：第一天金阁寺，方案B 平衡版：第一天伏见稻荷"),
    ]
    result = decider.decide(plan, messages, phase=3)
    assert result != "auto"
