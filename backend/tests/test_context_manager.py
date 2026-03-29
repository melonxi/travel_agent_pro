# backend/tests/test_context_manager.py
import pytest

from agent.types import Message, Role
from context.manager import ContextManager
from state.models import TravelPlanState, DateRange, Budget


@pytest.fixture
def ctx_manager():
    return ContextManager(soul_path="backend/context/soul.md")


def test_load_soul(ctx_manager):
    soul = ctx_manager._load_soul()
    assert "旅行规划 Agent" in soul


def test_build_system_message(ctx_manager):
    plan = TravelPlanState(session_id="s1", phase=1)
    msg = ctx_manager.build_system_message(
        plan, phase_prompt="你是灵感顾问", user_summary=""
    )
    assert msg.role == Role.SYSTEM
    assert "旅行规划 Agent" in msg.content  # from SOUL
    assert "灵感顾问" in msg.content  # from phase prompt


def test_build_runtime_context(ctx_manager):
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        budget=Budget(total=15000),
    )
    ctx = ctx_manager.build_runtime_context(plan)
    assert "Kyoto" in ctx
    assert "15000" in ctx
    assert "阶段：3" in ctx or "阶段: 3" in ctx


def test_should_compress_false(ctx_manager):
    messages = [Message(role=Role.USER, content="hello")]
    assert not ctx_manager.should_compress(messages, max_tokens=100000)


def test_classify_messages(ctx_manager):
    messages = [
        Message(role=Role.USER, content="我不坐红眼航班"),
        Message(role=Role.ASSISTANT, content="好的，已记录"),
        Message(role=Role.USER, content="今天天气怎么样"),
    ]
    must_keep, compressible = ctx_manager.classify_messages(messages)
    # "不坐红眼航班" contains preference signal → must_keep
    assert any("红眼" in m.content for m in must_keep)
