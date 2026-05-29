import logging

import pytest

from agent.message_filters import (
    assert_main_runtime_prompt_shape,
    strip_non_initial_system_messages,
)
from agent.types import Message, Role


def test_strip_non_initial_system_messages_warns_and_removes(caplog):
    messages = [
        Message(role=Role.SYSTEM, content="static"),
        Message(role=Role.USER, content="hi"),
        Message(role=Role.SYSTEM, content="dynamic"),
        Message(role=Role.ASSISTANT, content="ok"),
    ]

    logger = logging.getLogger("test.runtime_prompt")
    with caplog.at_level(logging.WARNING):
        removed = strip_non_initial_system_messages(
            messages,
            logger=logger,
            context="unit-test",
        )

    assert removed == 1
    assert [message.role for message in messages] == [
        Role.SYSTEM,
        Role.USER,
        Role.ASSISTANT,
    ]
    assert "Stripping non-initial system message" in caplog.text


def test_assert_main_runtime_prompt_shape_rejects_non_initial_system_message():
    messages = [
        Message(role=Role.SYSTEM, content="static"),
        Message(role=Role.USER, content="hi"),
        Message(role=Role.SYSTEM, content="dynamic"),
    ]

    with pytest.raises(AssertionError, match="Role.SYSTEM is only allowed at index 0"):
        assert_main_runtime_prompt_shape(messages)
