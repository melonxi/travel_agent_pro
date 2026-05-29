from agent.types import Message, Role
from context.manager import ContextManager
from state.models import TravelPlanState


def _stable_prefix(messages: list[Message]) -> list[tuple[str, str | None]]:
    prefix = []
    for index, message in enumerate(messages):
        if message.transient and not (index == 0 and message.role == Role.SYSTEM):
            break
        prefix.append((message.role.value, message.content))
    return prefix


def test_static_system_plus_history_prefix_is_stable_across_turns():
    ctx = ContextManager(soul_path="backend/context/soul.md")
    plan = TravelPlanState(session_id="s1", phase=1)
    history = [Message(role=Role.USER, content="我想去东京")]

    turn1_user = Message(role=Role.USER, content="继续")
    turn1 = [
        ctx.build_static_system_message(plan, "phase prompt"),
        *history,
        turn1_user,
        ctx.build_turn_context_message(
            plan=plan,
            available_tools=["update_trip_basics"],
            memory_context="暂无相关用户记忆",
        ),
    ]

    persisted_after_turn1 = [
        *history,
        turn1_user,
        Message(role=Role.ASSISTANT, content="好的"),
    ]
    turn2 = [
        ctx.build_static_system_message(plan, "phase prompt"),
        *persisted_after_turn1,
        Message(role=Role.USER, content="预算一万"),
        ctx.build_turn_context_message(
            plan=plan,
            available_tools=["update_trip_basics"],
            memory_context="- 偏好安静酒店",
        ),
    ]

    expected_prefix = [
        (turn1[0].role.value, turn1[0].content),
        *[(message.role.value, message.content) for message in persisted_after_turn1],
        ("user", "预算一万"),
    ]
    assert _stable_prefix(turn2) == expected_prefix
    assert all(message.role != Role.SYSTEM for message in turn2[1:])
    assert turn2[-1].transient is True
