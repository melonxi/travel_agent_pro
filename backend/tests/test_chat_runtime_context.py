from agent.message_filters import (
    active_runtime_messages,
    clean_persisted_session_messages,
    is_persisted_history_message,
)
from agent.types import Message, Role
from api.routes.chat_routes import _continuation_notice
from context.manager import ContextManager
from state.models import TravelPlanState


def test_persisted_history_excludes_system_and_transient_messages():
    messages = [
        Message(role=Role.SYSTEM, content="system"),
        Message(role=Role.USER, content="old user"),
        Message(role=Role.USER, content="<turn_context>", transient=True),
    ]

    assert is_persisted_history_message(messages[1]) is True
    assert clean_persisted_session_messages(messages) == [messages[1]]


def test_active_runtime_messages_prefers_turn_runtime_list():
    persisted = [Message(role=Role.USER, content="history")]
    runtime = [Message(role=Role.SYSTEM, content="static", transient=True)]
    session = {"messages": persisted, "_active_runtime_messages": runtime}

    assert active_runtime_messages(session) is runtime


def test_continuation_notice_maps_known_contexts():
    assert "从断点继续" in _continuation_notice("partial_text")
    assert "工具结果继续回复" in _continuation_notice("tools_read_only")


def test_continuation_notice_returns_none_for_unknown_context():
    assert _continuation_notice("unknown") is None


def test_main_runtime_order_is_static_history_user_turn_context():
    ctx_manager = ContextManager(soul_path="backend/context/soul.md")
    plan = TravelPlanState(session_id="s1", phase=1)
    history = [Message(role=Role.USER, content="旧消息")]
    current_user = Message(role=Role.USER, content="新消息")
    llm_messages = [
        ctx_manager.build_static_system_message(plan, "phase prompt"),
        *history,
        current_user,
        ctx_manager.build_turn_context_message(
            plan=plan,
            available_tools=["update_trip_basics"],
            memory_context="暂无相关用户记忆",
        ),
    ]

    assert [m.role for m in llm_messages] == [
        Role.SYSTEM,
        Role.USER,
        Role.USER,
        Role.USER,
    ]
    assert llm_messages[0].transient is True
    assert llm_messages[-1].transient is True
    assert llm_messages[-1].content.startswith("<turn_context>")
