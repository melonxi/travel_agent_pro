from __future__ import annotations

import pytest

from agent.execution.limits import AgentLoopLimits
from agent.hooks import HookManager
from agent.loop import AgentLoop
from tools.engine import ToolEngine


def test_limits_keep_max_retries_as_compatibility_alias():
    limits = AgentLoopLimits.from_constructor_args(
        max_iterations=None,
        max_retries=7,
        max_llm_errors=None,
    )

    assert limits.max_iterations == 7
    assert limits.max_llm_errors == 1


def test_max_iterations_takes_precedence_over_compatibility_alias():
    limits = AgentLoopLimits.from_constructor_args(
        max_iterations=4,
        max_retries=9,
        max_llm_errors=2,
    )

    assert limits.max_iterations == 4
    assert limits.max_llm_errors == 2


def test_agent_loop_exposes_legacy_max_retries_value():
    agent = AgentLoop(
        llm=object(),
        tool_engine=ToolEngine(),
        hooks=HookManager(),
        max_retries=5,
    )

    assert agent.max_iterations == 5
    assert agent.max_retries == 5
    assert agent.limits.max_llm_errors == 1


def test_limits_reject_non_positive_max_iterations():
    with pytest.raises(ValueError, match="max_iterations must be >= 1"):
        AgentLoopLimits.from_constructor_args(
            max_iterations=0,
            max_retries=None,
            max_llm_errors=None,
        )


def test_limits_reject_negative_max_llm_errors():
    with pytest.raises(ValueError, match="max_llm_errors must be >= 0"):
        AgentLoopLimits.from_constructor_args(
            max_iterations=None,
            max_retries=None,
            max_llm_errors=-1,
        )
