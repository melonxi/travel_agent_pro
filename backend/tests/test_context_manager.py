# backend/tests/test_context_manager.py
import pytest
from unittest.mock import MagicMock

from agent.types import Message, Role, ToolCall, ToolResult
from context.manager import ContextManager
from state.models import TravelPlanState, DateRange, Budget
from llm.types import ChunkType, LLMChunk


@pytest.fixture
def ctx_manager():
    return ContextManager(soul_path="backend/context/soul.md")


def test_load_soul(ctx_manager):
    soul = ctx_manager._load_soul()
    assert "旅行规划 Agent" in soul


def test_build_system_message(ctx_manager):
    plan = TravelPlanState(session_id="s1", phase=1)
    msg = ctx_manager.build_system_message(
        plan,
        phase_prompt="你是灵感顾问",
        user_summary="",
        available_tools=["update_plan_state", "xiaohongshu_search"],
    )
    assert msg.role == Role.SYSTEM
    assert "旅行规划 Agent" in msg.content  # from SOUL
    assert "灵感顾问" in msg.content  # from phase prompt
    assert "## 当前时间" in msg.content
    assert "当前本地日期" in msg.content
    assert "当前时区" in msg.content
    assert "必须先调用 `update_plan_state`" in msg.content
    assert "不要重复调用 `update_plan_state` 写入相同值" in msg.content
    assert "当前可用工具：update_plan_state, xiaohongshu_search" in msg.content


def test_build_runtime_context(ctx_manager):
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        budget=Budget(total=15000),
    )
    ctx = ctx_manager.build_runtime_context(
        plan,
        available_tools=["update_plan_state", "web_search"],
    )
    assert "Kyoto" in ctx
    assert "15000" in ctx
    assert "阶段：3" in ctx or "阶段: 3" in ctx
    assert "当前可用工具：update_plan_state, web_search" in ctx


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


@pytest.mark.asyncio
async def test_compress_for_transition_is_rule_based(ctx_manager):
    """The transition summary is now built deterministically from messages,
    without any extra LLM call. The factory must not be touched."""
    messages = [
        Message(role=Role.SYSTEM, content="system"),
        Message(role=Role.USER, content="我想去东京"),
        Message(
            role=Role.ASSISTANT,
            content="好的，先确认日期",
            tool_calls=[
                ToolCall(
                    id="tc1",
                    name="update_plan_state",
                    arguments={"field": "destination", "value": "东京"},
                )
            ],
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(
                tool_call_id="tc1",
                status="success",
                data={"updated_field": "destination", "new_value": "东京"},
            ),
        ),
    ]

    factory = MagicMock()

    summary = await ctx_manager.compress_for_transition(
        messages=messages,
        from_phase=1,
        to_phase=3,
        llm_factory=factory,
    )

    factory.assert_not_called()
    # User message kept verbatim.
    assert "用户: 我想去东京" in summary
    # Assistant text is kept (truncated at 200 chars; this one is short).
    assert "助手: 好的，先确认日期" in summary
    # update_plan_state rendered as a "决策" line referring to the tc arguments.
    assert "决策: update_plan_state destination = 东京" in summary


@pytest.mark.asyncio
async def test_compress_for_transition_never_calls_llm_even_for_long_context(ctx_manager):
    """Regression: older design spun up an LLM for 4+ messages. The new
    implementation must stay deterministic regardless of length."""

    async def should_not_be_called(*args, **kwargs):  # pragma: no cover
        raise AssertionError("llm_factory must not be invoked")
        yield  # pragma: no cover — keeps it a valid async generator

    fake_llm = MagicMock()
    fake_llm.chat = should_not_be_called
    factory = MagicMock(return_value=fake_llm)

    messages = [
        Message(role=Role.SYSTEM, content="system"),
        Message(role=Role.USER, content="我想五一去东京"),
        Message(role=Role.ASSISTANT, content="好的"),
        Message(role=Role.USER, content="预算 2 万以内"),
        Message(role=Role.ASSISTANT, content="明白"),
    ]

    summary = await ctx_manager.compress_for_transition(
        messages=messages,
        from_phase=1,
        to_phase=3,
        llm_factory=factory,
    )

    factory.assert_not_called()
    assert "用户: 我想五一去东京" in summary
    assert "用户: 预算 2 万以内" in summary
    assert "助手: 好的" in summary
    assert "助手: 明白" in summary


@pytest.mark.asyncio
async def test_compress_for_transition_truncates_long_assistant_text(ctx_manager):
    long_text = "这是一段非常长的解释。" * 60
    messages = [
        Message(role=Role.USER, content="问题"),
        Message(role=Role.ASSISTANT, content=long_text),
    ]
    summary = await ctx_manager.compress_for_transition(
        messages=messages,
        from_phase=1,
        to_phase=3,
        llm_factory=None,
    )
    assistant_line = next(
        line for line in summary.splitlines() if line.startswith("助手:")
    )
    # Ellipsis suffix indicates truncation
    assert assistant_line.endswith("…")
    assert len(assistant_line) <= len("助手: ") + 201


@pytest.mark.asyncio
async def test_compress_for_transition_renders_tool_success_and_failure(ctx_manager):
    messages = [
        Message(
            role=Role.ASSISTANT,
            tool_calls=[
                ToolCall(id="tc_ok", name="web_search", arguments={"query": "kyoto"}),
                ToolCall(id="tc_err", name="check_weather", arguments={"city": "东京"}),
            ],
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(
                tool_call_id="tc_ok",
                status="success",
                data={"answer": "京都樱花已经开了"},
            ),
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(
                tool_call_id="tc_err",
                status="error",
                error="City not found",
                error_code="NOT_FOUND",
            ),
        ),
    ]

    summary = await ctx_manager.compress_for_transition(
        messages=messages,
        from_phase=1,
        to_phase=3,
        llm_factory=None,
    )

    assert "工具 web_search 成功" in summary
    assert "京都樱花已经开了" in summary
    assert "工具 check_weather 失败" in summary
    assert "NOT_FOUND" in summary
    assert "City not found" in summary
